//! 复制编排:遍历目录树,按文件大小分流(大文件无缓冲 / 小文件缓冲)。
//! 对应执行文档 §2.0 缓冲策略与 P0/P1 范围。

use crate::checkpoint;
use crate::crc32;
use crate::event::Event;
use crate::job::{Job, Mode, Verify};
use crate::pipeline;
use crate::purge;
use crate::retry;
use crate::win_io;
use crate::reparse;
use crate::acl;
use crate::hardlink;
use crate::verify;
use crate::priority;
use std::path::{Path, PathBuf};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Instant;

/// 运行统计。
/// 进度事件由流水线 shared 统一发出(P6:walk 侧与流水线共用同一计数源,
/// 避免 files_done 回退),Stats 只保留最终汇总与错误计数。
#[derive(Default)]
struct Stats {
    files: u64,
    bytes: u64,
    errors: u32,
    skipped: u64, // 断点续传:目标已存在且一致被跳过的文件数(不重拷)
}

/// 主入口:执行 job,返回退出码(对应执行文档 §2.3.3)。
pub fn run(job: &crate::job::Job) -> i32 {
    let start = Instant::now();

    // P5:硬链接去重表(运行时状态,非 JSON 字段)。
    // 用局部变量持有,通过参数传递给 copy_one_file,避免修改 job(不可变引用)。
    // Arc 用于流水线读线程/内联路径的多线程共享。
    let hardlink_map: Option<Arc<hardlink::HardlinkMap>> = if job.preserve_hardlinks {
        Some(Arc::new(hardlink::HardlinkMap::new()))
    } else {
        None
    };
    let hm_ref: Option<&hardlink::HardlinkMap> = hardlink_map.as_deref();

    // 作业级校验(路径绝对/源存在/不嵌套)。
    // 必须在 run 内调用,而非只在 main.rs 调用 —— 否则通过 lib API
    // (集成测试 / P4 Python 适配层)调用 run() 时会绕过校验,
    // 嵌套路径会导致 walk 无限递归 → 栈溢出。
    if let Err(msg) = job.validate() {
        Event::FileError {
            path: job.source.to_string_lossy().into_owned(),
            code: 87, // ERROR_INVALID_PARAMETER
            stage: "validate".to_string(),
        }
        .emit();
        eprintln!("[rust-engine] job 校验失败: {}", msg);
        emit_done(0, 0, start.elapsed().as_millis() as u64, 16);
        return 16;
    }

    let mode = job.mode.as_str();
    Event::JobStart {
        source: job.source.to_string_lossy().into_owned(),
        target: job.target.to_string_lossy().into_owned(),
        mode: mode.to_string(),
    }
    .emit();

    if !job.source.exists() {
        Event::FileError {
            path: job.source.to_string_lossy().into_owned(),
            code: 2,
            stage: "startup".to_string(),
        }
        .emit();
        emit_done(0, 0, start.elapsed().as_millis() as u64, 16);
        return 16;
    }

    // P6:后台低优先级模式。
    // - background_mode=true:句柄级 FILE_IO_PRIORITY_HINT_INFO(VeryLow),
    //   由 win_io 各 open 函数按全局开关设置;不影响缓存,温和让路
    //   (实测性能损失小,见 job.rs process_background 注释)。
    // - process_background=true:进程级 PROCESS_MODE_BACKGROUND_BEGIN(极致让路,
    //   实测复制吞吐降 ~20 倍,默认不启用,按需选择)。
    // 失败仅记录,不阻断复制。
    // BackgroundGuard(RAII):run() 任意 return 路径(含 cancel/verify 早退/purge 取消)
    // 都会自动退出进程后台模式并复位全局开关,不留残留(同进程连续跑 job 场景)。
    // 注意:process_background 优先于 background_mode(同时开启时进程级生效,
    // 进程级本身已包含句柄级降级效果,set_enabled 双保险)。
    let _bg_guard = if job.process_background {
        priority::set_enabled(true);
        match priority::enter_background() {
            Ok(()) => {
                Event::Info {
                    key: "priority".to_string(),
                    value: "process".to_string(),
                }
                .emit();
            }
            Err(code) => {
                eprintln!("[rust-engine] 进入进程后台模式失败: 错误码 {}", code);
                Event::Info {
                    key: "priority".to_string(),
                    value: format!("process_enter_failed:{}", code),
                }
                .emit();
            }
        }
        Some(priority::BackgroundGuard)
    } else if job.background_mode {
        priority::set_enabled(true);
        Event::Info {
            key: "priority".to_string(),
            value: "file_io".to_string(),
        }
        .emit();
        Some(priority::BackgroundGuard)
    } else {
        None
    };

    // P5+:mode=verify:只校验不复制(目标树必须是之前复制的结果)。
    if job.mode == Mode::Verify {
        // 优化 B:纯校验也并行(线程预算同复制:冷热探测/RAYON_NUM_THREADS)
        let v_threads = decide_thread_count(&job.source, &job.target).0;
        match verify::verify_tree(&job.source, &job.target, job, (0, 0), None, v_threads) {
            Ok(v) => {
                let rc = if v.mismatches + v.errors == 0 {
                    if v.files == 0 { 0 } else { 1 }
                } else {
                    8 // 校验不一致/失败:按错误处理
                };
                emit_done(v.files, v.bytes, start.elapsed().as_millis() as u64, rc);
                return rc;
            }
            Err(1223) => {
                Event::Cancelled {
                    files_done: 0,
                    bytes_done: 0,
                }
                .emit();
                emit_done(0, 0, start.elapsed().as_millis() as u64, -1);
                return -1;
            }
            Err(code) => {
                Event::FileError {
                    path: job.source.to_string_lossy().into_owned(),
                    code,
                    stage: "verify".to_string(),
                }
                .emit();
                emit_done(0, 0, start.elapsed().as_millis() as u64, 16);
                return 16;
            }
        }
    }

    // P9:同卷快速移动(原子重命名,零复制)。
    // 参考 c_cleaner_plus 的 os.rename 思路,但差异:
    // - 仅当"目标不存在"时触发(目标已存在走复制+合并路径,绝不覆盖用户数据)
    // - 失败自动回退复制路径(rename 失败无任何副作用)
    // - 必须排除 Verify 模式(纯校验任务绝不能移动数据)
    // rename 原子性保证:成功后目标即完整数据,无需复制与校验。
    // Python 侧监听 fast_move=done 事件;崩溃恢复由 recover 的
    // "src 不存在 → 补建链接"分支兜底(数据完整在目标,安全)。
    if job.fast_move_same_volume && job.mode != Mode::Verify && !job.cancel_requested() {
        // 防御:源是符号链接时不移动(rename 会移动链接本身而非数据,
        // 语义错误;符号链接源由 Python 侧 migrate_symlink 走真实目标)
        if !job.source.is_symlink()
            && crate::job::same_volume(&job.source, &job.target)
            && !job.target.exists()
            && job.source.is_dir()
        {
            match std::fs::rename(&job.source, &job.target) {
                Ok(()) => {
                    Event::Info {
                        key: "fast_move".to_string(),
                        value: format!(
                            "done {} -> {}",
                            job.source.to_string_lossy(),
                            job.target.to_string_lossy()
                        ),
                    }
                    .emit();
                    eprintln!(
                        "[rust-engine] 同卷原子移动完成(零复制): {} -> {}",
                        job.source.display(),
                        job.target.display()
                    );
                    // 统计目标树(数据已在目标,快速 walk 计数供 Python 展示进度)
                    let mut s = Stats::default();
                    walk_count(&job.target, &mut s);
                    emit_done(s.files, s.bytes, start.elapsed().as_millis() as u64, 1);
                    return 1;
                }
                Err(_) => {
                    // 回退复制路径(目标已存在/跨卷/权限等)
                    Event::Info {
                        key: "fast_move".to_string(),
                        value: "fallback 回退复制路径".to_string(),
                    }
                    .emit();
                }
            }
        }
    }

    let large_threshold = job.large_threshold_bytes();
    let mut stats = Stats::default();

    // P6:自适应线程预算(冷/热探测,RAYON_NUM_THREADS 可覆盖)。
    // 小文件走读写分离流水线(读线程读源盘 / 写线程写目标盘,两盘各自满负荷)。
    let (total_threads, thread_reason) = decide_thread_count(&job.source, &job.target);
    eprintln!("[rust-engine] 线程数: {} ({})", total_threads, thread_reason);
    let (readers_n, writers_n) = pipeline::split_threads(total_threads);

    // thread::scope:流水线线程借用 run 的局部变量,scope 结束自动 join。
    // walk 在 scope 内执行,期间通过 rtx 向流水线投递小文件任务;
    // 大文件仍由 walk 内联复制(无缓冲 I/O / CopyFileW 路径,与旧行为一致)。
    let (pipe_shared, walk_cancelled) = std::thread::scope(|s| {
        let (rtx, rrx) = mpsc::sync_channel::<pipeline::ReadTask>(1024);
        let (wtx, wrx) = mpsc::sync_channel::<pipeline::WriteTask>(pipeline::writer_queue_cap());
        let rrx = Arc::new(Mutex::new(rrx));
        let wrx = Arc::new(Mutex::new(wrx));
        // 优化 A:copy+verify=hash 模式开启源哈希记录(reader 顺带算 BLAKE3,
        // 校验阶段查表免读源,省 31GB 校验读取);verify=none 不记录省内存
        let shared = Arc::new(if job.verify == Verify::Hash {
            pipeline::Shared::with_hash_tracking()
        } else {
            pipeline::Shared::new()
        });

        // 读线程:只读源盘(2-4 个,源盘并发读流越少 HDD 寻道调度越友好)
        for _ in 0..readers_n {
            let rrx = Arc::clone(&rrx);
            let wtx = wtx.clone();
            let shared = Arc::clone(&shared);
            s.spawn(move || {
                pipeline::reader_loop(
                    &rrx,
                    &wtx,
                    &shared,
                    job,
                    hm_ref,
                    pipeline::pipeline_cap_bytes(),
                    start,
                )
            });
        }
        // 写线程:只写目标盘(其余线程)
        for _ in 0..writers_n {
            let wrx = Arc::clone(&wrx);
            let shared = Arc::clone(&shared);
            s.spawn(move || pipeline::writer_loop(&wrx, &shared, job, start));
        }
        Event::Info {
            key: "pipeline".to_string(),
            value: format!(
                "readers={} writers={} cap={}MB",
                readers_n,
                writers_n,
                pipeline::pipeline_cap_bytes() / (1024 * 1024)
            ),
        }
        .emit();

        // walk:遍历 + 建目录 + 大文件内联 + 小文件投递流水线
        let cancelled = match walk(
            &job.source,
            &job.target,
            large_threshold,
            job,
            hm_ref,
            &mut stats,
            start,
            &rtx,
            &shared,
        ) {
            Ok(c) => c,
            Err(code) => {
                Event::FileError {
                    path: job.source.to_string_lossy().into_owned(),
                    code,
                    stage: "walk".to_string(),
                }
                .emit();
                stats.errors += 1;
                false
            }
        };
        drop(rtx); // 关闭读队列:读线程排空后退出,写发送端随读线程 drop

        (shared, cancelled)
    });

    // 流水线统计并入 stats(读/写线程已在 scope 结束前全部 join)
    let mut pipe = pipe_shared.finish();
    pipe.cancelled = pipe.cancelled || walk_cancelled;
    stats.files += pipe.files;
    stats.bytes += pipe.bytes;
    stats.errors += pipe.errors;
    for (path, code) in pipe.error_paths {
        Event::FileError {
            path,
            code,
            stage: "copy".to_string(),
        }
        .emit();
    }
    let cancelled = pipe.cancelled;

    // P7:断点续传统计(info 事件;适配层写入 app.log,重启后可见跳过了多少)
    if stats.skipped > 0 {
        Event::Info {
            key: "skipped".to_string(),
            value: format!(
                "{} 个文件已存在且大小/时间一致,跳过(断点续传,不重拷)",
                stats.skipped
            ),
        }
        .emit();
    }

    if cancelled {
        Event::Cancelled {
            files_done: stats.files,
            bytes_done: stats.bytes,
        }
        .emit();
        emit_done(
            stats.files,
            stats.bytes,
            start.elapsed().as_millis() as u64,
            -1,
        );
        return -1;
    }

    // P2:mirror 模式复制完成后执行 purge(v5 §3.2:先复制后 purge,两阶段分开)
    if job.mode == Mode::Mirror {
        let mut purge_stats = purge::PurgeStats::default();
        match purge::run(job, &mut purge_stats) {
            Ok(()) => {
                stats.errors += purge_stats.errors;
            }
            Err(1223) => {
                // purge 阶段取消
                stats.errors += purge_stats.errors;
                Event::Cancelled {
                    files_done: stats.files,
                    bytes_done: stats.bytes,
                }
                .emit();
                emit_done(
                    stats.files,
                    stats.bytes,
                    start.elapsed().as_millis() as u64,
                    -1,
                );
                return -1;
            }
            Err(code) => {
                Event::FileError {
                    path: job.target.to_string_lossy().into_owned(),
                    code,
                    stage: "purge".to_string(),
                }
                .emit();
                stats.errors += purge_stats.errors + 1;
            }
        }
    }

    // P5+:verify=hash:复制(含 mirror purge)完成后,逐文件 BLAKE3 内容校验。
    // 与复制共用同一 job 的 source/target;不一致/读取失败计入 errors。
    // base 传复制阶段的进度,使校验阶段的 Progress 连续不回退。
    // 复制阶段已有错误(目标树残缺)时跳过 verify:对残缺目标校验必然报"不一致",
    // 属噪音;失败路径不会删源(pending 事务兜底),完整性由下次续传重跑保证。
    if job.verify == Verify::Hash && stats.errors == 0 {
        // 优化 A+B:传复制阶段记录的源哈希表(查表免读源)+ 并行线程预算
        match verify::verify_tree(
            &job.source,
            &job.target,
            job,
            (stats.files, stats.bytes),
            pipe_shared.take_hashes().as_ref(),
            total_threads,
        ) {
            Ok(v) => {
                stats.errors += v.errors + v.mismatches;
            }
            Err(1223) => {
                Event::Cancelled {
                    files_done: stats.files,
                    bytes_done: stats.bytes,
                }
                .emit();
                emit_done(
                    stats.files,
                    stats.bytes,
                    start.elapsed().as_millis() as u64,
                    -1,
                );
                return -1;
            }
            Err(code) => {
                Event::FileError {
                    path: job.source.to_string_lossy().into_owned(),
                    code,
                    stage: "verify".to_string(),
                }
                .emit();
                stats.errors += 1;
            }
        }
    }

    let rc = if stats.errors == 0 {
        if stats.files == 0 {
            0
        } else {
            1
        }
    } else if (stats.errors as u64) < stats.files.max(1) {
        2
    } else {
        8
    };

    emit_done(
        stats.files,
        stats.bytes,
        start.elapsed().as_millis() as u64,
        rc,
    );
    rc
}

