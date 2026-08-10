//! P4.5:无缓冲 I/O 大文件复制路径(对齐 FastCopy 直接 I/O 策略)。
//!
//! 核心特性:
//! 1. FILE_FLAG_NO_BUFFERING:绕过系统缓存,冷启动不降速(FastCopy 速度核心)
//! 2. disk_mode 调度:
//!    - Same HDD:同步顺序大块读写(避免磁头抖动)
//!    - Diff HDD:双缓冲交替读写(读写并行,线程模型)
//! 3. 续传兼容:保留 P1 的 ckpt + CRC32 区间校验逻辑
//! 4. 扇区对齐:运行时查询扇区大小,确保缓冲区和读写字节数对齐
//!
//! 与 copy_large_resumable(缓冲 I/O 路径)的关系:
//! - P4.5 起 copy_large_resumable 根据 disk_mode 分发到本模块
//! - 缓冲 I/O 路径(CopyFileW + 缓冲 ReadFile/WriteFile)保留为 fallback

use crate::checkpoint;
use crate::crc32;
use crate::engine::{apply_file_times, apply_file_times_open};
use crate::event::Event;
use crate::job::{DiskMode, Job};
use crate::win_io;
use std::collections::BTreeMap;
use std::path::Path;
use std::sync::mpsc;
use std::thread;
use windows::Win32::System::IO::OVERLAPPED;

/// 大文件块大小:4MB(与 copy_large_resumable 一致,满足扇区对齐)。
/// P4.5 任务#8:保留为 fallback 常量(续传场景 ckpt.block_size 来自 ckpt 文件,
/// 不再依赖此常量),实际运行时块大小由 job.block_size_bytes() 提供。
pub const LARGE_BLOCK: usize = 4 * 1024 * 1024;

/// ckpt 频率:每 16 块写一次(默认 64MB,4MB 块时),与 copy_large_resumable 一致。
const CKPT_INTERVAL_BLOCKS: u64 = 16;

/// 无缓冲 I/O 大文件复制入口。根据 disk_mode 分发到同步或并行路径。
///
/// P4.5 任务#8:块大小通过 job.block_size_bytes() 传入,不再硬编码 4MB。
/// 续传兼容:ckpt.block_size 字段记录上次运行使用的块大小,本次运行取 max(ckpt.block_size, sector_size)
/// 确保对齐;若用户改了 block_size,继续用旧 ckpt 的 written 偏移(它是字节偏移,与块大小无关)。
///
/// 续传逻辑与 copy_large_resumable 一致:
/// 1. 读 ckpt:匹配则续传 2. 无 ckpt 但目标存在且 < 源大小:启发式续传 3. 否则重传
///
/// I/O 策略(对齐 FastCopy):
/// - Same HDD:同步顺序读写,AlignedBuf + no_buffering=true
/// - Diff HDD:双缓冲交替读写(读线程 + 写线程,mpsc channel 传递 buf 所有权)
pub fn copy_large_unbuffered(
    src: &Path,
    dst: &Path,
    size: u64,
    job: &Job,
) -> Result<(), u32> {
    let disk_mode = job.effective_disk_mode();
    // BUG 修复:取源/目标卷扇区大小的较大值,确保两端写入都对齐
    // (跨卷场景源卷512字节、目标卷4096字节时,只用源卷大小会导致目标卷写入未对齐)
    let sector_size = std::cmp::max(
        win_io::get_sector_size(src),
        win_io::get_sector_size(dst),
    ) as u64;
    let block_size = job.block_size_bytes() as u64;

    // 块大小必须扇区对齐(无缓冲 I/O 要求)
    debug_assert!(
        block_size % sector_size == 0,
        "block_size({}) 必须是 sector_size({}) 的整数倍",
        block_size,
        sector_size
    );

    // 1. 决定续传偏移(与 copy_large_resumable 逻辑一致)
    let mut offset: u64 = 0;
    if let Some(ckpt) = checkpoint::Checkpoint::load(dst, size, block_size as u32) {
        offset = ckpt.written;
    } else {
        let ckpt_exists = checkpoint::Checkpoint::path_for(dst).exists();
        if ckpt_exists {
            let _ = std::fs::remove_file(dst);
            checkpoint::Checkpoint::remove(dst);
        } else {
            let existing = checkpoint::existing_target_bytes(dst);
            if existing > 0 && existing < size {
                offset = (existing / block_size) * block_size;
            }
        }
    }

    // C9 修复:无缓冲 I/O 要求读写偏移必须是扇区对齐。
    // ckpt.written 是按实际写入字节累加,最后一块可能不是扇区对齐。
    // 续传时回退到上一个扇区边界,重传最后一个不完整块(数据安全 > 少量重传)。
    if offset > 0 && offset % sector_size != 0 {
        let aligned_offset = (offset / sector_size) * sector_size;
        Event::Info {
            key: "resume".to_string(),
            value: format!(
                "{} 扇区对齐回退: {} -> {} (of {})",
                dst.to_string_lossy(),
                offset,
                aligned_offset,
                size,
            ),
        }
        .emit();
        offset = aligned_offset;
    }

    if offset > 0 {
        Event::Info {
            key: "resume".to_string(),
            value: format!(
                "{} @{} (of {}) [{:?}] block={}MB",
                dst.to_string_lossy(),
                offset,
                size,
                disk_mode,
                block_size / (1024 * 1024),
            ),
        }
        .emit();
    }

    match disk_mode {
        // effective_disk_mode() 已将 Auto 解析为 Same 或 Diff,这里不会进入 Auto
        // P4.5+:diff HDD 优先走 IOCP 异步路径(打满队列深度,对齐 FastCopy 重叠 I/O)
        DiskMode::Same | DiskMode::Auto => copy_unbuffered_sync(src, dst, size, offset, job),
        DiskMode::Diff => copy_unbuffered_iocp(src, dst, size, offset, job),
    }
}

