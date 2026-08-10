//! P5:重解析点(符号链接/Junction)识别与处理。
//!
//! 对齐 FastCopy /link /relink 策略:
//! - 识别 reparse point(IO_REPARSE_TAG_SYMLINK / IO_REPARSE_TAG_MOUNT_POINT)
//! - 保留链接本身(不跟随),用 CreateSymbolicLinkW 重建符号链接,
//!   用 DeviceIoControl(FSCTL_SET_REPARSE_POINT) 重建 Junction
//!
//! 实现要点:
//! 1. FILE_FLAG_OPEN_REPARSE_POINT 打开链接本身(不跟随目标)
//! 2. DeviceIoControl(FSCTL_GET_REPARSE_POINT) 读取 reparse data
//! 3. DeviceIoControl(FSCTL_SET_REPARSE_POINT) 在目标侧重建
//! 4. 区分符号链接(可指向任意路径)和 Junction(只能指向本地绝对路径)

use std::path::Path;
use windows::Win32::Foundation::GetLastError;
use windows::Win32::Storage::FileSystem::{
    CreateFileW, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, OPEN_EXISTING,
};
use windows::Win32::System::IO::DeviceIoControl;
use windows::core::PCWSTR;

// FSCTL 常量(微软文档)
const FSCTL_GET_REPARSE_POINT: u32 = 0x0009_00A8;
const FSCTL_SET_REPARSE_POINT: u32 = 0x0009_00A4;

// IO_REPARSE_TAG_SYMLINK = 0xA000000C
const IO_REPARSE_TAG_SYMLINK: u32 = 0xA000_000C;
// IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003(Junction)
const IO_REPARSE_TAG_MOUNT_POINT: u32 = 0xA000_0003;

/// reparse point 类型。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReparseKind {
    /// 符号链接(IO_REPARSE_TAG_SYMLINK)
    Symlink,
    /// 目录 Junction(IO_REPARSE_TAG_MOUNT_POINT)
    Junction,
    /// 其他重解析点(不处理)
    Other(u32),
}

/// 读取路径的 reparse point 信息。
/// 返回 (类型, reparse data 原始字节)。
/// 若路径不是 reparse point,返回 None。
pub fn read_reparse(path: &Path) -> Result<Option<(ReparseKind, Vec<u8>)>, u32> {
    const GENERIC_READ: u32 = 0x8000_0000;
    let w = to_wide(path);
    let h = unsafe {
        CreateFileW(
            PCWSTR(w.as_ptr()),
            GENERIC_READ,
            windows::Win32::Storage::FileSystem::FILE_SHARE_READ,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
    };
    let handle = match h {
        Ok(h) if !h.is_invalid() => h,
        _ => return Err(unsafe { GetLastError() }.0),
    };

    // reparse data buffer:最大 16KB(微软文档)
    let mut buf = vec![0u8; 16384];
    let mut bytes_returned: u32 = 0;
    let r = unsafe {
        DeviceIoControl(
            handle,
            FSCTL_GET_REPARSE_POINT,
            None,
            0,
            Some(buf.as_mut_ptr() as *mut _),
            buf.len() as u32,
            Some(&mut bytes_returned),
            None,
        )
    };
    // 先保存错误码,再关闭句柄(CloseHandle 可能覆盖 GetLastError)
    let err_code = if r.is_err() {
        unsafe { GetLastError() }.0
    } else {
        0
    };
    let _ = unsafe { windows::Win32::Foundation::CloseHandle(handle) };

    if r.is_err() {
        let code = err_code;
        // 4390 = ERROR_NOT_A_REPARSE_POINT,返回 None
        if code == 4390 {
            return Ok(None);
        }
        return Err(code);
    }

    buf.truncate(bytes_returned as usize);

    // 解析 reparse tag:REPARSE_DATA_BUFFER 的前 4 字节
    if buf.len() < 4 {
        return Err(87); // ERROR_INVALID_PARAMETER
    }
    let tag = u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]);
    let kind = if tag == IO_REPARSE_TAG_SYMLINK {
        ReparseKind::Symlink
    } else if tag == IO_REPARSE_TAG_MOUNT_POINT {
        ReparseKind::Junction
    } else {
        ReparseKind::Other(tag)
    };

    Ok(Some((kind, buf)))
}

