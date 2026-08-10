//! P6:小文件读写分离流水线。
//!
//! 替代原 rayon par_iter 的"逐文件 read→write 交错"模型:
//! - 读线程(2-4 个):只读源盘,整文件读入缓冲后交给写队列。
//!   源盘并发读流少而有序,对 5400rpm HDD 的寻道调度友好
//!   (实测:冷读并发读流 10-12 为最优平台,过多 → 寻道风暴)
//! - 写线程(其余):只写目标盘,创建文件 + 落缓冲 + 时间戳 + ACL/ADS。
//!   目标盘写流稳定,两盘各自保持满负荷,不再互相等待。
//!
//! 内存控制:
//! - 单文件缓冲上限按物理内存分级(2-8MB),超过上限的中等文件
//!   由读线程走传统内联复制(复用 engine::copy_one_file_with_retry,
//!   含硬链接/ACL/ADS/重试,语义与旧行为完全一致)
//! - 写队列容量按内存分级(2-8 个在途缓冲)
//! - 读队列只存路径元组(容量固定 1024,~150KB)
//!
//! 取消:任一环节检测到取消即置位 cancelled;写线程只排空通道不写盘,
//! 读线程在任务间隙退出;通道关闭后全部线程自然退出(无死锁:
//! 写线程永不提前停收,读线程退出时 drop 写发送端)。

use crate::acl;
use crate::event::Event;
use crate::hardlink::HardlinkMap;
use crate::job::Job;
use crate::retry;
use crate::win_io;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Instant;

/// 进度上报频率(与 engine.rs 一致:每 500 文件一次)。
const PROGRESS_EVERY: u64 = 500;

/// 读任务:读线程的输入(路径元组,廉价)。
pub struct ReadTask {
    pub src: PathBuf,
    pub dst: PathBuf,
    pub size: u64,
}

/// 写任务:写线程的输入(整文件缓冲)。
pub(crate) struct WriteTask {
    src: PathBuf, // ACL/ADS 需要源路径
    dst: PathBuf,
    data: Vec<u8>,
    times: Option<win_io::FileTimes>,
}

/// 流水线共享状态(读/写线程共同更新,线程安全)。
pub struct Shared {
    files_done: AtomicU64,
    bytes_done: AtomicU64,
    errors: AtomicU32,
    last_progress: AtomicU64,
    error_paths: Mutex<Vec<(String, u32)>>,
    cancelled: AtomicBool,
    /// 源文件 BLAKE3 哈希表(copy+verify=hash 模式开启,校验阶段免读源)。
    /// None = 不记录(verify=none 或纯校验模式,省内存)。
    src_hashes: Option<Mutex<HashMap<PathBuf, [u8; 32]>>>,
}

impl Shared {
    pub fn new() -> Self {
        Shared {
            files_done: AtomicU64::new(0),
            bytes_done: AtomicU64::new(0),
            errors: AtomicU32::new(0),
            last_progress: AtomicU64::new(0),
            error_paths: Mutex::new(Vec::new()),
            cancelled: AtomicBool::new(false),
            src_hashes: None,
        }
    }

    /// 开启源哈希记录(仅 copy+verify=hash 模式调用)。
    /// 复制阶段 reader 顺带算源 BLAKE3(零额外 I/O),校验阶段直接查表,
    /// 省掉校验时重读源盘 31GB(优化 A,P5 加速)。
    pub fn with_hash_tracking() -> Self {
        Shared {
            src_hashes: Some(Mutex::new(HashMap::new())),
            ..Shared::new()
        }
    }

    /// 记录源文件哈希(reader 读文件时顺带计算,幂等覆盖)。
    pub fn record_src_hash(&self, path: &Path, hash: [u8; 32]) {
        if let Some(m) = &self.src_hashes {
            if let Ok(mut map) = m.lock() {
                map.insert(path.to_path_buf(), hash);
            }
        }
    }

