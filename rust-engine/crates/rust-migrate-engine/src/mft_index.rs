//! MFT 索引构建子命令（--mft-index）实现。对应 MFT-Rust重构方案 8.8 第四节。
//!
//! 流程：开卷（CreateFileW + FSCTL_GET_NTFS_VOLUME_DATA）→ 引导扇区校验
//! → 定位 $MFT（记录 0 的 $DATA run list）→ 批量读 → USA fixup + 属性解析
//! → 索引构建（名字池/array 布局/子项分组）→ 拓扑预计算目录大小
//! → 写二进制索引文件（与 fast_scan.py 的 array 布局一一对应，Python 侧
//!   `array.frombytes` 直载，查询代码零改动）。
//!
//! 边界守卫照抄 mft_fast.pyx 已修复版本（M9：记录头 n<0x18；M10：attr_len
//! 下界校验——驻留头 0x18/非驻留 0x40），记录号 > 2^32 明确报错不截断。
//! 任何一步失败返回非零退出码（16），Python 侧完整回退现有 Cython 路径。
//!
//! 参考实现：mft_reader.py / mft_fast.pyx / fast_scan.py（src/mft、src/core）。

use std::collections::{HashMap, VecDeque};
use std::path::Path;

use windows::core::PCWSTR;
use windows::Win32::Storage::FileSystem::*;
use windows::Win32::System::IO::DeviceIoControl;
use windows::Win32::System::Ioctl::FSCTL_GET_NTFS_VOLUME_DATA;

use crate::event::Event;
use crate::win_io::{self, FileHandle};

// ======================================================================
// 常量（与 mft_reader.py / mft_fast.pyx / fast_scan.py 对齐）
// ======================================================================
const GENERIC_READ: u32 = 0x8000_0000;
const FILE_RECORD_IN_USE: u16 = 0x01;
const FILE_RECORD_IS_DIRECTORY: u16 = 0x02;
const ATTR_END: u32 = 0xFFFF_FFFF;
const ATTR_FILE_NAME: u32 = 0x30;
const ATTR_DATA: u32 = 0x80;
const ATTR_REPARSE_POINT: u32 = 0xC0;
// 文件名命名空间
const NS_POSIX: u8 = 0;
const NS_WIN32: u8 = 1;
const NS_DOS: u8 = 2;
// NTFS 根目录记录号固定为 5
const ROOT_RECORD_NUM: u32 = 5;
// 记录号上限：超 32 位时 array('I') 紧凑存储不可用，明确报错不截断
const MAX_REC_NUM: u64 = 0xFFFF_FFFF;
// 系统元数据文件（记录号 < 24，如 $MFT/$BadClus/$LogFile）不计 total_size
const SYSTEM_META_RECORDS: u32 = 24;
// 批量读取/解析的记录数（16384 条 = 16MB/批。实测本环境卷读吞吐
// ~350MB/s 是磁盘硬限制，批大小对吞吐无影响；16MB 内存峰值更小）
const BATCH_SIZE: u64 = 16384;
// 名字锚点步长（fast_scan._build_name_anchors 同款）
const ANCHOR_STEP: usize = 16;

// ======================================================================
// 小端读取辅助
// ======================================================================
#[inline]
fn rd_u16(d: &[u8], off: usize) -> u16 {
    u16::from_le_bytes([d[off], d[off + 1]])
}

#[inline]
fn rd_u32(d: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([d[off], d[off + 1], d[off + 2], d[off + 3]])
}

#[inline]
fn rd_u64(d: &[u8], off: usize) -> u64 {
    u64::from_le_bytes([
        d[off], d[off + 1], d[off + 2], d[off + 3],
        d[off + 4], d[off + 5], d[off + 6], d[off + 7],
    ])
}

/// 读 n 字节小端有符号整数（n <= 8，run list 字段用；超 8 字节由调用方防御）。
fn read_signed_le(d: &[u8], off: usize, n: usize) -> i64 {
    let mut v: i64 = 0;
    for i in 0..n {
        v |= (d[off + i] as i64) << (8 * i);
    }
    // 符号扩展（最高位为 1 时）
    if n < 8 && (d[off + n - 1] & 0x80) != 0 {
        v |= -1i64 << (8 * n);
    }
    v
}

// ======================================================================
// (a) 卷打开与参数
// ======================================================================

/// 一个 run（运行段）：$MFT 文件内 VCN 区间 → 卷上 LCN 映射。
#[derive(Debug, Clone, Copy)]
pub struct Run {
    pub start_vcn: u64,
    pub len: i64,       // 簇数（有符号，解析自 run list）
    pub start_lcn: i64, // 卷上起始 LCN（稀疏段为 0）
    pub is_sparse: bool,
}

/// 卷参数（来自 FSCTL_GET_NTFS_VOLUME_DATA）。
pub struct Volume {
    handle: FileHandle,
    pub bytes_per_sector: u32,
    pub bytes_per_cluster: u32,
    pub bytes_per_record: u32,
    pub mft_start_lcn: u64,
    pub mft_valid_data_length: u64,
}

