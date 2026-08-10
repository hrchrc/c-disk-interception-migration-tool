//! integration_sparse.rs — 稀疏文件复制端到端测试(P4 补缺,对应 v5 §4.5)。
//!
//! 覆盖:
//! - 稀疏文件复制后目标仍为稀疏(FSCTL_SET_SPARSE 保留)
//! - 空洞不搬运:实际占用远小于表面大小(不按表面大小膨胀)
//! - 内容全量一致(逐字节比对)
//! - 多数据段稀疏文件(0/10MB/50MB 三段)
//! - 稀疏 + ACL 复制共存(copy_acl 开启时稀疏语义不破坏)
//!
//! 测试需要 Windows(NTFS;FAT32 不支持稀疏,测试会跳过)。

mod common;

use common::*;
use rust_migrate_engine::{engine, win_io};
use std::path::Path;

/// 构造稀疏文件:FSCTL_SET_SPARSE 后按 (offset, data) 段写入,段间留空洞,
/// 末尾扩展到最后一段末尾(与真实稀疏文件形态一致)。
fn make_sparse_file(path: &Path, segments: &[(u64, &[u8])]) {
    let h = win_io::open_target(path, false, false, true).expect("创建稀疏文件");
    win_io::set_sparse(&h).expect("设置稀疏标志");
    for (off, data) in segments {
        win_io::set_file_pointer(&h, *off).expect("定位段起点");
        let mut written = 0;
        while written < data.len() {
            let n = win_io::write(&h, &data[written..]).expect("写入段数据");
            written += n;
        }
    }
    let end = segments
        .last()
        .map(|(o, d)| o + d.len() as u64)
        .unwrap_or(0);
    win_io::set_file_pointer(&h, end).expect("定位文件末尾");
    win_io::set_end_of_file(&h).expect("扩展文件大小");
}

/// 构造 copy 模式 job。
fn sparse_job(src: &Path, dst: &Path, copy_acl: bool) -> rust_migrate_engine::job::Job {
    job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "copy",
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}},
            "large_file_threshold_mb": 1,
            "fast_move_same_volume": false,
            "copy_acl": {}
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\"),
        copy_acl
    ))
}

/// 实际占用(MB)。
fn alloc_mb(path: &Path) -> u64 {
    win_io::sparse_alloc_size(path) / (1024 * 1024)
}

/// 4KB 段数据(可辨识 pattern,逐字节比对用)。
fn seg_data(tag: u8) -> Vec<u8> {
    (0..4096).map(|i| tag.wrapping_add((i % 251) as u8)).collect()
}

/// 复制目录树并返回目标稀疏文件路径。
fn run_copy(src_dir: &Path, dst_dir: &Path, copy_acl: bool) -> (std::path::PathBuf, i32) {
    let job = sparse_job(src_dir, dst_dir, copy_acl);
    let rc = engine::run(&job);
    (dst_dir.join("sparse.bin"), rc)
}

#[test]
fn sparse_copy_preserves_sparseness() {
    let tmp = temp_dir("sparse_1");
    let src_dir = tmp.join("src");
    let dst_dir = tmp.join("dst");
    std::fs::create_dir_all(&src_dir).unwrap();

    // 100MB 表面大小:首部 4KB + 尾部 4KB,中间 ~99.99MB 空洞
    let head = seg_data(0xA0);
    let tail = seg_data(0xB0);
    let surface = 100u64 * 1024 * 1024;
    make_sparse_file(
        &src_dir.join("sparse.bin"),
        &[(0, &head), (surface - tail.len() as u64, &tail)],
    );

    assert!(win_io::is_sparse(&src_dir.join("sparse.bin")), "源应是稀疏文件");
    let src_alloc = alloc_mb(&src_dir.join("sparse.bin"));
    assert!(src_alloc < 5, "源占用应远小于表面大小, 实际 {}MB", src_alloc);

    let (dst, rc) = run_copy(&src_dir, &dst_dir, false);
    assert_eq!(rc, 1, "复制应成功 rc=1, 实际 {}", rc);
    assert!(dst.exists(), "目标文件应存在");
    assert!(win_io::is_sparse(&dst), "目标应保持稀疏标志");
    assert_eq!(
        std::fs::read(&src_dir.join("sparse.bin")).unwrap(),
        std::fs::read(&dst).unwrap(),
        "目标内容应与源逐字节一致"
    );
    let dst_alloc = alloc_mb(&dst);
    assert!(dst_alloc < 5, "目标占用不应膨胀(空洞未搬运), 实际 {}MB", dst_alloc);

    std::fs::remove_dir_all(&tmp).ok();
}

