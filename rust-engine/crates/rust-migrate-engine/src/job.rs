//! 任务定义:解析与校验 job.json 输入。
//! 对应执行文档 §2.3.1 字段表。

use serde::Deserialize;
use std::path::{Component, Path, PathBuf};

/// 复制模式:copy=/E 等价;mirror=/MIR 等价(含 purge);verify=只校验不复制。
#[derive(Debug, Deserialize, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Copy,
    Mirror,
    Verify,
}

impl Default for Mode {
    fn default() -> Self {
        Mode::Copy
    }
}

impl Mode {
    pub fn as_str(&self) -> &'static str {
        match self {
            Mode::Copy => "copy",
            Mode::Mirror => "mirror",
            Mode::Verify => "verify",
        }
    }
}

/// 磁盘模式:决定大文件 I/O 调度策略(对齐 FastCopy 的 disk_mode)。
/// - Same:源和目标在同一物理盘 → 顺序大块读写(避免磁头抖动)
/// - Diff:源和目标在不同物理盘 → 读写并行(双缓冲交替)
/// - Auto:运行时探测(默认)
#[derive(Debug, Deserialize, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum DiskMode {
    Auto,
    Same,
    Diff,
}

impl Default for DiskMode {
    fn default() -> Self {
        DiskMode::Auto
    }
}

/// 校验级别:none=大小+时间戳;hash=BLAKE3(P5 启用)。
#[derive(Debug, Deserialize, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Verify {
    None,
    Hash,
}

impl Default for Verify {
    fn default() -> Self {
        Verify::None
    }
}

/// 重试策略(v5 §4.4)。
/// P3 起由 engine.rs 的 copy_one_file_with_retry 实际消费。
#[derive(Debug, Deserialize)]
pub struct Retry {
    #[serde(default = "default_max_attempts")]
    pub max_attempts: u32,
    #[serde(default = "default_backoff_base")]
    pub backoff_base_ms: u32,
    #[serde(default)]
    pub network_path: bool,
}

impl Default for Retry {
    fn default() -> Self {
        Retry {
            max_attempts: 5,
            backoff_base_ms: 500,
            network_path: false,
        }
    }
}

fn default_max_attempts() -> u32 {
    5
}
fn default_backoff_base() -> u32 {
    500
}
fn default_flush_checkpoint_mb() -> u32 {
    64
}
fn default_large_file_threshold_mb() -> u32 {
    64
}
fn default_block_size_mb() -> u32 {
    // 0 = 自动按物理内存分级(见 block_size_bytes)。
    // 用户显式设置非 0 值时以用户值为准。
    0
}
fn default_queue_depth() -> u32 {
    // 0 = 自动按物理内存分级(见 queue_depth_value)。
    // 用户显式设置非 0 值时以用户值为准。
    0
}

/// 探测物理内存(MB)。失败返回 0(按低配处理)。
/// 直接 FFI 调 kernel32!GlobalMemoryStatusEx,避免依赖 windows-rs 的 feature 路径
/// (该 API 在 0.58 中属 Win32_System_SystemInformation 而非 Win32_System_Memory,
///  feature 漂移易导致编译失败;FFI 直调更稳定且零额外依赖)。
/// 兼容 XP+,32 位也能正确读 >4GB 物理内存的 ullTotalPhys。
pub fn physical_memory_mb() -> u64 {
    #[repr(C)]
    struct MemoryStatusEx {
        dw_length: u32,
        dw_memory_load: u32,
        ull_total_phys: u64,
        ull_avail_phys: u64,
        ull_total_page_file: u64,
        ull_avail_page_file: u64,
        ull_total_virtual: u64,
        ull_avail_virtual: u64,
        ull_avail_extended_virtual: u64,
    }
    extern "system" {
        fn GlobalMemoryStatusEx(lpBuffer: *mut MemoryStatusEx) -> i32;
    }
    let mut status = MemoryStatusEx {
        dw_length: std::mem::size_of::<MemoryStatusEx>() as u32,
        dw_memory_load: 0,
        ull_total_phys: 0,
        ull_avail_phys: 0,
        ull_total_page_file: 0,
        ull_avail_page_file: 0,
        ull_total_virtual: 0,
        ull_avail_virtual: 0,
        ull_avail_extended_virtual: 0,
    };
    let ok = unsafe { GlobalMemoryStatusEx(&mut status) };
    if ok != 0 {
        status.ull_total_phys / (1024 * 1024)
    } else {
        0
    }
}