/// 在目标路径重建 reparse point。
/// 直接写回原始 reparse data(包含目标路径),不修改链接内容。
pub fn write_reparse(path: &Path, data: &[u8]) -> Result<(), u32> {
    // FSCTL_SET_REPARSE_POINT 需要 FILE_WRITE_ATTRIBUTES 权限。
    // 注意:GENERIC_WRITE 对目录的映射不包含 FILE_WRITE_ATTRIBUTES,
    // 只用 GENERIC_WRITE 会导致 DeviceIoControl 返回"成功"但实际未设置 reparse point
    // (Windows 的静默失败行为)。必须显式指定 FILE_WRITE_ATTRIBUTES。
    const FILE_WRITE_ATTRIBUTES: u32 = 0x0000_0100;
    let w = to_wide(path);
    let h = unsafe {
        CreateFileW(
            PCWSTR(w.as_ptr()),
            FILE_WRITE_ATTRIBUTES,
            windows::Win32::Storage::FileSystem::FILE_SHARE_READ,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
    };
    let handle = match h {
        Ok(h) if !h.is_invalid() => h,
        _ => return Err(unsafe { GetLastError() }.0),
    };

    let mut bytes_returned: u32 = 0;
    let r = unsafe {
        DeviceIoControl(
            handle,
            FSCTL_SET_REPARSE_POINT,
            Some(data.as_ptr() as *const _),
            data.len() as u32,
            None,
            0,
            Some(&mut bytes_returned),
            None,
        )
    };
    // 先保存错误码,再关闭句柄(CloseHandle 可能覆盖 GetLastError)
    let err_code = if r.is_err() {
        unsafe { GetLastError() }.0
    } else {
        0
    };
    let _ = unsafe { windows::Win32::Foundation::CloseHandle(handle) };

    if r.is_err() {
        return Err(err_code);
    }
    Ok(())
}

/// 创建目录(为 Junction/Symlink 目录占位准备目标容器)。
/// 容忍 ERROR_ALREADY_EXISTS (183):续传/重新运行场景下目标目录可能已存在,
/// 此时复用已有空目录继续设置 reparse point(与 copy_small 的覆盖语义一致)。
pub fn create_dir_for_reparse(path: &Path) -> Result<(), u32> {
    std::fs::create_dir(path).or_else(|e| {
        // ERROR_ALREADY_EXISTS (183):目录已存在,容忍
        if e.raw_os_error() == Some(183) {
            Ok(())
        } else {
            Err(e.raw_os_error().map(|c| c as u32).unwrap_or(crate::ERR_NO_OS_ERROR))
        }
    })
}

/// 复制 reparse point:在目标重建链接。
///
/// - Junction:先创建空目录,再 FSCTL_SET_REPARSE_POINT
/// - Symlink:先创建占位(目录或文件,由 is_dir 决定),再 FSCTL_SET_REPARSE_POINT
///   (FSCTL_SET_REPARSE_POINT 要求目标已存在;符号链接本身不记录目标类型,
///    需调用方根据源路径的 file_type 传入 is_dir)
///
/// data 参数:调用方通过 read_reparse 已读取的 reparse data,避免重复 I/O。
///
/// 注意:跨卷复制时,Junction 的目标路径可能指向源卷(绝对路径),
/// 这是用户需自行评估的语义问题(FastCopy 也不自动改写)。
pub fn copy_reparse(dst: &Path, kind: ReparseKind, is_dir: bool, data: &[u8]) -> Result<(), u32> {
    match kind {
        ReparseKind::Junction => {
            // Junction 需要先创建空目录,再设置 reparse point
            create_dir_for_reparse(dst)?;
            write_reparse(dst, data)?;
        }
        ReparseKind::Symlink => {
            // 符号链接:FSCTL_SET_REPARSE_POINT 要求目标已存在。
            // 根据 is_dir 创建占位目录或占位文件,再设置 reparse point。
            // 设置 reparse point 后,占位对象变成符号链接(reparse data 覆盖类型语义)。
            if is_dir {
                create_dir_for_reparse(dst)?;
            } else {
                // 创建空文件占位:create_new 语义(已存在则失败,避免误覆盖)
                std::fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(dst)
                    .map_err(|e| e.raw_os_error().map(|c| c as u32).unwrap_or(crate::ERR_NO_OS_ERROR))?;
            }
            write_reparse(dst, data)?;
        }
        ReparseKind::Other(_) => {
            // 其他重解析点不处理
            return Err(1742);
        }
    }
    Ok(())
}

/// 路径转 UTF-16(NUL 结尾)。
fn to_wide(path: &Path) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    let mut v: Vec<u16> = path.as_os_str().encode_wide().collect();
    v.push(0);
    v
}
