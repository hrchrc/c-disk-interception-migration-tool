//! integration_verify.rs — BLAKE3 内容校验测试(P5+,verify: "hash" / mode=verify)。
//!
//! 覆盖:
//! - 复制后 verify=hash:内容一致 → rc=1(复制+校验一体路径)
//! - mode=verify:只校验不复制,内容一致 → rc=1
//! - 目标被篡改 → 检测不一致 → rc=8(VerifyMismatch 计入 errors)
//! - 目标文件缺失 → 读取失败计入 errors → rc=8

mod common;

use common::*;
use rust_migrate_engine::engine;

/// 复制后 verify=hash:内容一致 → rc=1。
#[test]
fn verify_consistent_tree_passes() {
    let base = temp_dir("verify_ok");
    let src = base.join("src");
    let dst = base.join("dst");
    std::fs::create_dir_all(&src).unwrap();
    std::fs::create_dir_all(src.join("sub")).unwrap();
    write_file(&src.join("a.txt"), b"hello");
    write_file(&src.join("b.bin"), b"data-bytes-12345");
    write_file(&src.join("sub").join("c.txt"), b"nested");

    // copy + verify=hash 一体:复制完成后自动逐文件 BLAKE3 校验
    let job = job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "copy",
            "verify": "hash",
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}},
            "fast_move_same_volume": false
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\"),
    ));
    let rc = engine::run(&job);
    assert_eq!(rc, 1, "复制+校验一致应返回 1,实际 {}", rc);
    cleanup(&base);
}

/// mode=verify:只校验不复制,内容一致 → rc=1。
#[test]
fn verify_mode_only_checks() {
    let base = temp_dir("verify_mode");
    let src = base.join("src");
    let dst = base.join("dst");
    std::fs::create_dir_all(&src).unwrap();
    write_file(&src.join("a.txt"), b"content-a");
    write_file(&src.join("b.txt"), b"content-b");

    // 先正常复制(无校验)
    let copy_job = copy_job(&src, &dst);
    assert_eq!(engine::run(&copy_job), 1);

    // 再只校验
    let job = job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "verify",
            "verify": "hash",
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}},
            "fast_move_same_volume": false
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\"),
    ));
    let rc = engine::run(&job);
    assert_eq!(rc, 1, "只校验一致应返回 1,实际 {}", rc);
    cleanup(&base);
}

/// 目标被篡改 → mode=verify 检测不一致 → rc=8。
#[test]
fn verify_detects_tamper() {
    let base = temp_dir("verify_tamper");
    let src = base.join("src");
    let dst = base.join("dst");
    std::fs::create_dir_all(&src).unwrap();
    write_file(&src.join("a.txt"), b"original-content");
    write_file(&src.join("b.txt"), b"another-file");

    assert_eq!(engine::run(&copy_job(&src, &dst)), 1);

    // 篡改目标 a.txt
    write_file(&dst.join("a.txt"), b"TAMPERED!!");

    let job = job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "verify",
            "verify": "hash",
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}},
            "fast_move_same_volume": false
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\"),
    ));
    let rc = engine::run(&job);
    assert_eq!(rc, 8, "内容不一致应返回 8,实际 {}", rc);
    cleanup(&base);
}

/// 目标文件缺失 → mode=verify 检测 → rc=8。
#[test]
fn verify_detects_missing_target() {
    let base = temp_dir("verify_missing");
    let src = base.join("src");
    let dst = base.join("dst");
    std::fs::create_dir_all(&src).unwrap();
    write_file(&src.join("a.txt"), b"content-a");
    write_file(&src.join("b.txt"), b"content-b");

    assert_eq!(engine::run(&copy_job(&src, &dst)), 1);

    // 删除目标 b.txt(模拟迁移丢失)
    std::fs::remove_file(dst.join("b.txt")).unwrap();

    let job = job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "verify",
            "verify": "hash",
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}},
            "fast_move_same_volume": false
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\"),
    ));
    let rc = engine::run(&job);
    assert_eq!(rc, 8, "目标缺失应返回 8,实际 {}", rc);
    cleanup(&base);
}