/// Same HDD:同步顺序无缓冲读写。
/// 读满一块 → 写一块 → 读下一块(避免磁头抖动)。
/// 保留 ckpt + CRC32 区间校验(与 copy_large_resumable 语义一致)。
///
/// C1 修复:每次写入前显式 seek 到 written 偏移。
/// 原因:无缓冲 I/O pad 后 write_len > n,文件指针会多前进 write_len-n 字节。
/// 若中间块 n<LARGE_BLOCK 且不对齐(网络挂载/读返回不足),下次写入位置错乱。
/// 显式 seek 保证写入位置始终正确,不依赖文件指针自动前进。
fn copy_unbuffered_sync(
    src: &Path,
    dst: &Path,
    size: u64,
    offset: u64,
    job: &Job,
) -> Result<(), u32> {
    // C4:块大小必须是扇区对齐的(否则无缓冲 I/O 的偏移和字节数都不对齐)
    // BUG 修复:取源/目标卷扇区大小的较大值,确保两端写入都对齐(跨卷场景)
    let sector_size = std::cmp::max(
        win_io::get_sector_size(src),
        win_io::get_sector_size(dst),
    ) as usize;
    let block_size = job.block_size_bytes();
    debug_assert!(
        block_size % sector_size == 0,
        "block_size({}) 必须是 sector_size({}) 的整数倍",
        block_size,
        sector_size
    );

    let s = win_io::open_source(src, true)?;
    let d = win_io::open_for_append(dst, job.write_through, true)?;
    if offset > 0 {
        win_io::seek(&s, offset)?;
        win_io::seek(&d, offset)?;
    }

    let mut buf = win_io::AlignedBuf::new(block_size);
    let mut written: u64 = offset;
    let mut blocks_since_ckpt: u64 = 0;
    let mut ckpt_base: u64 = offset;
    let mut interval_crc32: u32 = 0;

    while written < size {
        if job.cancel_requested() {
            // D2 修复:取消路径的 flush 错误用 let _ = 忽略
            // 原代码 flush(&d)? 失败会返回 flush 错误码而非 1223,
            // 导致调用方误判为错误而非取消,可能触发重试
            let _ = win_io::flush(&d);
            let ckpt = checkpoint::Checkpoint {
                target: dst.to_string_lossy().into_owned(),
                source_size: size,
                written,
                block_size: block_size as u32,
                ckpt_base,
                crc32: interval_crc32,
            };
            let _ = ckpt.save(dst);
            return Err(1223);
        }

        let want = std::cmp::min(block_size as u64, size - written) as usize;
        // BUG-7 修复:无缓冲 I/O 要求读取字节数扇区对齐。
        // 最后一块 want 可能非扇区对齐(size % sector_size != 0),ReadFile 会返回 ERROR_INVALID_PARAMETER(87)。
        // 修复:read_len 向上对齐到扇区边界,ReadFile 读取 read_len 字节但返回 n <= want(文件尾不足部分)。
        let read_len = align_up(want, sector_size);
        let n = win_io::read(&s, &mut buf.as_mut_slice()[..read_len])?;
        if n == 0 {
            break;
        }

        // 无缓冲写入:如果 n 不是扇区对齐(最后一块),pad 到扇区边界再写
        let write_len = align_up(n, sector_size);
        if write_len > n {
            let slice = buf.as_mut_slice();
            for b in &mut slice[n..write_len] {
                *b = 0;
            }
        }
        // C1 修复:显式 seek 到 written,不依赖文件指针自动前进
        // (pad 后 write_len > n,文件指针会多前进,不 seek 会导致下次写入位置错乱)
        win_io::seek(&d, written)?;
        win_io::write(&d, &buf.as_slice()[..write_len])?;

        written += n as u64;
        interval_crc32 = crc32::update(interval_crc32, &buf.as_slice()[..n]);
        blocks_since_ckpt += 1;

        if blocks_since_ckpt >= CKPT_INTERVAL_BLOCKS {
            win_io::flush(&d)?;
            let ckpt = checkpoint::Checkpoint {
                target: dst.to_string_lossy().into_owned(),
                source_size: size,
                written,
                block_size: block_size as u32,
                ckpt_base,
                crc32: interval_crc32,
            };
            let _ = ckpt.save(dst);
            ckpt_base = written;
            interval_crc32 = 0;
            blocks_since_ckpt = 0;
        }
    }

    // BUG-12 修复:成功收尾前校验 written == size,防止源文件提前 EOF 导致静默损坏
    // 场景:源文件在复制期间被截断,ReadFile 返回 0,break 退出时 written < size
    // 原代码直接 truncate 到 size → 空洞区域全零 → 静默数据损坏
    if written != size {
        let _ = win_io::flush(&d);
        return Err(crate::ERR_SOURCE_CHANGED); // 源文件复制期间被截断/变化:数据不完整
    }

    win_io::flush(&d)?;
    // 时间戳需在句柄 d 关闭前设置(句柄级 API,零额外打开)
    apply_file_times_open(&s, &d, dst);
    // BUG-13:无缓冲句柄 SetEndOfFile 非对齐大小会返回 ERROR_INVALID_PARAMETER(87)
    // (sync/IOCP 路径实测复现;缓冲句柄非对齐截断实测可靠)。
    // 先关闭无缓冲句柄避免缓冲句柄打开时的共享冲突,再换缓冲句柄截断。
    drop(d);
    truncate_buffered(dst, size, job.write_through)?;
    checkpoint::Checkpoint::remove(dst);
    Ok(())
}

