//! Win32 文件 I/O 的 safe 封装层。所有 unsafe 集中于此。
//! 对应执行文档 §2.2:HANDLE 一律用 OwnedHandle 包裹,业务代码只调 safe 接口。

use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle, RawHandle};
use std::path::Path;

use windows::core::PCWSTR;
use windows::Win32::Foundation::{
    CloseHandle, GetLastError, HANDLE, INVALID_HANDLE_VALUE, FILETIME,
};
use windows::Win32::Storage::FileSystem::*;
use windows::Win32::System::IO::{
    CreateIoCompletionPort, DeviceIoControl, GetQueuedCompletionStatus, OVERLAPPED,
};
use windows::Win32::System::Memory::{
    VirtualAlloc, VirtualFree, MEM_COMMIT, MEM_RELEASE, MEM_RESERVE, PAGE_READWRITE,
};

// 文件访问权限位(CreateFileW 的 dwDesiredAccess 为 u32)
const GENERIC_READ: u32 = 0x8000_0000;
const GENERIC_WRITE: u32 = 0x4000_0000;

/// 从 windows::core::Error 提取 Win32 错误码。
/// HRESULT 布局:严重位(1) + 保留(4) + facility(11) + code(16)
/// 对 facility=7(FACILITY_WIN32),低 16 位即 Win32 code;
/// 其他 facility 不应出现在文件 I/O 路径,回退为 87(ERROR_INVALID_PARAMETER)。
fn win32_err(e: windows::core::Error) -> u32 {
    let hr = e.code().0 as u32;
    let facility = (hr >> 16) & 0x1FFF;
    if facility == 7 {
        hr & 0xFFFF
    } else {
        87 // ERROR_INVALID_PARAMETER(非 Win32 facility,无法映射)
    }
}

/// 文件句柄:OwnedHandle 自动在 Drop 时 CloseHandle,无需手动关闭。
pub struct FileHandle(OwnedHandle);

impl FileHandle {
    /// pub(crate):mft_index.rs 卷读取复用 read/seek 封装时取底层句柄。
    pub(crate) fn raw(&self) -> HANDLE {
        HANDLE(self.0.as_raw_handle())
    }
}

/// 路径转 UTF-16(以 NUL 结尾),供 CreateFileW 使用。
fn to_wide(path: &Path) -> Vec<u16> {
    let mut v: Vec<u16> = path.as_os_str().encode_wide().collect();
    v.push(0);
    v
}

/// 把 CreateFileW 的结果转成 FileHandle。
/// 成功且句柄有效则包装;失败或句柄无效时取错误码。
/// pub(crate):mft_index.rs 卷设备打开(share 模式与文件不同)复用。
pub(crate) fn from_handle(h: windows::core::Result<HANDLE>) -> Result<FileHandle, u32> {
    match h {
        Ok(handle) => {
            if handle.is_invalid() || handle.0.is_null() {
                return Err(unsafe { GetLastError() }.0);
            }
            // P6:后台模式开启时,将句柄 I/O 优先级降为 VeryLow(尽力而为,失败静默)。
            // 挂载在 from_handle 一处,覆盖全部 open 函数
            // (open_source/open_target/open_for_append/open_source_overlapped/open_target_overlapped)。
            crate::priority::apply_if_enabled(handle.0);
            let raw: RawHandle = handle.0;
            // 已确认非 invalid,from_raw_handle 不会 panic
            let owned = unsafe { OwnedHandle::from_raw_handle(raw) };
            Ok(FileHandle(owned))
        }
        Err(e) => Err(win32_err(e)),
    }
}

/// 以读方式打开源文件。no_buffering=true 走无缓冲路径(读写需按扇区对齐)。
pub fn open_source(path: &Path, no_buffering: bool) -> Result<FileHandle, u32> {
    let w = to_wide(path);
    let flags = if no_buffering {
        FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN
    } else {
        FILE_FLAG_SEQUENTIAL_SCAN
    };
    let h = unsafe {
        CreateFileW(
            PCWSTR(w.as_ptr()),
            GENERIC_READ,
            FILE_SHARE_READ,
            None,
            OPEN_EXISTING,
            flags,
            None,
        )
    };
    from_handle(h)
}

