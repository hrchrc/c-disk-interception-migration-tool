//! integration_priority.rs — P6 后台低优先级模式端到端测试。
//!
//! 覆盖:
//! - background_mode=true 复制 job 正常完成(rc<8,内容一致)——低优先级不破坏功能
//! - background_mode=true + mirror purge 正常
//! - background_mode=false 对照
//!
//! 注意:进程级后台模式(PROCESS_MODE_BACKGROUND_BEGIN)是进程级互斥状态,
//! 测试并行跑多个 bg job 时后进入者会收到 402(已在后台模式),引擎按设计
//! 降级记录(enter_failed 事件)不阻断复制 —— 因此本文件不断言 enter 成功,
//! 只断言 rc/文件内容;enter 成功路径由 Python 侧测试(单 job 真实 exe)验证。
//! "事件流含 priority Info" 同样由 Python 侧测试验证(Engine::emit 写测试进程
//! stdout,cargo test 捕获管道无法可靠断言)。

mod common;

use common::*;
use rust_migrate_engine::engine;
use rust_migrate_engine::job::Job;
use std::path::Path;

/// 构造带 background_mode 的 copy job。
fn background_copy_job(src: &Path, dst: &Path, bg: bool) -> Job {
    job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "copy",
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}},
            "large_file_threshold_mb": 1,
            "fast_move_same_volume": false,
            "background_mode": {}
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\"),
        bg
    ))
}

/// background_mode=true:复制正常完成,内容一致。
#[test]
fn background_mode_copy_ok() {
    let base = temp_dir("pri_bg_copy");
    let src = base.join("src");
    let dst = base.join("dst");
    write_file(&src.join("a.txt"), &b"hello background\n".repeat(200));
    write_file(&src.join("sub/b.bin"), &b"\x00\x01\x02".repeat(5000));

    let job = background_copy_job(&src, &dst, true);
    let rc = engine::run(&job);

    assert!(rc < 8, "background_mode 复制失败 rc={}", rc);
    let src_tree = dir_tree_hash(&src);
    let dst_tree = dir_tree_hash(&dst);
    assert_eq!(src_tree, dst_tree, "background_mode 复制后内容不一致");
    cleanup(&base);
}

/// background_mode=true + mirror purge:全链路正常。
#[test]
fn background_mode_mirror_purge_ok() {
    let base = temp_dir("pri_bg_mir");
    let src = base.join("src");
    let dst = base.join("dst");
    write_file(&src.join("keep.txt"), b"mirror data");
    write_file(&dst.join("stale.txt"), b"should be purged");

    let job = job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "mirror",
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}},
            "large_file_threshold_mb": 1,
            "fast_move_same_volume": false,
            "purge": {{"enabled": true, "soft_delete": true, "dry_run": false}},
            "background_mode": true
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\")
    ));
    let rc = engine::run(&job);

    assert!(rc < 8, "background_mode mirror 失败 rc={}", rc);
    assert!(!dst.join("stale.txt").exists(), "stale 未被 purge");
    assert!(dst.join("keep.txt").exists(), "keep 未复制");
    cleanup(&base);
}

/// background_mode=false 对照:行为与未开启一致。
#[test]
fn normal_mode_unaffected() {
    let base = temp_dir("pri_norm");
    let src = base.join("src");
    let dst = base.join("dst");
    write_file(&src.join("a.txt"), b"normal data");

    let job = background_copy_job(&src, &dst, false);
    let rc = engine::run(&job);

    assert!(rc < 8, "普通模式复制失败 rc={}", rc);
    assert!(dst.join("a.txt").exists(), "文件未复制");
    cleanup(&base);
}