fn default_soft_delete() -> bool {
    true
}
fn default_fast_move() -> bool {
    // P9:同卷快速移动默认开启(仅同卷+目标不存在时触发,失败回退复制,零风险)
    true
}
fn default_adaptive_cache() -> bool {
    // 自适应缓存策略默认开启。
    // 热启动(源文件在系统缓存)→ CopyFileW(利用缓存,~4GB/s);
    // 冷启动(源文件不在缓存)→ 无缓冲 I/O(避免缓存污染)。
    // 关闭后强制走无缓冲 I/O(对齐 FastCopy AERO 策略)。
    true
}
fn default_reparse_mode() -> String {
    // 符号链接处理模式:skip=跳过(默认),copy=保留链接本身。
    // 默认 skip 保持向后兼容;copy 对齐 FastCopy /link。
    "skip".to_string()
}

/// purge 配置(仅 mirror 模式生效)。
#[derive(Debug, Deserialize, Default)]
pub struct Purge {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_soft_delete")]
    pub soft_delete: bool,
    /// dry-run:只输出将删除的清单,不真删(v5 §4.3)。
    #[serde(default)]
    pub dry_run: bool,
}

/// 顶层任务结构,对应 job.json。
#[allow(dead_code)] // verify/retry/flush_checkpoint_mb/purge/background_mode 在 P2-P5 逐步启用
#[derive(Debug, Deserialize)]
pub struct Job {
    pub source: PathBuf,
    pub target: PathBuf,
    #[serde(default)]
    pub mode: Mode,
    #[serde(default)]
    pub verify: Verify,
    #[serde(default)]
    pub retry: Retry,
    #[serde(default = "default_flush_checkpoint_mb")]
    pub flush_checkpoint_mb: u32,
    #[serde(default)]
    pub purge: Purge,
    #[serde(default)]
    pub background_mode: bool,
    /// P6 优化(2026-08-07 实测):进程级后台模式(PROCESS_MODE_BACKGROUND_BEGIN)
    /// 会限制进程工作集/缓存驻留 + 全 I/O VeryLow 排队,复制吞吐实测降 ~20 倍
    /// (SSD 1GB+5000 文件:314s vs 基线 13.6s)——对文件复制过于激进。
    /// 故 background_mode=true 只做句柄级 FILE_IO_PRIORITY_HINT_INFO(VeryLow,
    /// 不影响缓存,温和让路);本字段单独开启进程级"极致让路"模式,默认 false。
    #[serde(default)]
    pub process_background: bool,
    #[serde(default)]
    pub write_through: bool,
    /// P4.5:磁盘模式,决定大文件 I/O 调度策略(对齐 FastCopy disk_mode)。
    /// auto(默认):运行时探测源/目标是否在同一物理盘
    /// same:强制顺序读写(同盘优化)
    /// diff:强制读写并行(跨盘优化)
    #[serde(default)]
    pub disk_mode: DiskMode,
    #[serde(default = "default_large_file_threshold_mb")]
    pub large_file_threshold_mb: u32,
    /// P4.5 任务#8:大文件块大小(MB),用于无缓冲 I/O 路径。
    /// 可选值:1/4/16/64(其他值会被夹到 [1,64])。默认 64MB(实测调优结果见 default_block_size_mb)。
    /// 块大小影响:大块→吞吐高/系统调用少,但内存占用大、ckpt 粒度粗;
    /// 小块→内存占用小、ckpt 粒度细,但系统调用开销占比上升。
    /// 实测调优见 bench_block_size.py(任务#8)。
    #[serde(default = "default_block_size_mb")]
    pub block_size_mb: u32,
    /// P4.5+:IOCP 异步 I/O 队列深度(在途请求数)。
    /// 仅 diff HDD + IOCP 路径生效。打满队列深度提升读写并行度(对齐 FastCopy 重叠 I/O)。
    /// 默认 4(平衡内存占用与吞吐:4 块 × 64MB = 256MB)。
    #[serde(default = "default_queue_depth")]
    pub queue_depth: u32,
    /// P4.5+:自适应缓存策略(检测冷/热启动,动态选择 CopyFileW 或无缓冲路径)。
    /// true(默认):首次复制时探测源文件是否在系统缓存中,
    ///   热启动 → CopyFileW(利用缓存,~4GB/s),冷启动 → 无缓冲 I/O(避免缓存污染)。
    /// false:强制走无缓冲 I/O(对齐 FastCopy AERO 策略,始终绕过缓存)。
    /// 续传场景(有 ckpt 或目标部分存在)始终走无缓冲 I/O,不受此开关影响。
    #[serde(default = "default_adaptive_cache")]
    pub adaptive_cache: bool,
    /// P5:符号链接/reparse point 处理策略。
    /// "skip"(默认):跳过符号链接和 junction,发 FileError code=1742
    /// "copy":保留链接本身(用 CreateSymbolicLinkW/CreateDirectoryW 重建,不跟随)
    /// 对齐 FastCopy 的 /link /relink 参数。
    #[serde(default = "default_reparse_mode")]
    pub reparse_mode: String,
    /// P5:ACL/安全描述符复制(对齐 FastCopy /acl 选项)。
    /// true:复制源文件的安全描述符(DACL/Owner/Group,需 SeRestorePrivilege)
    /// false(默认):目标继承目标目录的默认 ACL
    /// 需要进程具备 SeBackupPrivilege + SeRestorePrivilege 才能完整复制其他用户的文件 ACL。
    #[serde(default)]
    pub copy_acl: bool,
    /// P5:备用数据流(ADS)复制(对齐 FastCopy /stream 选项)。
    /// true:用 BackupRead 枚举并复制所有 ADS(如 Zone.Identifier:$DATA)
    /// false(默认):只复制主流(未命名 $DATA 流),ADS 丢失
    #[serde(default)]
    pub copy_ads: bool,
    /// P5:硬链接去重(对齐 FastCopy /link 选项)。
    /// true:检测 nNumberOfLinks > 1 的文件,同源同目标只复制一次,其余用 CreateHardLinkW 重建
    /// false(默认):每个路径独立复制(浪费空间但兼容跨卷)
    #[serde(default)]
    pub preserve_hardlinks: bool,
    /// 取消标志文件路径;引擎周期性检查,存在即停止。
    #[serde(default)]
    pub cancel_token: Option<PathBuf>,
    /// P9:同卷快速移动(原子重命名,零复制)。
    /// 仅当源/目标同一卷且目标不存在时触发,失败自动回退复制路径。
    /// rename 原子性保证目标即完整数据,无需复制与校验;
    /// Python 侧监听 fast_move=done 事件感知(崩溃恢复由"src 不存在→补建链接"兜底)。
    #[serde(default = "default_fast_move")]
    pub fast_move_same_volume: bool,
}

