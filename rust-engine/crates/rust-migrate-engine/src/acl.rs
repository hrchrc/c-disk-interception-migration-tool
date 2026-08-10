//! P5:ACL/安全描述符 + 备用数据流(ADS)复制。
//!
//! 对齐 FastCopy /acl /stream 选项:
//! - ACL:用 GetFileSecurityW/SetFileSecurityW 复制 DACL/Owner/Group
//! - ADS:用 FindFirstStreamW/FindNextStreamW 枚举流,CreateFileW + ReadFile/WriteFile 复制
//!
//! 实现要点:
//! 1. ACL 复制需 SeBackupPrivilege(读) + SeRestorePrivilege(写)
//! 2. ADS 复制用 FindFirstStreamW 枚举流名,再用 "path:streamname" 方式打开读写
//! 3. 仅复制非主流(::$DATA 之外的 ADS,如 Zone.Identifier:$DATA)

use std::path::Path;
use std::ptr::null_mut;
use windows::core::PCWSTR;
use windows::Win32::Foundation::{GetLastError, CloseHandle};
use windows::Win32::Security::{
    GetFileSecurityW, SetFileSecurityW, PSECURITY_DESCRIPTOR,
    OWNER_SECURITY_INFORMATION, GROUP_SECURITY_INFORMATION, DACL_SECURITY_INFORMATION,
};
use windows::Win32::Storage::FileSystem::CreateFileW;

// ============================================================
// ACL 复制:GetFileSecurityW + SetFileSecurityW
// ============================================================

/// 复制源文件的安全描述符(DACL + Owner + Group)到目标。
///
/// 需要进程具备 SeBackupPrivilege + SeRestorePrivilege 才能完整复制其他用户的文件 ACL。
/// 失败时返回 Win32 错误码。
///
/// 注意:不复制 SACL(SACL 审计策略通常不需迁移,且需 SeSecurityPrivilege)。
pub fn copy_acl(src: &Path, dst: &Path) -> Result<(), u32> {
    // OWNER/GROUP/DACL 三位合一(SetFileSecurityW 需要 OBJECT_SECURITY_INFORMATION 类型)
    let info_obj = OWNER_SECURITY_INFORMATION
        | GROUP_SECURITY_INFORMATION
        | DACL_SECURITY_INFORMATION;
    // GetFileSecurityW 第二参数签名是 u32(0.58 binding 不一致)
    let info_u32: u32 = info_obj.0;

    // 1. 查询安全描述符所需大小
    // 注意:首次调用传 null+0 会返回 false 并设 GetLastError=ERROR_INSUFFICIENT_BUFFER(122),
    // 这是 Win32 API 的标准模式,不能通过返回值判断成败,只能通过 needed 是否为 0 判断。
    let w_src = to_wide(src);
    let mut needed: u32 = 0;
    let _ = unsafe {
        GetFileSecurityW(
            PCWSTR(w_src.as_ptr()),
            info_u32,
            PSECURITY_DESCRIPTOR(null_mut()),
            0,
            &mut needed,
        )
    };
    if needed == 0 {
        return Err(unsafe { GetLastError() }.0);
    }

    // 2. 分配缓冲区读取安全描述符(第二次调用,返回 true 才算成功)
    let mut buf = vec![0u8; needed as usize];
    let ok = unsafe {
        GetFileSecurityW(
            PCWSTR(w_src.as_ptr()),
            info_u32,
            PSECURITY_DESCRIPTOR(buf.as_mut_ptr() as *mut _),
            needed,
            &mut needed,
        )
    };
    if !ok.as_bool() {
        return Err(unsafe { GetLastError() }.0);
    }

    // 3. 写入目标文件(SetFileSecurityW 返回 BOOL,需用 as_bool 判断)
    let w_dst = to_wide(dst);
    let ok = unsafe {
        SetFileSecurityW(
            PCWSTR(w_dst.as_ptr()),
            info_obj,
            PSECURITY_DESCRIPTOR(buf.as_ptr() as *mut _),
        )
    };
    if !ok.as_bool() {
        return Err(unsafe { GetLastError() }.0);
    }
    Ok(())
}

// ============================================================
// ADS 复制:FindFirstStreamW + CreateFileW + ReadFile/WriteFile
// ============================================================