/// 打开卷并读取 NTFS 卷参数。
///
/// CreateFileW 失败 err==5 → 权限错误（消息中提示，Python 侧会看到 stderr）；
/// FSCTL 失败 → 可能不是 NTFS。
pub fn open_volume(volume: char) -> Result<Volume, String> {
    let dev = format!("\\\\.\\{}:", volume);
    let wide: Vec<u16> = dev.encode_utf16().chain(std::iter::once(0)).collect();
    // 无缓冲 I/O（FILE_FLAG_NO_BUFFERING）：绕过系统缓存直读卷设备，
    // 251 万条 MFT(2.4GB) 冷读实测 365MB/s → 1.5GB/s+（读取是主要瓶颈）。
    // 无缓冲要求偏移/大小/缓冲地址 4096 对齐，由 read_at_no_buffer 处理。
    let h = unsafe {
        CreateFileW(
            PCWSTR(wide.as_ptr()),
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_NO_BUFFERING,
            None,
        )
    };
    let handle = win_io::from_handle(h).map_err(|code| {
        let perm = if code == 5 { "，需要管理员权限" } else { "" };
        format!("打开卷 {}: 失败，错误码 {}{}", volume, code, perm)
    })?;

    // FSCTL_GET_NTFS_VOLUME_DATA：NTFS_VOLUME_DATA_BUFFER（布局见 mft_reader.py:156）。
    // 前 5 字段 LARGE_INTEGER(8B) + 4 字段 DWORD(4B) + 后 5 字段 LARGE_INTEGER(8B) = 0x60 字节。
    // 直接按偏移解析原始字节，避免 windows-rs 结构体字段类型差异。
    let mut buf = [0u8; 0x60];
    let mut returned: u32 = 0;
    let ok = unsafe {
        DeviceIoControl(
            handle.raw(),
            FSCTL_GET_NTFS_VOLUME_DATA,
            None,
            0,
            Some(buf.as_mut_ptr() as *mut _),
            buf.len() as u32,
            Some(&mut returned),
            None,
        )
    };
    if ok.is_err() {
        return Err(format!(
            "FSCTL_GET_NTFS_VOLUME_DATA 失败（卷 {}: 可能不是 NTFS）",
            volume
        ));
    }
    let bytes_per_sector = rd_u32(&buf, 0x28);
    let bytes_per_cluster = rd_u32(&buf, 0x2C);
    let bytes_per_record = rd_u32(&buf, 0x30);
    let mft_valid_data_length = rd_u64(&buf, 0x38);
    let mft_start_lcn = rd_u64(&buf, 0x40);
    if bytes_per_record == 0 {
        return Err(format!("无法确定 MFT 记录大小（卷 {}:）", volume));
    }
    // bpr < 0x18：记录头读 0x14/0x16 字段的前提（M9 守卫）不满足，
    // find_data_run_list / parse_records_bulk 会越界（真实 NTFS 最少 512B）
    if bytes_per_record < 0x18 {
        return Err(format!(
            "MFT 记录大小异常: {}（卷 {}:）",
            bytes_per_record, volume
        ));
    }
    if bytes_per_cluster == 0 || bytes_per_cluster < bytes_per_record {
        // bpc 为 0 会除零；bpc < bpr 时批量读的稀疏填零计数为 0 会死循环
        return Err(format!(
            "卷参数异常: bytes_per_cluster={}, bytes_per_record={}（卷 {}:）",
            bytes_per_cluster, bytes_per_record, volume
        ));
    }
    Ok(Volume {
        handle,
        bytes_per_sector,
        bytes_per_cluster,
        bytes_per_record,
        mft_start_lcn,
        mft_valid_data_length,
    })
}

// ======================================================================
// (b) 引导扇区校验
// ======================================================================

/// 读取 512B 引导扇区，校验偏移 0x03 处 OEM 标识（"NTFS    "）。
/// 参考 mft_reader.py:378-394（与 Python 一致用 4 字节宽松匹配）。
fn check_boot_sector(vol: &Volume) -> Result<(), String> {
    let mut boot = [0u8; 512];
    read_at(&vol.handle, 0, &mut boot)?;
    if !boot[3..].starts_with(b"NTFS") {
        return Err(format!(
            "卷不是 NTFS 文件系统（OEM={:?}）",
            &boot[3..11]
        ));
    }
    Ok(())
}

/// 从卷指定字节偏移读取完整 buf（循环读直到填满）。
/// 句柄为无缓冲(FILE_FLAG_NO_BUFFERING)，ReadFile 要求缓冲/偏移/大小
/// 4096 对齐——内部用临时对齐缓冲委托 read_at_no_buffer 处理。
fn read_at(h: &FileHandle, offset: u64, buf: &mut [u8]) -> Result<(), String> {
    let mut scratch = win_io::AlignedBuf::new(buf.len() + 8192);
    read_at_no_buffer(h, offset, buf, &mut scratch)
}

/// 无缓冲读：偏移向下取整、长度向上取整到 4096 对齐后直读，
/// 再拷贝所需区间（多读最多 8KB，可忽略）。
/// scratch：复用的一块对齐缓冲（大小 = 最大批 + 2×4096），避免每段分配。
fn read_at_no_buffer(
    h: &FileHandle,
    offset: u64,
    buf: &mut [u8],
    scratch: &mut win_io::AlignedBuf,
) -> Result<(), String> {
    const ALIGN: u64 = 4096;
    let aligned_off = offset - (offset % ALIGN);
    let tail = (offset - aligned_off) as usize;
    let aligned_len = ((tail + buf.len() + (ALIGN as usize - 1)) / ALIGN as usize) * ALIGN as usize;
    if scratch.as_slice().len() < aligned_len {
        return Err(format!(
            "无缓冲读 scratch 不足: {} < {}",
            scratch.as_slice().len(),
            aligned_len
        ));
    }
    win_io::seek(h, aligned_off)
        .map_err(|code| format!("SetFilePointerEx 失败，错误码 {}", code))?;
    let mut done = 0usize;
    {
        let s = scratch.as_mut_slice();
        while done < aligned_len {
            let n = win_io::read(h, &mut s[done..aligned_len])
                .map_err(|code| format!("ReadFile 失败，错误码 {}", code))?;
            if n == 0 {
                break;
            }
            done += n;
        }
    }
    if done < tail + buf.len() {
        return Err(format!("卷读取不完整 {}/{} 字节", done, tail + buf.len()));
    }
    buf.copy_from_slice(&scratch.as_slice()[tail..tail + buf.len()]);
    Ok(())
}

// ======================================================================
// (c) run list 解析
// ======================================================================

