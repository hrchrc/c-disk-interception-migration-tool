//! rust-migrate-engine 库入口。
//!
//! 将核心模块暴露为 pub,供:
//! - main.rs(CLI 入口)调用
//! - tests/ 目录下的集成测试直接调用(不经过 subprocess)
//!
//! 这样 cargo 集成测试可以直接调 engine::run(&job) 验证逻辑,
//! 而非通过 Python 脚本调 exe + 解析 stdout(文档 §7.2 要求)。

pub mod checkpoint;
pub mod crc32;
pub mod engine;
pub mod event;
pub mod job;
pub mod mft_index;
pub mod pipeline;
pub mod purge;
pub mod recycle;
pub mod retry;
pub mod unbuffered;
pub mod win_io;
pub mod reparse;
pub mod acl;
pub mod hardlink;
pub mod verify;
pub mod priority;

// ============================================================
// 引擎内部错误码(非 Win32,0xE0000000 保留段)
// ============================================================
// 严禁复用真实 Win32 错误码(如 87=ERROR_INVALID_PARAMETER)表达内部状态,
// 否则 Python 侧 _WIN32_ERR_MAP 会按真实错误码查表给出误导性诊断
// (2026-08-09 glm-pc-updater 事故:BUG-12"源文件复制期间被截断"哨兵 87
//  被翻译成"参数错误/路径包含非法字符")。
// 本段码值永不会出现在 raw_os_error()(Win32 < 0x10000 / NTSTATUS 0xC0000000+)。

/// 源文件在复制期间被截断/变化(written != size,数据不完整)。
pub const ERR_SOURCE_CHANGED: u32 = 0xE0000001;
/// 复制流水线异常退出(读/写线程全部退出)。
pub const ERR_PIPELINE_DISCONNECTED: u32 = 0xE0000002;
/// std::io::Error 无原始 OS 错误码时的兜底码(无法确定具体原因)。
pub const ERR_NO_OS_ERROR: u32 = 0xE0000003;