#[test]
fn sparse_copy_multiple_ranges() {
    let tmp = temp_dir("sparse_2");
    let src_dir = tmp.join("src");
    let dst_dir = tmp.join("dst");
    std::fs::create_dir_all(&src_dir).unwrap();

    // 3 个数据段:offset 0 / 10MB / 50MB,各 4KB
    let s1 = seg_data(0x11);
    let s2 = seg_data(0x22);
    let s3 = seg_data(0x33);
    make_sparse_file(
        &src_dir.join("sparse.bin"),
        &[(0, &s1), (10 * 1024 * 1024, &s2), (50 * 1024 * 1024, &s3)],
    );

    let (dst, rc) = run_copy(&src_dir, &dst_dir, false);
    assert_eq!(rc, 1, "复制应成功 rc=1, 实际 {}", rc);
    assert!(win_io::is_sparse(&dst));
    assert_eq!(
        std::fs::read(&src_dir.join("sparse.bin")).unwrap(),
        std::fs::read(&dst).unwrap(),
        "多段稀疏文件内容应一致"
    );
    let dst_alloc = alloc_mb(&dst);
    assert!(dst_alloc < 5, "3 段 12KB 数据目标占用应远小于 50MB 表面大小, 实际 {}MB", dst_alloc);

    std::fs::remove_dir_all(&tmp).ok();
}

#[test]
fn sparse_copy_with_acl() {
    let tmp = temp_dir("sparse_3");
    let src_dir = tmp.join("src");
    let dst_dir = tmp.join("dst");
    std::fs::create_dir_all(&src_dir).unwrap();

    let seg = seg_data(0x5A);
    make_sparse_file(&src_dir.join("sparse.bin"), &[(0, &seg), (20 * 1024 * 1024, &seg)]);

    // copy_acl 开启:稀疏语义必须保持(ACL/ADS 段与稀疏分支共存回归)
    let (dst, rc) = run_copy(&src_dir, &dst_dir, true);
    assert_eq!(rc, 1, "带 ACL 的稀疏复制应成功 rc=1, 实际 {}", rc);
    assert!(win_io::is_sparse(&dst), "开启 copy_acl 后目标仍应保持稀疏");
    assert_eq!(
        std::fs::read(&src_dir.join("sparse.bin")).unwrap(),
        std::fs::read(&dst).unwrap()
    );
    assert!(alloc_mb(&dst) < 5, "开启 copy_acl 后占用仍不应膨胀");

    std::fs::remove_dir_all(&tmp).ok();
}

// ============================================================
// 断点续传:构造"中断现场"(目标已写部分区间 + sidecar ckpt)后重跑引擎
// ============================================================

const LARGE_BLOCK: u32 = 4 * 1024 * 1024; // 与 engine.rs LARGE_BLOCK 一致(ckpt.block_size 校验)

/// 手工构造"目标已写前 N 段 + ckpt"的中断现场(等价于复制中途断电)。
fn make_interrupted_target(
    dst_file: &Path,
    src_file: &Path,
    segments: &[(u64, &[u8])],
    done_count: usize,
) {
    let h = win_io::open_for_append(dst_file, false, false).expect("打开目标");
    win_io::set_sparse(&h).expect("设置稀疏");
    for (off, data) in &segments[..done_count] {
        win_io::set_file_pointer(&h, *off).expect("定位段起点");
        let mut w = 0;
        while w < data.len() {
            let n = win_io::write(&h, &data[w..]).expect("写入段");
            w += n;
        }
    }
    win_io::flush(&h).expect("flush");
    let written = segments[done_count - 1].0 + segments[done_count - 1].1.len() as u64;
    let size = std::fs::metadata(src_file).unwrap().len();
    let crc =
        rust_migrate_engine::checkpoint::compute_interval_crc32(dst_file, 0, written)
            .expect("计算区间 CRC32");
    let ckpt = rust_migrate_engine::checkpoint::Checkpoint {
        target: dst_file.to_string_lossy().into_owned(),
        source_size: size,
        written,
        block_size: LARGE_BLOCK,
        ckpt_base: 0,
        crc32: crc,
    };
    ckpt.save(dst_file).expect("保存 ckpt");
}