/// 快速统计目录树文件数/字节(P9 同卷快速移动成功后填充统计)。
/// 不跟随 reparse point(与 walk 一致,防止套娃/循环)。
fn walk_count(root: &Path, s: &mut Stats) {
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        if let Ok(entries) = std::fs::read_dir(&dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                match entry.file_type() {
                    Ok(ft) if ft.is_dir() => {
                        if crate::reparse::read_reparse(&path).ok().flatten().is_some() {
                            continue; // junction/符号链接目录不跟随
                        }
                        stack.push(path);
                    }
                    Ok(ft) if ft.is_file() => {
                        if let Ok(meta) = entry.metadata() {
                            s.files += 1;
                            s.bytes += meta.len();
                        }
                    }
                    _ => {}
                }
            }
        }
    }
}

fn emit_done(files: u64, bytes: u64, ms: u64, rc: i32) {
    Event::JobDone {
        files_total: files,
        bytes_total: bytes,
        duration_ms: ms,
        rc,
    }
    .emit();
}

/// 递归遍历目录树。返回 Ok(true) 表示用户取消。
///
/// P6:遍历 + 分流:
/// - 大文件(>= large_threshold):walk 内联复制(无缓冲 I/O / CopyFileW 路径)
/// - 小文件:投递读写分离流水线(rtx),由读/写线程并发处理
///   (流水线内存有界:读队列 1024 项 + 写队列按内存分级,不再需要分批)
fn walk(
    src: &Path,
    dst: &Path,
    large_threshold: u64,
    job: &Job,
    hardlink_map: Option<&hardlink::HardlinkMap>,
    stats: &mut Stats,
    start: Instant,
    rtx: &mpsc::SyncSender<pipeline::ReadTask>,
    shared: &pipeline::Shared,
) -> Result<bool, u32> {
    if job.cancel_requested() {
        return Ok(true);
    }
    std::fs::create_dir_all(dst).map_err(io_err)?;
    walk_collect(src, dst, large_threshold, job, hardlink_map, stats, start, rtx, shared)
}