/// 备用数据流(ADS)复制。
///
/// 用 FindFirstStreamW/FindNextStreamW 枚举源文件的所有流,
/// 跳过主流("::$DATA"),对每个 ADS 用 "path:streamname" 方式打开读写。
///
/// 注意:此函数仅复制 ADS,不复制主流(主流由 copy_small/copy_large_resumable 处理)。
pub fn copy_ads(src: &Path, dst: &Path) -> Result<(), u32> {
    use windows::Win32::Storage::FileSystem::{
        FindFirstStreamW, FindNextStreamW, FindClose, WIN32_FIND_STREAM_DATA,
    };

    let w_src = to_wide(src);
    // WIN32_FIND_STREAM_DATA 字段:StreamSize(前) + cStreamName(后),与 Win32 SDK 一致
    let mut data: WIN32_FIND_STREAM_DATA = unsafe { std::mem::zeroed() };

    // 枚举流:FindFirstStreamW(FindStreamInfoStandard)
    let r = unsafe {
        FindFirstStreamW(
            PCWSTR(w_src.as_ptr()),
            windows::Win32::Storage::FileSystem::FindStreamInfoStandard,
            &mut data as *mut _ as *mut _,
            0,
        )
    };
    let find_handle: usize = match r {
        Ok(h) => h.0 as usize,
        Err(e) => {
            let code = err_to_win32(e);
            // 38 = ERROR_END_OF_FILE:无 ADS,正常
            if code == 38 {
                return Ok(());
            }
            return Err(code);
        }
    };

    // 遍历所有流
    loop {
        // 从 cStreamName 读 NUL 终止的 UTF-16 字符串
        let stream_name = {
            let name_len = data.cStreamName.iter().position(|&c| c == 0).unwrap_or(data.cStreamName.len());
            String::from_utf16_lossy(&data.cStreamName[..name_len])
        };

        // 跳过主流("::$DATA")
        if !stream_name.eq("::$DATA") {
            // 复制此 ADS:用 filename:streamname 方式打开
            if let Err(code) = copy_one_ads(src, dst, &stream_name) {
                let _ = unsafe { FindClose(windows::Win32::Foundation::HANDLE(find_handle as *mut _)) };
                return Err(code);
            }
        }

        // 下一个流
        let r = unsafe {
            FindNextStreamW(
                windows::Win32::Foundation::HANDLE(find_handle as *mut _),
                &mut data as *mut _ as *mut _,
            )
        };
        if r.is_err() {
            let code = err_to_win32(r.unwrap_err());
            // 38 = ERROR_END_OF_FILE:枚举结束
            if code == 38 {
                break;
            }
            let _ = unsafe { FindClose(windows::Win32::Foundation::HANDLE(find_handle as *mut _)) };
            return Err(code);
        }
    }

    let _ = unsafe { FindClose(windows::Win32::Foundation::HANDLE(find_handle as *mut _)) };
    Ok(())
}

