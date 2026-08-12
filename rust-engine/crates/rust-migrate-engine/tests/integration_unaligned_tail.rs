//! 回归:非扇区对齐尾部的无缓冲复制(sync + IOCP 两条路径)。
//!
//! 背景(2026-08-09 updater 事故):installer.exe 160622764 字节
//! (mod 4096 = 2220),C:→E: 跨盘走 copy_unbuffered_iocp。最后一块写入
//! 按扇区对齐 pad 后,收尾 `SetEndOfFile(非对齐大小)` 在无缓冲句柄上
//! 返回 ERROR_INVALID_PARAMETER(87) → 复制失败(实测 sync 路径同样复现)。
//! 此前 C:→C: 测试未复现是因为自适应缓存热启动走了 CopyFileW 掩盖了 bug,
//! 故本测试必须 adaptive_cache=false 强制无缓冲路径。
//!
//! 修复:收尾 truncate 改用缓冲句柄(缓冲句柄非对齐截断实测可靠)。

mod common;

use rust_migrate_engine::job::Job;
use rust_migrate_engine::engine;
use std::path::{Path, PathBuf};

/// 构造强制无缓冲(adaptive_cache=false)的 copy Job,大文件阈值 1MB。
/// disk_mode: "same"=sync 路径,"diff"=IOCP 路径。
fn no_adaptive_job(src: &Path, dst: &Path, disk: &str) -> Job {
    common::job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "copy",
            "disk_mode": "{}",
            "adaptive_cache": false,
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}},
            "large_file_threshold_mb": 1,
            "fast_move_same_volume": false,
            "verify": "hash"
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\"),
        disk
    ))
}

/// 非扇区对齐大小:4MB + 2220 字节(mod 4096 = 2220,与事故文件同款尾部形态)。
/// 也覆盖 512 扇区场景:2220 mod 512 = 172 ≠ 0。
const UNALIGNED_SIZE: usize = 4 * 1024 * 1024 + 2220;

fn run_unaligned_copy(disk: &str) {
    let root = common::temp_dir(&format!("unalign_{}", disk));
    let src_dir = root.join("src");
    let dst_dir = root.join("dst");
    std::fs::create_dir_all(&src_dir).unwrap();

    common::write_large_file(&src_dir.join("big.bin"), UNALIGNED_SIZE);
    // 顺带一个小文件验证 pipeline 路径不受影响
    common::write_file(&src_dir.join("small.txt"), b"hello unaligned tail");

    let job = no_adaptive_job(&src_dir, &dst_dir, disk);
    let rc = engine::run(&job);
    assert_eq!(
        rc, 1,
        "{} 路径复制非对齐尾部文件必须成功(rc=1),实际 rc={}",
        disk, rc
    );

    // 内容一致性:目标树 hash 与源一致
    let src_hash = common::dir_tree_hash(&src_dir);
    let dst_hash = common::dir_tree_hash(&dst_dir);
    assert_eq!(src_hash, dst_hash, "{} 路径目标树与源树内容不一致", disk);

    common::cleanup(&root);
}

#[test]
fn sync_unaligned_tail_copy_succeeds() {
    run_unaligned_copy("same");
}

#[test]
fn iocp_unaligned_tail_copy_succeeds() {
    run_unaligned_copy("diff");
}