/// 以写方式打开目标文件。no_buffering=true 走无缓冲路径。
/// create=true 覆盖已存在(CREATE_ALWAYS);create=false 打开已存在(OPEN_EXISTING),
///   用于大文件尾部缓冲补齐(避免截断已写的整数块)。
pub fn open_target(
    path: &Path,
    no_buffering: bool,
    write_through: bool,
    create: bool,
) -> Result<FileHandle, u32> {
    let w = to_wide(path);
    let flags = if no_buffering {
        // 无缓冲 I/O 已绕过缓存;WRITE_THROUGH 仅在高可靠模式按需开启,否则强制刷盘会显著拖慢吞吐
        let mut f = FILE_FLAG_NO_BUFFERING;
        if write_through {
            f |= FILE_FLAG_WRITE_THROUGH;
        }
        f
    } else if write_through {
        FILE_FLAG_WRITE_THROUGH | FILE_FLAG_SEQUENTIAL_SCAN
    } else {
        FILE_FLAG_SEQUENTIAL_SCAN
    };
    let disp = if create {
        CREATE_ALWAYS
    } else {
        OPEN_EXISTING
    };
    let h = unsafe {
        CreateFileW(
            PCWSTR(w.as_ptr()),
            GENERIC_WRITE,
            FILE_SHARE_READ,
            None,
            disp,
            flags,
            None,
        )
    };
    from_handle(h)
}

/// 续传场景打开目标:OPEN_ALWAYS(不存在则创建,存在则打开且不截断)。
/// 配合 seek 定位到续传偏移,保留已写部分。P1 断点续传专用。
/// P4.5:加 no_buffering 参数,无缓冲 I/O 路径续传专用。
pub fn open_for_append(path: &Path, write_through: bool, no_buffering: bool) -> Result<FileHandle, u32> {
    let w = to_wide(path);
    let flags = if no_buffering {
        let mut f = FILE_FLAG_NO_BUFFERING;
        if write_through {
            f |= FILE_FLAG_WRITE_THROUGH;
        }
        f
    } else if write_through {
        FILE_FLAG_WRITE_THROUGH | FILE_FLAG_SEQUENTIAL_SCAN
    } else {
        FILE_FLAG_SEQUENTIAL_SCAN
    };
    let h = unsafe {
        CreateFileW(
            PCWSTR(w.as_ptr()),
            GENERIC_WRITE,
            FILE_SHARE_READ,
            None,
            OPEN_ALWAYS,
            flags,
            None,
        )
    };
    from_handle(h)
}

/// 同步读。返回实际读取字节数(0 表示到达文件尾)。
pub fn read(h: &FileHandle, buf: &mut [u8]) -> Result<usize, u32> {
    let mut bytes: u32 = 0;
    let r = unsafe { ReadFile(h.raw(), Some(buf), Some(&mut bytes), None) };
    if let Err(e) = r {
        return Err(win32_err(e));
    }
    Ok(bytes as usize)
}

/// 同步写。返回实际写入字节数。
pub fn write(h: &FileHandle, buf: &[u8]) -> Result<usize, u32> {
    let mut bytes: u32 = 0;
    let r = unsafe { WriteFile(h.raw(), Some(buf), Some(&mut bytes), None) };
    if let Err(e) = r {
        return Err(win32_err(e));
    }
    Ok(bytes as usize)
}

/// 移动文件指针到绝对偏移(FILE_BEGIN)。
pub fn seek(h: &FileHandle, offset: u64) -> Result<(), u32> {
    let pos = i64::try_from(offset).unwrap_or(i64::MAX);
    let r = unsafe { SetFilePointerEx(h.raw(), pos, None, FILE_BEGIN) };
    if let Err(e) = r {
        return Err(win32_err(e));
    }
    Ok(())
}

/// 设置文件末尾(配合无缓冲写入:整数块写完后裁剪到真实大小)。
pub fn truncate(h: &FileHandle, size: u64) -> Result<(), u32> {
    seek(h, size)?;
    let r = unsafe { SetEndOfFile(h.raw()) };
    if let Err(e) = r {
        return Err(win32_err(e));
    }
    Ok(())
}

/// 刷盘(强制将缓冲数据写入磁盘)。
pub fn flush(h: &FileHandle) -> Result<(), u32> {
    let r = unsafe { FlushFileBuffers(h.raw()) };
    if let Err(e) = r {
        return Err(win32_err(e));
    }
    Ok(())
}

/// 文件时间戳(创建/最后访问/最后修改),Windows FILETIME 格式(100ns 单位)。
/// 对应 /COPY:DAT 中的 T(时间戳)。
#[repr(C)]
#[derive(Clone, Copy)]
pub struct FileTimes {
    pub creation: FILETIME,
    pub last_access: FILETIME,
    pub last_write: FILETIME,
}

impl FileTimes {
    /// 全零(用于"不修改某项时间"的占位)。
    fn zero() -> Self {
        FileTimes {
            creation: FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 },
            last_access: FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 },
            last_write: FILETIME { dwLowDateTime: 0, dwHighDateTime: 0 },
        }
    }
}