/// Diff HDD:双缓冲交替读写(读写并行)。
///
/// 线程模型:
/// - 读线程:循环读取数据块,通过 channel 发送给写线程
/// - 写线程(当前线程):接收数据块并写入
/// - 双缓冲:读线程有 buf_a/buf_b 交替使用,写完的 buf 通过回传 channel 归还
///
/// 这样读写可以在不同物理盘上并行(读盘1的同时写盘2)。
///
/// 注:P4.5+ diff HDD 路径已升级为 copy_unbuffered_iocp(IOCP 异步),本函数保留为 fallback。
#[allow(dead_code)]
fn copy_unbuffered_parallel(
    src: &Path,
    dst: &Path,
    size: u64,
    offset: u64,
    job: &Job,
) -> Result<(), u32> {
    // P4.5 任务#8:块大小通过 job.block_size_bytes() 传入
    // BUG 修复:取源/目标卷扇区大小的较大值,确保两端写入都对齐(跨卷场景)
    let sector_size = std::cmp::max(
        win_io::get_sector_size(src),
        win_io::get_sector_size(dst),
    ) as usize;
    let block_size = job.block_size_bytes();
    debug_assert!(
        block_size % sector_size == 0,
        "block_size({}) 必须是 sector_size({}) 的整数倍",
        block_size,
        sector_size
    );

    // channel:读线程 → 写线程(数据块),写线程 → 读线程(buf 归还)
    let (data_tx, data_rx) = mpsc::sync_channel::<(u64, Box<win_io::AlignedBuf>, usize)>(1);
    // BUG 修复:return channel 容量必须 >= 2,否则预投递 2 个 buf 时第二个 send 阻塞,
    // 而唯一 receiver(读线程)在此之后才 spawn → 死锁。容量 2 使两个 buf 都能预投递。
    let (return_tx, return_rx) = mpsc::sync_channel::<Box<win_io::AlignedBuf>>(2);

    // 预投递 2 个 buf 给读线程(双缓冲)
    let buf_a = Box::new(win_io::AlignedBuf::new(block_size));
    let buf_b = Box::new(win_io::AlignedBuf::new(block_size));
    return_tx.send(buf_a).map_err(|_| 87u32)?;
    return_tx.send(buf_b).map_err(|_| 87u32)?;

    let src_owned = src.to_path_buf();
    let cancel_token = job.cancel_token.clone();
    let write_through = job.write_through;

    // 读线程
    let read_handle = thread::Builder::new()
        .name("unbuffered-reader".into())
        .spawn(move || -> Result<u64, u32> {
            let s = win_io::open_source(&src_owned, true)?;
            if offset > 0 {
                win_io::seek(&s, offset)?;
            }
            let mut written: u64 = offset;

            while written < size {
                // 检查取消
                if cancel_token.as_deref().map_or(false, |p| p.exists()) {
                    return Err(1223);
                }

                // 从回传 channel 取一个空 buf(阻塞等待写线程归还)
                let mut buf = return_rx.recv().map_err(|_| 87u32)?;

                let want = std::cmp::min(block_size as u64, size - written) as usize;
                let n = win_io::read(&s, &mut buf.as_mut_slice()[..want])?;
                if n == 0 {
                    break;
                }

                // 发送数据块给写线程(阻塞直到写线程消费,背压控制)
                if data_tx.send((written, buf, n)).is_err() {
                    return Err(87u32);
                }
                written += n as u64;
            }
            Ok(written)
        })
        .map_err(|_| 87u32)?;

    // 写线程(当前线程)
    let d = win_io::open_for_append(dst, write_through, true)?;
    if offset > 0 {
        win_io::seek(&d, offset)?;
    }

    let mut written: u64 = offset;
    let mut blocks_since_ckpt: u64 = 0;
    let mut ckpt_base: u64 = offset;
    let mut interval_crc32: u32 = 0;
    let mut read_error: Option<u32> = None;
    let mut write_error: Option<u32> = None;

    while let Ok((block_offset, mut buf, n)) = data_rx.recv() {
        // 无缓冲写入:pad 到扇区对齐
        let write_len = align_up(n, sector_size);
        if write_len > n {
            let slice = buf.as_mut_slice();
            for b in &mut slice[n..write_len] {
                *b = 0;
            }
        }

        // C7 修复:写线程出错时不直接 ? return,改为记录错误后 break
        // (break 后 drop data_rx/return_tx 触发读线程退出,再 join 等待)
        if let Err(code) = win_io::seek(&d, block_offset) {
            write_error = Some(code);
            break;
        }
        if let Err(code) = win_io::write(&d, &buf.as_slice()[..write_len]) {
            write_error = Some(code);
            break;
        }

        written = block_offset + n as u64;
        interval_crc32 = crc32::update(interval_crc32, &buf.as_slice()[..n]);
        blocks_since_ckpt += 1;

        // ckpt 保存
        if blocks_since_ckpt >= CKPT_INTERVAL_BLOCKS {
            // C7 修复补全:flush 错误也走 break(与 seek/write 一致),
            // 原代码用 ? 会跳过 join 读线程,导致读线程泄漏(同 C7 回归)
            if let Err(code) = win_io::flush(&d) {
                write_error = Some(code);
                break;
            }
            let ckpt = checkpoint::Checkpoint {
                target: dst.to_string_lossy().into_owned(),
                source_size: size,
                written,
                block_size: block_size as u32,
                ckpt_base,
                crc32: interval_crc32,
            };
            let _ = ckpt.save(dst);
            ckpt_base = written;
            interval_crc32 = 0;
            blocks_since_ckpt = 0;
        }

        // 归还 buf 给读线程
        if return_tx.send(buf).is_err() {
            break;
        }
    }

    // C7 修复:drop channel 触发读线程退出(recv 返回 Err),再 join 等待
    // (原代码写线程出错 ? return 时未 join,读线程可能阻塞在 read 中,变为分离线程)
    drop(data_rx);
    drop(return_tx);

    match read_handle.join() {
        Ok(Ok(_)) => {}
        Ok(Err(1223)) => {
            // 读线程检测到取消
            // D2 修复:取消路径的 flush 错误忽略(同 sync 路径)
            let _ = win_io::flush(&d);
            let ckpt = checkpoint::Checkpoint {
                target: dst.to_string_lossy().into_owned(),
                source_size: size,
                written,
                block_size: block_size as u32,
                ckpt_base,
                crc32: interval_crc32,
            };
            let _ = ckpt.save(dst);
            return Err(1223);
        }
        Ok(Err(code)) => {
            read_error = Some(code);
        }
        Err(_) => {
            read_error = Some(87u32);
        }
    }

    // 写线程错误优先(发生更早,更可能是根因)
    if let Some(code) = write_error {
        return Err(code);
    }
    if let Some(code) = read_error {
        return Err(code);
    }

    // 收尾
    win_io::flush(&d)?;
    win_io::truncate(&d, size)?;
    // 设置目标文件时间戳:源句柄在读线程不可达,先关目标句柄再走路径级 API
    // (不 drop 会撞 FILE_SHARE 冲突:目标句柄共享模式只有 FILE_SHARE_READ)
    drop(d);
    apply_file_times(src, dst);
    checkpoint::Checkpoint::remove(dst);
    Ok(())
}