    /// 取走哈希表(scope 结束后调用一次;不记录时返回 None)。
    pub fn take_hashes(&self) -> Option<HashMap<PathBuf, [u8; 32]>> {
        self.src_hashes
            .as_ref()
            .and_then(|m| m.lock().ok())
            .map(|mut map| std::mem::take(&mut *map))
    }

    fn record_error(&self, path: &std::path::Path, code: u32) {
        self.errors.fetch_add(1, Ordering::Relaxed);
        if let Ok(mut ep) = self.error_paths.lock() {
            ep.push((path.to_string_lossy().into_owned(), code));
        }
    }

    /// 汇总流水线结果(scope 结束后调用)。
    pub fn finish(&self) -> PipelineResult {
        PipelineResult {
            files: self.files_done.load(Ordering::Relaxed),
            bytes: self.bytes_done.load(Ordering::Relaxed),
            errors: self.errors.load(Ordering::Relaxed),
            cancelled: self.cancelled.load(Ordering::Relaxed),
            error_paths: self
                .error_paths
                .lock()
                .map(|ep| ep.clone())
                .unwrap_or_default(),
        }
    }
}

/// 流水线汇总结果。
pub struct PipelineResult {
    pub files: u64,
    pub bytes: u64,
    pub errors: u32,
    pub cancelled: bool,
    pub error_paths: Vec<(String, u32)>,
}

impl Default for PipelineResult {
    fn default() -> Self {
        PipelineResult {
            files: 0,
            bytes: 0,
            errors: 0,
            cancelled: false,
            error_paths: Vec::new(),
        }
    }
}

/// 单文件缓冲上限(字节):大于此值的文件由读线程走内联复制。
/// 按物理内存分级,避免低配机器内存暴涨。
pub fn pipeline_cap_bytes() -> usize {
    match crate::job::physical_memory_mb() {
        m if m >= 4096 => 8 * 1024 * 1024,
        m if m >= 2048 => 4 * 1024 * 1024,
        _ => 2 * 1024 * 1024,
    }
}

/// 写队列容量(在途缓冲数),按内存分级。
pub fn writer_queue_cap() -> usize {
    match crate::job::physical_memory_mb() {
        m if m >= 4096 => 8,
        m if m >= 2048 => 4,
        _ => 2,
    }
}

/// 按总线程预算拆分读/写线程数:
/// 读线程 2-4 个(源盘并发读流越少,HDD 寻道调度越友好),其余给写线程。
/// total <= 2(如 RAYON_NUM_THREADS=1/2 显式指定)时退化为 1 读 1 写。
pub fn split_threads(total: usize) -> (usize, usize) {
    if total <= 2 {
        return (1, total.saturating_sub(1).max(1));
    }
    let readers = (total / 3).clamp(2, 4);
    let writers = total.saturating_sub(readers).max(2);
    (readers, writers)
}

// 读线程整文件读取的复用缓冲(与 engine::SMALL_BUF 同理,避免
// 42 万文件 × 256KB 的重复 malloc/memset/free)。
thread_local! {
    static READ_BUF: std::cell::RefCell<Vec<u8>> =
        std::cell::RefCell::new(vec![0u8; 256 * 1024]);
}

/// 进度上报(与 copy_small_files_parallel 相同的 fetch_max 模式,
/// 只有成功推进 last_progress 的线程才 emit)。
fn maybe_progress(shared: &Shared, start: Instant, files_done: u64) {
    let last = shared.last_progress.load(Ordering::Relaxed);
    if files_done.saturating_sub(last) >= PROGRESS_EVERY {
        let old = shared.last_progress.fetch_max(files_done, Ordering::Relaxed);
        if old == last {
            let elapsed = start.elapsed().as_secs_f64().max(1e-6);
            Event::Progress {
                files_done,
                bytes_done: shared.bytes_done.load(Ordering::Relaxed),
                rate_fps: files_done as f64 / elapsed,
            }
            .emit();
        }
    }
}

