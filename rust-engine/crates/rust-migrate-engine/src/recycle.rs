//! 软删除到回收站:基于 IFileOperation + FOF_ALLOWUNDO(v5 §4.3)。
//! 所有 COM/unsafe 集中于此模块,业务代码只调 safe 接口。
//!
//! 设计:RecycleBin 持有一个 IFileOperation 会话,queue_delete 收集删除项,
//! commit 时一次 PerformOperations 批量执行(数千文件只触发一次回收站操作)。
//! COM 初始化是线程局部的,RecycleBin 必须在创建它的线程内使用。

use std::os::windows::ffi::OsStrExt;
use std::path::{Path, PathBuf};

use windows::core::{GUID, PCWSTR};
use windows::Win32::Foundation::{S_FALSE, S_OK};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_ALL, COINIT_APARTMENTTHREADED,
};
use windows::Win32::UI::Shell::{
    IFileOperation, IShellItem, SHCreateItemFromParsingName, SHFileOperationW, SHFILEOPSTRUCTW,
    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_NOERRORUI, FOF_SILENT,
};

/// IFileOperation 的 CLSID:{3AD05575-8857-4850-9274-11B85BDB8E09}
/// windows-rs 0.58 未导出常量,手动构造(来自 MSDN 文档,非代码引用)。
/// 2026-08-10 修复:原 CLSID 后两段写错(8278-1054B1BFCD31),导致
/// CoCreateInstance 永远 REGDB_E_CLASSNOTREG,IFileOperation 从未生效,
/// 每次软删除都走 SHFileOperationW 兜底。
const CLSID_FILE_OPERATION: GUID = GUID::from_values(
    0x3AD05575,
    0x8857,
    0x4850,
    [0x92, 0x74, 0x11, 0xB8, 0x5B, 0xDB, 0x8E, 0x09],
);

/// 从 windows::core::Error 提取 Win32 错误码(与 win_io::win32_err 同逻辑,避免跨模块依赖)。
/// 非 Win32 facility 的 HRESULT 无法映射 → ERR_NO_OS_ERROR(引擎内部码),
/// 避免被 Python 侧按真实 Win32 码 87 误译成"参数错误"。
fn win32_err(e: windows::core::Error) -> u32 {
    let hr = e.code().0 as u32;
    let facility = (hr >> 16) & 0x1FFF;
    if facility == 7 {
        hr & 0xFFFF
    } else {
        crate::ERR_NO_OS_ERROR
    }
}

/// 路径转 UTF-16(NUL 结尾)。
fn to_wide(path: &Path) -> Vec<u16> {
    let mut v: Vec<u16> = path.as_os_str().encode_wide().collect();
    v.push(0);
    v
}

/// 回收站会话:封装 IFileOperation,支持批量软删除。
/// Drop 时自动 CoUninitialize,不会泄漏 COM 引用计数。
pub struct RecycleBin {
    op: IFileOperation,
    /// 是否由本实例初始化 COM(用于决定 drop 时是否 CoUninitialize)。
    /// CoInitializeEx 返回 S_FALSE 表示线程已初始化,此时不应 CoUninitialize(会破坏其他库)。
    owned_com: bool,
}

