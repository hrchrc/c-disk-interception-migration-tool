//! 断点续传 sidecar 文件。对应执行文档 §3 P1 与 v5 §4.2。
//!
//! 设计:与目标文件同目录的 `<target>.migrate-ckpt`,JSON 格式,记录已确认写入磁盘的字节数。
//! 续传时引擎检测 ckpt 存在且目标文件大小 >= ckpt.written,从该偏移继续写;
//! ckpt 缺失/损坏/目标小于 ckpt 记录 → 整文件重传(损坏兜底,v5 §4.2)。
//!
//! 原子性:每块写完 save(通过 tmp+rename 原子替换);周期性 ckpt 保存前先 flush 目标
//! 保证 written 之前数据落盘;cancel 时也先 flush 再 save。
//! 单文件级粒度(不搞块级状态):实现简单,大文件续传足够;海量小文件重传代价低,不需 ckpt。
//!
//! v5 §4.2 末段的 CRC32 区间校验:
//! "已写字节数不完全可信,需要配合校验点"——目标文件大小可能反映的是写入尝试的目标长度,
//! 但最后一块数据在崩溃前只写了一半。解决方案:ckpt 记录 [ckpt_base, written) 区间的 CRC32,
//! 续传时重算该区间验证。不匹配 → ckpt 不可信 → 整文件重传。
//!
//! flush 解决 kill(TerminateProcess)场景:进程被杀但系统正常,缓存不丢,flush 过的数据安全。
//! CRC32 解决断电/崩溃场景:磁盘缓存丢失,flush 过的数据也可能丢,CRC32 检测后重传。

use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use crate::crc32;

#[derive(Debug, Serialize, Deserialize)]
pub struct Checkpoint {
    /// 目标文件绝对路径(校验 ckpt 是否属于当前目标)
    pub target: String,
    /// 源文件大小(校验源未变)
    pub source_size: u64,
    /// 已确认写入磁盘的字节数(下次从此偏继续)
    pub written: u64,
    /// 块大小(校验续传参数一致)
    pub block_size: u32,
    /// CRC32 校验区间起始偏移(v5 §4.2):[ckpt_base, written) 的数据 CRC32
    /// 续传时重算此区间验证数据完整性,不匹配 → ckpt 不可信 → 整文件重传
    #[serde(default)]
    pub ckpt_base: u64,
    /// [ckpt_base, written) 区间的 CRC32 校验值
    /// ckpt_base == 0 且 written == 0 时校验值为 0(空区间)
    #[serde(default)]
    pub crc32: u32,
}

impl Checkpoint {
    /// sidecar 文件路径:`<target>.migrate-ckpt`
    pub fn path_for(target: &Path) -> PathBuf {
        let mut p = target.to_path_buf();
        let mut name = p.file_name().unwrap_or_default().to_os_string();
        name.push(".migrate-ckpt");
        p.set_file_name(name);
        p
    }

    /// 读取 ckpt。返回 None 表示:不存在/损坏/不匹配/CRC32 校验失败 → 整文件重传。
    ///
    /// 匹配条件:
    /// 1. target 路径一致 + source_size 一致 + block_size 一致
    /// 2. 目标文件实际大小 >= ckpt.written(否则 ckpt 比实际还新,不可信)
    /// 3. CRC32 校验通过:[ckpt_base, written) 区间重算 CRC32 与 ckpt.crc32 一致
    ///    (v5 §4.2:检测断电导致缓存丢失、最后一块只写了一半的损坏)
    pub fn load(target: &Path, source_size: u64, block_size: u32) -> Option<Checkpoint> {
        let p = Self::path_for(target);
        let content = fs::read(&p).ok()?;
        let ckpt: Checkpoint = serde_json::from_slice(&content).ok()?;
        if ckpt.target != target.to_string_lossy()
            || ckpt.source_size != source_size
            || ckpt.block_size != block_size
        {
            return None;
        }
        // 目标文件必须至少有 ckpt 记录的字节数(否则目标被外部截断,ckpt 不可信)
        let target_size = fs::metadata(target).map(|m| m.len()).unwrap_or(0);
        if target_size < ckpt.written {
            return None;
        }
        // CRC32 区间校验(v5 §4.2 末段):
        // 重读目标 [ckpt_base, written) 区间,重算 CRC32,与 ckpt.crc32 比对。
        // 不匹配说明该区间数据损坏(断电/崩溃导致缓存丢失,半块脏数据),ckpt 不可信 → 重传。
        // 空区间(ckpt_base == written)跳过校验(首次 ckpt 或 written==0)。
        if ckpt.ckpt_base < ckpt.written {
            if !verify_interval_crc32(target, ckpt.ckpt_base, ckpt.written, ckpt.crc32) {
                return None;
            }
        }
        Some(ckpt)
    }