#[test]
fn sparse_resume_from_checkpoint() {
    use rust_migrate_engine::checkpoint::Checkpoint;

    let tmp = temp_dir("sparse_r1");
    let src_dir = tmp.join("src");
    let dst_dir = tmp.join("dst");
    std::fs::create_dir_all(&src_dir).unwrap();
    std::fs::create_dir_all(&dst_dir).unwrap();
    let src_file = src_dir.join("sparse.bin");
    let dst_file = dst_dir.join("sparse.bin");

    // 3 段稀疏源(0 / 10MB / 50MB)
    let s1 = seg_data(0x11);
    let s2 = seg_data(0x22);
    let s3 = seg_data(0x33);
    make_sparse_file(
        &src_file,
        &[(0, &s1), (10 * 1024 * 1024, &s2), (50 * 1024 * 1024, &s3)],
    );

    // 中断现场:目标已写前 2 段 + ckpt(written=第 2 段末尾)
    make_interrupted_target(&dst_file, &src_file, &[(0, &s1), (10 * 1024 * 1024, &s2), (50 * 1024 * 1024, &s3)], 2);
    assert!(Checkpoint::path_for(&dst_file).exists(), "ckpt 应存在");

    // 重跑引擎 → 应从第 3 段续传完成
    let job = sparse_job(&src_dir, &dst_dir, false);
    let rc = engine::run(&job);
    assert_eq!(rc, 1, "续传复制应成功 rc=1, 实际 {}", rc);
    assert!(win_io::is_sparse(&dst_file), "续传后目标仍应保持稀疏");
    assert_eq!(
        std::fs::read(&src_file).unwrap(),
        std::fs::read(&dst_file).unwrap(),
        "续传后内容应与源逐字节一致"
    );
    assert!(alloc_mb(&dst_file) < 5, "续传后占用仍不应膨胀");
    assert!(!Checkpoint::path_for(&dst_file).exists(), "完成后 ckpt 应删除");

    std::fs::remove_dir_all(&tmp).ok();
}

#[test]
fn sparse_resume_corrupt_ckpt_retransfers() {
    use rust_migrate_engine::checkpoint::Checkpoint;

    let tmp = temp_dir("sparse_r2");
    let src_dir = tmp.join("src");
    let dst_dir = tmp.join("dst");
    std::fs::create_dir_all(&src_dir).unwrap();
    std::fs::create_dir_all(&dst_dir).unwrap();
    let src_file = src_dir.join("sparse.bin");
    let dst_file = dst_dir.join("sparse.bin");

    let s1 = seg_data(0x41);
    let s2 = seg_data(0x42);
    let s3 = seg_data(0x43);
    make_sparse_file(
        &src_file,
        &[(0, &s1), (10 * 1024 * 1024, &s2), (50 * 1024 * 1024, &s3)],
    );
    make_interrupted_target(&dst_file, &src_file, &[(0, &s1), (10 * 1024 * 1024, &s2), (50 * 1024 * 1024, &s3)], 2);

    // 篡改 ckpt 的 CRC32 → load 校验失败(模拟断电丢数据/损坏)→ 整文件重传
    let ckpt_path = Checkpoint::path_for(&dst_file);
    let mut ckpt: Checkpoint =
        serde_json::from_slice(&std::fs::read(&ckpt_path).unwrap()).expect("解析 ckpt");
    ckpt.crc32 ^= 0xFFFF_FFFF;
    std::fs::write(&ckpt_path, serde_json::to_vec(&ckpt).unwrap()).unwrap();

    let job = sparse_job(&src_dir, &dst_dir, false);
    let rc = engine::run(&job);
    assert_eq!(rc, 1, "损坏 ckpt 应整文件重传并成功 rc=1, 实际 {}", rc);
    assert!(win_io::is_sparse(&dst_file), "重传后目标仍应保持稀疏");
    assert_eq!(
        std::fs::read(&src_file).unwrap(),
        std::fs::read(&dst_file).unwrap(),
        "重传后内容应与源逐字节一致"
    );
    assert!(!Checkpoint::path_for(&dst_file).exists(), "完成后 ckpt 应删除");

    std::fs::remove_dir_all(&tmp).ok();
}

#[test]
fn sparse_copy_overwrites_stale_target() {
    let tmp = temp_dir("sparse_r3");
    let src_dir = tmp.join("src");
    let dst_dir = tmp.join("dst");
    std::fs::create_dir_all(&src_dir).unwrap();
    std::fs::create_dir_all(&dst_dir).unwrap();
    let src_file = src_dir.join("sparse.bin");
    let dst_file = dst_dir.join("sparse.bin");

    let s1 = seg_data(0x61);
    let s2 = seg_data(0x62);
    make_sparse_file(&src_file, &[(0, &s1), (10 * 1024 * 1024, &s2)]);

    // 目标预置陈旧垃圾数据(无 ckpt,模拟上次失败残留/用户放的同名文件):
    // 陈旧数据必须被截断清掉,空洞位置不得残留(否则内容不一致)
    std::fs::write(&dst_file, vec![0xEEu8; 20 * 1024 * 1024]).unwrap();

    let job = sparse_job(&src_dir, &dst_dir, false);
    let rc = engine::run(&job);
    assert_eq!(rc, 1, "覆盖陈旧目标应成功 rc=1, 实际 {}", rc);
    assert!(win_io::is_sparse(&dst_file), "覆盖后目标应为稀疏");
    assert_eq!(
        std::fs::read(&src_file).unwrap(),
        std::fs::read(&dst_file).unwrap(),
        "覆盖后内容应与源逐字节一致(空洞处为 0,无残留)"
    );

    std::fs::remove_dir_all(&tmp).ok();
}
