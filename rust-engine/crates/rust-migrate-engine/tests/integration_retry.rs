//! integration_retry.rs — 重试机制测试(文档 §7.2 + P3)。
//!
//! 覆盖:
//! - 不可重试错误(路径不存在)立即失败,不重试
//! - 可重试错误(文件被占用)退避后重试
//! - 取消(1223)不重试

mod common;

use common::*;
use rust_migrate_engine::engine;
use std::path::Path;

/// 不可重试错误:源路径不存在 → 立即失败,返回 16。
#[test]
fn non_retryable_error_immediate_fail() {
    let base = temp_dir("retry_fatal");
    let src = base.join("nonexistent"); // 不存在
    let dst = base.join("dst");

    let job = copy_job(&src, &dst);
    let rc = engine::run(&job);
    // 源不存在 → 返回 16(严重失败)
    assert_eq!(rc, 16, "源不存在应返回 16,实际 {}", rc);
    cleanup(&base);
}

/// 可重试错误:目标文件被占用 → 重试后失败(但不 crash)。
#[test]
fn retryable_error_sharing_violation() {
    let base = temp_dir("retry_share");
    let src = base.join("src");
    let dst = base.join("dst");
    write_file(&src.join("locked.txt"), b"content");

    // 独占打开目标文件(触发 ERROR_SHARING_VIOLATION=32,可重试)
    // 注意:需要先创建目标目录
    std::fs::create_dir_all(&dst).unwrap();
    std::fs::copy(src.join("locked.txt"), dst.join("locked.txt")).unwrap();
    // P7 断点续传:目标已存在且大小+mtime 一致 → 引擎跳过,不触碰锁定目标(正确行为)。
    // CopyFileW 复制会保留时间戳,故上面的 copy 使目标与源"完全一致" → 会被跳过。
    // 修改源文件使跳过判断(大小+mtime 双一致)不成立,保留"占用→重试→失败"的测试意图。
    std::fs::write(&src.join("locked.txt"), b"content2").unwrap();
    use std::os::windows::fs::OpenOptionsExt;
    let _handle = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .share_mode(0) // 独占,不共享
        .open(dst.join("locked.txt"))
        .unwrap();

    // max_attempts=2,重试 1 次后失败
    let job = job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "copy",
            "retry": {{"max_attempts": 2, "backoff_base_ms": 10, "network_path": false}},
            "large_file_threshold_mb": 1,
            "fast_move_same_volume": false
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\")
    ));
    let rc = engine::run(&job);
    // 文件被占用 → 重试耗尽后失败
    // rc 可能是 2(部分成功)或 8(多数失败),取决于文件数
    assert!(rc >= 2, "文件被占用应返回错误码,实际 {}", rc);
    // drop handle 释放锁
    drop(_handle);
    cleanup(&base);
}

/// 取消(1223)不重试:cancel_token 存在时立即返回。
#[test]
fn cancel_not_retried() {
    let base = temp_dir("retry_cancel");
    let src = base.join("src");
    let dst = base.join("dst");
    write_file(&src.join("a.txt"), b"content");
    let cancel_token = base.join("cancel.flag");
    // 预先创建 cancel_token
    std::fs::write(&cancel_token, b"cancel").unwrap();

    let job = copy_job_with_cancel(&src, &dst, &cancel_token);
    let rc = engine::run(&job);
    // cancel → 返回 -1
    assert_eq!(rc, -1, "cancel 应返回 -1,实际 {}", rc);
    cleanup(&base);
}
