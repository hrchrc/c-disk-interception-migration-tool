//! 复现/回归:BUG-12 防护 —— 源文件在复制期间被截断/变化(written != size)。
//!
//! 背景(2026-08-09 glm-pc-updater 事故):更新器在 walk 读取元数据后、
//! 引擎打开文件前原子替换了源文件(新文件更短),复制读到 EOF 早退,
//! written != size。旧代码返回 Err(87) 作为内部哨兵,Python 侧按真实
//! Win32 错误码翻译成"参数错误/路径包含非法字符"——误导性诊断。
//!
//! 本测试直接调用 copy_large_unbuffered,传入比实际内容大的 size
//! (模拟 walk 记录的过期元数据),确定性触发 BUG-12:
//! 断言返回 Err(ERR_SOURCE_CHANGED = 0xE0000001),而不是 Win32 87。

mod common;

use rust_migrate_engine::retry::{classify, RetryKind};
use rust_migrate_engine::{unbuffered, ERR_SOURCE_CHANGED};
use std::fs;

#[test]
fn source_truncated_between_metadata_and_open_returns_engine_code() {
    let root = common::temp_dir("src_changed");
    let src_dir = root.join("src");
    let dst_dir = root.join("dst");
    fs::create_dir_all(&src_dir).unwrap();
    fs::create_dir_all(&dst_dir).unwrap();

    // 源文件:先写入 32MB(walk 元数据记录的 size),再截断为 1KB
    // (模拟更新器在元数据读取后把文件替换成更短的版本)
    let src_file = src_dir.join("installer.bin");
    let dst_file = dst_dir.join("installer.bin");
    let recorded_size: u64 = 32 * 1024 * 1024; // walk 记录的过期大小
    common::write_large_file(&src_file, recorded_size as usize);
    {
        let f = fs::OpenOptions::new().write(true).open(&src_file).unwrap();
        f.set_len(1024).unwrap();
    }

    let job = common::copy_job(&src_dir, &dst_dir);
    let err = unbuffered::copy_large_unbuffered(&src_file, &dst_file, recorded_size, &job)
        .expect_err("源文件比记录大小短,必须报错");

    assert_eq!(
        err, ERR_SOURCE_CHANGED,
        "BUG-12 哨兵必须返回引擎内部码 0xE0000001,而非误导性的 Win32 87"
    );

    // 目标残留:部分写入,大小 < 记录大小(不允许 truncate 到 size 产生空洞)
    let dest_size = fs::metadata(&dst_file).map(|m| m.len()).unwrap_or(0);
    assert!(
        dest_size > 0 && dest_size < recorded_size,
        "目标应为部分文件(0 < {} < {}),实际 {}",
        dest_size, recorded_size, dest_size
    );

    // 内部码必须归 Fatal(不允许落入默认 Retry 分支被重试)
    assert_eq!(classify(ERR_SOURCE_CHANGED), RetryKind::Fatal);

    common::cleanup(&root);
}