impl Job {
    /// 作业级校验:路径必须绝对、源必须存在、source/target 不可嵌套。
    /// v5 §10.3 路径穿越防护:规范化 `.`/`..` 组件,防止 mirror purge 误删源自身。
    pub fn validate(&self) -> Result<(), String> {
        if !self.source.is_absolute() {
            return Err("source 必须是绝对路径".into());
        }
        if !self.target.is_absolute() {
            return Err("target 必须是绝对路径".into());
        }
        if self.source == self.target {
            return Err("source 与 target 不能相同".into());
        }
        // 规范化组件:去除 `.`、处理 `..`,得到不含相对组件的可比较路径
        let src_norm = normalize_path(&self.source);
        let tgt_norm = normalize_path(&self.target);
        // 嵌套校验:source 是 target 的祖先或反之 → mirror purge 会自删源
        if src_norm.starts_with(&tgt_norm) {
            return Err(format!(
                "source({}) 在 target({}) 内部,mirror 模式 purge 会误删源",
                src_norm.display(),
                tgt_norm.display()
            ));
        }
        if tgt_norm.starts_with(&src_norm) {
            return Err(format!(
                "target({}) 在 source({}) 内部,mirror 模式 purge 会误删源",
                tgt_norm.display(),
                src_norm.display()
            ));
        }
        Ok(())
    }