/// 读取源文件的时间戳(创建/访问/修改)。
/// 用 GetFileTime,失败返回错误码。
pub fn get_file_times(h: &FileHandle) -> Result<FileTimes, u32> {
    let mut ft = FileTimes::zero();
    let r = unsafe {
        GetFileTime(
            h.raw(),
            Some(&mut ft.creation),
            Some(&mut ft.last_access),
            Some(&mut ft.last_write),
        )
    };
    if r.is_err() {
        return Err(unsafe { GetLastError().0 });
    }
    Ok(ft)
}

/// 设置目标文件的时间戳。
/// 传入的时间戳来自 get_file_times(源文件),保留原创建/访问/修改时间。
/// 对应 /COPY:DAT 中的 T(时间戳)。
pub fn set_file_times(h: &FileHandle, times: &FileTimes) -> Result<(), u32> {
    let r = unsafe {
        SetFileTime(
            h.raw(),
            Some(&times.creation),
            Some(&times.last_access),
            Some(&times.last_write),
        )
    };
    if r.is_err() {
        return Err(unsafe { GetLastError().0 });
    }
    Ok(())
}

/// 复制源文件时间戳到目标文件(路径级封装)。
/// 打开源(读)+ 目标(写),把三个时间戳从源复制到目标。
/// 用于大文件路径收尾(CopyFileW / 无缓冲 I/O 均不保留源创建时间)。
/// 调用前提:目标文件已写完且**无其他写句柄占用**
/// (open_target 共享模式为 FILE_SHARE_READ,若目标仍有写句柄打开会撞
///  ERROR_SHARING_VIOLATION → 调用方应改用句柄级 get_file_times/set_file_times)。
/// 失败返回错误码,调用方决定是否致命。
pub fn copy_file_times(src: &Path, dst: &Path) -> Result<(), u32> {
    let s = open_source(src, false)?;
    let t = get_file_times(&s)?;
    let d = open_target(dst, false, false, false)?; // create=false → OPEN_EXISTING
    set_file_times(&d, &t)
}

/// 查询路径所在磁盘的扇区大小(字节)。
/// 无缓冲 I/O 要求读写缓冲区地址和字节数都是扇区大小的整数倍。
/// 查询失败时返回 4096(绝大多数现代存储的默认值)。
pub fn get_sector_size(path: &Path) -> u32 {
    let w = to_wide(path);
    let mut sectors_per_cluster = 0u32;
    let mut bytes_per_sector = 0u32;
    let mut number_of_free_clusters = 0u32;
    let mut total_number_of_clusters = 0u32;
    let ok = unsafe {
        GetDiskFreeSpaceW(
            PCWSTR(w.as_ptr()),
            Some(&mut sectors_per_cluster),
            Some(&mut bytes_per_sector),
            Some(&mut number_of_free_clusters),
            Some(&mut total_number_of_clusters),
        )
    };
    if ok.is_ok() && bytes_per_sector > 0 {
        bytes_per_sector
    } else {
        4096
    }
}

/// 页对齐缓冲区。VirtualAlloc 返回页对齐地址(>=4096),满足无缓冲 I/O 的扇区对齐要求。
/// P4.5 起启用:无缓冲 I/O 路径(对齐 FastCopy 的直接 I/O 策略)。
pub struct AlignedBuf {
    ptr: *mut u8,
    size: usize,
}

impl AlignedBuf {
    pub fn new(size: usize) -> Self {
        let ptr = unsafe {
            VirtualAlloc(None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE) as *mut u8
        };
        assert!(!ptr.is_null(), "VirtualAlloc 分配失败");
        Self { ptr, size }
    }

    pub fn as_mut_slice(&mut self) -> &mut [u8] {
        unsafe { std::slice::from_raw_parts_mut(self.ptr, self.size) }
    }

    pub fn as_slice(&self) -> &[u8] {
        unsafe { std::slice::from_raw_parts(self.ptr, self.size) }
    }
}

impl Drop for AlignedBuf {
    fn drop(&mut self) {
        if !self.ptr.is_null() {
            // MEM_RELEASE 时 dwSize 必须为 0;失败通常意味着分配/释放不配对,属于程序 bug
            let _ = unsafe { VirtualFree(self.ptr as *mut _, 0, MEM_RELEASE) };
        }
    }
}

// AlignedBuf 可跨线程移动(Send):指针独占所有权随 Self 转移。
// 契约:同一时刻只能有一个线程访问 as_mut_slice/as_slice(不可并发借用);
// 跨线程传递后,原所有者不得再访问。P1 双缓冲设计必须遵守此契约。
unsafe impl Send for AlignedBuf {}