impl RecycleBin {
    /// 创建会话:初始化 COM + 创建 IFileOperation + 设 flags(允许撤销=到回收站)。
    pub fn new() -> Result<Self, u32> {
        // CoInitializeEx 返回 HRESULT:S_OK=刚初始化,S_FALSE=已初始化,负值=失败
        let hr = unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED) };
        let owned_com = if hr == S_OK {
            true
        } else if hr == S_FALSE {
            false // 线程已初始化,不 owned,不 uninit
        } else {
            // RPC_E_CHANGED_MODE(0x80010106)或其他
            return Err(hr.0 as u32);
        };
        let op: IFileOperation = unsafe {
            CoCreateInstance(&CLSID_FILE_OPERATION, None, CLSCTX_ALL).map_err(|e| {
                if owned_com {
                    CoUninitialize();
                }
                win32_err(e)
            })?
        };
        // FOF_ALLOWUNDO:到回收站;NOCONFIRMATION:不弹确认框;SILENT/NOERRORUI:不弹错误框
        let flags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI;
        unsafe {
            op.SetOperationFlags(flags).map_err(|e| {
                if owned_com {
                    CoUninitialize();
                }
                win32_err(e)
            })?
        };
        Ok(Self { op, owned_com })
    }

    /// 加入删除队列(实际执行在 commit 时批量进行)。
    pub fn queue_delete(&self, path: &Path) -> Result<(), u32> {
        let w = to_wide(path);
        let item: IShellItem =
            unsafe { SHCreateItemFromParsingName(PCWSTR(w.as_ptr()), None) }.map_err(win32_err)?;
        unsafe { self.op.DeleteItem(&item, None) }.map_err(win32_err)?;
        Ok(())
    }

    /// 执行所有排队的删除操作(到回收站)。
    pub fn commit(&self) -> Result<(), u32> {
        unsafe { self.op.PerformOperations() }.map_err(win32_err)?;
        Ok(())
    }
}

impl Drop for RecycleBin {
    fn drop(&mut self) {
        // op(IFileOperation)由 windows-rs 自动 Release
        if self.owned_com {
            unsafe { CoUninitialize() };
        }
    }
}

// ============================================================
// SHFileOperationW 兼容路径(IFileOperation 类未注册时的兜底)
// ============================================================

/// 用 SHFileOperationW 批量软删除到回收站(FO_DELETE + FOF_ALLOWUNDO)。
///
/// 背景(2026-08-09):部分系统 IFileOperation 的 CLSID 未注册
/// (REGDB_E_CLASSNOTREG 0x80040154,如精简系统/注册表被清理软件删项),
/// RecycleBin::new() 必然失败,旧逻辑直接回退硬删除(数据不可恢复)。
/// SHFileOperationW 是 shell32 导出函数,不依赖 COM 类注册,WinXP+ 全兼容,
/// 作为软删除的兼容兜底。
///
/// 返回 Ok:调用已执行(返回值 0;部分项失败时 fAnyOperationsAborted=true
/// 但返回值仍为 0,调用方必须做存在性检查精确统计)。
/// 返回 Err:SHFileOperationW 返回非 0(shell 自定义错误码)。
pub fn recycle_via_shfileop(files: &[PathBuf], dirs: &[PathBuf]) -> Result<(), u32> {
    // 安全护栏:SHFileOperationW 的 pFrom 会把 `*`/`?` 当通配符展开,
    // Windows 文件名可合法包含这些字符(如 "a*b.txt")→ 会误匹配删除其他文件。
    // 含通配符路径时拒绝走本路径,由调用方回退硬删除(精确路径,无通配语义)。
    for p in files.iter().chain(dirs.iter()) {
        if let Some(name) = p.file_name() {
            let name = name.to_string_lossy();
            if name.contains('*') || name.contains('?') {
                return Err(crate::ERR_NO_OS_ERROR);
            }
        }
    }
    // pFrom:双 null 结尾的多路径列表(文件+目录,FO_DELETE 递归删除目录树)
    let mut pfrom: Vec<u16> = Vec::new();
    for p in files.iter().chain(dirs.iter()) {
        pfrom.extend(p.as_os_str().encode_wide());
        pfrom.push(0);
    }
    pfrom.push(0);
    if pfrom.len() <= 1 {
        return Ok(()); // 空列表:无可删项
    }
    let mut op = SHFILEOPSTRUCTW {
        hwnd: Default::default(),
        wFunc: FO_DELETE,
        pFrom: PCWSTR(pfrom.as_ptr()),
        pTo: PCWSTR::null(),
        fFlags: (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI).0 as u16,
        fAnyOperationsAborted: false.into(),
        hNameMappings: std::ptr::null_mut(),
        lpszProgressTitle: PCWSTR::null(),
    };
    let ret = unsafe { SHFileOperationW(&mut op) };
    if ret != 0 {
        return Err(ret as u32);
    }
    Ok(())
}