/// 向上对齐到 align 的整数倍。
/// align 必须是 2 的幂(扇区大小满足此条件)。
fn align_up(n: usize, align: usize) -> usize {
    if align == 0 {
        return n;
    }
    (n + align - 1) & !(align - 1)
}

/// Diff HDD:IOCP 异步批量读写(打满队列深度,对齐 FastCopy 重叠 I/O)。
///
/// 线程模型:单线程驱动(提交异步请求 + GetQueuedCompletionStatus 收通知),无额外线程。
/// 队列深度 > 2,读写重叠并行(内核 IOCP 调度,优于手写双缓冲的 2 块交替)。
///
/// 流程:
/// 1. 创建 IOCP,关联源/目标句柄(key=1=源读完成,key=2=目标写完成)
/// 2. 预提交 queue_depth 个异步读请求
/// 3. 循环收完成通知:
///    - 读完成 → CRC32 + pad → 提交异步写(复用 ctx)
///    - 写完成 → 归还 buf → 如有数据提交新读
/// 4. 全部 in-flight 完成 → flush + truncate + 清 ckpt
///
/// 续传兼容:offset 扇区对齐(在 copy_large_unbuffered 处理),ckpt 每 16 块保存。
/// 取消:停止提交新请求,等 in-flight 完成(200ms 轮询),保存 ckpt,返回 1223。
fn copy_unbuffered_iocp(
    src: &Path,
    dst: &Path,
    size: u64,
    offset: u64,
    job: &Job,
) -> Result<(), u32> {
    let sector_size = std::cmp::max(
        win_io::get_sector_size(src),
        win_io::get_sector_size(dst),
    ) as usize;
    let block_size = job.block_size_bytes();
    let queue_depth = job.queue_depth_value() as usize;
    debug_assert!(
        block_size % sector_size == 0,
        "block_size({}) 必须是 sector_size({}) 的整数倍",
        block_size,
        sector_size
    );

    // 打开源(overlapped + no_buffering)
    let s = win_io::open_source_overlapped(src, true)?;
    // 打开目标:续传(offset>0)用 OPEN_EXISTING,首次用 CREATE_ALWAYS
    let d = if offset > 0 {
        win_io::open_target_overlapped(dst, true, job.write_through, false)?
    } else {
        win_io::open_target_overlapped(dst, true, job.write_through, true)?
    };

    // 创建 IOCP + 关联源/目标(key 区分读写完成)
    let iocp = win_io::create_iocp(0)?;
    const SRC_KEY: usize = 1; // 源读完成
    const DST_KEY: usize = 2; // 目标写完成
    win_io::associate_to_iocp(&iocp, &s, SRC_KEY)?;
    win_io::associate_to_iocp(&iocp, &d, DST_KEY)?;

    // 状态
    let mut next_read_offset: u64 = offset;
    let mut written: u64 = offset;
    let mut blocks_since_ckpt: u64 = 0;
    // BUG-9 修复:IOCP 读完成不保证按 offset 顺序,CRC32 是顺序相关的,乱序累加会得到错误值。
    // 修复:IOCP 路径不计算 CRC32(ckpt_base = written → 空区间 → 跳过 CRC32 校验)。
    // 续传安全性由 ckpt.written 的连续写入跟踪保证(见 continuous_write_end)。
    // BUG-10/11 修复:IOCP 写完成也不保证按 offset 顺序,written 取最后完成的 offset+n
    // 可能倒退或跳过空洞。用 continuous_write_end 跟踪"连续写入末尾":
    // - 写完成时,若 offset == continuous_write_end,推进 continuous_write_end
    // - 否则存入 pending_writes,后续检查是否有连续的
    // written = continuous_write_end(保证不跳过空洞)
    let mut continuous_write_end: u64 = offset;
    let mut pending_writes: BTreeMap<u64, u64> = BTreeMap::new();
    let mut in_flight: u32 = 0;
    let mut error_code: Option<u32> = None;
    let mut cancelled = false;

    // 空闲 buf 池:queue_depth 个
    let mut free_bufs: Vec<Box<win_io::AlignedBuf>> = (0..queue_depth)
        .map(|_| Box::new(win_io::AlignedBuf::new(block_size)))
        .collect();

    // 预提交读请求(打满队列深度)
    while in_flight < queue_depth as u32 && next_read_offset < size && !cancelled {
        if job.cancel_requested() {
            cancelled = true;
            break;
        }
        if let Some(buf) = free_bufs.pop() {
                    let want = std::cmp::min(block_size as u64, size - next_read_offset) as usize;
                    // BUG-7 修复:无缓冲 I/O 要求读取字节数扇区对齐(同 sync 路径)
                    let read_len = align_up(want, sector_size);
                    let ctx = Box::new(win_io::OverlappedContext::new(buf, next_read_offset));
                    let raw = Box::into_raw(ctx);
                    unsafe {
                        let ctx_ref = &mut *raw;
                        let buf_slice = std::slice::from_raw_parts_mut(
                            ctx_ref.buf.as_mut_slice().as_mut_ptr(),
                            read_len,
                        );
                        if let Err(code) = win_io::async_read(&s, buf_slice, next_read_offset, raw as *mut OVERLAPPED) {
                            // 提交失败:回收 ctx + buf
                            let ctx = Box::from_raw(raw);
                            free_bufs.push(ctx.buf);
                            error_code = Some(code);
                            break;
                        }
                    }
                    next_read_offset += want as u64;
                    in_flight += 1;
                }
    }

    // 主循环:收完成通知
    while in_flight > 0 {
        // 取消检查(在等待前,200ms 超时保证及时响应取消)
        if !cancelled && job.cancel_requested() {
            cancelled = true;
        }

        match win_io::get_queued_completion(&iocp, 200) {
            Ok(None) => {
                // 超时:继续循环(检查取消)
                continue;
            }
            Ok(Some((bytes, key, overlapped))) => {
                let ctx_box = unsafe { win_io::OverlappedContext::from_overlapped_ptr(overlapped) };
                in_flight -= 1;

                if key == SRC_KEY {
                    // 读完成
                    let n = bytes as usize;
                    let blk_offset = ctx_box.offset;
                    if n == 0 {
                        // EOF:回收 buf,不提交写
                        let ctx_inner = *ctx_box;
                        free_bufs.push(ctx_inner.buf);
                        continue;
                    }
                    // 有错误时不提交新写,回收 buf
                    if error_code.is_some() || cancelled {
                        let ctx_inner = *ctx_box;
                        free_bufs.push(ctx_inner.buf);
                        continue;
                    }
                    // BUG-9 修复:IOCP 读完成不保证按 offset 顺序,CRC32 顺序相关,乱序累加错误。
                    // 移除 CRC32 计算,ckpt_base=written 跳过续传时的 CRC32 校验。
                    // pad
                    let write_len = align_up(n, sector_size);
                    // 复用 ctx 提交写:改状态 + pad buf
                    let mut ctx = ctx_box;
                    ctx.is_write = true;
                    ctx.n = n;
                    if write_len > n {
                        let slice = ctx.buf.as_mut_slice();
                        for b in &mut slice[n..write_len] {
                            *b = 0;
                        }
                    }
                    let raw = Box::into_raw(ctx);
                    unsafe {
                        let ctx_ref = &mut *raw;
                        let buf_slice = std::slice::from_raw_parts_mut(
                            ctx_ref.buf.as_mut_slice().as_mut_ptr(),
                            write_len,
                        );
                        if let Err(code) = win_io::async_write(&d, buf_slice, blk_offset, raw as *mut OVERLAPPED) {
                            let ctx = Box::from_raw(raw);
                            free_bufs.push(ctx.buf);
                            if error_code.is_none() {
                                error_code = Some(code);
                            }
                        } else {
                            in_flight += 1;
                        }
                    }
                } else {
                    // 写完成(DST_KEY)
                    let ctx_inner = *ctx_box;
                    let write_end = ctx_inner.offset + ctx_inner.n as u64;
                    let blk_offset = ctx_inner.offset;
                    let buf = ctx_inner.buf; // 取出 buf 归还

                    // BUG-10/11 修复:用 continuous_write_end 跟踪连续写入末尾
                    // IOCP 写完成可能乱序,直接取 write_end 会导致 written 倒退或跳过空洞。
                    // 若 write_end.offset == continuous_write_end,推进;否则存入 pending。
                    if blk_offset == continuous_write_end {
                        continuous_write_end = write_end;
                        // 检查 pending 中是否有后续连续的写
                        while let Some((&off, &end)) = pending_writes.range(..=continuous_write_end).next() {
                            if off == continuous_write_end {
                                continuous_write_end = end;
                                pending_writes.remove(&off);
                            } else {
                                break;
                            }
                        }
                    } else {
                        pending_writes.insert(blk_offset, write_end);
                    }
                    written = continuous_write_end;
                    blocks_since_ckpt += 1;

                    // BUG-9 修复:IOCP 路径不计算 CRC32(乱序不可靠)
                    // ckpt_base = written → 空区间 → 续传时跳过 CRC32 校验

                    // 周期 ckpt(每 16 块):flush 后保存
                    if blocks_since_ckpt >= CKPT_INTERVAL_BLOCKS {
                        if let Err(code) = win_io::flush(&d) {
                            if error_code.is_none() {
                                error_code = Some(code);
                            }
                        } else {
                            let ckpt = checkpoint::Checkpoint {
                                target: dst.to_string_lossy().into_owned(),
                                source_size: size,
                                written,
                                block_size: block_size as u32,
                                ckpt_base: written, // 空区间,跳过 CRC32 校验
                                crc32: 0,
                            };
                            let _ = ckpt.save(dst);
                            blocks_since_ckpt = 0;
                        }
                    }

                    free_bufs.push(buf);

                    // 提交新读(未取消、无错、有数据、有空闲 buf)
                    if !cancelled && error_code.is_none() && next_read_offset < size {
                        if let Some(buf) = free_bufs.pop() {
                            let want = std::cmp::min(block_size as u64, size - next_read_offset) as usize;
                            // BUG-7 修复:无缓冲 I/O 要求读取字节数扇区对齐(同 sync 路径)
                            let read_len = align_up(want, sector_size);
                            let ctx = win_io::OverlappedContext::new(buf, next_read_offset);
                            let raw = Box::into_raw(Box::new(ctx));
                            unsafe {
                                let ctx_ref = &mut *raw;
                                let buf_slice = std::slice::from_raw_parts_mut(
                                    ctx_ref.buf.as_mut_slice().as_mut_ptr(),
                                    read_len,
                                );
                                if let Err(code) = win_io::async_read(&s, buf_slice, next_read_offset, raw as *mut OVERLAPPED) {
                                    let ctx = Box::from_raw(raw);
                                    free_bufs.push(ctx.buf);
                                    if error_code.is_none() {
                                        error_code = Some(code);
                                    }
                                } else {
                                    next_read_offset += want as u64;
                                    in_flight += 1;
                                }
                            }
                        }
                    }
                }
            }
            Err((code, _key, overlapped)) => {
                // 失败 I/O 完成:回收 buf,记录错误,继续等剩余 in-flight
                let ctx_box = unsafe { win_io::OverlappedContext::from_overlapped_ptr(overlapped) };
                in_flight -= 1;
                let ctx_inner = *ctx_box;
                free_bufs.push(ctx_inner.buf);
                if error_code.is_none() {
                    error_code = Some(code);
                }
            }
        }
    }

    // 收尾
    if cancelled {
        let _ = win_io::flush(&d);
        let ckpt = checkpoint::Checkpoint {
            target: dst.to_string_lossy().into_owned(),
            source_size: size,
            written, // continuous_write_end:连续写入末尾,保证续传不跳过空洞
            block_size: block_size as u32,
            ckpt_base: written, // 空区间,跳过 CRC32 校验(BUG-9:IOCP 乱序不可靠)
            crc32: 0,
        };
        let _ = ckpt.save(dst);
        return Err(1223);
    }
    if let Some(code) = error_code {
        // BUG-10 修复:error 路径也保存 ckpt(written 已通过 continuous_write_end 准确跟踪)
        let _ = win_io::flush(&d);
        if written > offset {
            let ckpt = checkpoint::Checkpoint {
                target: dst.to_string_lossy().into_owned(),
                source_size: size,
                written,
                block_size: block_size as u32,
                ckpt_base: written,
                crc32: 0,
            };
            let _ = ckpt.save(dst);
        }
        return Err(code);
    }
    // BUG-12 修复:成功收尾前校验 written == size,防止源文件提前 EOF 导致静默损坏
    // 场景:源文件在复制期间被截断,读完成返回 n=0,该块不提交写,continuous_write_end 留下空洞
    // 原代码直接 truncate 到 size → 空洞区域全零 → 静默数据损坏
    if written != size {
        let _ = win_io::flush(&d);
        return Err(crate::ERR_SOURCE_CHANGED); // 源文件复制期间被截断/变化:数据不完整
    }
    win_io::flush(&d)?;
    // 时间戳需在句柄 d 关闭前设置(句柄级 API,零额外打开)
    apply_file_times_open(&s, &d, dst);
    // BUG-13:无缓冲句柄 SetEndOfFile 非对齐大小会返回 ERROR_INVALID_PARAMETER(87)
    // (sync/IOCP 路径实测复现;缓冲句柄非对齐截断实测可靠)。
    // 先关闭无缓冲句柄避免缓冲句柄打开时的共享冲突,再换缓冲句柄截断。
    drop(d);
    truncate_buffered(dst, size, job.write_through)?;
    checkpoint::Checkpoint::remove(dst);
    Ok(())
}

/// BUG-13:无缓冲句柄 SetEndOfFile 非对齐大小会返回 ERROR_INVALID_PARAMETER(87)
/// (sync/IOCP 路径实测复现;缓冲句柄非对齐截断实测可靠)。
/// 调用前须先关闭无缓冲句柄(避免 open_for_append 的 FILE_SHARE_READ 共享冲突)。
fn truncate_buffered(dst: &Path, size: u64, write_through: bool) -> Result<(), u32> {
    let d = win_io::open_for_append(dst, write_through, false)?;
    win_io::truncate(&d, size)
}