    /// 检查取消标志是否被置位。
    /// 带 50ms TTL 缓存:高频调用(每文件/每块)时避免每次都 GetFileAttributesW syscall。
    /// 取消是紧急事件,50ms 延迟可接受(对齐 FastCopy 的取消响应粒度)。
    /// 无 cancel_token 时直接返回 false(零开销)。
    ///
    /// BUG 修复:缓存条目带 token 路径,避免跨 job 串扰
    /// (旧实现用无路径的全局静态缓存,连续跑两个不同 token 的 job 时,
    /// 后一个 job 的 50ms 窗口内会误用前一个 job 的检查结果)。
    pub fn cancel_requested(&self) -> bool {
        match &self.cancel_token {
            Some(p) => {
                use std::sync::Mutex;
                use std::time::{SystemTime, UNIX_EPOCH};
                // 全局缓存:token 路径 + 上次检查时间戳(ms) + 结果。
                // 用全局而非 &mut self,因为 Job 是不可变引用。
                static CACHE: Mutex<Option<(String, u64, bool)>> = Mutex::new(None);

                let now = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|d| d.as_millis() as u64)
                    .unwrap_or(0);
                let path_str = p.to_string_lossy().into_owned();
                // 锁内检查:命中(同路径 + 50ms TTL 内)直接返回缓存
                if let Ok(mut cache) = CACHE.lock() {
                    if let Some((cached_path, cached_ms, cached_result)) = cache.as_ref() {
                        if *cached_path == path_str && now.saturating_sub(*cached_ms) < 50 {
                            return *cached_result;
                        }
                    }
                    // 未命中/过期:在锁内做一次 syscall 并更新缓存
                    // (50ms 才一次,锁内 syscall 的阻塞可忽略)
                    let r = p.exists();
                    *cache = Some((path_str, now, r));
                    r
                } else {
                    // 锁中毒(极罕见):退化为无缓存直接检查
                    p.exists()
                }
            }
            None => false,
        }
    }

    /// 大文件阈值(字节)。补 .max(1) 下限保护,避免 0 导致所有文件走大文件路径。
    pub fn large_threshold_bytes(&self) -> u64 {
        (self.large_file_threshold_mb.max(1) as u64) << 20
    }

    /// P4.5 任务#8:大文件块大小(字节),用于无缓冲 I/O 路径。
    /// block_size_mb=0(默认):按物理内存自动分级
    ///   - >=4GB:64MB(实测调优最优,对齐 FastCopy)
    ///   - 2-4GB:16MB(平衡)
    ///   - <2GB:4MB(低配内存保护,避免换页)
    /// 用户显式设置非 0 值时夹到 [1, 64] 以用户值为准。
    pub fn block_size_bytes(&self) -> usize {
        let mb = if self.block_size_mb == 0 {
            // 自动分级
            match physical_memory_mb() {
                m if m >= 4096 => 64,
                m if m >= 2048 => 16,
                _ => 4, // <2GB 低配
            }
        } else {
            self.block_size_mb.min(64)
        };
        (mb.max(1) as usize) << 20
    }

    /// P4.5+:IOCP 队列深度(在途异步请求数)。
    /// queue_depth=0(默认):按物理内存自动分级
    ///   - >=4GB:4(4×64MB=256MB,现代系统可接受)
    ///   - 2-4GB:2(2×16MB=32MB)
    ///   - <2GB:2(2×4MB=8MB,低配内存保护)
    /// 用户显式设置非 0 值时夹到 [1, 16] 以用户值为准。
    pub fn queue_depth_value(&self) -> u32 {
        if self.queue_depth == 0 {
            match physical_memory_mb() {
                m if m >= 4096 => 4,
                _ => 2, // <4GB 用 2,省内存
            }
        } else {
            self.queue_depth.min(16)
        }
    }

    /// 自适应缓存探测缓冲大小(字节)。
    /// 按内存分级:>=4GB 用 1MB,<4GB 用 256KB(低配省内存)。
    pub fn probe_size_bytes(&self) -> usize {
        match physical_memory_mb() {
            m if m >= 4096 => 1024 * 1024,
            _ => 256 * 1024,
        }
    }

    /// P4.5:获取有效的磁盘模式。Auto 时运行时探测源/目标是否在同一卷。
    /// 探测方法:比较源和目标路径的卷序列号(GetVolumeInformationW)。
    /// 同卷 → Same(顺序读写);不同卷 → Diff(读写并行)。
    pub fn effective_disk_mode(&self) -> DiskMode {
        match self.disk_mode {
            DiskMode::Same => DiskMode::Same,
            DiskMode::Diff => DiskMode::Diff,
            DiskMode::Auto => {
                if same_volume(&self.source, &self.target) {
                    DiskMode::Same
                } else {
                    DiskMode::Diff
                }
            }
        }
    }
}