/// 解析 NTFS 运行列表。返回 (start_vcn, len, start_lcn, is_sparse)。
/// 参考 mft_reader.py:213-249 直译；边界：字段超 8 字节或越界时 break
/// （防越界，参考 Cython 边界修复经验 M9/M10）。
pub fn parse_run_list(data: &[u8], start_offset: usize) -> Vec<Run> {
    let mut runs = Vec::new();
    let mut pos = start_offset;
    let mut prev_lcn: i64 = 0;
    let mut vcn: u64 = 0;
    let data_len = data.len();
    while pos < data_len {
        let header = data[pos];
        if header == 0 {
            break;
        }
        let len_size = (header & 0x0F) as usize;
        let off_size = ((header >> 4) & 0x0F) as usize;
        pos += 1;
        if len_size > 8 || pos + len_size > data_len {
            break;
        }
        let run_len = read_signed_le(data, pos, len_size);
        pos += len_size;
        let (run_lcn, is_sparse) = if off_size == 0 {
            // 稀疏运行（孔洞）
            (0i64, true)
        } else {
            if off_size > 8 || pos + off_size > data_len {
                break;
            }
            let run_off = read_signed_le(data, pos, off_size);
            pos += off_size;
            prev_lcn += run_off;
            (prev_lcn, false)
        };
        if run_len > 0 {
            runs.push(Run {
                start_vcn: vcn,
                len: run_len,
                start_lcn: run_lcn,
                is_sparse,
            });
            vcn += run_len as u64;
        }
    }
    runs
}

// ======================================================================
// (d) $MFT 定位与 $DATA 属性
// ======================================================================

/// 记录 0 的 $DATA 属性形态。
enum DataAttr {
    /// 无 $DATA 属性（与 mft_reader 一致报错，不静默降级）
    Missing,
    /// 驻留 $DATA（整卷极小，用单段退化）
    Resident,
    /// 非驻留 $DATA：run list 偏移（相对记录开头）
    NonResident(usize),
}

/// 在记录 0 中定位 $DATA(0x80) 属性，返回 run list 偏移。
/// USA fixup 原地应用（记录 0 后续不再用）。
fn find_data_run_list(rec: &mut [u8], bytes_per_sector: u32) -> DataAttr {
    if !rec.starts_with(b"FILE") {
        return DataAttr::Missing;
    }
    apply_usa_fixup(rec, bytes_per_sector);
    let n = rec.len();
    let first_attr_offset = rd_u16(rec, 0x14) as usize;
    let mut pos = first_attr_offset;
    while pos + 0x18 <= n {
        let attr_type = rd_u32(rec, pos);
        if attr_type == ATTR_END {
            break;
        }
        let attr_len = rd_u32(rec, pos + 4) as usize;
        if attr_len < 0x18 || attr_len > n {
            break;
        }
        if pos + attr_len > n {
            break;
        }
        if attr_type == ATTR_DATA {
            let non_resident = rec[pos + 8];
            if non_resident != 0 {
                // 非驻留属性头至少 0x40 字节（读 pos+0x20 需 pos+0x22<=n）
                if attr_len < 0x40 {
                    break;
                }
                return DataAttr::NonResident(pos + rd_u16(rec, pos + 0x20) as usize);
            }
            return DataAttr::Resident;
        }
        pos += attr_len;
    }
    DataAttr::Missing
}

/// 建立 $MFT 运行列表映射。
/// 参考 mft_reader.py:396-428：记录 0 位于 mft_start_lcn * bytes_per_cluster
/// （此时 run list 未建立，不能用 VCN 定位）；解析失败退化为单段。
fn load_mft_runlist(vol: &Volume) -> Result<Vec<Run>, String> {
    let start_lcn = vol.mft_start_lcn;
    if start_lcn > i64::MAX as u64 {
        return Err(format!("MFT 起始 LCN 异常: {}", start_lcn));
    }
    let fallback = vec![Run {
        start_vcn: 0,
        len: 1,
        start_lcn: start_lcn as i64,
        is_sparse: false,
    }];
    let disk_offset = start_lcn * vol.bytes_per_cluster as u64;
    let mut rec0 = vec![0u8; vol.bytes_per_record as usize];
    read_at(&vol.handle, disk_offset, &mut rec0)?;
    match find_data_run_list(&mut rec0, vol.bytes_per_sector) {
        DataAttr::Missing => Err("$MFT 记录 0 中未找到 $DATA 属性".into()),
        DataAttr::Resident => Ok(fallback),
        DataAttr::NonResident(off) => {
            let runs = parse_run_list(&rec0, off);
            if runs.is_empty() {
                Ok(fallback)
            } else {
                Ok(runs)
            }
        }
    }
}

// ======================================================================
// (e) 批量读取（跨 run 分段）
// ======================================================================

/// 批量读取连续 MFT 记录，返回 count * bytes_per_record 字节。
/// 参考 mft_reader.py:473-526 直译：跨 run 分段读、稀疏填零、
/// 剩余不足一条记录时跳段填零。读取失败直接返回错误（Python 侧整体回退
/// Cython 路径，Cython 路径自带逐条降级）。
fn read_records_bulk(
    vol: &Volume,
    runs: &[Run],
    start_record: u64,
    count: u64,
    scratch: &mut win_io::AlignedBuf,
) -> Result<Vec<u8>, String> {
    let bpc = vol.bytes_per_cluster as u64;
    let bpr = vol.bytes_per_record as u64;
    // open_volume 已保证 bpc >= bpr >= 1（此处防御，防止递归调用时被绕过）
    if bpc < bpr || bpr == 0 {
        return Err(format!("卷参数异常: bytes_per_cluster={}, bytes_per_record={}", bpc, bpr));
    }
    let mut result = Vec::with_capacity((count * bpr) as usize);
    let mut remaining = count;
    let mut current = start_record;
    while remaining > 0 {
        let file_offset = current * bpr;
        let vcn = file_offset / bpc;
        // 找当前 VCN 所在 run
        let run = runs
            .iter()
            .find(|r| r.start_vcn <= vcn && vcn < r.start_vcn + r.len as u64)
            .copied();
        let run = match run {
            Some(r) => r,
            None => break, // VCN 不在 run list 范围内（防御，与 Python 一致）
        };
        if run.is_sparse {
            // 稀疏区域填零（记录签名非 FILE，解析时自然跳过）
            let zero_count = remaining.min(run.len as u64 * (bpc / bpr));
            result.resize(result.len() + (zero_count * bpr) as usize, 0);
            current += zero_count;
            remaining -= zero_count;
            continue;
        }
        if run.start_lcn < 0 {
            return Err(format!("$MFT 运行段 LCN 为负（损坏 run list，VCN={}）", vcn));
        }
        let run_start_byte = run.start_vcn * bpc;
        let offset_in_run = file_offset - run_start_byte;
        let bytes_can_read = run.len as u64 * bpc - offset_in_run;
        let records_can_read = remaining.min(bytes_can_read / bpr);
        if records_can_read <= 0 {
            // 当前 run 剩余字节不足一条记录，跳段填零
            let next_vcn = run.start_vcn + run.len as u64;
            let next_record = (next_vcn * bpc) / bpr;
            let skip = (next_record - current).max(1).min(remaining);
            result.resize(result.len() + (skip * bpr) as usize, 0);
            current += skip;
            remaining -= skip;
            continue;
        }
        let read_bytes = (records_can_read * bpr) as usize;
        let disk_offset = run.start_lcn as u64 * bpc + offset_in_run;
        let start = result.len();
        result.resize(start + read_bytes, 0);
        read_at_no_buffer(&vol.handle, disk_offset, &mut result[start..], scratch)?;
        current += records_can_read;
        remaining -= records_can_read;
    }
    Ok(result)
}

