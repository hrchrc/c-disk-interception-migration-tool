//! integration_mir.rs — mirror 模式端到端测试(文档 §7.2)。
//!
//! 覆盖:
//! - mirror 复制后源和目标内容完全一致
//! - mirror purge 删除目标多余文件
//! - mirror 后源目录不被修改
//! - 空目录保留
//! - 多层级目录树完整性

mod common;

use common::*;
use rust_migrate_engine::engine;
use std::path::Path;

/// mirror 复制后内容一致 + purge 删除目标多余文件。
#[test]
fn mirror_content_equal_and_purge() {
    let base = temp_dir("mir_purge");
    let src = base.join("src");
    let dst = base.join("dst");
    write_file(&src.join("a.txt"), &b"hello world\n".repeat(100));
    write_file(&src.join("sub/b.txt"), &b"sub content\n".repeat(50));
    // 目标预置 stale 文件(应被 purge)
    write_file(&dst.join("stale.txt"), b"should be deleted");

    let job = mirror_job(&src, &dst, true, false);
    let rc = engine::run(&job);

    assert!(rc < 8, "mirror 失败 rc={}", rc);
    // 内容一致
    let src_tree = dir_tree_hash(&src);
    let dst_tree = dir_tree_hash(&dst);
    assert_eq!(src_tree, dst_tree, "mirror 后源和目标内容不一致");
    // stale 被删除
    assert!(!dst.join("stale.txt").exists(), "stale 文件未被 purge");
    cleanup(&base);
}

/// mirror 后源目录不被修改。
#[test]
fn mirror_source_not_modified() {
    let base = temp_dir("mir_src");
    let src = base.join("src");
    let dst = base.join("dst");
    write_file(&src.join("keep.txt"), b"source data");
    let src_before = dir_tree_hash(&src);

    let job = mirror_job(&src, &dst, true, false);
    let rc = engine::run(&job);
    assert!(rc < 8, "mirror 失败");

    let src_after = dir_tree_hash(&src);
    assert_eq!(src_before, src_after, "mirror 导致源目录被修改");
    cleanup(&base);
}

/// 空目录保留(copy /E 等价)。
#[test]
fn copy_preserves_empty_dirs() {
    let base = temp_dir("mir_empty");
    let src = base.join("src");
    let dst = base.join("dst");
    std::fs::create_dir_all(src.join("empty_dir")).unwrap();
    write_file(&src.join("a.txt"), b"data");

    let job = copy_job(&src, &dst);
    let rc = engine::run(&job);
    assert!(rc < 8, "copy 失败");

    assert!(dst.join("empty_dir").is_dir(), "空目录未保留");
    cleanup(&base);
}

/// 多层级目录树完整性(5 层深度)。
#[test]
fn deep_tree_integrity() {
    let base = temp_dir("mir_deep");
    let src = base.join("src");
    let dst = base.join("dst");
    let mut deep = src.clone();
    for level in 0..5 {
        deep = deep.join(format!("level{}", level));
        std::fs::create_dir_all(&deep).unwrap();
        write_file(
            &deep.join(format!("file{}.txt", level)),
            &format!("level {} content\n", level).as_bytes().repeat(100),
        );
    }

    let job = copy_job(&src, &dst);
    let rc = engine::run(&job);
    assert!(rc < 8, "deep tree copy 失败");

    let src_tree = dir_tree_hash(&src);
    let dst_tree = dir_tree_hash(&dst);
    assert_eq!(src_tree, dst_tree, "深层目录树内容不一致");
    cleanup(&base);
}
