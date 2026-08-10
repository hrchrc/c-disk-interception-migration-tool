//! 错误分类 + 退避策略(v5 §4.4)。
//!
//! 设计要点:
//! - 错误分类:可重试(暂时性故障) vs 不可重试(永久性故障)
//! - 退避策略:指数退避 backoff = base * 2^(attempt-1),封顶 30s
//! - 网络/本地路径分离:UNC 路径放宽退避基数(网络抖动恢复慢)
//! - 取消(1223)绝不重试,直接向上抛
//!
//! 重试由调用方(engine.rs)在外层循环驱动,retry.rs 提供判定 + 退避时长计算 + 分段 sleep。
//! classify / backoff_ms 为纯函数,sleep_backoff 做 I/O(检查 cancel_token)但分段化便于取消。

use std::path::Path;
use std::thread;
use std::time::Duration;

use crate::job::Retry;

// ============================================================
// 错误分类(按 Win32 错误码)
// ============================================================

/// 错误可重试性分类。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RetryKind {
    /// 可重试:暂时性故障,退避后重试有机会成功。
    /// 如:共享冲突(文件被占用)、网络闪断、瞬时拒绝访问。
    Retry,
    /// 不可重试:永久性故障,重试也无用。
    /// 如:磁盘满、路径不存在、参数非法、取消。
    Fatal,
}

/// 判定 Win32 错误码的可重试性。
///
/// 依据:v5 §4.4 + Windows 实践经验。
/// - 1223(ERROR_CANCELLED):用户取消,绝不重试(由调用方短路,这里也归 Fatal 双保险)
/// - 2/3(ERROR_FILE_NOT_FOUND/PATH_NOT_FOUND):路径不存在,重试无用
/// - 5(ERROR_ACCESS_DENIED):通常权限不足,重试无用;少数情况(ACL 继承延迟)可重试,
///   但无法区分,保守归 Fatal。调用方若需重试可显式覆盖。
/// - 19(ERROR_WRITE_PROTECT):介质写保护,永久性
/// - 87(ERROR_INVALID_PARAMETER):程序 bug 或参数非法,重试无用
/// - 108(ERROR_DISK_CHANGE):软盘类介质未插,不可重试
/// - 112(ERROR_DISK_FULL):磁盘空间不足,重试无用(用户清理后需手动重跑)
/// - 145(ERROR_DIR_NOT_EMPTY):目录非空(purge 场景),通常瞬时但 rmdir 重试可能成功
///   归 Retry(purge 竞态场景常见)
/// - 其他未列出码:保守归 Retry(未知错误多给一次机会,反正是最后兜底)
pub fn classify(code: u32) -> RetryKind {
    match code {
        // === 取消:绝不重试 ===
        1223 => RetryKind::Fatal, // ERROR_CANCELLED
        // === 永久性故障:路径/参数/介质/空间/权限 ===
        2 => RetryKind::Fatal,    // ERROR_FILE_NOT_FOUND
        3 => RetryKind::Fatal,    // ERROR_PATH_NOT_FOUND
        5 => RetryKind::Fatal,    // ERROR_ACCESS_DENIED(权限性,非瞬时占用)
        19 => RetryKind::Fatal,   // ERROR_WRITE_PROTECT
        87 => RetryKind::Fatal,   // ERROR_INVALID_PARAMETER
        108 => RetryKind::Fatal,  // ERROR_DISK_CHANGE
        112 => RetryKind::Fatal,  // ERROR_DISK_FULL
        161 => RetryKind::Fatal,  // ERROR_BAD_PATHNAME
        183 => RetryKind::Fatal,  // ERROR_ALREADY_EXISTS(目标已存在且不可覆盖)
        // === 引擎内部码(非 Win32,0xE0000000 段,lib.rs 定义):内部状态错误,重试无意义 ===
        0xE0000001 => RetryKind::Fatal, // ERR_SOURCE_CHANGED(源文件复制期间被截断/变化)
        0xE0000002 => RetryKind::Fatal, // ERR_PIPELINE_DISCONNECTED(流水线异常退出)
        0xE0000003 => RetryKind::Fatal, // ERR_NO_OS_ERROR(无原始错误码兜底)
        // === 暂时性故障:可重试 ===
        32 => RetryKind::Retry,   // ERROR_SHARING_VIOLATION(文件被占用)
        33 => RetryKind::Retry,   // ERROR_LOCK_VIOLATION(文件锁冲突)
        145 => RetryKind::Retry,  // ERROR_DIR_NOT_EMPTY(purge 竞态)
        232 => RetryKind::Retry,  // ERROR_PIPE_CLOSED(管道/网络断开)
        233 => RetryKind::Retry,  // ERROR_PIPE_NOT_CONNECTED(管道未连接)
        121 => RetryKind::Retry,  // ERROR_SEM_TIMEOUT(信号量超时)
        120 => RetryKind::Retry,  // ERROR_CALL_NOT_IMPLEMENTED(罕见,重试无害)
        1130 => RetryKind::Retry, // ERROR_NOT_ENOUGH_SERVER_MEMORY(服务器内存不足,瞬时)
        1722 => RetryKind::Retry, // RPC_S_SERVER_UNAVAILABLE(RPC 不可达,网络抖动)
        53 => RetryKind::Retry,   // ERROR_BAD_NETPATH(网络路径暂时不可达)
        67 => RetryKind::Retry,   // ERROR_BAD_NET_NAME(网络名找不到,可能瞬时)
        // === 默认:保守归 Retry ===
        // 未知错误多给一次机会,反正 max_attempts 兜底,最坏多等几次退避
        _ => RetryKind::Retry,
    }
}