// ============================================================
// CopyFileW:内核零拷贝复制路径(首次复制专用)
// ============================================================
//
// 性能关键:必须用 CopyFileW,**不能用 CopyFileExW**。
// 实测:CopyFileExW 即使不传回调、不传 pbCancel,也不命中"写缓存"
// (文件刚创建时数据在写缓存中,CopyFileW/旧复制引擎能命中 ~4000 MB/s,
//  CopyFileExW 只有 ~300 MB/s)。疑因 CopyFileExW 内部走了不同 I/O 路径。
//
// 取消机制:CopyFileW 不支持内核级取消。如需取消,由 Python 适配层 kill 进程。
// 进程被 kill 后目标文件可能不完整,下次重跑时从头复制(无 ckpt 可续传)。
// 这与 CopyFileExW+pbCancel 的行为一致(后者取消后也删除目标文件)。

/// CopyFileW 内核零拷贝复制(首次复制专用,不支持续传和内核级取消)。
///
/// 函数名 `copy_file_zero_copy` 反映"内核零拷贝"语义,避免误导为 CopyFileExW。
/// 实际 FFI 调用的是 `CopyFileW`(详见上方注释,CopyFileExW 性能仅 CopyFileW 的 ~6%)。
///
/// 性能:命中写缓存(冷启动 ~4000 MB/s),对齐旧复制引擎速度。
///
/// 成功返回 Ok(());其他失败返回 Win32 错误码。
/// cancel_token 参数保留(兼容接口),CopyFileW 不使用,取消靠进程 kill。
///
/// 注意:必须直接 FFI 调 kernel32!CopyFileW,**不能用**:
///   - CopyFileExW(不命中写缓存,~300 MB/s)
///   - std::fs::copy(内部调 CopyFileExW,同样慢)
///   - windows-rs 的 CopyFileW(疑内部转发到 CopyFileExW,同样慢)
/// 只有直接 FFI 的 CopyFileW 才命中写缓存(~4600 MB/s)。
pub fn copy_file_zero_copy(src: &Path, dst: &Path, _cancel_token: Option<&Path>) -> Result<(), u32> {
    extern "system" {
        fn CopyFileW(
            lpexistingfilename: *const u16,
            lpnewfilename: *const u16,
            bfailifexists: i32,
        ) -> i32;
    }
    let w_src = to_wide(src);
    let w_dst = to_wide(dst);
    let ok = unsafe { CopyFileW(w_src.as_ptr(), w_dst.as_ptr(), 0) };
    if ok != 0 {
        Ok(())
    } else {
        Err(unsafe { GetLastError().0 })
    }
}

// ============================================================
// P4.5+: IOCP 异步批量读写(对齐 FastCopy 重叠 I/O 策略)
// ============================================================
//
// 设计要点:
// 1. OverlappedContext 内嵌 OVERLAPPED 为第一个字段(repr(C)),指针重合
//    → IOCP 完成通知返回的 OVERLAPPED 指针即为 ctx 指针,Box::from_raw 取回所有权
// 2. 无缓冲 I/O + FILE_FLAG_OVERLAPPED:绕过缓存 + 异步重叠(队列深度 > 2)
// 3. ERROR_IO_PENDING (997):异步操作进行中,非错误,等待 IOCP 完成通知
// 4. 立即同步完成(Ok(true))仍会发 IOCP 通知,统一在完成端口处理

/// IOCP 完成端口句柄(OwnedHandle 自动 CloseHandle)。
pub struct IocpHandle(OwnedHandle);

impl IocpHandle {
    fn raw(&self) -> HANDLE {
        HANDLE(self.0.as_raw_handle())
    }
}

/// 创建 IOCP 完成端口。
/// concurrent_threads:允许并发处理完成通知的线程数(0=按处理器数)。
pub fn create_iocp(concurrent_threads: u32) -> Result<IocpHandle, u32> {
    // windows-rs 0.58:CreateIoCompletionPort 第二参数传 HANDLE(非 Option)
    // 创建新端口时传 INVALID_HANDLE_VALUE + null HANDLE
    let h = unsafe {
        CreateIoCompletionPort(
            INVALID_HANDLE_VALUE, // 创建新端口
            HANDLE::default(),    // 无现有端口(null)
            0,
            concurrent_threads,
        )
    };
    match h {
        Ok(handle) => {
            if handle.is_invalid() || handle.0.is_null() {
                return Err(unsafe { GetLastError() }.0);
            }
            let raw: RawHandle = handle.0;
            let owned = unsafe { OwnedHandle::from_raw_handle(raw) };
            Ok(IocpHandle(owned))
        }
        Err(e) => Err(win32_err(e)),
    }
}

/// 关联文件句柄到 IOCP 完成端口。key:完成通知携带的完成键(区分源/目标)。
pub fn associate_to_iocp(iocp: &IocpHandle, handle: &FileHandle, key: usize) -> Result<(), u32> {
    // 第二参数传现有端口的 HANDLE(非 Option)
    let r = unsafe { CreateIoCompletionPort(handle.raw(), iocp.raw(), key, 0) };
    r.map(|_| ()).map_err(win32_err)
}