/// 递归遍历:创建目录,大文件立即复制,小文件投递流水线。
fn walk_collect(
    src: &Path,
    dst: &Path,
    large_threshold: u64,
    job: &Job,
    hardlink_map: Option<&hardlink::HardlinkMap>,
    stats: &mut Stats,
    start: Instant,
    rtx: &mpsc::SyncSender<pipeline::ReadTask>,
    shared: &pipeline::Shared,
) -> Result<bool, u32> {
    if job.cancel_requested() {
        return Ok(true);
    }
    std::fs::create_dir_all(dst).map_err(io_err)?;
    let entries = std::fs::read_dir(src).map_err(io_err)?;
    for entry in entries {
        if job.cancel_requested() {
            return Ok(true);
        }
        let entry = entry.map_err(io_err)?;
        let ft = entry.file_type().map_err(io_err)?;
        let from = entry.path();
        let to = dst.join(entry.file_name());
        if ft.is_dir() {
            // P5:检查目录是否为 reparse point(Junction)
            if job.reparse_mode == "copy" {
                if let Ok(Some((kind, data))) = reparse::read_reparse(&from) {
                    // 是 reparse point:保留链接本身,不递归遍历
                    match reparse::copy_reparse(&to, kind, true, &data) {
                        Ok(()) => {
                            // 统一进度源:计入流水线 shared(避免 Progress files_done 回退)
                            pipeline::record_inline_success(shared, start, 0);
                        }
                        Err(code) => {
                            Event::FileError {
                                path: from.to_string_lossy().into_owned(),
                                code,
                                stage: "reparse".to_string(),
                            }
                            .emit();
                            stats.errors += 1;
                        }
                    }
                    continue;
                }
            }
            if walk_collect(&from, &to, large_threshold, job, hardlink_map, stats, start, rtx, shared)? {
                return Ok(true);
            }
        } else if ft.is_file() {
            // P5:检查文件是否为符号链接(reparse point)
            if job.reparse_mode == "copy" {
                if let Ok(Some((kind, data))) = reparse::read_reparse(&from) {
                    match reparse::copy_reparse(&to, kind, false, &data) {
                        Ok(()) => {
                            // 统一进度源:计入流水线 shared(避免 Progress files_done 回退)
                            pipeline::record_inline_success(shared, start, 0);
                        }
                        Err(code) => {
                            Event::FileError {
                                path: from.to_string_lossy().into_owned(),
                                code,
                                stage: "reparse".to_string(),
                            }
                            .emit();
                            stats.errors += 1;
                        }
                    }
                    continue;
                }
            }
            // 大文件:立即复制(走无缓冲 I/O 路径,单文件已优化)
            // 小文件:收集到 small_files,稍后批量并发复制
            // 用 entry.metadata() 复用 WIN32_FIND_DATA 缓存(零额外 syscall),
            // 避免 std::fs::metadata(&from) 的二次 GetFileAttributesExW 调用。
            let meta = entry.metadata();
            let (need_collect, file_size) = match &meta {
                Ok(m) => (m.len() < large_threshold, m.len()),
                Err(_) => (false, 0), // 无法获取大小,按大文件处理(立即复制,有错误立即上报)
            };
            if need_collect {
                // P7:断点续传(断电/中断重跑不重拷)——目标已存在且大小+mtime 一致 → 跳过。
                // 安全闭环:跳过 ≠ 信任——删源前仍有全量 BLAKE3 校验兜底(跳过的文件
                // 源哈希不在表,校验时回退读源 verify.rs,总 I/O 不比首跑多);
                // 大小/mtime 双一致的误判(内容损坏)会被校验抓出 mismatch → 重拷,不删源。
                if let Ok(m) = &meta {
                    if target_matches(&to, file_size, m) {
                        stats.skipped += 1;
                        // 计入流水线进度(跳过的算"已处理",进度连续不回退)
                        pipeline::record_inline_success(shared, start, file_size);
                        continue;
                    }
                }
                // 小文件:投递读写分离流水线。
                // 用 try_send + 轮询取消,不用阻塞 send:
                // 取消时读线程会退出,若队列已满且无消费者,阻塞 send 会永不返回
                // → walk 卡死 → scope 永不结束(死锁)。轮询保证取消可及时退出。
                let mut task = pipeline::ReadTask {
                    src: from.clone(),
                    dst: to,
                    size: file_size,
                };
                loop {
                    if job.cancel_requested() {
                        return Ok(true);
                    }
                    match rtx.try_send(task) {
                        Ok(()) => break,
                        Err(mpsc::TrySendError::Full(t)) => {
                            task = t; // 队列满:稍等重试(2ms,避免忙等)
                            std::thread::sleep(std::time::Duration::from_millis(2));
                        }
                        Err(mpsc::TrySendError::Disconnected(_)) => {
                            return Err(crate::ERR_PIPELINE_DISCONNECTED); // 流水线已退出(异常)
                        }
                    }
                }
            } else {
                match copy_one_file_with_retry(&from, &to, large_threshold, job, hardlink_map, file_size) {
                    Ok(n) => {
                        // 统一进度源:计入流水线 shared(避免 Progress files_done 回退)
                        pipeline::record_inline_success(shared, start, n);
                    }
                    Err(1223) => return Ok(true),
                    Err(code) => {
                        Event::FileError {
                            path: from.to_string_lossy().into_owned(),
                            code,
                            stage: "copy".to_string(),
                        }
                        .emit();
                        stats.errors += 1;
                    }
                }
            }
        } else {
            // P5:reparse point 处理(skip 模式或未知类型)
            if job.reparse_mode == "copy" {
                // 尝试读取 reparse point,成功则复制链接本身
                match reparse::read_reparse(&from) {
                    Ok(Some((kind, data))) => {
                        // else 分支:既非 dir 也非 file(std 的 file_type().is_dir()
                        // 对 reparse point 恒为 false,目录/文件符号链接都落在这里)。
                        // is_dir 必须跟随后判断:目录符号链接要创建目录占位,
                        // 否则目标变成"文件型符号链接指向目录",被 stat 跟随后
                        // 返回 ERROR_ACCESS_DENIED(5),exists()/metadata() 全部失败,
                        // 链接不可用(实测验证,原"保守按文件占位"是缺陷)。
                        // 源链接已损坏(目标不存在)时 metadata 失败 → 保守按文件占位。
                        let is_dir = std::fs::metadata(&from)
                            .map(|m| m.is_dir())
                            .unwrap_or(false);
                        match reparse::copy_reparse(&to, kind, is_dir, &data) {
                            Ok(()) => {
                                // 统一进度源:计入流水线 shared(避免 Progress files_done 回退)
                                pipeline::record_inline_success(shared, start, 0);
                            }
                            Err(code) => {
                                Event::FileError {
                                    path: from.to_string_lossy().into_owned(),
                                    code,
                                    stage: "reparse".to_string(),
                                }
                                .emit();
                                stats.errors += 1;
                            }
                        }
                    }
                    Ok(None) => {
                        // 非 reparse point(理论不会到这里,但防御性处理)
                        Event::FileError {
                            path: from.to_string_lossy().into_owned(),
                            code: 1742,
                            stage: "skip_unknown".to_string(),
                        }
                        .emit();
                        stats.errors += 1;
                    }
                    Err(code) => {
                        Event::FileError {
                            path: from.to_string_lossy().into_owned(),
                            code,
                            stage: "reparse_read".to_string(),
                        }
                        .emit();
                        stats.errors += 1;
                    }
                }
            } else {
                // skip 模式:跳过 reparse point
                Event::FileError {
                    path: from.to_string_lossy().into_owned(),
                    code: 1742,
                    stage: "skip_reparse".to_string(),
                }
                .emit();
                stats.errors += 1;
            }
        }
    }
    Ok(false)
}