/// 复制单个 ADS:src:streamname → dst:streamname。
///
/// stream_name 从 FindFirstStreamW 返回,格式为 ":Zone.Identifier:$DATA",
/// 需提取纯流名(去掉前导冒号和 :$DATA 后缀),构造 "path:streamname" 路径。
fn copy_one_ads(src: &Path, dst: &Path, stream_name: &str) -> Result<(), u32> {
    const GENERIC_READ: u32 = 0x8000_0000;
    const GENERIC_WRITE: u32 = 0x4000_0000;
    // stream_name 格式: ":Zone.Identifier:$DATA" → 提取 "Zone.Identifier"
    // 用 strip_prefix/strip_suffix 而非 trim_*(trim 是逐字符匹配,会误删末尾字符)
    let pure_name = stream_name.strip_prefix(':').unwrap_or(stream_name);
    let pure_name = pure_name.strip_suffix(":$DATA").unwrap_or(pure_name);
    if pure_name.is_empty() {
        return Ok(()); // 主流,跳过
    }
    // 构造 ADS 路径: "C:\path\file.txt:Zone.Identifier"
    let src_ads = format!("{}:{}", src.to_string_lossy(), pure_name);
    let dst_ads = format!("{}:{}", dst.to_string_lossy(), pure_name);

    let w_src = to_wide(std::path::Path::new(&src_ads));
    let w_dst = to_wide(std::path::Path::new(&dst_ads));

    // 打开源流
    // 错误码提取:windows-rs 的 CreateFileW 返回 Result<HANDLE>,
    // 失败时 Error 内已封装 win32 code(HRESULT_FROM_WIN32),用 err_to_win32 提取。
    let s_handle = unsafe {
        CreateFileW(
            PCWSTR(w_src.as_ptr()),
            GENERIC_READ,
            windows::Win32::Storage::FileSystem::FILE_SHARE_READ,
            None,
            windows::Win32::Storage::FileSystem::OPEN_EXISTING,
            windows::Win32::Storage::FileSystem::FILE_FLAG_SEQUENTIAL_SCAN,
            None,
        )
    }
    .map_err(err_to_win32)?;

    // 打开目标流(创建)
    let d_handle = unsafe {
        CreateFileW(
            PCWSTR(w_dst.as_ptr()),
            GENERIC_WRITE,
            windows::Win32::Storage::FileSystem::FILE_SHARE_READ,
            None,
            windows::Win32::Storage::FileSystem::CREATE_ALWAYS,
            windows::Win32::Storage::FileSystem::FILE_FLAG_SEQUENTIAL_SCAN,
            None,
        )
    }
    .map_err(|e| {
        let _ = unsafe { CloseHandle(s_handle) };
        err_to_win32(e)
    })?;

    // 读写流内容
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let mut bytes_read: u32 = 0;
        let r = unsafe {
            windows::Win32::Storage::FileSystem::ReadFile(
                s_handle,
                Some(buf.as_mut_slice()),
                Some(&mut bytes_read),
                None,
            )
        };
        if r.is_err() {
            let code = err_to_win32(r.unwrap_err());
            let _ = unsafe { CloseHandle(s_handle) };
            let _ = unsafe { CloseHandle(d_handle) };
            return Err(code);
        }
        if bytes_read == 0 {
            break;
        }
        // WriteFile 可能部分写入(尤其是网络/特殊流),循环写完
        let mut written_total: u32 = 0;
        while written_total < bytes_read {
            let mut bytes_written: u32 = 0;
            let r = unsafe {
                windows::Win32::Storage::FileSystem::WriteFile(
                    d_handle,
                    Some(&buf[written_total as usize..bytes_read as usize]),
                    Some(&mut bytes_written),
                    None,
                )
            };
            if r.is_err() {
                let code = err_to_win32(r.unwrap_err());
                let _ = unsafe { CloseHandle(s_handle) };
                let _ = unsafe { CloseHandle(d_handle) };
                return Err(code);
            }
            if bytes_written == 0 {
                // 理论不应发生(磁盘满会返回 Err),防御性处理
                let _ = unsafe { CloseHandle(s_handle) };
                let _ = unsafe { CloseHandle(d_handle) };
                return Err(112); // ERROR_DISK_FULL
            }
            written_total += bytes_written;
        }
    }

    let _ = unsafe { CloseHandle(s_handle) };
    let _ = unsafe { CloseHandle(d_handle) };
    Ok(())
}

/// 从 windows::core::Error 提取 Win32 错误码。
/// windows-rs 的 Error::from_win32() 用 HRESULT_FROM_WIN32(code) 封装,
/// 格式为 0x80070000 | code。**必须校验 facility == 7(FACILITY_WIN32)**,
/// 否则非 Win32 HRESULT(如 E_FAIL 0x80004005)的低 16 位会被误当成错误码
/// (与 win_io::win32_err 保持一致)。
fn err_to_win32(e: windows::core::Error) -> u32 {
    let hresult = e.code().0 as u32;
    let facility = (hresult >> 16) & 0x1FFF;
    if facility == 7 {
        hresult & 0xFFFF
    } else {
        // 非 FACILITY_WIN32 的 HRESULT:返回 ERROR_INVALID_PARAMETER 兜底。
        // 不用 last_err(),因为 last_err 可能是其他 API 调用的残留错误码,
        // 会导致错误的错误码上报。
        87
    }
}

/// 路径转 UTF-16(NUL 结尾)。
fn to_wide(path: &Path) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    let mut v: Vec<u16> = path.as_os_str().encode_wide().collect();
    v.push(0);
    v
}