/// P4.5:检测两个路径是否在同一物理卷上。
/// 用 GetVolumeInformationW 取卷序列号比较,相同=同卷(Same),不同=跨卷(Diff)。
/// 任一查询失败时保守返回 false(视为跨卷,走读写并行路径,更安全)。
pub(crate) fn same_volume(a: &Path, b: &Path) -> bool {
    let va = volume_serial(a);
    let vb = volume_serial(b);
    match (va, vb) {
        (Some(sa), Some(sb)) => sa == sb,
        _ => false,
    }
}

/// 取路径所在卷的序列号(GetVolumeInformationW)。
/// 路径不必存在,只需是有效的卷根(如 C:\、D:\)。
fn volume_serial(path: &Path) -> Option<u32> {
    use std::os::windows::ffi::OsStrExt;
    use windows::Win32::Storage::FileSystem::GetVolumeInformationW;

    // 取路径的卷根(如 C:\Users\foo → C:\)
    let root = path
        .components()
        .next()
        .map(|c| {
            let mut s = c.as_os_str().encode_wide().collect::<Vec<u16>>();
            // 确保以反斜杠结尾(如 C: → C:\)
            let backslash = b'\\' as u16;
            if s.last() != Some(&backslash) {
                s.push(backslash);
            }
            s.push(0); // NUL
            s
        })?;
    let mut serial: u32 = 0;
    let ok = unsafe {
        GetVolumeInformationW(
            windows::core::PCWSTR(root.as_ptr()),
            None,
            Some(&mut serial),  // lpvolumeserialnumber（windows crate 第3参数）
            None,               // lpmaximumcomponentlength
            None,               // lpfilesystemflags
            None,               // lpfilesystemnamebuffer
        )
    };
    if ok.is_ok() {
        Some(serial)
    } else {
        None
    }
}

/// 路径组件级规范化:去除 `.`、消解 `..`(不解析符号链接,文件不存在也可用)。
/// 用于嵌套校验与路径穿越防护(v5 §10.3)。
/// 不调 std::fs::canonicalize:它要求路径存在,且会解析符号链接导致语义变化。
fn normalize_path(p: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            Component::CurDir => {} // 跳过 `.`
            Component::ParentDir => {
                // 仅当栈顶是普通组件(非根/前缀)才 pop,避免消解掉盘符根
                let pop_ok = out
                    .components()
                    .next_back()
                    .map_or(false, |last| matches!(last, Component::Normal(_)));
                if pop_ok {
                    out.pop();
                } else {
                    out.push("..");
                }
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}