/// 复制单个文件(带重试)。
///
/// 重试策略(v5 §4.4):
/// - 可重试错误(32 共享冲突 / 33 锁冲突 / 网络瞬时故障):指数退避后重试
/// - 不可重试错误(磁盘满 / 路径不存在 / 取消):立即返回
/// - 取消(1223):绝不重试,直接向上抛(由 walk 处理)
/// - 重试时发 retry 事件(Python 侧可显示"重试中...")
///
/// attempt=1 是首次尝试,attempt=2..=max_attempts 是重试。
/// 重试间隔:backoff = base * 2^(attempt-1),本地封顶 30s,网络封顶 60s。
pub(crate) fn copy_one_file_with_retry(
    src: &Path,
    dst: &Path,
    large_threshold: u64,
    job: &Job,
    hardlink_map: Option<&hardlink::HardlinkMap>,
    size: u64,
) -> Result<u64, u32> {
    let max_attempts = job.retry.max_attempts.max(1);
    let mut last_code: u32 = 0;

    for attempt in 1..=max_attempts {
        // 每次尝试前检查取消(退避 sleep 后可能用户已取消)
        if job.cancel_requested() {
            return Err(1223);
        }

        match copy_one_file(src, dst, large_threshold, job, hardlink_map, size) {
            Ok(n) => return Ok(n),
            Err(1223) => return Err(1223), // 取消:绝不重试
            Err(code) => {
                last_code = code;
                let kind = retry::classify(code);
                if kind == retry::RetryKind::Fatal || attempt >= max_attempts {
                    // 不可重试 或 重试次数耗尽:返回最后错误码
                    return Err(code);
                }
                // 可重试:发 retry 事件,退避后重试
                Event::Retry {
                    path: src.to_string_lossy().into_owned(),
                    code,
                    attempt,
                }
                .emit();
                retry::sleep_backoff(&job.retry, attempt, job.cancel_token.as_deref());
                // 退避后继续循环 → 下一次 attempt
            }
        }
    }

    // 理论上不会到这(for 循环里已 return),兜底
    Err(last_code)
}