/// IOCP 完成端口等待结果。
/// - Ok(Some((bytes, key, overlapped))):正常 I/O 完成
/// - Ok(None):超时(无完成通知)
/// - Err((code, key, overlapped)):失败的 I/O 完成(如磁盘错误),仍需回收 overlapped
pub fn get_queued_completion(
    iocp: &IocpHandle,
    timeout_ms: u32,
) -> Result<Option<(u32, usize, *mut OVERLAPPED)>, (u32, usize, *mut OVERLAPPED)> {
    let mut bytes: u32 = 0;
    let mut key: usize = 0;
    let mut overlapped: *mut OVERLAPPED = std::ptr::null_mut();
    // windows-rs 0.58:GetQueuedCompletionStatus 返回 Result<()>(BOOL 包装)
    // 但语义上:FALSE + overlapped非null = 失败I/O完成(非API失败),需用 overlapped 判断
    let r = unsafe {
        GetQueuedCompletionStatus(iocp.raw(), &mut bytes, &mut key, &mut overlapped, timeout_ms)
    };
    if overlapped.is_null() {
        Ok(None) // 超时或端口错误(r 必为 Err,但无 overlapped 可回收)
    } else {
        match r {
            Ok(()) => Ok(Some((bytes, key, overlapped))),       // 成功完成
            Err(e) => Err((win32_err(e), key, overlapped)), // 失败 I/O 完成
        }
    }
}

/// 异步读。设置 OVERLAPPED 偏移后发起 ReadFile(异步)。
/// 返回 Ok(true)=立即同步完成,Ok(false)=异步进行中(ERROR_IO_PENDING),Err=真实错误。
/// 无论同步还是异步完成,都会发 IOCP 完成通知(需在完成端口处理)。
pub fn async_read(
    handle: &FileHandle,
    buf: &mut [u8],
    offset: u64,
    ctx_ptr: *mut OVERLAPPED,
) -> Result<bool, u32> {
    unsafe {
        (*ctx_ptr).Anonymous.Anonymous.Offset = (offset & 0xFFFF_FFFF) as u32;
        (*ctx_ptr).Anonymous.Anonymous.OffsetHigh = (offset >> 32) as u32;
    }
    let r = unsafe { ReadFile(handle.raw(), Some(buf), None, Some(ctx_ptr)) };
    match r {
        Ok(()) => Ok(true), // 立即同步完成(仍会发 IOCP 通知)
        Err(e) => {
            let code = win32_err(e);
            if code == 997 {
                // ERROR_IO_PENDING:异步进行中,正常
                Ok(false)
            } else {
                Err(code)
            }
        }
    }
}

/// 异步写。设置 OVERLAPPED 偏移后发起 WriteFile(异步)。
pub fn async_write(
    handle: &FileHandle,
    buf: &[u8],
    offset: u64,
    ctx_ptr: *mut OVERLAPPED,
) -> Result<bool, u32> {
    unsafe {
        (*ctx_ptr).Anonymous.Anonymous.Offset = (offset & 0xFFFF_FFFF) as u32;
        (*ctx_ptr).Anonymous.Anonymous.OffsetHigh = (offset >> 32) as u32;
    }
    let r = unsafe { WriteFile(handle.raw(), Some(buf), None, Some(ctx_ptr)) };
    match r {
        Ok(()) => Ok(true),
        Err(e) => {
            let code = win32_err(e);
            if code == 997 {
                Ok(false)
            } else {
                Err(code)
            }
        }
    }
}

/// 以异步(overlapped)+ 可选无缓冲方式打开源文件。
/// IOCP 路径必须用 FILE_FLAG_OVERLAPPED,否则 ReadFile 传 overlapped 会报参数错误。
pub fn open_source_overlapped(path: &Path, no_buffering: bool) -> Result<FileHandle, u32> {
    let w = to_wide(path);
    let mut flags = FILE_FLAG_OVERLAPPED;
    if no_buffering {
        flags |= FILE_FLAG_NO_BUFFERING;
    }
    flags |= FILE_FLAG_SEQUENTIAL_SCAN;
    let h = unsafe {
        CreateFileW(
            PCWSTR(w.as_ptr()),
            GENERIC_READ,
            FILE_SHARE_READ,
            None,
            OPEN_EXISTING,
            flags,
            None,
        )
    };
    from_handle(h)
}