// ============================================================
// 退避策略
// ============================================================

/// 退避时长上限(避免单次退避太久拖垮整个任务)。
const BACKOFF_CAP_MS: u64 = 30_000; // 30s

/// 计算第 `attempt` 次重试的退避时长(1-based:第 1 次重试前等 base*2^0 = base)。
///
/// 公式:backoff = min(base * 2^(attempt-1), CAP)
/// - attempt=1:base * 1
/// - attempt=2:base * 2
/// - attempt=3:base * 4
///   ...
///
/// 网络路径(`network_path=true`)自动放宽:base 翻倍,且 CAP 提高到 60s
/// (网络抖动恢复慢,本地共享冲突通常几百 ms 即可释放)。
pub fn backoff_ms(retry: &Retry, attempt: u32) -> u64 {
    let base = retry.backoff_base_ms as u64;
    // 网络/UNC 路径:放宽基数(抖动恢复慢)
    let effective_base = if retry.network_path { base * 2 } else { base };
    let cap = if retry.network_path {
        60_000 // 网络 CAP: 60s
    } else {
        BACKOFF_CAP_MS
    };

    // 2^(attempt-1),防溢出:attempt 不会很大(max_attempts 默认 5)
    let exp = attempt.saturating_sub(1).min(20); // 2^20 = ~17M,够用
    let raw = effective_base.saturating_mul(1u64 << exp);
    raw.min(cap)
}

/// 阻塞等待退避时长(用于 engine.rs 重试循环)。
///
/// 分段 sleep(每 200ms 一段),中途检测 cancel_token 文件是否存在;
/// 用户取消后最长 200ms 即可提前退出退避,无需等满(网络路径退避可达 60s)。
///
/// :param cancel_token: 取消标志文件路径;为 None 则不检查(纯退避,用于测试)
pub fn sleep_backoff(retry: &Retry, attempt: u32, cancel_token: Option<&Path>) {
    let ms = backoff_ms(retry, attempt);
    if ms == 0 {
        return;
    }
    const POLL_INTERVAL_MS: u64 = 200;
    let mut remaining = ms;
    while remaining > 0 {
        let step = remaining.min(POLL_INTERVAL_MS);
        thread::sleep(Duration::from_millis(step));
        remaining -= step;
        if let Some(p) = cancel_token {
            if p.exists() {
                return; // 用户已取消,提前退出退避
            }
        }
    }
}

