//! integration_purge.rs — purge 安全性测试(文档 §7.2)。
//!
//! 覆盖:
//! - dry-run 不删除任何文件
//! - 软删除到回收站
//! - purge 不跨目录(目标外文件不被触碰)
//! - 嵌套路径被拒绝(防误删源)
//! - 相同路径被拒绝

mod common;

use common::*;
use rust_migrate_engine::engine;
use std::path::Path;

/// dry-run 模式不删除任何文件。
#[test]
fn dry_run_no_delete() {
    let base = temp_dir("purge_dry");
    let src = base.join("src");
    let dst = base.join("dst");
    write_file(&src.join("keep.txt"), b"source");
    write_file(&dst.join("stale.txt"), b"should not be deleted");

    let job = mirror_job(&src, &dst, true, true); // dry_run=true
    let rc = engine::run(&job);
    assert!(rc < 8, "dry-run 失败");

    assert!(
        dst.join("stale.txt").exists(),
        "dry-run 删除了文件(不应删除)"
    );
    cleanup(&base);
}

/// purge 不跨目录:目标外的文件不被触碰。
#[test]
fn purge_does_not_cross_dirs() {
    let base = temp_dir("purge_cross");
    let src = base.join("src");
    let dst = base.join("dst");
    let outside = base.join("outside");
    write_file(&src.join("a.txt"), b"source");
    write_file(&dst.join("stale_in_target.txt"), b"in target");
    write_file(&outside.join("outside.txt"), b"outside, should not be touched");

    let outside_md5_before = file_md5(&outside.join("outside.txt"));

    let job = mirror_job(&src, &dst, true, false);
    let rc = engine::run(&job);
    assert!(rc < 8, "mirror 失败");

    // 目标内 stale 被删
    assert!(
        !dst.join("stale_in_target.txt").exists(),
        "目标内 stale 未被 purge"
    );
    // 目标外文件未被触碰
    assert!(
        outside.join("outside.txt").exists(),
        "目标外文件被删除"
    );
    assert_eq!(
        file_md5(&outside.join("outside.txt")),
        outside_md5_before,
        "目标外文件内容被修改"
    );
    cleanup(&base);
}

/// 嵌套路径被拒绝:source 包含 target。
#[test]
fn nested_source_contains_target_rejected() {
    let base = temp_dir("purge_nest1");
    let src = base.join("src");
    let dst = src.join("nested_dst"); // target 在 source 内
    std::fs::create_dir_all(&src).unwrap();
    write_file(&src.join("a.txt"), b"test");

    let job = mirror_job(&src, &dst, true, false);
    let rc = engine::run(&job);
    assert_eq!(rc, 16, "嵌套路径应返回 16,实际 {}", rc);
    cleanup(&base);
}

/// 嵌套路径被拒绝:target 包含 source。
#[test]
fn nested_target_contains_source_rejected() {
    let base = temp_dir("purge_nest2");
    let dst = base.join("outer");
    let src = dst.join("inner_src"); // source 在 target 内
    std::fs::create_dir_all(&src).unwrap();
    write_file(&src.join("a.txt"), b"test");

    let job = mirror_job(&src, &dst, true, false);
    let rc = engine::run(&job);
    assert_eq!(rc, 16, "反向嵌套路径应返回 16,实际 {}", rc);
    cleanup(&base);
}

/// 相同路径被拒绝。
#[test]
fn same_path_rejected() {
    let base = temp_dir("purge_same");
    std::fs::create_dir_all(&base).unwrap();
    write_file(&base.join("a.txt"), b"test");

    let job = copy_job(&base, &base);
    let rc = engine::run(&job);
    assert_eq!(rc, 16, "相同路径应返回 16,实际 {}", rc);
    cleanup(&base);
}
