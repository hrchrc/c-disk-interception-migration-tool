#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MFT 读取核心模块 — 通过直接解析 NTFS 主文件表实现极速扫描。

不依赖 Everything.exe / es.exe / pywin32，纯 Python + ctypes 实现。

技术原理：
  1. 读取卷引导扇区 → 获取 BPB（BIOS Parameter Block）→ 定位 $MFT 起始位置
  2. 读取 $MFT 自身的记录（记录 0）→ 解析其 $DATA 属性的运行列表
  3. 通过运行列表按记录号读取任意 MFT 记录
  4. 逐条解析 MFT 记录：$FILE_NAME（文件名+父目录引用）、$DATA（文件大小）、
     $INDEX_ROOT（目录标记）

为何不用 FSCTL_ENUM_USN_DATA：
  USN 记录（USN_RECORD_V2/V3）只含文件名、父目录引用、属性，**不含文件大小**。
  目录大小计算必须依赖 $DATA 属性中的 real size 字段，因此直接解析 MFT 是
  同时获取"文件名 + 父子关系 + 文件大小"的唯一单遍方案。

性能目标：C 盘全量枚举 < 5 秒（Everything 约 2-3 秒）。
"""

import ctypes
from ctypes import wintypes
import os
import sys
import struct
import time

# 尝试加载 Cython 加速模块（编译后生成 mft_fast.pyd）
# 若未编译则 fallback 到纯 Python 实现（apply_usa_fixup / _parse_record_attributes 等）
try:
    import mft_fast as _cext
    _HAS_CYTHON = True
    # 确认关键函数存在
    _HAS_CYTHON = all(hasattr(_cext, name) for name in (
        "apply_usa_fixup_inplace", "parse_record_attributes",
        "parse_file_name_attr", "parse_records_bulk"))
except Exception as e:
    _cext = None
    _HAS_CYTHON = False
    print("[mft_reader] Cython 加速模块未加载（%s），使用纯 Python 模式" % e)

# ======================================================================
# Windows API 常量
# ======================================================================
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

FILE_BEGIN = 0
FILE_CURRENT = 1
FILE_END = 2

# MFT 记录标志位
FILE_RECORD_IN_USE = 0x01
FILE_RECORD_IS_DIRECTORY = 0x02

# NTFS 属性类型
ATTR_STANDARD_INFORMATION = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_OBJECT_ID = 0x40
ATTR_SECURITY_DESCRIPTOR = 0x50
ATTR_VOLUME_NAME = 0x60
ATTR_VOLUME_INFORMATION = 0x70
ATTR_DATA = 0x80
ATTR_INDEX_ROOT = 0x90
ATTR_INDEX_ALLOCATION = 0xA0
ATTR_BITMAP = 0xB0
ATTR_REPARSE_POINT = 0xC0
ATTR_EA_INFORMATION = 0xD0
ATTR_EA = 0xE0
ATTR_LOGGED_UTILITY_STREAM = 0x100
ATTR_END = 0xFFFFFFFF

# 文件名命名空间
NAMESPACE_POSIX = 0
NAMESPACE_WIN32 = 1
NAMESPACE_DOS = 2
NAMESPACE_WIN32_AND_DOS = 3

# MFT 记录签名
FILE_SIGNATURE = b"FILE"
BAAD_SIGNATURE = b"BAAD"

# ======================================================================
# Windows API 绑定
# ======================================================================
kernel32 = ctypes.windll.kernel32

CreateFileW = kernel32.CreateFileW
ReadFile = kernel32.ReadFile
SetFilePointerEx = kernel32.SetFilePointerEx
DeviceIoControl = kernel32.DeviceIoControl
CloseHandle = kernel32.CloseHandle
GetLastError = kernel32.GetLastError

# 原型设置
CreateFileW.restype = wintypes.HANDLE
CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]

ReadFile.restype = wintypes.BOOL
ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]

SetFilePointerEx.restype = wintypes.BOOL
SetFilePointerEx.argtypes = [
    wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong),
    wintypes.DWORD,
]

DeviceIoControl.restype = wintypes.BOOL
DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]

CloseHandle.restype = wintypes.BOOL
CloseHandle.argtypes = [wintypes.HANDLE]


# FSCTL_ENUM_USN_DATA（用于辅助枚举/校验，本模块主路径走直接 MFT 解析）
FSCTL_ENUM_USN_DATA = 0x000900B3
FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_GET_NTFS_VOLUME_DATA = 0x00090064


class MFT_ENUM_DATA_V0(ctypes.Structure):
    _fields_ = [
        ("StartFileReferenceNumber", ctypes.c_ulonglong),
        ("LowUsn", ctypes.c_longlong),
        ("HighUsn", ctypes.c_longlong),
    ]


class USN_JOURNAL_DATA_V0(ctypes.Structure):
    _fields_ = [
        ("UsnJournalID", ctypes.c_ulonglong),
        ("FirstUsn", ctypes.c_longlong),
        ("NextUsn", ctypes.c_longlong),
        ("LowestValidUsn", ctypes.c_longlong),
        ("MaxUsn", ctypes.c_longlong),
        ("MaximumSize", ctypes.c_ulonglong),
        ("AllocationDelta", ctypes.c_ulonglong),
    ]


class NTFS_VOLUME_DATA_BUFFER(ctypes.Structure):
    """FSCTL_GET_NTFS_VOLUME_DATA 返回结构。

    注意：Microsoft 文档中前 5 个字段是 ULONG(4字节)，但实际 Windows
    返回的是 NTFS_VOLUME_DATA_BUFFER 结构，其中前 5 个字段是 LARGE_INTEGER
    (8字节)，后 9 个字段是 ULONG(4字节)。但实测发现字节布局表明前 5 个字段
    是 8 字节，BytesPerSector 开始是 4 字节字段。以下是正确的字段布局。
    """
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_ulonglong),    # 0x00
        ("NumberSectors", ctypes.c_ulonglong),          # 0x08
        ("TotalClusters", ctypes.c_ulonglong),          # 0x10
        ("FreeClusters", ctypes.c_ulonglong),           # 0x18
        ("TotalReserved", ctypes.c_ulonglong),          # 0x20
        ("BytesPerSector", ctypes.c_ulong),             # 0x28 (4字节)
        ("BytesPerCluster", ctypes.c_ulong),            # 0x2C (4字节)
        ("BytesPerFileRecordSegment", ctypes.c_ulong),  # 0x30 (4字节)
        ("ClustersPerFileRecordSegment", ctypes.c_ulong), # 0x34 (4字节)
        ("MftValidDataLength", ctypes.c_ulonglong),     # 0x38 (8字节)
        ("MftStartLcn", ctypes.c_ulonglong),            # 0x40 (8字节)
        ("Mft2StartLcn", ctypes.c_ulonglong),           # 0x48 (8字节)
        ("MftZoneStart", ctypes.c_ulonglong),           # 0x50 (8字节)
        ("MftZoneEnd", ctypes.c_ulonglong),             # 0x58 (8字节)
    ]


# ======================================================================
# 错误定义
# ======================================================================
class MftError(Exception):
    """MFT 读取基础异常"""


class MftPermissionError(MftError):
    """权限不足（需管理员/备份权限）"""


class MftNotNtfsError(MftError):
    """目标卷不是 NTFS 文件系统"""


# ======================================================================
# 工具函数
# ======================================================================
def is_admin():
    """检测当前进程是否拥有管理员权限"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _ref_number_to_mft_num(file_ref):
    """将 64 位文件引用号转为 MFT 记录号（低 48 位）"""
    return file_ref & 0x0000FFFFFFFFFFFF