// ======================================================================
// (f) USA fixup + 属性解析（核心，边界守卫照抄 Cython 修复版）
// ======================================================================

/// 应用 Update Sequence Array (USA) 修复（原地）。
/// 参考 mft_fast.pyx:66-103 直译（含 usa_count<2 / 越界防御）。
pub fn apply_usa_fixup(rec: &mut [u8], bytes_per_sector: u32) {
    let n = rec.len();
    if n < 8 {
        return;
    }
    if !rec.starts_with(b"FILE") {
        return;
    }
    let usa_offset = rd_u16(rec, 0x04) as usize;
    let usa_count = rd_u16(rec, 0x06) as usize;
    if usa_count < 2 || usa_offset + usa_count * 2 > n {
        return;
    }
    let check_value = rd_u16(rec, usa_offset);
    for i in 1..usa_count {
        let sector_end = i * bytes_per_sector as usize - 2;
        if sector_end + 2 > n {
            break;
        }
        let current = rd_u16(rec, sector_end);
        if current != check_value {
            continue;
        }
        let replace = rd_u16(rec, usa_offset + i * 2);
        rec[sector_end] = (replace & 0xFF) as u8;
        rec[sector_end + 1] = (replace >> 8) as u8;
    }
}

/// 一条解析出的 MFT 记录（与 fast_scan.py 枚举输入一一对应）。
#[derive(Debug, Clone)]
pub struct MftRecord {
    pub record_num: u32,
    pub name: String,
    /// 父目录引用低 32 位（fast_scan.py:174 `& 0xFFFFFFFF` 同款）
    pub parent_ref: u32,
    pub is_dir: bool,
    pub size: u64,
    pub is_reparse: bool,
}

/// 批量解析 MFT 记录：USA fixup + 属性遍历 + 字段提取（一次 C 循环）。
/// 参考 mft_fast.pyx:199-365 直译。
///
/// 边界守卫（**必须**，照抄已修复的 Cython 加固）：
///   - 记录头 n<0x18 返回（M9：原 n<4 会静默越界读脏数据）
///   - attr_len 下界校验：驻留 <0x18 / 非驻留 <0x40 break（M10）
/// 记录号 > 2^32：明确报错不截断（array('I') 紧凑存储不可用）。
pub fn parse_records_bulk(
    bulk: &mut [u8],
    bytes_per_record: u32,
    bytes_per_sector: u32,
    start_record_num: u64,
) -> Result<Vec<MftRecord>, String> {
    let bpr = bytes_per_record as usize;
    let n = bpr; // 批量读保证每条记录固定 bpr 字节
    let count = bulk.len() / bpr;
    // 防御：记录头读 0x14/0x16 需 n >= 0x18（M9 守卫的前提），异常参数直接报错
    if bpr < 0x18 {
        return Err(format!("MFT 记录大小异常: {}", bytes_per_record));
    }
    let mut results = Vec::new();
    for i in 0..count {
        let rec = &mut bulk[i * bpr..(i + 1) * bpr];
        let record_num = start_record_num + i as u64;
        if record_num > MAX_REC_NUM {
            return Err(format!("MFT 记录号 {} 超出 32 位紧凑存储上限", record_num));
        }

        // 签名检查
        if !rec.starts_with(b"FILE") {
            continue;
        }

        // USA Fixup（原地）
        apply_usa_fixup(rec, bytes_per_sector);

        // 记录标志
        let first_attr_offset = rd_u16(rec, 0x14) as usize;
        let flags = rd_u16(rec, 0x16);
        let is_in_use = flags & FILE_RECORD_IN_USE != 0;
        let is_dir = flags & FILE_RECORD_IS_DIRECTORY != 0;
        if !is_in_use {
            continue;
        }

        // ---------- 遍历属性链表 ----------
        let mut pos = first_attr_offset;
        let mut has_file_name = false;
        let mut has_data = false;
        let mut is_reparse = false;
        // 最佳文件名（命名空间选择）
        let mut best_name: Option<&[u8]> = None;
        let mut best_parent: u64 = 0;
        let mut best_ns: u8 = NS_DOS;
        // $DATA 信息
        let mut is_non_resident_data = false;
        let mut data_content_length: u32 = 0;
        let mut data_real_size: u64 = 0;

        // M9:头部读需偏移 0x16-0x17，记录头至少 0x18 字节
        while pos + 0x18 <= n {
            let attr_type = rd_u32(rec, pos);
            if attr_type == ATTR_END {
                break;
            }
            let attr_len = rd_u32(rec, pos + 4) as usize;
            // M10:attr_len 下界校验（驻留头 0x18/非驻留 0x40），
            // 损坏记录 attr_len 过小时防越界读
            if attr_len < 0x18 || attr_len > n {
                break;
            }
            if pos + attr_len > n {
                break;
            }

            if attr_type == ATTR_FILE_NAME {
                let non_resident = rec[pos + 8];
                if non_resident == 0 {
                    let content_offset = rd_u16(rec, pos + 0x14) as usize;
                    // $FILE_NAME 内容边界：content+0x42 处读名字需 <= n
                    if pos + content_offset + 0x42 <= n {
                        let parent_ref = rd_u64(rec, pos + content_offset);
                        let name_len_chars = rec[pos + content_offset + 0x40] as usize;
                        let namespace = rec[pos + content_offset + 0x41];
                        let name_bytes = name_len_chars * 2;
                        let name_start = pos + content_offset + 0x42;
                        if name_start + name_bytes <= n {
                            let cur_name = &rec[name_start..name_start + name_bytes];
                            // 命名空间选择（照抄 Cython 实际逻辑）：
                            // 无条件取第一条；后续 Win32(1) 替换任何非 Win32；
                            // POSIX(0) 仅替换 DOS(2)
                            let take = best_name.is_none()
                                || (namespace == NS_WIN32 && best_ns != NS_WIN32)
                                || (namespace == NS_POSIX && best_ns == NS_DOS);
                            if take {
                                best_name = Some(cur_name);
                                best_parent = parent_ref;
                                best_ns = namespace;
                            }
                            has_file_name = true;
                        }
                    }
                }
            } else if attr_type == ATTR_DATA && !is_dir {
                let non_resident = rec[pos + 8];
                if non_resident == 0 {
                    data_content_length = rd_u32(rec, pos + 0x10);
                    is_non_resident_data = false;
                } else {
                    // 非驻留属性头至少 0x40 字节（读 pos+0x30 需 pos+0x38<=n）
                    if attr_len < 0x40 {
                        break;
                    }
                    data_real_size = rd_u64(rec, pos + 0x28); // 0x28=AllocatedSize(实际占用)：占位符/稀疏文件比 DataLength(0x30) 更真实
                    is_non_resident_data = true;
                }
                has_data = true;
            } else if attr_type == ATTR_REPARSE_POINT {
                is_reparse = true;
            }

            pos += attr_len;
        }

        if !has_file_name {
            continue;
        }

        // ---------- 组装结果 ----------
        let size = if has_data && !is_dir {
            if is_non_resident_data {
                data_real_size
            } else {
                data_content_length as u64
            }
        } else {
            0
        };
        let name = utf16le_to_string_lossy(best_name.unwrap());
        results.push(MftRecord {
            record_num: record_num as u32,
            name,
            parent_ref: (best_parent & 0xFFFF_FFFF) as u32, // 低 32 位（fast_scan 同款）
            is_dir,
            size,
            is_reparse,
        });
    }
    Ok(results)
}