/// 以异步(overlapped)+ 可选无缓冲方式打开目标文件。
/// create=true 覆盖(CREATE_ALWAYS),create=false 续传(OPEN_EXISTING)。
pub fn open_target_overlapped(
    path: &Path,
    no_buffering: bool,
    write_through: bool,
    create: bool,
) -> Result<FileHandle, u32> {
    let w = to_wide(path);
    let mut flags = FILE_FLAG_OVERLAPPED;
    if no_buffering {
        flags |= FILE_FLAG_NO_BUFFERING;
    }
    if write_through {
        flags |= FILE_FLAG_WRITE_THROUGH;
    }
    if !no_buffering && !write_through {
        flags |= FILE_FLAG_SEQUENTIAL_SCAN;
    }
    let disp = if create { CREATE_ALWAYS } else { OPEN_EXISTING };
    let h = unsafe {
        CreateFileW(
            PCWSTR(w.as_ptr()),
            GENERIC_WRITE,
            FILE_SHARE_READ,
            None,
            disp,
            flags,
            None,
        )
    };
    from_handle(h)
}

/// IOCP 异步 I/O 的上下文:内嵌 OVERLAPPED(第一个字段,repr(C) 保证指针重合)+
/// 缓冲区所有权 + 块偏移 + 实际字节数 + 读/写状态。
///
/// 生命周期:Box 分配 → 提交异步请求 → IOCP 完成通知 → from_overlapped_ptr 取回所有权。
/// 同一时刻一个 ctx 只在一个环节(读/写/空闲)。
#[repr(C)]
pub struct OverlappedContext {
    pub overlapped: OVERLAPPED, // 第一个字段:OVERLAPPED 指针 = ctx 指针
    pub buf: Box<AlignedBuf>,
    pub offset: u64, // 块在文件中的偏移
    pub n: usize,    // 读完成后的实际读取字节数(写请求时为待写字节数)
    pub is_write: bool, // true=写完成通知,false=读完成通知
}

impl OverlappedContext {
    pub fn new(buf: Box<AlignedBuf>, offset: u64) -> Self {
        Self {
            overlapped: unsafe { std::mem::zeroed() },
            buf,
            offset,
            n: 0,
            is_write: false,
        }
    }

    pub fn overlapped_ptr(&mut self) -> *mut OVERLAPPED {
        &mut self.overlapped as *mut OVERLAPPED
    }

    /// 从 IOCP 完成通知返回的 OVERLAPPED 指针取回 ctx 所有权。
    /// 安全性:ptr 必须来自 Box<OverlappedContext> 的 overlapped 字段地址,
    /// 且 OVERLAPPED 是 repr(C) 结构的第一个字段(指针重合)。
    pub unsafe fn from_overlapped_ptr(ptr: *mut OVERLAPPED) -> Box<OverlappedContext> {
        Box::from_raw(ptr as *mut OverlappedContext)
    }
}

// OverlappedContext 拥有 AlignedBuf,跨线程传递(提交异步请求到内核,完成通知在另一线程取回)。
// 契约:同一时刻只有一个线程访问 ctx(提交后内核持有指针,完成通知后单线程取回)。
unsafe impl Send for OverlappedContext {}

// ============================================================
// P4.5+:自适应缓存策略(检测冷/热启动,动态选择 CopyFileW 或无缓冲路径)
// ============================================================
//
// 核心思想:
// - 热启动(源文件已在系统缓存中)→ CopyFileW:利用缓存读 + 写缓存,~4GB/s
// - 冷启动(源文件不在缓存)→ 无缓冲 I/O:绕过缓存,避免缓存污染
//
// 检测方法:两次读比较法(不依赖绝对速度阈值,适配所有存储类型)
// 1. 第一次读 1MB(缓冲 I/O):可能命中缓存(快)或从磁盘读(慢)
// 2. 第二次读同一 1MB:必然命中缓存(第一次读已载入)
// 3. 比较两次耗时:比值 < 2 → 第一次也从缓存(热启动);比值 >= 2 → 第一次从磁盘(冷启动)
//
// 优势:比值法不受存储类型影响(NVMe/HDD/SATA 均适用),
//   因为第二次读必从缓存,作为基准,第一次读与之比较即可判断。
// 缓存代价:探测读入 1MB 数据(相对大文件可忽略)。