/// 读线程主循环。
pub(crate) fn reader_loop(
    rrx: &Arc<Mutex<mpsc::Receiver<ReadTask>>>,
    wtx: &mpsc::SyncSender<WriteTask>,
    shared: &Shared,
    job: &Job,
    hm: Option<&HardlinkMap>,
    cap: usize,
    start: Instant,
) {
    loop {
        // 任务间隙检查取消:取消则退出(写发送端随之 drop,写线程排空后退出)
        if shared.cancelled.load(Ordering::Relaxed) || job.cancel_requested() {
            shared.cancelled.store(true, Ordering::Relaxed);
            break;
        }
        let task = match rrx
            .lock()
            .unwrap_or_else(|e| e.into_inner()) // 中毒恢复:锁内仅 recv,不可达但防御
            .recv()
        {
            Ok(t) => t,
            Err(_) => break, // 通道关闭:所有读任务已投递
        };
        match read_one_with_retry(&task, wtx, shared, job, hm, cap, start) {
            Ok(()) => {}
            Err(1223) => {
                // 取消:置位后退出(不记错误)
                shared.cancelled.store(true, Ordering::Relaxed);
                break;
            }
            Err(code) => shared.record_error(&task.src, code),
        }
    }
}

/// 读单个任务(带重试,语义与 copy_one_file_with_retry 一致)。
fn read_one_with_retry(
    task: &ReadTask,
    wtx: &mpsc::SyncSender<WriteTask>,
    shared: &Shared,
    job: &Job,
    hm: Option<&HardlinkMap>,
    cap: usize,
    start: Instant,
) -> Result<(), u32> {
    let max_attempts = job.retry.max_attempts.max(1);
    let mut last_code: u32 = 0;
    for attempt in 1..=max_attempts {
        if job.cancel_requested() {
            return Err(1223);
        }
        match read_one(task, wtx, shared, job, hm, cap, start) {
            Ok(()) => return Ok(()),
            Err(1223) => return Err(1223),
            Err(code) => {
                last_code = code;
                let kind = retry::classify(code);
                if kind == retry::RetryKind::Fatal || attempt >= max_attempts {
                    return Err(code);
                }
                Event::Retry {
                    path: task.src.to_string_lossy().into_owned(),
                    code,
                    attempt,
                }
                .emit();
                retry::sleep_backoff(&job.retry, attempt, job.cancel_token.as_deref());
            }
        }
    }
    Err(last_code)
}

/// 内联复制成功后的统计更新(与写线程成功路径一致,保证 job_done 不丢文件)。
/// pub(crate):engine 的 walk 侧(大文件/reparse 内联成功)也用它,
/// 统一进度事件源,避免 walk 与流水线交替发 Progress 导致 files_done 回退。
pub(crate) fn record_inline_success(shared: &Shared, start: Instant, bytes: u64) {
    let done = shared.files_done.fetch_add(1, Ordering::Relaxed) + 1;
    shared.bytes_done.fetch_add(bytes, Ordering::Relaxed);
    maybe_progress(shared, start, done);
}