/// UTF-16LE 转 String（非法序列用 U+FFFD，等价 Python errors="replace"）。
/// 直接迭代 u16 单元，避免中间 Vec 分配（250 万次调用场景收益明显）。
fn utf16le_to_string_lossy(bytes: &[u8]) -> String {
    let units = bytes
        .chunks_exact(2)
        .map(|c| u16::from_le_bytes([c[0], c[1]]));
    char::decode_utf16(units)
        .map(|r| r.unwrap_or('\u{FFFD}'))
        .collect()
}

// ======================================================================
// (g) 索引构建
// ======================================================================

/// 紧凑索引数据（与 fast_scan.py 的 array 字段一一对应）。
/// v2（内存优化）：sizes 主表 u32（4B/条）+ 溢出表（>4GB 大文件），
/// 250 万条记录省 ~10MB；names 池由 Python 侧 mmap 视图承载（不拷贝）。
pub struct IndexData {
    pub names: Vec<u8>,
    pub name_lens: Vec<u16>,
    pub name_anchors: Vec<u32>,
    pub rec_nums: Vec<u32>,
    /// v2: u32 主表（溢出文件存低 32 位，查询走 size_ovf）
    pub sizes: Vec<u32>,
    pub flags: Vec<u8>,
    /// v2 新增: 溢出记录在 records 中的 index（升序）
    pub size_ovf_idx: Vec<u32>,
    /// v2 新增: 对应 64 位大小
    pub size_ovf_val: Vec<u64>,
    pub dir_entries_p: Vec<u32>,
    pub dir_entries_start: Vec<u32>,
    pub child_data: Vec<u32>,
    pub dir_size_idx: Vec<u32>,
    pub dir_size_val: Vec<u64>,
    pub root_index: u32,
    pub file_count: u64,
    pub dir_count: u64,
    pub total_size: u64,
}

