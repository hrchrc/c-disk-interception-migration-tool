//! P5+:BLAKE3 内容校验(verify: "hash")。
//!
//! 复制完成后逐文件计算源/目标 BLAKE3 哈希对比,保证"不丢数据"——
//! 这是复制引擎新增能力(旧版只比对大小/时间戳)。
//!
//! 2026-08-05 优化(A+B,42 万文件实测 33 分钟 → 目标 ~22 分钟):
//! - 优化 A:复制阶段 reader 顺带记录源哈希(Shared.src_hashes),校验查表免读源,
//!   省一半校验 I/O(源 31GB);查表缺失(内联/大文件/hardlink 路径)回退读源。
//! - 优化 B:多线程并行校验(42 万小文件,单线程顺序读是寻道瓶颈;
//!   与复制流水线同经验,线程预算由调用方传入)。
//! - 校验统计/进度用原子计数,Progress 事件 fetch_max 去重(与流水线一致)。

use crate::event::Event;
use crate::job::Job;
use crate::reparse;
use crate::win_io;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, AtomicU64, AtomicUsize, Ordering};
use std::time::Instant;

/// 校验结果统计。
pub struct VerifyStats {
    pub files: u64,
    pub bytes: u64,
    pub mismatches: u32,
    pub errors: u32,
}

impl Default for VerifyStats {
    fn default() -> Self {
        VerifyStats {
            files: 0,
            bytes: 0,
            mismatches: 0,
            errors: 0,
        }
    }
}

// 读线程整文件读取的复用缓冲(避免 42 万文件 × 256KB 重复 malloc/memset)。
thread_local! {
    static HASH_BUF: std::cell::RefCell<Vec<u8>> =
        std::cell::RefCell::new(vec![0u8; 256 * 1024]);
}

/// 校验整棵树:源 vs 目标,逐文件 BLAKE3 哈希对比。
/// base: 校验前的进度偏移(复制后校验时传复制阶段的 files/bytes,
/// 使 Progress 事件连续不回退;mode=verify 纯校验时传 (0, 0))。
/// src_hashes: 复制阶段记录的源哈希表(优化 A);None 或查表缺失 → 读源计算。
/// threads: 校验并行线程数(与复制线程预算同源)。
/// 差异/错误通过事件上报,返回统计。取消返回 Err(1223)。
pub fn verify_tree(
    src: &Path,
    dst: &Path,
    job: &Job,
    base: (u64, u64),
    src_hashes: Option<&HashMap<PathBuf, [u8; 32]>>,
    threads: usize,
) -> Result<VerifyStats, u32> {
    let start = Instant::now();

    // 1. 收集文件对(只遍历目录结构,不读内容;reparse/链接跳过,与原逻辑一致)
    let mut pairs: Vec<(PathBuf, PathBuf)> = Vec::new();
    collect_pairs(src, dst, job, &mut pairs)?;
    let total = pairs.len();
    if total == 0 {
        return Ok(VerifyStats::default());
    }

    // 校验开始标记：copy+verify 一体时供 UI 区分"复制阶段→校验阶段"
    // （复制完成事件与校验事件同构，无此标记 UI 无法切换进度文案）
    Event::Info {
        key: "verify_start".to_string(),
        value: format!("开始: {} 个文件", total),
    }
    .emit();

    // 2. 并行校验:原子取任务下标,每 worker 线程独立处理
    let files = AtomicU64::new(0);
    let bytes = AtomicU64::new(0);
    let mismatches = AtomicU32::new(0);
    let errors = AtomicU32::new(0);
    let last_progress = AtomicU64::new(0);
    let idx = AtomicUsize::new(0);
    let n_threads = threads.clamp(1, total);

    std::thread::scope(|s| {
        for _ in 0..n_threads {
            s.spawn(|| {
                loop {
                    if job.cancel_requested() {
                        return; // 取消:部分完成,scope 结束后统一判 Err(1223)
                    }
                    let i = idx.fetch_add(1, Ordering::Relaxed);
                    if i >= total {
                        break;
                    }
                    let (s, d) = &pairs[i];
                    verify_one(
                        s, d, src_hashes,
                        &files, &bytes, &mismatches, &errors,
                        &last_progress, start, base,
                    );
                }
            });
        }
    });

    if job.cancel_requested() {
        return Err(1223);
    }
    let stats = VerifyStats {
        files: files.load(Ordering::Relaxed),
        bytes: bytes.load(Ordering::Relaxed),
        mismatches: mismatches.load(Ordering::Relaxed),
        errors: errors.load(Ordering::Relaxed),
    };
    if stats.files > 0 {
        Event::Info {
            key: "verify".to_string(),
            value: format!(
                "完成: {} 文件 / {:.2} GB,不一致 {}",
                stats.files,
                stats.bytes as f64 / 1073741824.0,
                stats.mismatches
            ),
        }
        .emit();
    }
    Ok(stats)
}