/// 探测源文件是否在系统缓存中(热启动检测)。
///
/// 用缓冲 I/O(FILE_FLAG_SEQUENTIAL_SCAN,不带 NO_BUFFERING)读取前 1MB,
/// 采用"两次读比较法"判断冷/热启动:
/// - 第一次读:可能命中缓存(快)或从磁盘读(慢,有寻道延迟)
/// - 第二次读:必然命中缓存(第一次读已将数据载入)
/// - 比值 < 2 → 第一次也从缓存(热启动);比值 >= 2 → 第一次从磁盘(冷启动)
///
/// 探测失败时保守返回 false(冷启动,走无缓冲 I/O 更安全)。
pub fn probe_cache_hot(path: &Path) -> bool {
    let s = match open_source(path, false) {
        Ok(h) => h,
        Err(_) => return false,
    };
    // 探测缓冲按内存分级:>=4GB 用 1MB,<4GB 用 256KB(低配省内存)
    let probe_size: usize = if crate::job::physical_memory_mb() >= 4096 {
        1024 * 1024
    } else {
        256 * 1024
    };
    let mut buf = vec![0u8; probe_size];

    // 第一次读(可能命中缓存或从磁盘读)
    let t1 = std::time::Instant::now();
    let n1 = match read(&s, &mut buf) {
        Ok(n) => n,
        Err(_) => return false,
    };
    let d1 = t1.elapsed();
    if n1 == 0 {
        return false;
    }

    // seek 回开头,第二次读同一块数据(必然命中缓存)
    if seek(&s, 0).is_err() {
        return false;
    }
    let t2 = std::time::Instant::now();
    let n2 = match read(&s, &mut buf) {
        Ok(n) => n,
        Err(_) => return false,
    };
    let d2 = t2.elapsed();
    if n2 == 0 {
        return false;
    }

    // 比较两次耗时:第二次必从缓存(基准),第一次与之比较
    // 比值 < 2 → 第一次也从缓存(热启动);比值 >= 2 → 第一次从磁盘(冷启动)
    // 阈值 2.0:实测 NVMe 冷读/缓存读比值约 2-3x,缓存/缓存约 1.0x,2.0 居中
    let d1_ns = d1.as_nanos() as f64;
    let d2_ns = d2.as_nanos().max(1) as f64;
    let ratio = d1_ns / d2_ns;
    ratio < 2.0
}

/// P8:检测盘符所在卷的物理介质是否为 SSD(无寻道惩罚)。
/// 打开卷设备 "\\\\.\\X:",IOCTL_STORAGE_QUERY_PROPERTY 查询 SeekPenalty。
/// 失败(网络盘/无权限/可移动介质不支持)保守返回 false(按 HDD 处理,
/// 线程数不放大 —— 只影响性能,不影响正确性)。
pub fn is_ssd(drive: char) -> bool {
    use std::mem::{size_of, zeroed};
    use windows::Win32::System::Ioctl::{
        IOCTL_STORAGE_QUERY_PROPERTY, PropertyStandardQuery, STORAGE_PROPERTY_QUERY,
        StorageDeviceSeekPenaltyProperty, DEVICE_SEEK_PENALTY_DESCRIPTOR,
    };
    let dev = format!("\\\\.\\{}:", drive);
    let wide: Vec<u16> = dev.encode_utf16().chain(std::iter::once(0)).collect();
    let h = unsafe {
        CreateFileW(
            PCWSTR(wide.as_ptr()),
            0, // IOCTL 查询不需要读写访问
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            FILE_FLAGS_AND_ATTRIBUTES(0),
            None,
        )
    }
    .unwrap_or(INVALID_HANDLE_VALUE);
    if h == INVALID_HANDLE_VALUE {
        return false;
    }
    let mut query: STORAGE_PROPERTY_QUERY = unsafe { zeroed() };
    query.PropertyId = StorageDeviceSeekPenaltyProperty;
    query.QueryType = PropertyStandardQuery;
    let mut desc: DEVICE_SEEK_PENALTY_DESCRIPTOR = unsafe { zeroed() };
    let mut returned: u32 = 0;
    let ok = unsafe {
        DeviceIoControl(
            h,
            IOCTL_STORAGE_QUERY_PROPERTY,
            Some(&query as *const STORAGE_PROPERTY_QUERY as *const _),
            size_of::<STORAGE_PROPERTY_QUERY>() as u32,
            Some(&mut desc as *mut DEVICE_SEEK_PENALTY_DESCRIPTOR as *mut _),
            size_of::<DEVICE_SEEK_PENALTY_DESCRIPTOR>() as u32,
            Some(&mut returned),
            None,
        )
        .is_ok()
    };
    unsafe {
        let _ = CloseHandle(h);
    }
    ok && returned >= size_of::<DEVICE_SEEK_PENALTY_DESCRIPTOR>() as u32
        && !desc.IncursSeekPenalty.as_bool()
}

// ============================================================
// 稀疏文件支持(P4 补缺,对应 v5 §4.5 / 执行文档 P4 验收单)
// ============================================================

/// 检测文件是否为稀疏文件(FILE_ATTRIBUTE_SPARSE,0x200)。
/// GetFileAttributesW 失败(INVALID_FILE_ATTRIBUTES)返回 false,按普通文件处理,不阻塞。
pub fn is_sparse(path: &Path) -> bool {
    let w = to_wide(path);
    let attrs = unsafe { GetFileAttributesW(PCWSTR(w.as_ptr())) };
    attrs != u32::MAX && (attrs & FILE_ATTRIBUTE_SPARSE_FILE.0) != 0
}