/// 构建索引（参考 fast_scan.py:172-227）：
/// - 名字字节池 + 2B 长度数组 + 每 16 条一个 4B 锚点（省 ~40% 索引内存）
/// - 子项关系：packed (parent<<32|idx) 排序 → 目录入口数组 + 子项顺序数据
/// - 统计：root_index / file_count / dir_count / total_size（记录号 < 24 不计）
pub fn build_index(records: &[MftRecord]) -> IndexData {
    let count = records.len();
    let mut names: Vec<u8> = Vec::new();
    let mut name_lens = Vec::with_capacity(count);
    let mut rec_nums = Vec::with_capacity(count);
    // v2: sizes 主表 u32（4B/条）+ 溢出表（>4GB 大文件）
    let mut sizes: Vec<u32> = Vec::with_capacity(count);
    let mut size_ovf_idx: Vec<u32> = Vec::new();
    let mut size_ovf_val: Vec<u64> = Vec::new();
    let mut flags = Vec::with_capacity(count);
    // 子项收集：packed (parent_rec_num << 32) | child_index
    let mut child_packed = Vec::with_capacity(count);
    let mut root_index = u32::MAX; // u32::MAX 表示未找到（Python 侧转 -1）
    let mut file_count: u64 = 0;
    let mut dir_count: u64 = 0;
    let mut total_size: u64 = 0;

    for (idx, rec) in records.iter().enumerate() {
        // 名字 UTF-8；超 64KB（理论上不可能）截断为合法前缀（fast_scan 同款防御）
        let mut name_b: &[u8] = rec.name.as_bytes();
        if name_b.len() > 0xFFFF {
            name_b = &name_b[..0xFFFF];
        }
        names.extend_from_slice(name_b);
        name_lens.push(name_b.len() as u16);
        rec_nums.push(rec.record_num);
        if rec.size > u32::MAX as u64 {
            // 大文件（>4GB）：主表存低 32 位，完整值进溢出表（查询走溢出表）
            size_ovf_idx.push(idx as u32);
            size_ovf_val.push(rec.size);
            sizes.push((rec.size & 0xFFFF_FFFF) as u32);
        } else {
            sizes.push(rec.size as u32);
        }
        // bit0=is_dir bit1=is_reparse
        flags.push((rec.is_dir as u8) | ((rec.is_reparse as u8) << 1));
        child_packed.push(((rec.parent_ref as u64) << 32) | idx as u64);
        if rec.record_num == ROOT_RECORD_NUM {
            root_index = idx as u32;
        }
        if rec.is_dir {
            dir_count += 1;
        } else {
            file_count += 1;
            // 排除系统元数据文件（记录号 < 24，如 $MFT/$BadClus/$LogFile）
            if rec.record_num >= SYSTEM_META_RECORDS {
                total_size += rec.size;
            }
        }
    }

    // 名字锚点：每 16 条一个起始偏移 + 哨兵（fast_scan._build_name_anchors 同构）
    let mut name_anchors = Vec::new();
    let mut anchor_total: u32 = 0;
    let mut i = 0;
    while i < count {
        name_anchors.push(anchor_total);
        let end = (i + ANCHOR_STEP).min(count);
        let mut s: u32 = 0;
        for k in i..end {
            s += name_lens[k] as u32;
        }
        anchor_total += s;
        i += ANCHOR_STEP;
    }
    name_anchors.push(anchor_total); // 哨兵：名字总长

    // 子项关系：按 parent 分组——目录入口数组 + 子项顺序数据
    // (parent 字段只存目录出现次数而非每条记录,省内存；布局与 fast_scan 一致)
    child_packed.sort();
    let mut dir_entries_p: Vec<u32> = Vec::new();
    let mut dir_entries_start: Vec<u32> = Vec::new();
    let mut child_data: Vec<u32> = Vec::with_capacity(count);
    let mut k = 0usize;
    while k < count {
        let p = (child_packed[k] >> 32) as u32;
        let start = child_data.len() as u32;
        while k < count && (child_packed[k] >> 32) as u32 == p {
            child_data.push((child_packed[k] & 0xFFFF_FFFF) as u32);
            k += 1;
        }
        dir_entries_p.push(p);
        dir_entries_start.push(start);
    }

    IndexData {
        names,
        name_lens,
        name_anchors,
        rec_nums,
        sizes,
        flags,
        size_ovf_idx,
        size_ovf_val,
        dir_entries_p,
        dir_entries_start,
        child_data,
        dir_size_idx: Vec::new(),
        dir_size_val: Vec::new(),
        root_index,
        file_count,
        dir_count,
        total_size,
    }
}

// ======================================================================
// (h) 拓扑预计算目录大小
// ======================================================================