/// 复制单个文件。
/// 大文件(>= threshold):自研 win_io 路径 + checkpoint sidecar,支持 kill 后续传(P1);
/// 小文件:缓冲 I/O(重传代价低,不需 ckpt)。
fn copy_one_file(
    src: &Path,
    dst: &Path,
    large_threshold: u64,
    job: &Job,
    hardlink_map: Option<&hardlink::HardlinkMap>,
    size: u64,
) -> Result<u64, u32> {
    // size 由调用方(walk_collect 的 entry.metadata() 缓存)传入,
    // 避免此处的二次 std::fs::metadata syscall。
    // 注意:size==0 表示 walk 时获取失败,按大文件路径处理(copy_large_resumable 内部会重新 stat)。
    //
    // 不发 FileStart/FileDone 事件:42 万文件 × 2 事件 = 84 万次 stdout 写入,
    // 是测速场景的最大瓶颈(168MB 日志 I/O)。
    // Progress 事件(每 500 文件)已含 files_done/bytes_done,足够 Python 侧更新进度;
    // FileError 事件仍逐个上报(错误少,有诊断价值)。
    // FileStart/FileDone 事件类型保留(event.rs),供未来按需启用。

    // P5:硬链接去重 — 检测 nNumberOfLinks > 1,同 inode 只复制一次
    if job.preserve_hardlinks {
        if let Some(hm) = hardlink_map {
            // 先无锁查询:大多数文件 nNumberOfLinks == 1,不需串行锁
            // (避免所有文件都被串行化,导致小文件并发复制性能崩溃)
            if let Some((n_links, vol, idx)) = hardlink::HardlinkMap::query_file_info(src) {
                if n_links > 1 {
                    // 有硬链接:获取串行锁保护 lookup + copy + insert,避免并发 race
                    // (两个硬链接文件同时 lookup miss → 都走复制 → 硬链接关系丢失)
                    // 串行化仅影响 nNumberOfLinks > 1 的文件,普通文件不受影响
                    let _serial_guard = hm.lock_serial();
                    // 查全局硬链接表:此 inode 是否已复制
                    if let Some(existing) = hm.lookup(vol, idx) {
                        // 已复制:用 CreateHardLinkW 重建硬链接(同卷才行)
                        if hardlink::HardlinkMap::create_hardlink(&existing, dst) {
                            return Ok(0); // 硬链接成功,不占额外空间
                        }
                        // 创建硬链接失败(可能跨卷),降级为普通复制
                    } else {
                        // 首次遇到此 inode:普通复制,然后记录到硬链接表
                        copy_content(src, dst, size, large_threshold, job)?;
                        // P5:ACL 复制(主流完成后)
                        if job.copy_acl {
                            if let Err(code) = acl::copy_acl(src, dst) {
                                Event::FileError {
                                    path: src.to_string_lossy().into_owned(),
                                    code,
                                    stage: "acl".to_string(),
                                }
                                .emit();
                            }
                        }
                        // P5:ADS 复制(主流完成后)
                        if job.copy_ads {
                            if let Err(code) = acl::copy_ads(src, dst) {
                                Event::FileError {
                                    path: src.to_string_lossy().into_owned(),
                                    code,
                                    stage: "ads".to_string(),
                                }
                                .emit();
                            }
                        }
                        hm.insert(vol, idx, dst.to_path_buf());
                        return Ok(size);
                    }
                }
            }
        }
    }

    copy_content(src, dst, size, large_threshold, job)?;

    // P5:ACL 复制(主流完成后)
    if job.copy_acl {
        if let Err(code) = acl::copy_acl(src, dst) {
            Event::FileError {
                path: src.to_string_lossy().into_owned(),
                code,
                stage: "acl".to_string(),
            }
            .emit();
        }
    }
    // P5:ADS 复制(主流完成后)
    if job.copy_ads {
        if let Err(code) = acl::copy_ads(src, dst) {
            Event::FileError {
                path: src.to_string_lossy().into_owned(),
                code,
                stage: "ads".to_string(),
            }
            .emit();
        }
    }

    Ok(size)
}

/// 复制文件主体,自动分派:P4 补缺的稀疏文件按实际数据区间复制(空洞跳过,目标设稀疏),
/// 其余按大小分派大文件(带 ckpt 续传)/小文件(缓冲 I/O)。
/// 稀疏路径失败(如 set_sparse 失败)→ 清理半成品后降级普通复制(数据正确,仅占用不省)。
fn copy_content(
    src: &Path,
    dst: &Path,
    size: u64,
    large_threshold: u64,
    job: &Job,
) -> Result<(), u32> {
    if win_io::is_sparse(src) {
        match copy_sparse(src, dst, size, job) {
            Ok(_) => return Ok(()),
            Err(1223) => return Err(1223), // 取消:不降级不清理,已保存的 ckpt 进度保留(下次续传)
            Err(_) => {
                // 降级:清掉稀疏路径可能残留的半成品目标与 ckpt(否则残留 ckpt 会
                // 触发 load 校验失败 + 误导性的 resume_reset 提示),走普通复制
                let _ = std::fs::remove_file(dst);
                checkpoint::Checkpoint::remove(dst);
            }
        }
    }
    if size >= large_threshold {
        copy_large_resumable(src, dst, size, job)
    } else {
        copy_small(src, dst, job.write_through)
    }
}

/// P4 补缺:稀疏文件复制——FSCTL_QUERY_ALLOCATED_RANGES 拿实际数据区间,
/// 逐区间 SetFilePointerEx 定位后读写(空洞跳过),目标先 FSCTL_SET_SPARSE 再按偏移写,
/// 收尾 SetEndOfFile 扩展目标到源大小(末尾空洞必须补上)。
///
/// 断点续传:复用 Checkpoint sidecar(与普通大文件同级完整性):
/// - 每个区间完成 → flush + 重读目标[本次新写区间]算 CRC32(空洞读回 0 与内容一致)
///   + save ckpt(written=区间末尾);取消时同样保存(取消不丢进度)
/// - 续传:load ckpt 成功(含 CRC32 校验)→ 过滤已写区间,部分区间从 written 续写;
///   ckpt 残留但校验失败(断电丢数据/损坏)→ 整文件重传,并提示用户
/// - 无 ckpt 一律重来(稀疏目标大小不可靠,不做启发式)
fn copy_sparse(src: &Path, dst: &Path, size: u64, job: &Job) -> Result<u64, u32> {
    // 1. 决定续传点:优先 ckpt(load 内含 CRC32 区间校验,不匹配 → None)
    let mut written: u64 = 0;
    let ckpt_ok = match checkpoint::Checkpoint::load(dst, size, LARGE_BLOCK as u32) {
        Some(ckpt) => {
            written = ckpt.written;
            true
        }
        None => false,
    };
    if !ckpt_ok && checkpoint::Checkpoint::path_for(dst).exists() {
        // 有 ckpt 残留但校验失败(断电丢数据/损坏):整文件重传,明确提示用户
        Event::Info {
            key: "resume_reset".to_string(),
            value: format!(
                "{} 上次复制的数据校验未通过(可能断电损坏),已从头重新复制",
                dst.to_string_lossy()
            ),
        }
        .emit();
        let _ = std::fs::remove_file(dst);
        checkpoint::Checkpoint::remove(dst);
    }
    if written > 0 {
        Event::Info {
            key: "resume".to_string(),
            value: format!(
                "{} @{} (of {})",
                dst.to_string_lossy(),
                written,
                size
            ),
        }
        .emit();
    }

    let s = win_io::open_source(src, false)?;
    // ⚠️ 打开方式必须区分:
    // - 无 ckpt(written==0,首次复制):open_target(create=true) 截断目标——
    //   否则目标残留旧数据会污染空洞位置(源空洞读回 0,目标却有旧数据 → 内容不一致)
    // - 有 ckpt(续传):open_for_append(OPEN_ALWAYS 不截断,保留已写部分)
    let d = if written > 0 {
        win_io::open_for_append(dst, job.write_through, false)?
    } else {
        win_io::open_target(dst, false, job.write_through, true)?
    };
    // 续传时目标已是稀疏,重复设置幂等无害
    win_io::set_sparse(&d)?;
    let ranges = win_io::query_allocated_ranges(&s, size)?;
    SMALL_BUF.with(|cell| {
        let mut buf = cell.borrow_mut();
        let mut ckpt_base: u64 = written;
        for (off, len) in ranges {
            if off + len <= written {
                continue; // 该区间已完成,跳过
            }
            // 部分区间:仅当区间起点在 written 之前且终点在之后才发生(中断于区间内)
            let start = off.max(written);
            if start >= off + len {
                continue;
            }
            win_io::set_file_pointer(&s, start)?;
            win_io::set_file_pointer(&d, start)?;
            let mut remain = off + len - start;
            while remain > 0 {
                if job.cancel_requested() {
                    // 取消:flush + 重读校验 + save ckpt(不丢进度),返回取消码
                    let _ = win_io::flush(&d);
                    let written_now = (off + len - remain).max(start);
                    if written_now > ckpt_base {
                        let crc = checkpoint::compute_interval_crc32(dst, ckpt_base, written_now);
                        if let Some(crc) = crc {
                            let ckpt = checkpoint::Checkpoint {
                                target: dst.to_string_lossy().into_owned(),
                                source_size: size,
                                written: written_now,
                                block_size: LARGE_BLOCK as u32,
                                ckpt_base,
                                crc32: crc,
                            };
                            let _ = ckpt.save(dst);
                        }
                    }
                    return Err(1223);
                }
                let want = (remain as usize).min(buf.len());
                let n = win_io::read(&s, &mut buf[..want])?;
                if n == 0 {
                    break; // 提前 EOF(源被外部截断),剩余按空洞跳过
                }
                win_io::write(&d, &buf[..n])?;
                remain -= n as u64;
            }
            // 区间完成:flush + 重读本次新写区间算 CRC32 + save ckpt(断电完整性兜底)
            let written_now = off + len;
            win_io::flush(&d)?;
            let crc = checkpoint::compute_interval_crc32(dst, ckpt_base, written_now);
            if let Some(crc) = crc {
                let ckpt = checkpoint::Checkpoint {
                    target: dst.to_string_lossy().into_owned(),
                    source_size: size,
                    written: written_now,
                    block_size: LARGE_BLOCK as u32,
                    ckpt_base,
                    crc32: crc,
                };
                let _ = ckpt.save(dst);
            }
            ckpt_base = written_now;
            written = written_now;
        }
        // 末尾空洞:扩展目标到源大小,保证目标大小与源一致
        win_io::set_file_pointer(&d, size)?;
        win_io::set_end_of_file(&d)?;
        Ok::<(), u32>(())
    })?;
    win_io::flush(&d)?;
    // P4 时间戳保真:句柄仍打开时用句柄级版本(与普通路径一致,零额外打开;
    // 缺失会导致重跑跳过逻辑 target_matches 失效 → 每次重跑重复复制)
    apply_file_times_open(&s, &d, dst);
    checkpoint::Checkpoint::remove(dst);
    Ok(size)
}

