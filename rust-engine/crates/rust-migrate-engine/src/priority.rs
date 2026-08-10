//! P6:I/O 优先级与后台模式(执行文档 P6 Rust 侧)。
//!
//! 两层降级,避免迁移/校验挤占用户前台操作:
//! - 进程级:`PROCESS_MODE_BACKGROUND_BEGIN` 让进程所有线程的 I/O 自动以
//!   后台优先级调度(等效 VeryLow),影响整个进程(含流水线读/写线程);
//! - 句柄级:`SetFileInformationByHandle(FileIoPriorityHintInfo)` 将单个
//!   文件句柄的 I/O 优先级显式降为 VeryLow(经 `apply_if_enabled` 挂载到
//!   win_io 各 open 函数)。
//!
//! 全部裸 `extern "system"` FFI 直调 kernel32,零 Cargo.toml 依赖变更,
//! 对齐项目先例(copy_file_zero_copy: win_io.rs / physical_memory_mb: job.rs)。
//! 注意:windows crate 中该枚举常量名是 `IoPriorityHintVeryLow`(值 0),
//! 不是 SDK 的 `FileIoPriorityHintVeryLow`(值 4),此处用数值自定结构体绕开命名歧义。

use std::sync::atomic::{AtomicBool, Ordering};

/// PROCESS_MODE_BACKGROUND_BEGIN(SetPriorityClass 的 dwPriorityClass)。
/// 进入后进程所有线程 I/O 自动降为后台优先级。
const PROCESS_MODE_BACKGROUND_BEGIN: u32 = 0x0010_0000;
/// PROCESS_MODE_BACKGROUND_END:退出后台模式(进程退出时系统自动清除,显式恢复更干净)。
const PROCESS_MODE_BACKGROUND_END: u32 = 0x0020_0000;

/// FILE_INFO_BY_HANDLE_CLASS 的 FileIoPriorityHintInfo(class 18)。
const FILE_INFO_BY_HANDLE_CLASS_FILE_IO_PRIORITY_HINT_INFO: u32 = 18;

/// FILE_IO_PRIORITY_HINT 枚举:IoPriorityHintVeryLow = 0(最低)。
const IO_PRIORITY_HINT_VERY_LOW: i32 = 0;

/// 全局开关:engine.rs 在 run() 开头按 job.background_mode 设置,
/// win_io.rs 的 open 函数(无 job 引用)据此决定是否设置句柄优先级。
/// AtomicBool 对线程可见(流水线读/写线程同进程,无需 thread_local 传播)。
static BACKGROUND_ENABLED: AtomicBool = AtomicBool::new(false);

/// FILE_IO_PRIORITY_HINT_INFO(Win SDK 定义,repr(C) 与 C 布局一致)。
#[repr(C)]
#[derive(Clone, Copy)]
struct FileIoPriorityHintInfo {
    priority_hint: i32,
}

extern "system" {
    fn GetCurrentProcess() -> *mut core::ffi::c_void;
    fn SetPriorityClass(hProcess: *mut core::ffi::c_void, dwPriorityClass: u32) -> i32;
    fn SetFileInformationByHandle(
        hFile: *mut core::ffi::c_void,
        fileInformationClass: u32,
        lpFileInformation: *const core::ffi::c_void,
        dwBufferSize: u32,
    ) -> i32;
    fn GetLastError() -> u32;
}

/// 开启/关闭全局句柄级低优先级开关(engine.rs run() 开头/收尾调用)。
pub fn set_enabled(enabled: bool) {
    BACKGROUND_ENABLED.store(enabled, Ordering::Relaxed);
}

/// 当前是否处于后台低优先级模式。
pub fn enabled() -> bool {
    BACKGROUND_ENABLED.load(Ordering::Relaxed)
}

/// 进入进程后台模式(进程级,所有线程 I/O 自动降优先级)。
/// 失败返回 Win32 错误码,调用方记录但不阻断复制(低优先级是尽力而为)。
pub fn enter_background() -> Result<(), u32> {
    unsafe {
        let proc = GetCurrentProcess();
        if SetPriorityClass(proc, PROCESS_MODE_BACKGROUND_BEGIN) != 0 {
            Ok(())
        } else {
            Err(GetLastError())
        }
    }
}

/// 退出进程后台模式。进程退出时系统自动清除,此处显式恢复用于
/// engine.rs 正常收尾路径(单进程连续跑多个 job 的场景不残留)。
pub fn leave_background() -> Result<(), u32> {
    unsafe {
        let proc = GetCurrentProcess();
        if SetPriorityClass(proc, PROCESS_MODE_BACKGROUND_END) != 0 {
            Ok(())
        } else {
            Err(GetLastError())
        }
    }
}

/// 若全局后台开关开启,将句柄的 I/O 优先级降为 VeryLow。
/// 尽力而为:失败静默(优先级设置失败不应阻断复制)。
/// 由 win_io.rs 的 from_handle 统一挂载(覆盖全部 5 个 open 函数)。
pub fn apply_if_enabled(handle: *mut core::ffi::c_void) {
    if !BACKGROUND_ENABLED.load(Ordering::Relaxed) {
        return;
    }
    let info = FileIoPriorityHintInfo {
        priority_hint: IO_PRIORITY_HINT_VERY_LOW,
    };
    unsafe {
        SetFileInformationByHandle(
            handle,
            FILE_INFO_BY_HANDLE_CLASS_FILE_IO_PRIORITY_HINT_INFO,
            &info as *const FileIoPriorityHintInfo as *const core::ffi::c_void,
            std::mem::size_of::<FileIoPriorityHintInfo>() as u32,
        );
    }
}

/// RAII 收尾守卫:作用域(engine::run 栈)结束时自动退出进程后台模式并复位全局开关。
/// 覆盖 run() 的所有提前 return 路径(cancel / verify 早退 / purge 取消等),
/// 避免后台模式与全局开关在库调用/集成测试场景下残留(进程退出虽会自动清除,
/// 但同进程连续跑多个 job 时残留会让后续 job 意外降级)。
pub struct BackgroundGuard;

impl Drop for BackgroundGuard {
    fn drop(&mut self) {
        let _ = leave_background();
        set_enabled(false);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn enabled_switch_defaults_off() {
        set_enabled(false);
        assert!(!enabled());
        set_enabled(true);
        assert!(enabled());
        set_enabled(false);
    }

    #[test]
    fn enter_leave_background_ok_on_windows() {
        // 当前测试进程进入/退出后台模式:后台模式只降低 I/O 优先级,
        // 不影响功能,进入失败(如权限/旧系统)也不应 panic。
        assert!(enter_background().is_ok());
        assert!(leave_background().is_ok());
    }

    #[test]
    fn guard_resets_state_on_drop() {
        // 模拟"设置后提前 return"场景:guard 作用域结束(drop)必须复位全局开关。
        set_enabled(true);
        {
            let _g = BackgroundGuard;
            assert!(enabled());
        }
        assert!(!enabled(), "guard drop 后全局开关应复位");
    }
}