/// 读一个文件:
/// - 需要硬链接处理(源 n_links>1)或超过缓冲上限 → 传统内联复制
///   (复用 engine::copy_one_file_with_retry:含硬链接串行锁/ACL/ADS/时间戳,
///   与旧行为完全一致;硬链接不进水线,避免写线程在途时 CreateHardLinkW
///   撞 FILE_SHARE 冲突)
/// - 否则:整文件读入缓冲 → 写队列(读/写分离)
fn read_one(
    task: &ReadTask,
    wtx: &mpsc::SyncSender<WriteTask>,
    shared: &Shared,
    job: &Job,
    hm: Option<&HardlinkMap>,
    cap: usize,
    start: Instant,
) -> Result<(), u32> {
    if task.size as usize > cap {
        // BUG 修复:内联路径的字节数必须计入统计,否则 job_done 少算文件
        return crate::engine::copy_one_file_with_retry(
            &task.src,
            &task.dst,
            job.large_threshold_bytes(),
            job,
            hm,
            task.size,
        )
        .map(|n| record_inline_success(shared, start, n));
    }
    if job.preserve_hardlinks {
        let multi_link = crate::hardlink::HardlinkMap::query_file_info(&task.src)
            .map(|(n, _, _)| n > 1)
            .unwrap_or(false);
        if multi_link {
            return crate::engine::copy_one_file_with_retry(
                &task.src,
                &task.dst,
                job.large_threshold_bytes(),
                job,
                hm,
                task.size,
            )
            .map(|n| record_inline_success(shared, start, n));
        }
    }
    // 整文件读入(≤ 缓冲上限;若源在遍历后被追加导致超出上限,回退内联复制,
    // 避免无界内存增长 —— 旧 copy_small 是流式写,无此风险,流水线必须兜底)
    let s = win_io::open_source(&task.src, false)?;
    // 读取源时间戳;失败不致命(目标仍有正确内容),但必须记录,
    // 否则时间戳静默丢失且无迹可查(verify 阶段也无法发现)。
    let times = match win_io::get_file_times(&s) {
        Ok(t) => Some(t),
        Err(code) => {
            Event::Info {
                key: "filetime".to_string(),
                value: format!("{} 读取源时间戳失败: {}", task.src.to_string_lossy(), code),
            }
            .emit();
            None
        }
    };
    let mut data: Vec<u8> = Vec::with_capacity(task.size as usize);
    // 优化 A:读文件时顺带算 BLAKE3(计算远快于 HDD 读,零额外 I/O),
    // 校验阶段用查表替代重读源盘(42 万文件省 31GB 读取)。
    let mut hasher = blake3::Hasher::new();
    let oversized = read_all(&s, &mut data, cap, &mut hasher)?;
    drop(s);
    if oversized {
        drop(data); // 释放已读部分,重新走流式内联复制
        // 回退路径不记录哈希(数据不完整);对应文件校验时回退读源,成本可忽略
        return crate::engine::copy_one_file_with_retry(
            &task.src,
            &task.dst,
            job.large_threshold_bytes(),
            job,
            hm,
            task.size,
        )
        .map(|n| record_inline_success(shared, start, n));
    }
    // 记录源哈希(copy+verify=hash 模式;verify=none 时 Shared.src_hashes=None,空操作)
    shared.record_src_hash(&task.src, hasher.finalize().into());
    // 提交写队列:try_send + 轮询取消,不用阻塞 send。
    // BUG 修复:取消时写线程可能已全部退出(见 writer_loop 1223 处理),
    // 阻塞 send 会因无人消费而永久挂起;轮询保证取消可及时退出。
    let mut wt = WriteTask {
        src: task.src.clone(),
        dst: task.dst.clone(),
        data,
        times,
    };
    loop {
        if job.cancel_requested() || shared.cancelled.load(Ordering::Relaxed) {
            return Err(1223);
        }
        match wtx.try_send(wt) {
            Ok(()) => return Ok(()),
            Err(mpsc::TrySendError::Full(t)) => {
                wt = t;
                std::thread::sleep(std::time::Duration::from_millis(2));
            }
            Err(mpsc::TrySendError::Disconnected(_)) => {
                return Err(crate::ERR_PIPELINE_DISCONNECTED); // 写线程全部退出(异常)
            }
        }
    }
}

/// 整文件读入 data。返回 true 表示读入量超过 cap*2
/// (源文件在遍历后被追加/增长,调用方应回退内联复制)。
/// 复用 thread_local 缓冲,避免每文件重复分配。
/// hasher:顺带算源 BLAKE3(优化 A,校验阶段免读源)。
fn read_all(
    s: &win_io::FileHandle,
    data: &mut Vec<u8>,
    cap: usize,
    hasher: &mut blake3::Hasher,
) -> Result<bool, u32> {
    READ_BUF.with(|cell| {
        let mut buf = cell.borrow_mut();
        loop {
            let n = win_io::read(s, &mut buf)?;
            if n == 0 {
                return Ok(false);
            }
            data.extend_from_slice(&buf[..n]);
            hasher.update(&buf[..n]);
            if data.len() > cap * 2 {
                return Ok(true); // 超出上限:调用方回退内联复制
            }
        }
    })
}

