//! P5:硬链接去重。
//!
//! 对齐 FastCopy /link 选项:
//! - 检测 nNumberOfLinks > 1 的文件(GetFileInformationByHandle)
//! - 同源同目标只复制一次,其余用 CreateHardLinkW 重建
//!
//! 实现要点:
//! 1. 复制前查询文件的 nNumberOfLinks 和 FileIndex(inode)
//! 2. nNumberOfLinks > 1 → 记录 (volume_serial, file_index) → 已复制目标路径 映射
//! 3. 后续遇到同 inode 的文件,用 CreateHardLinkW 重建硬链接(节省空间 + 保留链接关系)
//! 4. 跨卷无法重建硬链接(硬链接不能跨卷),自动降级为普通复制

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use windows::Win32::Storage::FileSystem::{
    CreateFileW, GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    FILE_FLAG_BACKUP_SEMANTICS, OPEN_EXISTING, FILE_SHARE_READ,
};
use windows::core::PCWSTR;

/// 硬链接去重表:全局共享,线程安全。
/// key = (volume_serial, file_index),value = 已复制的目标路径。
///
/// 串行锁(serial)用于保护"lookup + copy + insert"流程(query 在锁外执行),
/// 避免多线程并发时两个硬链接文件同时进入"首次遇到"分支导致都走复制路径
/// (race condition:thread A lookup miss → copy;thread B lookup miss → copy;
///  两者都 insert,后一个覆盖前一个,硬链接关系丢失)。
/// 串行化仅影响 nNumberOfLinks > 1 的文件,普通文件不受影响。
#[derive(Debug)]
pub struct HardlinkMap {
    map: Mutex<HashMap<(u32, u64), PathBuf>>,
    serial: Mutex<()>,
}

impl HardlinkMap {
    pub fn new() -> Self {
        Self {
            map: Mutex::new(HashMap::new()),
            serial: Mutex::new(()),
        }
    }

    /// 获取串行锁。在 copy_one_file 的 lookup + copy + insert 全程持有,
    /// 确保同一时刻只有一个线程在处理硬链接文件(nNumberOfLinks > 1)。
    pub fn lock_serial(&self) -> std::sync::MutexGuard<'_, ()> {
        self.serial.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// 查询源文件是否有硬链接,返回 (nNumberOfLinks, volume_serial, file_index)。
    /// 失败返回 None(不启用硬链接去重)。
    pub fn query_file_info(path: &Path) -> Option<(u32, u32, u64)> {
        const GENERIC_READ: u32 = 0x8000_0000;
        let w = to_wide(path);
        let h = unsafe {
            CreateFileW(
                PCWSTR(w.as_ptr()),
                GENERIC_READ,
                FILE_SHARE_READ,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
        };
        let handle = match h {
            Ok(h) if !h.is_invalid() => h,
            _ => return None,
        };

        let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
        let r = unsafe { GetFileInformationByHandle(handle, &mut info) };
        let _ = unsafe { windows::Win32::Foundation::CloseHandle(handle) };

        if r.is_err() {
            return None;
        }

        // nNumberOfLinks:硬链接数(>=1,>1 表示有多个硬链接)
        let n_links = info.nNumberOfLinks;
        // volume_serial:卷序列号(区分不同卷的相同 file_index)
        let vol_serial = info.dwVolumeSerialNumber;
        // file_index:文件唯一标识(inode),nFileIndexHigh + nFileIndexLow 组成 64 位
        let file_index = ((info.nFileIndexHigh as u64) << 32) | (info.nFileIndexLow as u64);

        Some((n_links, vol_serial, file_index))
    }

    /// 查询此 inode 是否已复制,返回已复制目标路径。
    pub fn lookup(&self, vol_serial: u32, file_index: u64) -> Option<PathBuf> {
        let map = self.map.lock().ok()?;
        map.get(&(vol_serial, file_index)).cloned()
    }

    /// 记录 inode → 已复制目标路径。
    pub fn insert(&self, vol_serial: u32, file_index: u64, dst: PathBuf) {
        if let Ok(mut map) = self.map.lock() {
            map.insert((vol_serial, file_index), dst);
        }
    }

    /// 尝试用 CreateHardLinkW 重建硬链接。
    /// 成功返回 true,失败返回 false(调用方应降级为普通复制)。
    pub fn create_hardlink(existing: &Path, new_link: &Path) -> bool {
        let w_existing = to_wide(existing);
        let w_new = to_wide(new_link);
        let r = unsafe {
            windows::Win32::Storage::FileSystem::CreateHardLinkW(
                PCWSTR(w_new.as_ptr()),
                PCWSTR(w_existing.as_ptr()),
                None,
            )
        };
        r.is_ok()
    }
}

/// 路径转 UTF-16(NUL 结尾)。
fn to_wide(path: &Path) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    let mut v: Vec<u16> = path.as_os_str().encode_wide().collect();
    v.push(0);
    v
}