// 小文件:缓冲 I/O,64KB 块。
// 默认不调 FlushFileBuffers(对齐复制引擎默认行为,数据留系统缓存异步刷盘);
// write_through 模式下 open_target 已加 FILE_FLAG_WRITE_THROUGH,每次写直达磁盘。
//
// 不用 CopyFileW:实测跨卷小文件复制 CopyFileW 反而比 read/write 慢 ~7%。
// CopyFileW 优势在命中写缓存(同卷 ~4GB/s),跨卷场景写缓存效果有限,
// read/write 循环的缓冲 I/O 预读/延迟写更稳定。
//
// P4:thread_local 复用 64KB 缓冲。42 万小文件原本各调一次 vec! → malloc/free,
// 改为每线程一份复用,消除 ~42 万次堆分配开销(rayon 线程池固定 N 个线程,
// 缓冲区只分配 N 次而非每文件一次)。RefCell 保证同线程内独占借用。
thread_local! {
    static SMALL_BUF: std::cell::RefCell<Vec<u8>> = std::cell::RefCell::new(vec![0u8; 64 * 1024]);
}

fn copy_small(src: &Path, dst: &Path, write_through: bool) -> Result<(), u32> {
    let s = win_io::open_source(src, false)?;
    let d = win_io::open_target(dst, false, write_through, true)?;
    // P4:复用 thread_local 缓冲区,避免每文件 malloc/free 64KB
    SMALL_BUF.with(|cell| {
        let mut buf = cell.borrow_mut();
        loop {
            let n = win_io::read(&s, &mut buf)?;
            if n == 0 {
                break;
            }
            win_io::write(&d, &buf[..n])?;
        }
        Ok::<(), u32>(())
    })?;
    if write_through {
        win_io::flush(&d)?;
    }
    // 设置目标文件时间戳(数据写完后,关闭前),对齐 /COPY:DAT
    apply_file_times_open(&s, &d, dst);
    Ok(())
}

/// 数据写完后,把源文件句柄上的时间戳(创建/访问/修改)应用到目标文件句柄。
/// 对齐 /COPY:DAT 中的 T(时间戳)。
/// 失败不致命:只发 Info 事件供诊断,不中断复制(时间戳错误不影响数据完整性)。
pub(crate) fn apply_file_times_open(
    s: &win_io::FileHandle,
    d: &win_io::FileHandle,
    dst: &Path,
) {
    match win_io::get_file_times(s) {
        Ok(t) => {
            if let Err(code) = win_io::set_file_times(d, &t) {
                Event::Info {
                    key: "filetime".to_string(),
                    value: format!("{} 设置时间戳失败: {}", dst.to_string_lossy(), code),
                }
                .emit();
            }
        }
        Err(code) => {
            // 读取失败同样记录(否则时间戳静默丢失无迹可查)
            Event::Info {
                key: "filetime".to_string(),
                value: format!("{} 读取源时间戳失败: {}", dst.to_string_lossy(), code),
            }
            .emit();
        }
    }
}

/// 数据写完后,把源文件时间戳应用到目标文件(路径级,内部重开句柄)。
/// 仅用于目标文件已写完且**无其他写句柄占用**的场景(CopyFileW 等路径;
/// 句柄仍打开时请用 apply_file_times_open,否则会撞 FILE_SHARE 冲突)。
/// 失败不致命:只发 Info 事件供诊断,不中断复制。
pub(crate) fn apply_file_times(src: &Path, dst: &Path) {
    if let Err(code) = win_io::copy_file_times(src, dst) {
        Event::Info {
            key: "filetime".to_string(),
            value: format!("{} 设置时间戳失败: {}", dst.to_string_lossy(), code),
        }
        .emit();
    }
}

/// 大文件块大小:4MB。
/// 选 4MB:(1) 充分利用系统顺序预读;(2) ckpt 更新频率合理(1GB 文件 ~256 次 ckpt 写);
/// (3) 无缓冲 I/O 时满足大多数存储的扇区对齐(磁盘扇区 4K/8K,4MB 是其整数倍)。
const LARGE_BLOCK: usize = 4 * 1024 * 1024;