/// 写线程主循环。
pub(crate) fn writer_loop(
    wrx: &Arc<Mutex<mpsc::Receiver<WriteTask>>>,
    shared: &Shared,
    job: &Job,
    start: Instant,
) {
    loop {
        if shared.cancelled.load(Ordering::Relaxed) {
            // 取消:只排空通道,不写盘
            match wrx
                .lock()
                .unwrap_or_else(|e| e.into_inner()) // 中毒恢复:锁内仅 recv,不可达但防御
                .recv()
            {
                Ok(_) => continue,
                Err(_) => break,
            }
        }
        let task = match wrx
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .recv()
        {
            Ok(t) => t,
            Err(_) => break, // 通道关闭:所有写任务已消费
        };
        match write_one_with_retry(&task, job) {
            Ok(()) => {
                let done = shared.files_done.fetch_add(1, Ordering::Relaxed) + 1;
                // 用实际读入字节数而非 walk 时的大小(源文件增长时统计不失真)
                shared
                    .bytes_done
                    .fetch_add(task.data.len() as u64, Ordering::Relaxed);
                maybe_progress(shared, start, done);
            }
            Err(1223) => {
                // 取消:置位后**不退出**,转入顶部排空模式。
                // BUG 修复:若此处 break,所有写线程会在取消时相继退出,
                // 而读线程可能正阻塞在 wtx.send(写队列满) → 无人消费 → 永久挂死。
                shared.cancelled.store(true, Ordering::Relaxed);
            }
            Err(code) => shared.record_error(&task.dst, code),
        }
    }
}

/// 写单个任务(带重试)。
fn write_one_with_retry(task: &WriteTask, job: &Job) -> Result<(), u32> {
    let max_attempts = job.retry.max_attempts.max(1);
    let mut last_code: u32 = 0;
    for attempt in 1..=max_attempts {
        if job.cancel_requested() {
            return Err(1223);
        }
        match write_one(task, job) {
            Ok(()) => return Ok(()),
            Err(1223) => return Err(1223),
            Err(code) => {
                last_code = code;
                let kind = retry::classify(code);
                if kind == retry::RetryKind::Fatal || attempt >= max_attempts {
                    return Err(code);
                }
                Event::Retry {
                    path: task.dst.to_string_lossy().into_owned(),
                    code,
                    attempt,
                }
                .emit();
                retry::sleep_backoff(&job.retry, attempt, job.cancel_token.as_deref());
            }
        }
    }
    Err(last_code)
}

/// 写一个文件:创建目标 + 整缓冲落盘 + 时间戳 + ACL/ADS。
fn write_one(task: &WriteTask, job: &Job) -> Result<(), u32> {
    let d = win_io::open_target(&task.dst, false, job.write_through, true)?;
    win_io::write(&d, &task.data)?;
    if job.write_through {
        win_io::flush(&d)?;
    }
    // 时间戳(数据写完后,关闭前),对齐 /COPY:DAT
    // 失败不致命(数据完整性不受影响),但必须发 Info 事件记录,
    // 否则 verify=hash 与用户预期会失真且无迹可查。
    if let Some(t) = task.times {
        if let Err(code) = win_io::set_file_times(&d, &t) {
            Event::Info {
                key: "filetime".to_string(),
                value: format!("{} 设置时间戳失败: {}", task.dst.to_string_lossy(), code),
            }
            .emit();
        }
    }
    drop(d);
    // ACL/ADS(主流完成后,与 copy_one_file 一致)
    if job.copy_acl {
        if let Err(code) = acl::copy_acl(&task.src, &task.dst) {
            Event::FileError {
                path: task.src.to_string_lossy().into_owned(),
                code,
                stage: "acl".to_string(),
            }
            .emit();
        }
    }
    if job.copy_ads {
        if let Err(code) = acl::copy_ads(&task.src, &task.dst) {
            Event::FileError {
                path: task.src.to_string_lossy().into_owned(),
                code,
                stage: "ads".to_string(),
            }
            .emit();
        }
    }
    Ok(())
}