/// 递归遍历源树,收集 (源, 目标) 文件对(不读内容)。
/// 与 engine::walk_collect 保持集合一致(BUG 修复):
/// reparse_mode="copy" 时 reparse 复制的是链接本身,内容不参与校验;
/// 若这里不截断,Windows 上 Junction 的 is_dir() 为 true → 递归跟随链接,
/// 会把链接指向的内容当校验对象,而目标只有链接本身 → 误报 FileError。
fn collect_pairs(
    src: &Path,
    dst: &Path,
    job: &Job,
    pairs: &mut Vec<(PathBuf, PathBuf)>,
) -> Result<(), u32> {
    if job.cancel_requested() {
        return Err(1223);
    }
    let entries = std::fs::read_dir(src).map_err(io_err)?;
    for entry in entries {
        if job.cancel_requested() {
            return Err(1223);
        }
        let entry = entry.map_err(io_err)?;
        let ft = entry.file_type().map_err(io_err)?;
        let from = entry.path();
        let to = dst.join(entry.file_name());
        // 与 walk_collect 对齐:reparse(目录 Junction/文件符号链接)跳过
        if job.reparse_mode == "copy" {
            if let Ok(Some(_)) = reparse::read_reparse(&from) {
                continue;
            }
        }
        if ft.is_dir() {
            collect_pairs(&from, &to, job, pairs)?;
        } else if ft.is_file() {
            pairs.push((from, to));
        }
        // 其他类型(链接等)跳过:reparse 分支复制的是链接本身,内容不参与校验
    }
    Ok(())
}

/// 校验单个文件(worker 线程内调用):
/// 目标 hash + 源 hash(优先查复制阶段记录,缺失回退读源)对比。
/// 统计/进度用原子计数更新(多线程安全)。
#[allow(clippy::too_many_arguments)]
fn verify_one(
    src: &Path,
    dst: &Path,
    src_hashes: Option<&HashMap<PathBuf, [u8; 32]>>,
    files: &AtomicU64,
    bytes: &AtomicU64,
    mismatches: &AtomicU32,
    errors: &AtomicU32,
    last_progress: &AtomicU64,
    start: Instant,
    base: (u64, u64),
) {
    let dst_hash = match hash_file(dst) {
        Ok(h) => h,
        Err(code) => {
            errors.fetch_add(1, Ordering::Relaxed);
            Event::FileError {
                path: dst.to_string_lossy().into_owned(),
                code,
                stage: "verify".to_string(),
            }
            .emit();
            return;
        }
    };
    // 源 hash:复制阶段记录优先;查表缺失(内联/大文件/hardlink 回退路径)读源
    let src_hash: [u8; 32] = match src_hashes.and_then(|m| m.get(src)) {
        Some(h) => *h,
        None => match hash_file(src) {
            Ok((h, _)) => h,
            Err(code) => {
                errors.fetch_add(1, Ordering::Relaxed);
                Event::FileError {
                    path: src.to_string_lossy().into_owned(),
                    code,
                    stage: "verify".to_string(),
                }
                .emit();
                return;
            }
        },
    };
    let done = files.fetch_add(1, Ordering::Relaxed) + 1;
    bytes.fetch_add(dst_hash.1, Ordering::Relaxed);
    if src_hash != dst_hash.0 {
        mismatches.fetch_add(1, Ordering::Relaxed);
        Event::VerifyMismatch {
            path: src.to_string_lossy().into_owned(),
        }
        .emit();
    }
    // 进度:每 500 文件一次,fetch_max 去重(多 worker 并发只一个 emit)
    if done.saturating_sub(last_progress.load(Ordering::Relaxed)) >= 500 {
        let old = last_progress.fetch_max(done, Ordering::Relaxed);
        if old < done {
            let elapsed = start.elapsed().as_secs_f64().max(1e-6);
            Event::Progress {
                files_done: base.0 + done,
                bytes_done: base.1 + bytes.load(Ordering::Relaxed),
                rate_fps: done as f64 / elapsed,
            }
            .emit();
        }
    }
}

/// 流式计算文件 BLAKE3 哈希,返回 (32 字节摘要, 字节数)。
fn hash_file(path: &Path) -> Result<([u8; 32], u64), u32> {
    let s = win_io::open_source(path, false)?;
    let mut hasher = blake3::Hasher::new();
    let mut total: u64 = 0;
    HASH_BUF.with(|cell| {
        let mut buf = cell.borrow_mut();
        loop {
            let n = win_io::read(&s, &mut buf)?;
            if n == 0 {
                break;
            }
            hasher.update(&buf[..n]);
            total += n as u64;
        }
        Ok::<(), u32>(())
    })?;
    Ok((hasher.finalize().into(), total))
}

fn io_err(e: std::io::Error) -> u32 {
    e.raw_os_error().map(|c| c as u32).unwrap_or(crate::ERR_NO_OS_ERROR)
}