/// 大文件:根据 disk_mode 分发到无缓冲 I/O 路径(P4.5)或缓冲 I/O 路径(fallback)。
///
/// P4.5 起:
/// - 默认走 unbuffered::copy_large_unbuffered(无缓冲 I/O,对齐 FastCopy)
/// - write_through=true 时走缓冲 I/O 路径(保留原 copy_large_resumable 逻辑)
///   理由:write_through 要求每次写直达磁盘,无缓冲 I/O 的 FILE_FLAG_WRITE_THROUGH
///   会叠加 NO_BUFFERING,性能极差,此时缓冲 I/O + WRITE_THROUGH 更合理
///
/// P4.5+ 自适应缓存策略(adaptive_cache=true 时生效):
/// - 首次复制(无 ckpt + 无部分目标):探测源文件是否在系统缓存中
///   - 热启动(在缓存中)→ CopyFileW:利用缓存读 + 写缓存,~4GB/s
///   - 冷启动(不在缓存)→ 无缓冲 I/O:绕过缓存,避免缓存污染
/// - 续传场景(有 ckpt 或部分目标):始终走无缓冲 I/O(CopyFileW 不支持续传)
///
/// P4.5 任务#9 决策:CopyFileW 路径保留(不移除)
/// 理由:
/// 1. CopyFileW 仅在 write_through=true 且首次复制(offset==0)时使用,非默认路径
/// 2. write_through 是高可靠场景,CopyFileW 的内核零拷贝 + 内核保证完整性是最佳选择
/// 3. 默认路径(write_through=false)已走 unbuffered,不受 CopyFileW 影响
/// 4. 移除 CopyFileW 会让 write_through 模式变慢且无实际收益
/// 已知限制(可接受):
/// - CopyFileW 不支持续传(无 ckpt,取消后重传)——write_through 场景可接受
/// - CopyFileW 不支持内核级取消(靠进程 kill)——同上
/// - CopyFileW 绕过 ckpt+CRC32 体系——write_through 首次复制由内核保证完整性
fn copy_large_resumable(
    src: &Path,
    dst: &Path,
    size: u64,
    job: &Job,
) -> Result<(), u32> {
    if !job.write_through {
        // P4.5+ 自适应缓存策略:首次复制时探测冷/热启动
        if job.adaptive_cache {
            // 判断是否首次复制:无 ckpt + 目标不存在或已满(非部分写入)
            let has_ckpt = checkpoint::Checkpoint::path_for(dst).exists();
            let target_size = std::fs::metadata(dst).map(|m| m.len()).unwrap_or(0);
            let is_fresh_copy = !has_ckpt && (target_size == 0 || target_size >= size);

            if is_fresh_copy {
                if win_io::probe_cache_hot(src) {
                    // 热启动:源文件在系统缓存中 → CopyFileW(利用缓存,~4GB/s)
                    Event::Info {
                        key: "adaptive_cache".to_string(),
                        value: format!(
                            "{} 热启动 → CopyFileW",
                            dst.to_string_lossy()
                        ),
                    }
                    .emit();
                    return win_io::copy_file_zero_copy(
                        src,
                        dst,
                        job.cancel_token.as_deref(),
                    )
                    .map(|_| {
                        // CopyFileW 不保留源创建时间:成功后补全三个时间戳
                        apply_file_times(src, dst);
                    })
                    .or_else(|code| {
                        if code == 1223 {
                            // 取消:CopyFileW 无 ckpt,直接返回取消码
                            return Err(code);
                        }
                        // CopyFileW 失败:降级到无缓冲 I/O(保证可用性)
                        // BUG-6 修复:CopyFileW 失败时目标文件可能已被部分写入(缓冲 I/O,无 ckpt),
                        // 直接降级会走启发式续传信任未校验数据。清理目标文件 + ckpt 后从零开始最安全。
                        Event::Info {
                            key: "adaptive_cache".to_string(),
                            value: format!(
                                "{} CopyFileW 失败({}),清理后降级无缓冲 I/O",
                                dst.to_string_lossy(),
                                code
                            ),
                        }
                        .emit();
                        let _ = std::fs::remove_file(dst);
                        checkpoint::Checkpoint::remove(dst);
                        crate::unbuffered::copy_large_unbuffered(src, dst, size, job)
                    });
                }
                // 冷启动:走无缓冲 I/O(避免缓存污染)
            }
        }
        // 默认 / 冷启动 / 续传 / adaptive_cache=false:走无缓冲 I/O 路径
        return crate::unbuffered::copy_large_unbuffered(src, dst, size, job);
    }

    // write_through 模式:保留原缓冲 I/O 路径(高可靠场景)
    copy_large_resumable_buffered(src, dst, size, job)
}

/// 缓冲 I/O 大文件复制(原 copy_large_resumable,保留为 write_through/fallback 路径)。
fn copy_large_resumable_buffered(
    src: &Path,
    dst: &Path,
    size: u64,
    job: &Job,
) -> Result<(), u32> {
    // 1. 决定续传偏移:优先 ckpt,其次目标已有大小
    let mut offset: u64 = 0;
    if let Some(ckpt) = checkpoint::Checkpoint::load(dst, size, LARGE_BLOCK as u32) {
        offset = ckpt.written;
    } else {
        let ckpt_exists = checkpoint::Checkpoint::path_for(dst).exists();
        if ckpt_exists {
            // 有 ckpt 残留但校验失败(断电丢数据/损坏):整文件重传,明确提示用户
            Event::Info {
                key: "resume_reset".to_string(),
                value: format!(
                    "{} 上次复制的数据校验未通过(可能断电损坏),已从头重新复制",
                    dst.to_string_lossy()
                ),
            }
            .emit();
            let _ = std::fs::remove_file(dst);
            checkpoint::Checkpoint::remove(dst);
        } else {
            let existing = checkpoint::existing_target_bytes(dst);
            if existing > 0 && existing < size {
                offset = (existing / LARGE_BLOCK as u64) * LARGE_BLOCK as u64;
            }
        }
    }

    // 首次复制(offset==0):CopyFileW 内核零拷贝(write_through 模式仍用 CopyFileW,
    // 因 write_through 主要影响续传路径的刷盘策略,首次复制用 CopyFileW 更快)
    if offset == 0 {
        let cancel_token = job.cancel_token.as_deref();
        return win_io::copy_file_zero_copy(src, dst, cancel_token).map(|_| {
            checkpoint::Checkpoint::remove(dst);
            // CopyFileW 不保留源创建时间:成功后补全三个时间戳
            apply_file_times(src, dst);
        }).or_else(|code| {
            if code == 1223 {
                checkpoint::Checkpoint::remove(dst);
            }
            Err(code)
        });
    }

    if offset > 0 {
        Event::Info {
            key: "resume".to_string(),
            value: format!(
                "{} @{} (of {})",
                dst.to_string_lossy(),
                offset,
                size
            ),
        }
        .emit();
    }

    // 2. 打开句柄(源只读;目标读写,不 truncate 以保留已写部分)
    let s = win_io::open_source(src, false)?;
    let d = win_io::open_for_append(dst, job.write_through, false)?;
    if offset > 0 {
        win_io::seek(&s, offset)?;
        win_io::seek(&d, offset)?;
    }
    let mut buf = vec![0u8; LARGE_BLOCK];
    let mut written: u64 = offset;
    const CKPT_INTERVAL_BLOCKS: u64 = 16;
    let mut blocks_since_ckpt: u64 = 0;
    let mut ckpt_base: u64 = offset;
    let mut interval_crc32: u32 = 0;

    // 3. 主循环:读-写,按频率 flush + 算 CRC32 + 写 ckpt
    while written < size {
        if job.cancel_requested() {
            // E1 修复(对齐 D2):取消路径的 flush 错误用 let _ = 忽略
            // 原代码 flush(&d)? 失败会返回 flush 错误码而非 1223,
            // 导致调用方误判为错误而非取消,可能触发重试;
            // 且 ? 提前返回会跳过下方 ckpt.save,续传能力受损。
            // 与 unbuffered.rs 的 sync/parallel 取消路径保持一致。
            let _ = win_io::flush(&d);
            let ckpt = checkpoint::Checkpoint {
                target: dst.to_string_lossy().into_owned(),
                source_size: size,
                written,
                block_size: LARGE_BLOCK as u32,
                ckpt_base,
                crc32: interval_crc32,
            };
            let _ = ckpt.save(dst);
            return Err(1223);
        }
        let want = std::cmp::min(LARGE_BLOCK as u64, size - written) as usize;
        let n = win_io::read(&s, &mut buf[..want])?;
        if n == 0 {
            break;
        }
        win_io::write(&d, &buf[..n])?;
        written += n as u64;
        interval_crc32 = crc32::update(interval_crc32, &buf[..n]);
        blocks_since_ckpt += 1;
        if blocks_since_ckpt >= CKPT_INTERVAL_BLOCKS {
            win_io::flush(&d)?;
            let ckpt = checkpoint::Checkpoint {
                target: dst.to_string_lossy().into_owned(),
                source_size: size,
                written,
                block_size: LARGE_BLOCK as u32,
                ckpt_base,
                crc32: interval_crc32,
            };
            let _ = ckpt.save(dst);
            ckpt_base = written;
            interval_crc32 = 0;
            blocks_since_ckpt = 0;
        }
    }

    // 4. 收尾:flush → truncate → 补时间戳 → 删除 ckpt
    win_io::flush(&d)?;
    win_io::truncate(&d, size)?;
    // 设置目标文件时间戳(源/目标句柄仍在打开,用句柄级 API,零额外打开)
    apply_file_times_open(&s, &d, dst);
    checkpoint::Checkpoint::remove(dst);
    Ok(())
}