/// 把目标文件设为稀疏(FSCTL_SET_SPARSE,无输入输出缓冲)。
/// 失败返回错误码,调用方降级为普通复制(数据仍正确,仅占用不省)。
pub fn set_sparse(h: &FileHandle) -> Result<(), u32> {
    use windows::Win32::System::Ioctl::FSCTL_SET_SPARSE;
    let mut bytes_returned: u32 = 0;
    let ok = unsafe {
        DeviceIoControl(
            h.raw(),
            FSCTL_SET_SPARSE,
            None,
            0,
            None,
            0,
            Some(&mut bytes_returned),
            None,
        )
    };
    if ok.is_ok() {
        Ok(())
    } else {
        Err(unsafe { GetLastError() }.0)
    }
}

/// 查询文件实际分配(有数据)的字节区间(FSCTL_QUERY_ALLOCATED_RANGES)。
/// 语义:单次调用填满输出缓冲(最多 64 个区间),还有更多时返回
/// ERROR_MORE_DATA(234) 且已写部分区间——循环用返回的最后区间末尾推进
/// FileOffset 继续查,直到 bytes_returned==0。
/// 返回 (offset, length) 列表;失败返回错误码。
pub fn query_allocated_ranges(h: &FileHandle, file_size: u64) -> Result<Vec<(u64, u64)>, u32> {
    use std::mem::size_of;
    use windows::Win32::System::Ioctl::{
        FILE_ALLOCATED_RANGE_BUFFER, FSCTL_QUERY_ALLOCATED_RANGES,
    };
    const MAX_RANGES: usize = 64;
    let mut ranges = Vec::new();
    let mut offset: u64 = 0;
    loop {
        if offset >= file_size {
            break;
        }
        let query = FILE_ALLOCATED_RANGE_BUFFER {
            FileOffset: offset as i64,
            Length: (file_size - offset) as i64,
        };
        let mut out = [FILE_ALLOCATED_RANGE_BUFFER {
            FileOffset: 0,
            Length: 0,
        }; MAX_RANGES];
        let mut bytes_returned: u32 = 0;
        let ok = unsafe {
            DeviceIoControl(
                h.raw(),
                FSCTL_QUERY_ALLOCATED_RANGES,
                Some(&query as *const FILE_ALLOCATED_RANGE_BUFFER as *const _),
                size_of::<FILE_ALLOCATED_RANGE_BUFFER>() as u32,
                Some(out.as_mut_ptr() as *mut _),
                (size_of::<FILE_ALLOCATED_RANGE_BUFFER>() * MAX_RANGES) as u32,
                Some(&mut bytes_returned),
                None,
            )
        };
        if ok.is_err() {
            let err = unsafe { GetLastError() }.0;
            // ERROR_MORE_DATA(234):缓冲已满但还有区间,已写部分结果,继续推进
            if err != 234 || bytes_returned == 0 {
                return Err(err);
            }
        }
        let count = (bytes_returned as usize) / size_of::<FILE_ALLOCATED_RANGE_BUFFER>();
        if count == 0 {
            break; // 无更多已分配区间
        }
        for i in 0..count {
            let r = out[i];
            ranges.push((r.FileOffset as u64, r.Length as u64));
        }
        let last = out[count - 1];
        let next = (last.FileOffset as u64) + (last.Length as u64);
        if next <= offset {
            break; // 防御:区间不前进则终止,避免死循环
        }
        offset = next;
    }
    Ok(ranges)
}

/// 移动文件指针(64 位偏移)。稀疏复制定位到区间起点/文件末尾用。
pub fn set_file_pointer(h: &FileHandle, offset: u64) -> Result<(), u32> {
    let r = unsafe { SetFilePointerEx(h.raw(), offset as i64, None, FILE_BEGIN) };
    r.map_err(|e| win32_err(e))
}

/// 截断/扩展文件到当前指针位置。稀疏文件末尾空洞需 SetEndOfFile 扩展目标大小。
pub fn set_end_of_file(h: &FileHandle) -> Result<(), u32> {
    let r = unsafe { SetEndOfFile(h.raw()) };
    r.map_err(|e| win32_err(e))
}

/// 取文件实际占用大小(GetCompressedFileSizeW;稀疏/压缩文件返回实际分配字节数)。
/// 仅测试用:验证稀疏复制后目标占用未膨胀。失败返回 0。
pub fn sparse_alloc_size(path: &Path) -> u64 {
    let w = to_wide(path);
    let mut high: u32 = 0;
    let low = unsafe { GetCompressedFileSizeW(PCWSTR(w.as_ptr()), Some(&mut high)) };
    if low == u32::MAX {
        return 0; // 失败(文件不存在等)
    }
    ((high as u64) << 32) | low as u64
}

