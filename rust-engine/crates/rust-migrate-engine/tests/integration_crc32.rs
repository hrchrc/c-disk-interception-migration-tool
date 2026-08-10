//! integration_crc32.rs — CRC32 区间校验测试(P1 补丁)。
//!
//! 覆盖 v5 §4.2 末段:"已写字节数不完全可信,需要配合校验点"。
//! - 正常续传:cancel → 续传 → 数据一致
//! - 篡改目标:CRC32 检测不匹配 → 删目标重传 → 数据一致
//! - ckpt 文件含 crc32 字段

mod common;

use common::*;
use rust_migrate_engine::engine;
use std::thread;
use std::time::Duration;

/// 正常续传:cancel → ckpt 保存(含 CRC32) → 续传 → 数据一致。
#[test]
fn normal_resume_content_match() {
    let base = temp_dir("crc32_normal");
    let src = base.join("src");
    let dst = base.join("dst");
    let big = src.join("big.bin");
    write_large_file(&big, 80 * 1024 * 1024);

    let cancel_token = base.join("cancel.flag");
    let job = copy_job_with_cancel(&src, &dst, &cancel_token);

    // 子线程运行引擎
    let job_clone = copy_job_with_cancel(&src, &dst, &cancel_token);
    let handle = thread::spawn(move || engine::run(&job_clone));

    // 等 ckpt 出现
    let ckpt_path = dst.join("big.bin.migrate-ckpt");
    let mut waited = 0;
    while !ckpt_path.exists() && waited < 30 {
        thread::sleep(Duration::from_millis(200));
        waited += 1;
    }

    if ckpt_path.exists() {
        // 触发取消
        std::fs::write(&cancel_token, b"cancel").unwrap();
        let _ = handle.join().unwrap();

        // 验证 ckpt 含 crc32 字段
        let ckpt_content = std::fs::read_to_string(&ckpt_path).unwrap();
        assert!(
            ckpt_content.contains("\"crc32\""),
            "ckpt 缺少 crc32 字段: {}",
            ckpt_content
        );
        assert!(
            ckpt_content.contains("\"ckpt_base\""),
            "ckpt 缺少 ckpt_base 字段"
        );

        // 续传
        let resume_job = copy_job(&src, &dst);
        let rc = engine::run(&resume_job);
        assert!(rc < 8, "续传失败 rc={}", rc);

        // 数据一致
        let src_hash = file_md5(&big);
        let dst_hash = file_md5(&dst.join("big.bin"));
        assert_eq!(src_hash, dst_hash, "续传后数据不一致");
    } else {
        handle.join().unwrap();
        eprintln!("SKIP: ckpt 未生成(复制太快)");
    }
    cleanup(&base);
}

/// 篡改目标:CRC32 检测不匹配 → 删目标重传 → 数据一致。
#[test]
fn tampered_target_detected_and_recopied() {
    let base = temp_dir("crc32_tamper");
    let src = base.join("src");
    let dst = base.join("dst");
    let big = src.join("big.bin");
    write_large_file(&big, 80 * 1024 * 1024);

    let cancel_token = base.join("cancel.flag");
    let job = copy_job_with_cancel(&src, &dst, &cancel_token);

    // 子线程运行引擎
    let job_clone = copy_job_with_cancel(&src, &dst, &cancel_token);
    let handle = thread::spawn(move || engine::run(&job_clone));

    // 等 ckpt 出现
    let ckpt_path = dst.join("big.bin.migrate-ckpt");
    let mut waited = 0;
    while !ckpt_path.exists() && waited < 30 {
        thread::sleep(Duration::from_millis(200));
        waited += 1;
    }

    if ckpt_path.exists() {
        // 触发取消
        std::fs::write(&cancel_token, b"cancel").unwrap();
        let _ = handle.join().unwrap();

        // 读 ckpt 获取 ckpt_base
        let ckpt_content = std::fs::read_to_string(&ckpt_path).unwrap();
        let ckpt_json: serde_json::Value = serde_json::from_str(&ckpt_content).unwrap();
        let ckpt_base = ckpt_json["ckpt_base"].as_u64().unwrap();
        let written = ckpt_json["written"].as_u64().unwrap();

        // 篡改 [ckpt_base, written) 区间内 1 字节
        let tamper_offset = ckpt_base + 100;
        use std::io::{Seek, SeekFrom, Read, Write};
        let mut f = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(dst.join("big.bin"))
            .unwrap();
        f.seek(SeekFrom::Start(tamper_offset)).unwrap();
        let mut buf = [0u8; 1];
        f.read_exact(&mut buf).unwrap();
        f.seek(SeekFrom::Start(tamper_offset)).unwrap();
        f.write_all(&[buf[0] ^ 0xFF]).unwrap();
        drop(f);

        // 续传:CRC32 应检测到篡改 → 删目标重传
        let resume_job = copy_job(&src, &dst);
        let rc = engine::run(&resume_job);
        assert!(rc < 8, "篡改后续传失败 rc={}", rc);

        // 数据一致(整文件重传后应正确)
        let src_hash = file_md5(&big);
        let dst_hash = file_md5(&dst.join("big.bin"));
        assert_eq!(
            src_hash, dst_hash,
            "CRC32 检测篡改后重传,数据仍不一致"
        );
    } else {
        handle.join().unwrap();
        eprintln!("SKIP: ckpt 未生成(复制太快)");
    }
    cleanup(&base);
}