fn io_err(e: std::io::Error) -> u32 {
    e.raw_os_error().map(|c| c as u32).unwrap_or(crate::ERR_NO_OS_ERROR)
}

/// P7 断点续传跳过判断:目标已存在 && 大小一致 && 修改时间一致 → 判定"已完成"。
/// 复制时写线程按源设置目标时间戳(win_io::apply_file_times,同源 FILETIME 100ns),
/// 重跑时目标 mtime == 源 mtime → 可跳过。任一条件失败 → 不跳过(重拷)。
/// 时间戳取不到 → 保守不跳过。判断失败只多一次目标 stat,不重拷不误跳。
fn target_matches(dst: &Path, size: u64, src_meta: &std::fs::Metadata) -> bool {
    let dm = match std::fs::metadata(dst) {
        Ok(m) => m,
        Err(_) => return false, // 目标不存在:必拷
    };
    if dm.len() != size {
        return false;
    }
    match (src_meta.modified(), dm.modified()) {
        (Ok(s), Ok(d)) => s == d,
        _ => false,
    }
}

/// 取路径的盘符(C:\... → 'C');非盘符路径返回 '?'(is_ssd 对 '?' 返回 false)。
fn drive_of(p: &Path) -> char {
    p.to_string_lossy().chars().next().unwrap_or('?')
}

/// 自适应线程数决策:
/// 1. RAYON_NUM_THREADS 环境变量显式指定(手动调优入口,优先)
/// 2. 源数据在系统缓存(热)→ 逻辑核数(系统调用并行受益,磁盘不参与)
/// 3. 冷读且任一盘为 SSD(无寻道惩罚)→ 逻辑核数:
///    12 线程是 5400rpm HDD 的实测最优,套用到 SSD 会白白浪费并发
///    (SSD 没有寻道风暴,并发读流可以吃满核数)
/// 4. 双 HDD 冷读 → 12 以内按核数收缩:
///    5400rpm HDD 冷读实测扫点(4=43.2分 / 6=18.6 / 8=20.5 / 10=15.7 /
///    12=15.4分 / 20=63.7分,最优平台 10-12;并发读流太少寻道调度差,
///    太多则寻道风暴) —— 磁盘瓶颈,与 CPU 核数无关,高核机器保持 12;
///    但低核机器(2-4 核)12 线程调度竞争过大,按 min(12, 核数*2) 收缩。
/// 返回 (线程数, 决策来源说明)。
fn decide_thread_count(source: &Path, target: &Path) -> (usize, &'static str) {
    if let Ok(v) = std::env::var("RAYON_NUM_THREADS") {
        if let Ok(n) = v.trim().parse::<usize>() {
            if n >= 1 {
                return (n, "env:RAYON_NUM_THREADS");
            }
        }
    }
    let cores = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(20);
    if source_cache_hot(source) {
        return (cores, "adaptive:hot");
    }
    // P8:盘型检测 —— 任一盘 SSD 则按核数(SSD 无寻道惩罚,并发受益)
    if win_io::is_ssd(drive_of(source)) || win_io::is_ssd(drive_of(target)) {
        return (cores, "adaptive:ssd");
    }
    // P8:双 HDD 冷读 —— 12 为实测最优;低核机器按核数收缩(2 核→4,4 核→8,6 核以上→12)
    let cold = std::cmp::min(12, std::cmp::max(4, cores * 2));
    (cold, "adaptive:cold")
}

/// 源目录树冷/热探测:按枚举序取分散位置的目录,每个取文件,
/// 用 win_io::probe_cache_hot(两次读比较法)判定是否命中系统缓存。
/// 分散采样避免只测到"缓存前端"(前几轮运行留下的部分缓存)。
/// >=50% 样本热 → 热。无样本时保守返回热(与现状一致)。
fn source_cache_hot(src: &Path) -> bool {
    // 1. 枚举全部目录(仅元数据,数秒级),DFS 收集。
    //    源目录本身也放入候选(扁平目录树场景:文件直接在源根下)
    let mut dirs: Vec<PathBuf> = vec![src.to_path_buf()];
    let mut stack: Vec<PathBuf> = vec![src.to_path_buf()];
    while let Some(d) = stack.pop() {
        if dirs.len() >= 8192 {
            break; // 目录数上限,防超大目录树拖慢启动
        }
        if let Ok(rd) = std::fs::read_dir(&d) {
            for entry in rd.flatten() {
                let p = entry.path();
                if entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                    dirs.push(p.clone());
                    stack.push(p);
                }
            }
        }
    }

    // 2. 分散采样:取第 0, step, 2*step, ... 个目录的文件。
    //    优先选 >=64KB 的文件做探测:小文件两次读都在微秒级,计时噪声
    //    会让 probe_cache_hot 误判为冷;大文件冷读有真实寻道延迟(~10ms),
    //    判定稳定(DirEntry::metadata 在 Windows 复用 find-data,零额外 syscall)
    let mut hot = 0usize;
    let mut total = 0usize;
    if !dirs.is_empty() {
        let step = (dirs.len() / 8).max(1);
        let mut idx = 0;
        while idx < dirs.len() {
            if let Ok(rd) = std::fs::read_dir(&dirs[idx]) {
                // 第一个 >=64KB 的文件优先,否则用第一个文件
                let mut candidate: Option<PathBuf> = None;
                for e in rd.flatten() {
                    if e.file_type().map(|t| t.is_file()).unwrap_or(false) {
                        let p = e.path();
                        if e.metadata().map(|m| m.len() >= 65536).unwrap_or(false) {
                            candidate = Some(p);
                            break;
                        }
                        if candidate.is_none() {
                            candidate = Some(p);
                        }
                    }
                }
                if let Some(p) = candidate {
                    total += 1;
                    if win_io::probe_cache_hot(&p) {
                        hot += 1;
                    }
                }
            }
            idx += step;
        }
    }
    eprintln!(
        "[rust-engine] 缓存探测: {}/{} 样本命中缓存",
        hot, total
    );
    if total == 0 {
        return true; // 无样本:保守保持高并发
    }
    hot * 2 >= total
}