    /// 写入 ckpt(覆盖式)。调用前应已对目标文件 FlushFileBuffers。
    pub fn save(&self, target: &Path) -> std::io::Result<()> {
        let p = Self::path_for(target);
        let s = serde_json::to_vec(self)?;
        // 先写 .tmp 再 rename,保证 ckpt 自身原子性(避免写一半被 kill 损坏)
        // tmp 路径:<target>.migrate-ckpt.tmp(手动拼接,不用 with_extension 避免替换掉复合后缀)
        let mut tmp = p.clone().into_os_string();
        tmp.push(".tmp");
        let tmp = PathBuf::from(tmp);
        {
            let mut f = OpenOptions::new()
                .create(true)
                .write(true)
                .truncate(true)
                .open(&tmp)?;
            f.write_all(&s)?;
            f.flush()?;
        }
        fs::rename(&tmp, &p)?;
        Ok(())
    }

    /// 删除 ckpt(文件复制完成后调用)。
    pub fn remove(target: &Path) {
        let _ = fs::remove_file(Self::path_for(target));
    }
}

/// 简化的"读已有目标字节数"辅助:用于无 ckpt 时的启发式续传。
/// 若目标已存在且大小 < 源大小,从此偏移续传(无 ckpt 但目标部分有效)。
pub fn existing_target_bytes(target: &Path) -> u64 {
    fs::metadata(target).map(|m| m.len()).unwrap_or(0)
}

/// 验证目标文件 [start, end) 区间的 CRC32 是否与 expected 一致。
/// 用于续传时校验 ckpt 记录的区间数据完整性(v5 §4.2)。
///
/// 性能:读 [start, end) 区间(默认 64MB),SSD 上约 50-100ms,可接受。
/// 失败(文件无法读取/CRC32 不匹配)返回 false,调用方判定 ckpt 不可信 → 整文件重传。
fn verify_interval_crc32(target: &Path, start: u64, end: u64, expected: u32) -> bool {
    compute_interval_crc32(target, start, end).map_or(false, |crc| crc == expected)
}

/// 重读目标文件 [start, end) 区间并计算 CRC32。
/// 稀疏文件支持:空洞区域读回为 0 字节,与写入内容(数据 + 空洞 0 填充)天然一致。
/// 用于稀疏复制保存 ckpt 前对"本次新写区间"做完整性校验(v5 §4.2,与普通路径同级)。
/// 读取失败返回 None。
pub fn compute_interval_crc32(target: &Path, start: u64, end: u64) -> Option<u32> {
    let mut f = match fs::OpenOptions::new().read(true).open(target) {
        Ok(f) => f,
        Err(_) => return None,
    };
    use std::io::Seek;
    if f.seek(std::io::SeekFrom::Start(start)).is_err() {
        return None;
    }
    // 分块读 + 增量更新 CRC32(避免一次性读大区间到内存)
    let mut buf = vec![0u8; 64 * 1024]; // 64KB 块
    let mut remaining = end.saturating_sub(start);
    let mut crc = 0u32;
    while remaining > 0 {
        let want = std::cmp::min(buf.len() as u64, remaining) as usize;
        match f.read(&mut buf[..want]) {
            // EOF 提前 = 目标文件比预期短 = 数据不完整
            Ok(0) => return None,
            Ok(n) => {
                crc = crc32::update(crc, &buf[..n]);
                remaining -= n as u64;
            }
            Err(_) => return None,
        }
    }
    Some(crc)
}