def _parse_run_list(data, start_offset):
    """解析 NTFS 运行列表（run list）。

    返回: list of (start_vcn, length_clusters, start_lcn, is_sparse)
    """
    runs = []
    pos = start_offset
    prev_lcn = 0
    vcn = 0
    data_len = len(data)
    while pos < data_len:
        header = data[pos]
        if header == 0:
            break
        len_size = header & 0x0F
        off_size = (header >> 4) & 0x0F
        pos += 1
        if pos + len_size > data_len:
            break
        run_len = int.from_bytes(data[pos:pos + len_size], "little", signed=True)
        pos += len_size
        if off_size == 0:
            # 稀疏运行（孔洞）
            run_lcn = 0
            is_sparse = True
        else:
            if pos + off_size > data_len:
                break
            run_off = int.from_bytes(data[pos:pos + off_size], "little", signed=True)
            pos += off_size
            run_lcn = prev_lcn + run_off
            prev_lcn = run_lcn
            is_sparse = False
        if run_len > 0:
            runs.append((vcn, run_len, run_lcn, is_sparse))
            vcn += run_len
    return runs


# ======================================================================
# 核心读取器
# ======================================================================
class MftReader:
    """直接读取并解析 NTFS MFT 的核心类。

    用法:
        reader = MftReader("C:")
        reader.open()
        for rec in reader.enum_all_records():
            print(rec["name"], rec["size"], rec["is_dir"])
        reader.close()
    """

    def __init__(self, volume="C:"):
        self.volume = volume.rstrip("\\:")
        self.handle = None
        # BPB 参数
        self.bytes_per_sector = 0
        self.sectors_per_cluster = 0
        self.bytes_per_cluster = 0
        self.mft_start_lcn = 0
        self.bytes_per_record = 0
        self.mft_valid_data_length = 0  # $MFT 文件有效数据长度（字节）
        self.total_records = 0
        # $MFT 运行列表
        self.mft_runs = []  # list of (start_vcn, length_clusters, start_lcn, is_sparse)
        # 读取统计
        self._read_buffer = None

    # ------------------------------------------------------------------
    # 打开 / 关闭
    # ------------------------------------------------------------------
    def open(self):
        """打开卷，读取引导扇区，建立 $MFT 运行列表映射。"""
        vol_path = "\\\\.\\%s:" % self.volume
        self.handle = CreateFileW(
            vol_path, GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, 0, None,
        )
        if not self.handle or self.handle == INVALID_HANDLE_VALUE:
            err = GetLastError()
            if err == 5:
                raise MftPermissionError(
                    "权限不足，无法打开卷 %s:。请以管理员身份运行。" % self.volume
                )
            raise MftError("CreateFileW 失败，错误码 %d" % err)

        # 后续任一步抛异常都必须关闭句柄，否则反复清缓存会泄漏卷句柄至进程退出
        try:
            # 优先用 FSCTL_GET_NTFS_VOLUME_DATA 获取参数（最可靠）
            self._load_volume_data()
            # 读取引导扇区作为校验/备用
            self._read_boot_sector()

            # 读取 $MFT 记录 0，解析其 $DATA 运行列表
            self._load_mft_runlist()

            self.total_records = self.mft_valid_data_length // self.bytes_per_record
            self._read_buffer = (ctypes.c_char * self.bytes_per_record)()
        except Exception:
            # 异常路径先释放句柄，避免泄漏
            try:
                CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None
            raise
        return self

    def close(self):
        if self.handle:
            try:
                CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        # 兜底：对象被回收时若句柄未关闭则补关闭，防止异常路径漏 close 造成泄漏
        try:
            if getattr(self, "handle", None):
                CloseHandle(self.handle)
                self.handle = None
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 卷参数加载
    # ------------------------------------------------------------------
    def _load_volume_data(self):
        """用 FSCTL_GET_NTFS_VOLUME_DATA 获取 NTFS 卷参数。"""
        vdb = NTFS_VOLUME_DATA_BUFFER()
        bytes_returned = wintypes.DWORD(0)
        ok = DeviceIoControl(
            self.handle, FSCTL_GET_NTFS_VOLUME_DATA,
            None, 0,
            ctypes.byref(vdb), ctypes.sizeof(vdb),
            ctypes.byref(bytes_returned), None,
        )
        if not ok:
            err = GetLastError()
            raise MftError(
                "FSCTL_GET_NTFS_VOLUME_DATA 失败，错误码 %d（卷 %s: 可能不是 NTFS）"
                % (err, self.volume)
            )
        self.bytes_per_sector = vdb.BytesPerSector
        self.bytes_per_cluster = vdb.BytesPerCluster
        self.bytes_per_record = vdb.BytesPerFileRecordSegment
        self.mft_start_lcn = vdb.MftStartLcn
        self.mft_valid_data_length = vdb.MftValidDataLength
        # sectors_per_cluster 用于校验
        if self.bytes_per_sector and self.bytes_per_cluster:
            self.sectors_per_cluster = (
                self.bytes_per_cluster // self.bytes_per_sector
            )
        if not self.bytes_per_record:
            raise MftError("无法确定 MFT 记录大小")

    def _read_boot_sector(self):
        """读取引导扇区，校验 NTFS 签名（备用校验路径）。"""
        boot = (ctypes.c_char * 512)()
        self._read_at(0, boot, 512)
        # 偏移 0x03 处应为 "NTFS    "
        oem = bytes(boot[3:11])
        if not oem.startswith(b"NTFS"):
            raise MftNotNtfsError(
                "卷 %s: 不是 NTFS 文件系统（OEM=%r）" % (self.volume, oem)
            )
        # BPB 字段（仅当 _load_volume_data 未成功时使用，此处仅校验一致性）
        bps = struct.unpack_from("<H", boot, 0x0B)[0]
        spc = boot[0x0D]
        if bps:
            self.bytes_per_sector = self.bytes_per_sector or bps
        if spc:
            self.sectors_per_cluster = self.sectors_per_cluster or spc

    def _load_mft_runlist(self):
        """读取 $MFT 自身记录（记录 0），解析其 $DATA 属性运行列表。

        注意：此时 mft_runs 还未建立，不能用 _read_record_raw（它会调用
        _vcn_to_lcn 依赖 mft_runs）。直接用 mft_start_lcn（来自卷数据）
        定位记录 0 的物理位置。
        """
        # $MFT 记录 0 位于 mft_start_lcn * bytes_per_cluster 字节偏移处
        disk_offset = self.mft_start_lcn * self.bytes_per_cluster
        buf = (ctypes.c_char * self.bytes_per_record)()
        self._read_at(disk_offset, buf, self.bytes_per_record)
        record_data = bytearray(buf)
        self.apply_usa_fixup(record_data, self.bytes_per_sector)

        if record_data[:4] != FILE_SIGNATURE:
            raise MftError("$MFT 记录 0 签名错误: %r" % record_data[:4])

        attrs, _flags = self._parse_record_attributes(record_data)
        data_attr = attrs.get(ATTR_DATA)
        if data_attr is None:
            raise MftError("$MFT 记录中未找到 $DATA 属性")
        is_non_resident, content_offset, content_length, run_list_offset, \
            real_size = data_attr
        if not is_non_resident:
            # $MFT 的 $DATA 几乎总是非驻留；若驻留则整卷极小，直接用单段
            self.mft_runs = [(0, 1, self.mft_start_lcn, False)]
            return
        runs = _parse_run_list(record_data, run_list_offset)
        if not runs:
            # 运行列表解析失败，退化为单段（仅覆盖 MFT 起始部分）
            self.mft_runs = [(0, 1, self.mft_start_lcn, False)]
        else:
            self.mft_runs = runs

    # ------------------------------------------------------------------
    # 底层读取
    # ------------------------------------------------------------------
    def _read_at(self, offset, buffer, size):
        """从卷的指定字节偏移读取数据。"""
        li_offset = ctypes.c_longlong(offset)
        if not SetFilePointerEx(self.handle, li_offset, None, FILE_BEGIN):
            raise MftError("SetFilePointerEx 失败，错误码 %d" % GetLastError())
        bytes_read = wintypes.DWORD(0)
        ok = ReadFile(self.handle, buffer, size, ctypes.byref(bytes_read), None)
        if not ok or bytes_read.value != size:
            raise MftError(
                "ReadFile 失败: ok=%s, read=%d/%d, err=%d"
                % (ok, bytes_read.value, size, GetLastError())
            )

    def _read_record_raw(self, record_num):
        """读取单条 MFT 记录的原始字节（已应用 USA 修复）。

        通过 $MFT 运行列表将记录号映射到卷上的物理位置。
        返回 bytearray（原地 USA 修复）。
        """
        file_offset = record_num * self.bytes_per_record
        vcn = file_offset // self.bytes_per_cluster
        offset_in_cluster = file_offset % self.bytes_per_cluster
        # 在运行列表中找到包含此 VCN 的运行
        lcn = self._vcn_to_lcn(vcn)
        disk_offset = lcn * self.bytes_per_cluster + offset_in_cluster
        buf = (ctypes.c_char * self.bytes_per_record)()
        self._read_at(disk_offset, buf, self.bytes_per_record)
        # 转 bytearray 以支持原地 USA 修复
        record_data = bytearray(buf)
        return self.apply_usa_fixup(record_data, self.bytes_per_sector)

    def _vcn_to_lcn(self, vcn):
        """将 $MFT 文件内的 VCN 转为卷上的 LCN。"""
        for start_vcn, length, lcn, is_sparse in self.mft_runs:
            if start_vcn <= vcn < start_vcn + length:
                if is_sparse:
                    raise MftError("MFT 记录位于稀疏区域（VCN=%d）" % vcn)
                return lcn + (vcn - start_vcn)
        raise MftError("VCN %d 不在 $MFT 运行列表范围内" % vcn)

    def _read_records_bulk(self, start_record, count):
        """批量读取连续 MFT 记录（用于全量枚举优化）。

        返回 bytearray，长度为 count * bytes_per_record。
        当记录跨越运行边界时分段读取。返回 bytearray 而非 bytes
        是为了让调用方能原地修改（USA fixup）而无需再次拷贝。
        """
        bpc = self.bytes_per_cluster
        bpr = self.bytes_per_record
        result = bytearray()
        remaining = count
        current = start_record
        while remaining > 0:
            file_offset = current * bpr
            vcn = file_offset // bpc
            # 找到当前 VCN 所在运行
            run_info = None
            for start_vcn, length, lcn, is_sparse in self.mft_runs:
                if start_vcn <= vcn < start_vcn + length:
                    run_info = (start_vcn, length, lcn, is_sparse)
                    break
            if run_info is None:
                break
            sv, rl, sl, sparse = run_info
            if sparse:
                # 稀疏区域填零
                zero_count = min(remaining, rl * (bpc // bpr))
                result.extend(b"\x00" * (zero_count * bpr))
                current += zero_count
                remaining -= zero_count
                continue
            # 按字节计算当前运行可读取的记录数
            run_start_byte = sv * bpc
            offset_in_run = file_offset - run_start_byte
            bytes_can_read = rl * bpc - offset_in_run
            records_can_read = min(remaining, bytes_can_read // bpr)
            if records_can_read <= 0:
                # 当前运行剩余字节不足一条记录，跳到下一个运行
                next_vcn = sv + rl
                next_record = (next_vcn * bpc) // bpr
                skip = max(1, next_record - current)
                skip = min(skip, remaining)
                result.extend(b"\x00" * (skip * bpr))
                current += skip
                remaining -= skip
                continue
            read_bytes = records_can_read * bpr
            disk_offset = sl * bpc + offset_in_run
            buf = (ctypes.c_char * read_bytes)()
            self._read_at(disk_offset, buf, read_bytes)
            result.extend(buf)
            current += records_can_read
            remaining -= records_can_read
        return result  # 返回 bytearray，不转 bytes

    # ------------------------------------------------------------------
    # MFT 记录解析
    # ------------------------------------------------------------------
    @staticmethod
    def apply_usa_fixup(record_data, bytes_per_sector=512):
        """应用 Update Sequence Array (USA) 修复（原地修改 bytearray）。

        NTFS 每扇区最后 2 字节在写盘时被 USA 校验值替换，读取时需还原。
        本方法就地修改 record_data (必须是 bytearray)，不创建新对象。
        """
        # 优先用 Cython 实现
        if _HAS_CYTHON and isinstance(record_data, bytearray):
            return _cext.apply_usa_fixup_inplace(record_data, bytes_per_sector)
        # 纯 Python 实现
        if record_data[:4] != FILE_SIGNATURE:
            return record_data
        if len(record_data) < 8:
            return record_data
        usa_offset = struct.unpack_from("<H", record_data, 0x04)[0]
        usa_count = struct.unpack_from("<H", record_data, 0x06)[0]
        if usa_count < 2 or usa_offset + usa_count * 2 > len(record_data):
            return record_data

        # 读取 USA 数组（一次性读取整个数组，减少函数调用开销）
        usa = struct.unpack_from("<%dH" % usa_count, record_data, usa_offset)
        check_value = usa[0]

        # 原地还原每扇区末尾 2 字节（直接切片赋值，不用 struct.pack_into）
        for i in range(1, usa_count):
            sector_end = i * bytes_per_sector - 2
            if sector_end + 2 > len(record_data):
                break
            # 验证：当前末尾应等于 check_value
            current = record_data[sector_end] | (record_data[sector_end + 1] << 8)
            if current != check_value:
                continue
            # 原地还原（切片赋值，比 struct.pack_into 快）
            record_data[sector_end:sector_end + 2] = usa[i].to_bytes(2, "little")
        return record_data

    @classmethod
    def _parse_record_attributes(cls, record_data):
        """解析 MFT 记录中的所有属性（含目录标志）。

        返回: (attrs_dict, flags)
            attrs_dict: {attr_type: (is_non_resident, content_offset,
                            content_length, run_list_offset, real_size)}
            flags: 记录标志（FILE_RECORD_IN_USE | FILE_RECORD_IS_DIRECTORY）
        """
        # 优先用 Cython 实现
        if _HAS_CYTHON and isinstance(record_data, bytearray):
            return _cext.parse_record_attributes(record_data)
        # 纯 Python 实现
        attrs = {}
        if record_data[:4] != FILE_SIGNATURE:
            return attrs, 0
        first_attr_offset = struct.unpack_from("<H", record_data, 0x14)[0]
        flags = struct.unpack_from("<H", record_data, 0x16)[0]
        pos = first_attr_offset
        data_len = len(record_data)
        while pos + 16 <= data_len:
            attr_type = struct.unpack_from("<I", record_data, pos)[0]
            if attr_type == ATTR_END:
                break
            attr_len = struct.unpack_from("<I", record_data, pos + 4)[0]
            if attr_len == 0 or attr_len > data_len:
                break
            if pos + attr_len > data_len:
                break
            non_resident = record_data[pos + 8]
            if non_resident == 0:
                content_length = struct.unpack_from("<I", record_data, pos + 0x10)[0]
                content_offset = struct.unpack_from("<H", record_data, pos + 0x14)[0]
                attrs[attr_type] = (False, pos + content_offset, content_length,
                                    None, content_length)
            else:
                run_list_offset = struct.unpack_from("<H", record_data, pos + 0x20)[0]
                real_size = struct.unpack_from("<Q", record_data, pos + 0x30)[0]
                attrs[attr_type] = (True, None, None, pos + run_list_offset, real_size)
            pos += attr_len
        return attrs, flags

    @staticmethod
    def _parse_file_name_attr(record_data, content_offset, content_length):
        """解析 $FILE_NAME 属性内容，返回 (parent_ref, name, namespace)。

        $FILE_NAME 内容布局:
          offset 0x00: parent directory reference (8 bytes)
          offset 0x38: flags (4 bytes)
          offset 0x40: name length in chars (1 byte)
          offset 0x41: namespace (1 byte)
          offset 0x42: name (UTF-16, name_length * 2 bytes)
        """
        # 优先用 Cython 实现
        if _HAS_CYTHON and isinstance(record_data, bytearray):
            return _cext.parse_file_name_attr(record_data, content_offset, content_length)
        # 纯 Python 实现
        if content_offset + 0x42 > len(record_data):
            return None
        parent_ref = struct.unpack_from("<Q", record_data, content_offset)[0]
        name_len_chars = record_data[content_offset + 0x40]
        namespace = record_data[content_offset + 0x41]
        name_bytes = name_len_chars * 2
        name_start = content_offset + 0x42
        if name_start + name_bytes > len(record_data):
            return None
        try:
            name = record_data[name_start:name_start + name_bytes].decode(
                "utf-16-le", errors="replace")
        except Exception:
            name = ""
        return (parent_ref, name, namespace)

    def parse_record(self, record_data):
        """解析单条 MFT 记录，返回结构化信息。

        返回 dict 或 None（记录未使用/损坏）:
            {
                "record_num": int,      # MFT 记录号
                "name": str,            # 文件名（Win32 命名空间优先）
                "parent_ref": int,      # 父目录 MFT 记录号
                "is_dir": bool,         # 是否为目录
                "is_in_use": bool,      # 是否在使用中
                "size": int,            # 文件大小（字节）；目录为 0
            }
        """
        if record_data[:4] != FILE_SIGNATURE:
            return None
        attrs, flags = self._parse_record_attributes(record_data)
        is_in_use = bool(flags & FILE_RECORD_IN_USE)
        is_dir = bool(flags & FILE_RECORD_IS_DIRECTORY)
        if not is_in_use:
            return None

        # 解析 $FILE_NAME 属性（可能有多个，优先选 Win32 命名空间）
        best_name = None
        best_parent = 0
        best_ns = NAMESPACE_DOS
        for attr_type, info in attrs.items():
            if attr_type != ATTR_FILE_NAME:
                continue
            is_nr, c_off, c_len, rl_off, r_size = info
            if is_nr:
                continue
            parsed = self._parse_file_name_attr(record_data, c_off, c_len)
            if parsed is None:
                continue
            parent_ref, name, ns = parsed
            # 优先级: Win32(1) > POSIX(0) > Win32&DOS(3) > DOS(2)
            if best_name is None:
                best_name, best_parent, best_ns = name, parent_ref, ns
            elif ns == NAMESPACE_WIN32 and best_ns != NAMESPACE_WIN32:
                best_name, best_parent, best_ns = name, parent_ref, ns
            elif ns == NAMESPACE_POSIX and best_ns in (NAMESPACE_DOS,):
                best_name, best_parent, best_ns = name, parent_ref, ns

        if best_name is None:
            return None

        # 解析 $DATA 属性获取文件大小（仅文件，目录的 $DATA 通常为空）
        size = 0
        if not is_dir and ATTR_DATA in attrs:
            is_nr, c_off, c_len, rl_off, r_size = attrs[ATTR_DATA]
            size = r_size if is_nr else c_len

        # 检测 reparse point（符号链接/junction）：记录中存在 $REPARSE_POINT 属性
        is_reparse = ATTR_REPARSE_POINT in attrs

        return {
            "name": best_name,
            "parent_ref": _ref_number_to_mft_num(best_parent),
            "is_dir": is_dir,
            "is_in_use": is_in_use,
            "size": size,
            "is_reparse": is_reparse,
        }

    # ------------------------------------------------------------------
    # 全量枚举
    # ------------------------------------------------------------------
    def enum_all_records(self, progress_cb=None, batch_size=4096):
        """枚举卷上所有 MFT 记录（生成器）。

        逐条 yield parse_record 的结果（跳过未使用/损坏的记录）。
        progress_cb(current, total) 可选，用于报告进度。

        性能（Cython 模式）：批量读取 + C 层批量解析，C 盘约 1-2 秒。
        性能（纯 Python）：批量读取 + 逐条解析，C 盘约 3-5 秒。
        """
        bpr = self.bytes_per_record
        bps = self.bytes_per_sector
        total = self.total_records
        record_num = 0

        # Cython 加速路径：批量读取 + C 层批量解析
        if _HAS_CYTHON:
            while record_num < total:
                count = min(batch_size, total - record_num)
                try:
                    bulk = self._read_records_bulk(record_num, count)
                except MftError:
                    # 批量读取失败，逐条降级（仍用 Cython 的单条解析）
                    for i in range(count):
                        rn = record_num + i
                        try:
                            raw = self._read_record_raw(rn)
                        except Exception:
                            continue
                        # raw 是 bytearray，可直接传给 Cython 函数
                        _cext.apply_usa_fixup_inplace(raw, bps)
                        rec = self.parse_record(raw)
                        if rec is not None:
                            rec["record_num"] = rn
                            yield rec
                        if progress_cb and (rn % 10000 == 0):
                            progress_cb(rn + 1, total)
                    record_num += count
                    continue
                # C 层一次性解析整批记录（USA fixup + 属性遍历 + 字段提取）
                rec_list = _cext.parse_records_bulk(bulk, bpr, bps, record_num)
                for rec in rec_list:
                    yield rec
                record_num += count
                if progress_cb:
                    progress_cb(min(record_num, total), total)
            return

        # 纯 Python 路径（原实现）
        while record_num < total:
            count = min(batch_size, total - record_num)
            try:
                bulk = self._read_records_bulk(record_num, count)
            except MftError:
                # 批量读取失败（可能跨稀疏区域），逐条降级
                for i in range(count):
                    rn = record_num + i
                    try:
                        raw = self._read_record_raw(rn)
                    except Exception:
                        continue
                    rec = self.parse_record(raw)
                    if rec is not None:
                        rec["record_num"] = rn
                        yield rec
                    if progress_cb and (rn % 10000 == 0):
                        progress_cb(rn + 1, total)
                record_num += count
                continue
            # 解析批量数据（bulk 是 bytearray，切片得到 bytearray，可原地修改）
            for i in range(count):
                rn = record_num + i
                raw = bulk[i * bpr:(i + 1) * bpr]
                if raw[:4] != FILE_SIGNATURE:
                    continue
                # 原地 USA 修复（不创建新对象，避免 222 万次内存分配）
                self.apply_usa_fixup(raw, self.bytes_per_sector)
                rec = self.parse_record(raw)
                if rec is not None:
                    rec["record_num"] = rn
                    yield rec
            record_num += count
            if progress_cb:
                progress_cb(min(record_num, total), total)

    def enum_all_files(self, progress_cb=None):
        """枚举所有文件记录，返回列表。

        返回: list of {record_num, name, parent_ref, is_dir, is_in_use, size}
        """
        results = []
        for rec in self.enum_all_records(progress_cb=progress_cb):
            results.append(rec)
        return results


# ======================================================================
# 自测入口
# ======================================================================
if __name__ == "__main__":
    if not is_admin():
        print("[错误] 需要 administrator 权限读取 MFT。请以管理员身份运行。")
        sys.exit(1)

    volume = sys.argv[1] if len(sys.argv) > 1 else "C"
    print("[*] 打开卷 %s: ..." % volume)
    t0 = time.time()
    try:
        reader = MftReader(volume)
        reader.open()
    except MftPermissionError as e:
        print("[错误] %s" % e)
        sys.exit(1)
    except MftNotNtfsError as e:
        print("[错误] %s" % e)
        sys.exit(1)

    print("[*] MFT 记录大小: %d 字节" % reader.bytes_per_record)
    print("[*] 每簇字节数:   %d" % reader.bytes_per_cluster)
    print("[*] MFT 有效长度: %d 字节 (%d 条记录)"
          % (reader.mft_valid_data_length, reader.total_records))
    print("[*] $MFT 运行段数: %d" % len(reader.mft_runs))

    print("[*] 开始全量枚举...")
    t1 = time.time()
    file_count = 0
    dir_count = 0
    total_size = 0

    def on_progress(current, total):
        if current % 100000 == 0:
            pct = current * 100 // total if total else 0
            print("    进度: %d / %d (%d%%)" % (current, total, pct))

    for rec in reader.enum_all_records(progress_cb=on_progress):
        if rec["is_dir"]:
            dir_count += 1
        else:
            file_count += 1
            total_size += rec["size"]

    t2 = time.time()
    print("[*] 枚举完成")
    print("    文件数: %d" % file_count)
    print("    目录数: %d" % dir_count)
    print("    总大小: %.2f GB" % (total_size / 1024 / 1024 / 1024))
    print("    打开耗时:   %.2f 秒" % (t1 - t0))
    print("    枚举耗时:   %.2f 秒" % (t2 - t1))
    print("    总耗时:     %.2f 秒" % (t2 - t0))
    reader.close()