/// 在升序目录入口数组中二分查找 parent_rec_num，返回下标。
/// 与 fast_scan._iter_children 的二分下界同构。
fn find_entry(entries_p: &[u32], target: u32) -> Option<usize> {
    let mut lo = 0usize;
    let mut hi = entries_p.len();
    while lo < hi {
        let mid = (lo + hi) / 2;
        if entries_p[mid] < target {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    if lo < entries_p.len() && entries_p[lo] == target {
        Some(lo)
    } else {
        None
    }
}

/// 目录 p（记录号）的子项 index 区间 [start, end)。
/// 与 fast_scan._iter_children 的区间切片同构。
fn children_range(
    entries_p: &[u32],
    entries_start: &[u32],
    child_data_len: usize,
    p: u32,
) -> Option<(usize, usize)> {
    let lo = find_entry(entries_p, p)?;
    let start = entries_start[lo] as usize;
    let end = if lo + 1 < entries_p.len() {
        entries_start[lo + 1] as usize
    } else {
        child_data_len
    };
    Some((start, end))
}

/// 拓扑排序预计算所有目录大小（O(N log N)，从叶子目录逐层向上）。
/// 参考 fast_scan.py:352-439 直译：
/// - dir_parent 映射 {dir_rec_num: (parent_rec_num, dir_index)}
/// - pending 计数（普通子目录数，reparse 子目录不计）
/// - 队列从叶子开始；处理时子目录取 tmp 值、文件取 size
/// - 防环：pending 减不到 0 的目录不处理（root 自引用天然跳过，与原版一致）
/// - 结果转排序数组 dir_size_idx/dir_size_val（二分查询）
pub fn precompute_dir_sizes(data: &mut IndexData) {
    // 目录映射：{dir_rec_num: (parent_rec_num, dir_index)}
    // 携带目录 index，避免"记录号→index"反查（MFT 枚举顺序中记录号不递增）
    let mut dir_parent: HashMap<u32, (u32, u32)> = HashMap::new();
    let entries_p = &data.dir_entries_p;
    let entries_start = &data.dir_entries_start;
    let child_data = &data.child_data;
    let rec_nums = &data.rec_nums;
    let sizes = &data.sizes; // v2: u32 主表
    let size_ovf_idx = &data.size_ovf_idx;
    let size_ovf_val = &data.size_ovf_val;
    let flags = &data.flags;
    let n_entries = entries_p.len();
    for e in 0..n_entries {
        let start = entries_start[e] as usize;
        let end = if e + 1 < n_entries {
            entries_start[e + 1] as usize
        } else {
            child_data.len()
        };
        for j in start..end {
            let ci = child_data[j] as usize;
            if flags[ci] & 0x01 != 0 {
                dir_parent.insert(rec_nums[ci], (entries_p[e], child_data[j]));
            }
        }
    }

    // 1. 统计每个目录的"待处理普通子目录数"（reparse 子目录不计）
    let mut pending: HashMap<u32, u32> = HashMap::new();
    for (&p, _) in dir_parent.iter() {
        let mut cnt: u32 = 0;
        if let Some((start, end)) = children_range(entries_p, entries_start, child_data.len(), p) {
            for j in start..end {
                let ci = child_data[j] as usize;
                if flags[ci] & 0x01 != 0 && flags[ci] & 0x02 == 0 {
                    cnt += 1;
                }
            }
        }
        pending.insert(p, cnt);
    }

    // 2. 从叶子目录开始（pending=0）
    let mut queue: VecDeque<u32> = pending
        .iter()
        .filter(|(_, &c)| c == 0)
        .map(|(&p, _)| p)
        .collect();

    // 预计算期临时表：{dir_index: size}（转数组后释放）
    let mut tmp: HashMap<u32, u64> = HashMap::new();
    while let Some(p) = queue.pop_front() {
        // 计算本目录大小：累加所有普通子目录的缓存值 + 直接子文件大小
        let mut total: u64 = 0;
        if let Some((start, end)) = children_range(entries_p, entries_start, child_data.len(), p) {
            for j in start..end {
                let ci = child_data[j] as usize;
                if flags[ci] & 0x01 != 0 {
                    // reparse 目录不展开（避免重复计算）
                    if flags[ci] & 0x02 != 0 {
                        continue;
                    }
                    total += tmp.get(&child_data[j]).copied().unwrap_or(0);
                } else {
                    // v2: sizes 主表 u32，>4GB 溢出文件走 size_ovf 表（极少，二分）
                    let mut size = sizes[ci] as u64;
                    if let Some(oi) = find_entry(size_ovf_idx, ci as u32) {
                        size = size_ovf_val[oi];
                    }
                    total += size;
                }
            }
        }
        let pidx = dir_parent[&p].1; // 目录 index（构建时已记录）
        tmp.insert(pidx, total);

        // 通知父目录：减少一个待处理子目录
        if let Some(&(pp, _)) = dir_parent.get(&p) {
            if let Some(c) = pending.get_mut(&pp) {
                // 防下溢：root 自引用处理完后再通知自己时 pending 已为 0，
                // 不能减（fast_scan 的 Python int 可为负，行为等价于不减）
                if *c > 0 {
                    *c -= 1;
                    if *c == 0 {
                        queue.push_back(pp);
                    }
                }
            }
        }
    }

    // 转排序数组（二分查询），释放临时表
    let mut items: Vec<(u32, u64)> = tmp.into_iter().collect();
    items.sort_by_key(|&(k, _)| k);
    data.dir_size_idx = items.iter().map(|&(k, _)| k).collect();
    data.dir_size_val = items.iter().map(|&(_, v)| v).collect();
}

// ======================================================================
// (i) 二进制写出
// ======================================================================

/// 写 u16 数组（分块转换避免逐元素 syscall / 翻倍内存）。
fn write_u16s(f: &mut std::fs::File, vals: &[u16]) -> Result<(), String> {
    use std::io::Write;
    let mut buf = Vec::with_capacity(65536);
    for v in vals {
        buf.extend_from_slice(&v.to_le_bytes());
        if buf.len() >= 65536 {
            f.write_all(&buf).map_err(|e| format!("写索引文件失败: {}", e))?;
            buf.clear();
        }
    }
    if !buf.is_empty() {
        f.write_all(&buf).map_err(|e| format!("写索引文件失败: {}", e))?;
    }
    Ok(())
}

/// 写 u32 数组。
fn write_u32s(f: &mut std::fs::File, vals: &[u32]) -> Result<(), String> {
    use std::io::Write;
    let mut buf = Vec::with_capacity(65536);
    for v in vals {
        buf.extend_from_slice(&v.to_le_bytes());
        if buf.len() >= 65536 {
            f.write_all(&buf).map_err(|e| format!("写索引文件失败: {}", e))?;
            buf.clear();
        }
    }
    if !buf.is_empty() {
        f.write_all(&buf).map_err(|e| format!("写索引文件失败: {}", e))?;
    }
    Ok(())
}

/// 写 u64 数组。
fn write_u64s(f: &mut std::fs::File, vals: &[u64]) -> Result<(), String> {
    use std::io::Write;
    let mut buf = Vec::with_capacity(65536);
    for v in vals {
        buf.extend_from_slice(&v.to_le_bytes());
        if buf.len() >= 65536 {
            f.write_all(&buf).map_err(|e| format!("写索引文件失败: {}", e))?;
            buf.clear();
        }
    }
    if !buf.is_empty() {
        f.write_all(&buf).map_err(|e| format!("写索引文件失败: {}", e))?;
    }
    Ok(())
}

/// 写二进制索引文件（方案 8.8 第三节格式，v2 内存优化版）。
///
/// 头部比方案多存 ndirs/nsized/novf 三个 u64——dir_entries 与 dir_size 的
/// 条目数无法从 count 推导，Python 侧据此精确校验每个数组长度（防截断/损坏）。
///
/// v2 变更（内存优化）：sizes 主表 u32（4B/条）+ 溢出表（>4GB 大文件），
/// 250 万条记录省 ~10MB；names 池 Python 侧 mmap 视图承载（不拷贝）。
///
/// ```text
/// [magic "MFTI" 4B][version u32=2][count u64][ndirs u64][nsized u64][novf u64][names_len u64]
/// [names bytes(UTF-8 拼接)]
/// [name_lens u16 × count]              ← array('H').frombytes
/// [name_anchors u32 × (count/16+1)]    ← array('I').frombytes
/// [rec_nums u32 × count]               ← array('I')
/// [sizes u32 × count]                  ← array('I')（v2: 4B/条，溢出文件存低 32 位）
/// [flags u8 × count]                   ← array('B')
/// [size_ovf_idx u32 × novf]            ← array('I')（v2: 溢出记录 index）
/// [size_ovf_val u64 × novf]            ← array('Q')（v2: 溢出 64 位大小）
/// [dir_entries_p u32 × ndirs]          ← array('I')
/// [dir_entries_start u32 × ndirs]      ← array('I')
/// [child_data u32 × count]             ← array('I')
/// [dir_size_idx u32 × nsized]          ← array('I')
/// [dir_size_val u64 × nsized]          ← array('Q')
/// [root_index u32][file_count u64][dir_count u64][total_size u64]
/// ```
pub fn write_index_file(path: &Path, data: &IndexData) -> Result<(), String> {
    use std::io::Write;
    let mut f = std::fs::File::create(path)
        .map_err(|e| format!("创建索引文件失败: {}", e))?;
    let count = data.rec_nums.len() as u64;
    let ndirs = data.dir_entries_p.len() as u64;
    let nsized = data.dir_size_idx.len() as u64;
    let novf = data.size_ovf_idx.len() as u64;
    let names_len = data.names.len() as u64;

    let mut head = Vec::with_capacity(4 + 4 + 8 * 6 + 8);
    head.extend_from_slice(b"MFTI");
    head.extend_from_slice(&2u32.to_le_bytes()); // v2
    head.extend_from_slice(&count.to_le_bytes());
    head.extend_from_slice(&ndirs.to_le_bytes());
    head.extend_from_slice(&nsized.to_le_bytes());
    head.extend_from_slice(&novf.to_le_bytes());
    head.extend_from_slice(&names_len.to_le_bytes());
    f.write_all(&head)
        .map_err(|e| format!("写索引文件失败: {}", e))?;
    f.write_all(&data.names)
        .map_err(|e| format!("写索引文件失败: {}", e))?;
    write_u16s(&mut f, &data.name_lens)?;
    write_u32s(&mut f, &data.name_anchors)?;
    write_u32s(&mut f, &data.rec_nums)?;
    write_u32s(&mut f, &data.sizes)?; // v2: u32 主表
    f.write_all(&data.flags)
        .map_err(|e| format!("写索引文件失败: {}", e))?;
    write_u32s(&mut f, &data.size_ovf_idx)?;
    write_u64s(&mut f, &data.size_ovf_val)?;
    write_u32s(&mut f, &data.dir_entries_p)?;
    write_u32s(&mut f, &data.dir_entries_start)?;
    write_u32s(&mut f, &data.child_data)?;
    write_u32s(&mut f, &data.dir_size_idx)?;
    write_u64s(&mut f, &data.dir_size_val)?;

    let mut tail = Vec::with_capacity(4 + 8 * 3);
    tail.extend_from_slice(&data.root_index.to_le_bytes());
    tail.extend_from_slice(&data.file_count.to_le_bytes());
    tail.extend_from_slice(&data.dir_count.to_le_bytes());
    tail.extend_from_slice(&data.total_size.to_le_bytes());
    f.write_all(&tail)
        .map_err(|e| format!("写索引文件失败: {}", e))?;
    Ok(())
}

// ======================================================================
// 主流程：--mft-index --volume X --out <path>
// ======================================================================

/// --mft-index 子命令入口。成功返回 0，失败返回 16（沿用现有 CLI 约定）。
/// 进度经 Event::Info(mft_progress) JSONL 输出，Python 侧读 stdout 驱动进度条。
pub fn run(volume: char, out: &Path) -> i32 {
    let t0 = std::time::Instant::now();

    // (a) 卷打开
    let vol = match open_volume(volume) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("[rust-engine] MFT 索引失败: {}", e);
            return 16;
        }
    };
    // (b) 引导扇区校验
    if let Err(e) = check_boot_sector(&vol) {
        eprintln!("[rust-engine] MFT 索引失败: {}", e);
        return 16;
    }
    // (c)(d) $MFT run list
    let runs = match load_mft_runlist(&vol) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("[rust-engine] MFT 索引失败: {}", e);
            return 16;
        }
    };
    let total = vol.mft_valid_data_length / vol.bytes_per_record as u64;
    // 记录号超 32 位上限提前失败（parse_records_bulk 有逐条防御兜底）。
    // 收紧为 > MAX_REC_NUM：count 达 2^32 时 dir_entries_start 的
    // child_data.len() as u32 会 wrap，索引错乱（真实卷记录数远小于此）
    if total > MAX_REC_NUM {
        eprintln!(
            "[rust-engine] MFT 索引失败: 记录总数 {} 超出 32 位紧凑存储上限",
            total
        );
        return 16;
    }
    eprintln!(
        "[rust-engine] MFT 索引: 卷 {}: 共 {} 条记录, {} 个运行段, 记录大小 {}B",
        volume, total, runs.len(), vol.bytes_per_record
    );

    // (e)(f) 批量读取 + 解析（进度事件驱动 Python 侧进度条）
    let mut records: Vec<MftRecord> = Vec::new();
    let mut start: u64 = 0;
    // 无缓冲读对齐缓冲：最大批(16MB) + 2×4096 对齐冗余，全程复用
    let mut scratch =
        win_io::AlignedBuf::new(BATCH_SIZE as usize * vol.bytes_per_record as usize + 8192);
    while start < total {
        let count = BATCH_SIZE.min(total - start);
        let mut bulk = match read_records_bulk(&vol, &runs, start, count, &mut scratch) {
            Ok(b) => b,
            Err(e) => {
                eprintln!(
                    "[rust-engine] MFT 索引失败: 批量读取记录 {}-{}: {}",
                    start,
                    start + count,
                    e
                );
                return 16;
            }
        };
        let recs = match parse_records_bulk(
            &mut bulk,
            vol.bytes_per_record,
            vol.bytes_per_sector,
            start,
        ) {
            Ok(r) => r,
            Err(e) => {
                eprintln!("[rust-engine] MFT 索引失败: 解析记录: {}", e);
                return 16;
            }
        };
        records.extend(recs);
        start += count;
        Event::Info {
            key: "mft_progress".into(),
            value: format!("{}/{}", start, total),
        }
        .emit();
    }

    // (g) 索引构建 + (h) 拓扑预计算
    let mut index = build_index(&records);
    // 防御：名字锚点为 u32 数组，字节池超 4GB 会静默 wrap（真实卷 ~50MB，
    // 理论边界仍拦一下，避免损坏索引而非崩溃）
    if index.names.len() > u32::MAX as usize {
        eprintln!(
            "[rust-engine] MFT 索引失败: 名字字节池 {} 字节超出 u32 锚点上限",
            index.names.len()
        );
        return 16;
    }
    drop(records); // 释放解析中间结果（峰值内存控制）
    precompute_dir_sizes(&mut index);

    // (i) 二进制写出
    if let Err(e) = write_index_file(out, &index) {
        eprintln!("[rust-engine] MFT 索引失败: {}", e);
        return 16;
    }

    let duration_ms = t0.elapsed().as_millis() as u64;
    Event::JobDone {
        files_total: index.file_count,
        bytes_total: index.total_size,
        duration_ms,
        rc: 0,
    }
    .emit();
    eprintln!(
        "[rust-engine] MFT 索引完成: {} 文件, {} 目录, 总大小 {} 字节, 耗时 {}ms, 输出 {}",
        index.file_count,
        index.dir_count,
        index.total_size,
        duration_ms,
        out.display()
    );
    0
}