// ============================================================
// 单元测试
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;

    fn retry_local() -> Retry {
        Retry {
            max_attempts: 5,
            backoff_base_ms: 100,
            network_path: false,
        }
    }

    fn retry_net() -> Retry {
        Retry {
            max_attempts: 5,
            backoff_base_ms: 100,
            network_path: true,
        }
    }

    // --- classify ---

    #[test]
    fn test_classify_cancel_is_fatal() {
        assert_eq!(classify(1223), RetryKind::Fatal);
    }

    #[test]
    fn test_classify_sharing_violation_retryable() {
        // 32 = ERROR_SHARING_VIOLATION,文件被占用,重试有机会
        assert_eq!(classify(32), RetryKind::Retry);
    }

    #[test]
    fn test_classify_disk_full_fatal() {
        assert_eq!(classify(112), RetryKind::Fatal);
    }

    #[test]
    fn test_classify_path_not_found_fatal() {
        assert_eq!(classify(2), RetryKind::Fatal);
        assert_eq!(classify(3), RetryKind::Fatal);
    }

    #[test]
    fn test_classify_unknown_defaults_retry() {
        // 未知错误码保守归可重试(max_attempts 兜底)
        assert_eq!(classify(99999), RetryKind::Retry);
    }

    #[test]
    fn test_classify_network_errors_retryable() {
        assert_eq!(classify(53), RetryKind::Retry);
        assert_eq!(classify(67), RetryKind::Retry);
        assert_eq!(classify(1722), RetryKind::Retry);
    }

    // --- backoff_ms ---

    #[test]
    fn test_backoff_exponential() {
        let r = retry_local(); // base=100
        assert_eq!(backoff_ms(&r, 1), 100); // 100 * 2^0
        assert_eq!(backoff_ms(&r, 2), 200); // 100 * 2^1
        assert_eq!(backoff_ms(&r, 3), 400); // 100 * 2^2
        assert_eq!(backoff_ms(&r, 4), 800); // 100 * 2^3
    }

    #[test]
    fn test_backoff_cap() {
        let r = Retry {
            max_attempts: 5,
            backoff_base_ms: 10_000, // base=10s
            network_path: false,
        };
        // attempt=1: 10s, attempt=2: 20s, attempt=3: 30s(cap)
        assert_eq!(backoff_ms(&r, 1), 10_000);
        assert_eq!(backoff_ms(&r, 2), 20_000);
        assert_eq!(backoff_ms(&r, 3), 30_000); // capped
        assert_eq!(backoff_ms(&r, 4), 30_000); // capped
    }

    #[test]
    fn test_backoff_network_path_doubles_base() {
        let r_net = retry_net(); // base=100, network=true → effective_base=200
        assert_eq!(backoff_ms(&r_net, 1), 200); // 200 * 2^0
        assert_eq!(backoff_ms(&r_net, 2), 400); // 200 * 2^1
    }

    #[test]
    fn test_backoff_network_path_higher_cap() {
        let r = Retry {
            max_attempts: 5,
            backoff_base_ms: 20_000, // base=20s
            network_path: true,      // effective_base=40s, cap=60s
        };
        assert_eq!(backoff_ms(&r, 1), 40_000);
        assert_eq!(backoff_ms(&r, 2), 60_000); // capped at 60s(网络)
    }

    #[test]
    fn test_backoff_attempt_zero_safe() {
        // attempt=0(边界):saturating_sub → 0,exp=0,backoff=base
        let r = retry_local();
        assert_eq!(backoff_ms(&r, 0), 100); // 等同 attempt=1
    }
}
