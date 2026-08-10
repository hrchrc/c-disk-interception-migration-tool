# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
"""MFT 记录解析 Cython 加速模块。

只加速最热的 3 个函数（每个被调用 200 万次以上）：
  1. apply_usa_fixup_inplace  — USA 校验还原（原地修改）
  2. parse_record_attributes  — 属性链表遍历
  3. parse_file_name_attr     — $FILE_NAME 属性解析

外加批量解析入口 parse_records_bulk，把"USA fixup + 属性解析 + 文件名提取
+ 大小提取 + reparse 检测"合并成一次 C 循环，避免 Python 层 200 万次函数
调用开销。返回 Python dict 列表，兼容原 parse_record 的输出格式。

编译：
    python setup.py build_ext --inplace
"""

from libc.stdint cimport uint32_t, uint16_t, uint64_t, int64_t
from libc.string cimport memcmp
cimport cython

# MFT 记录签名 "FILE"
cdef bytes FILE_SIGNATURE = b"FILE"
cdef unsigned char[4] _FILE_SIG
_FILE_SIG[0] = ord('F')
_FILE_SIG[1] = ord('I')
_FILE_SIG[2] = ord('L')
_FILE_SIG[3] = ord('E')

# NTFS 属性类型常量
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_REPARSE_POINT = 0xC0
ATTR_END = 0xFFFFFFFF

# 文件名命名空间
NAMESPACE_POSIX = 0
NAMESPACE_WIN32 = 1
NAMESPACE_DOS = 2
NAMESPACE_WIN32_AND_DOS = 3

# MFT 记录标志位
FILE_RECORD_IN_USE = 0x01
FILE_RECORD_IS_DIRECTORY = 0x02


# ======================================================================
# 读取小端整数的内联函数（避免 struct.unpack 开销）
# ======================================================================
cdef inline uint16_t _read_u16(const unsigned char* p) nogil:
    return (<uint16_t>p[0]) | (<uint16_t>p[1] << 8)

cdef inline uint32_t _read_u32(const unsigned char* p) nogil:
    return (<uint32_t>p[0]) | (<uint32_t>p[1] << 8) | \
           (<uint32_t>p[2] << 16) | (<uint32_t>p[3] << 24)

cdef inline uint64_t _read_u64(const unsigned char* p) nogil:
    return (<uint64_t>p[0]) | (<uint64_t>p[1] << 8) | \
           (<uint64_t>p[2] << 16) | (<uint64_t>p[3] << 24) | \
           (<uint64_t>p[4] << 32) | (<uint64_t>p[5] << 40) | \
           (<uint64_t>p[6] << 48) | (<uint64_t>p[7] << 56)


# ======================================================================
# 1. USA Fixup（原地修改 bytearray）
# ======================================================================
def apply_usa_fixup_inplace(bytearray record_data, int bytes_per_sector=512):
    """应用 Update Sequence Array (USA) 修复（原地修改 bytearray）。

    NTFS 每扇区最后 2 字节在写盘时被 USA 校验值替换，读取时需还原。
    返回 record_data 本身（已原地修改）。
    """
    cdef int n = len(record_data)
    if n < 8:
        return record_data
    cdef unsigned char* buf = record_data
    # 检查签名 "FILE"
    if buf[0] != _FILE_SIG[0] or buf[1] != _FILE_SIG[1] or \
       buf[2] != _FILE_SIG[2] or buf[3] != _FILE_SIG[3]:
        return record_data

    cdef uint16_t usa_offset = _read_u16(buf + 0x04)
    cdef uint16_t usa_count = _read_u16(buf + 0x06)
    if usa_count < 2:
        return record_data
    if usa_offset + usa_count * 2 > n:
        return record_data

    cdef uint16_t check_value = _read_u16(buf + usa_offset)
    cdef int i
    cdef int sector_end
    cdef uint16_t current, replace_value

    for i in range(1, usa_count):
        sector_end = i * bytes_per_sector - 2
        if sector_end + 2 > n:
            break
        current = _read_u16(buf + sector_end)
        if current != check_value:
            continue
        replace_value = _read_u16(buf + usa_offset + i * 2)
        buf[sector_end] = <unsigned char>(replace_value & 0xFF)
        buf[sector_end + 1] = <unsigned char>(replace_value >> 8)
    return record_data


# ======================================================================
# 2. 属性链表解析（返回 Python dict）
# ======================================================================
def parse_record_attributes(bytearray record_data):
    """解析 MFT 记录中的所有属性。

    返回 (attrs_dict, flags)
        attrs_dict: {attr_type: (is_non_resident, content_offset,
                                 content_length, run_list_offset, real_size)}
                     其中 content_offset/run_list_offset 已加上 pos 基址
        flags: 记录标志
    """
    cdef int n = len(record_data)
    cdef unsigned char* buf = record_data
    # M9 修复:头部读需要偏移 0x16-0x17(buf+0x14/0x16 各读 2 字节),
    # 记录头至少 0x18 字节;n<0x18 直接返回(原 n<4 会静默越界读,
    # boundscheck=False + 裸指针下无 IndexError,实测读脏数据)
    if n < 0x18:
        return {}, 0
    if buf[0] != _FILE_SIG[0] or buf[1] != _FILE_SIG[1] or \
       buf[2] != _FILE_SIG[2] or buf[3] != _FILE_SIG[3]:
        return {}, 0

    cdef uint16_t first_attr_offset = _read_u16(buf + 0x14)
    cdef uint16_t flags = _read_u16(buf + 0x16)
    cdef int pos = first_attr_offset
    cdef uint32_t attr_type, attr_len
    cdef unsigned char non_resident
    cdef uint16_t content_offset, run_list_offset
    cdef uint32_t content_length
    cdef uint64_t real_size

    attrs = {}
    # M10 修复:循环条件由 pos+16<=n 收紧为 pos+0x18<=n(读 pos+0x14
    # 需 pos+0x16<=n),并给 attr_len 加下界校验(驻留头 0x18/非驻留 0x40)
    while pos + 0x18 <= n:
        attr_type = _read_u32(buf + pos)
        if attr_type == ATTR_END:
            break
        attr_len = _read_u32(buf + pos + 4)
        if attr_len < 0x18 or attr_len > n:
            break
        if pos + attr_len > n:
            break
        non_resident = buf[pos + 8]
        if non_resident == 0:
            content_length = _read_u32(buf + pos + 0x10)
            content_offset = _read_u16(buf + pos + 0x14)
            attrs[attr_type] = (False, pos + content_offset, content_length,
                                None, content_length)
        else:
            # 非驻留属性头至少 0x40 字节(读 pos+0x30 的 8 字节需 pos+0x38<=n)
            if attr_len < 0x40:
                break
            run_list_offset = _read_u16(buf + pos + 0x20)
            real_size = _read_u64(buf + pos + 0x30)
            attrs[attr_type] = (True, None, None, pos + run_list_offset, real_size)
        pos += attr_len
    return attrs, flags


# ======================================================================
# 3. $FILE_NAME 属性解析
# ======================================================================
def parse_file_name_attr(bytearray record_data, int content_offset, int content_length=0):
    """解析 $FILE_NAME 属性内容，返回 (parent_ref, name, namespace) 或 None。

    $FILE_NAME 内容布局：
        offset 0x00: parent directory reference (8 bytes)
        offset 0x40: name length in chars (1 byte)
        offset 0x41: namespace (1 byte)
        offset 0x42: name (UTF-16, name_length * 2 bytes)
    """
    cdef int n = len(record_data)
    if content_offset + 0x42 > n:
        return None
    cdef unsigned char* buf = record_data
    cdef uint64_t parent_ref = _read_u64(buf + content_offset)
    cdef unsigned char name_len_chars = buf[content_offset + 0x40]
    cdef unsigned char namespace = buf[content_offset + 0x41]
    cdef int name_bytes = name_len_chars * 2
    cdef int name_start = content_offset + 0x42
    if name_start + name_bytes > n:
        return None
    # 从指针切片创建 bytes（Cython 的 char* 切片 [a:b] 返回 bytes，不会在 \x00 截断）
    cdef bytes name_bytes_raw = buf[name_start:name_start + name_bytes]
    name = name_bytes_raw.decode("utf-16-le", errors="replace")
    return (parent_ref, name, namespace)


# ======================================================================
# 4. 批量解析入口（核心加速函数）
# ======================================================================
def parse_records_bulk(bytearray bulk, int bytes_per_record, int bytes_per_sector,
                       int start_record_num=0):
    """批量解析 MFT 记录，一次性完成 USA fixup + 属性解析 + 字段提取。

    参数：
        bulk: 包含多条 MFT 记录的 bytearray（来自 _read_records_bulk）
        bytes_per_record: 单条记录字节数（通常 1024）
        bytes_per_sector: 扇区字节数（通常 512，用于 USA fixup）
        start_record_num: 起始记录号

    返回：list of dict，每个 dict 含：
        record_num, name, parent_ref, is_dir, is_in_use, size, is_reparse
    （跳过未使用/损坏的记录，格式与 MftReader.parse_record 兼容）
    """
    cdef int total_bytes = len(bulk)
    cdef int count = total_bytes // bytes_per_record
    cdef unsigned char* buf = bulk

    cdef int i, j, pos, n
    cdef int record_num
    cdef unsigned char* rec
    cdef uint16_t first_attr_offset, flags, usa_offset, usa_count, check_value
    cdef uint16_t current, replace_value, content_offset, run_list_offset
    cdef uint32_t attr_type, attr_len, content_length
    cdef uint64_t real_size, parent_ref
    cdef unsigned char non_resident, name_len_chars, namespace
    cdef int name_bytes, name_start
    cdef bint is_in_use, is_dir, is_reparse, has_file_name, has_data
    cdef bint is_non_resident_data
    cdef int data_content_offset, data_content_length, data_run_list_offset
    cdef uint64_t data_real_size
    cdef int best_ns, cur_ns
    cdef bytes best_name_raw, cur_name_raw
    cdef uint64_t best_parent, cur_parent

    results = []

    for i in range(count):
        rec = buf + i * bytes_per_record
        n = bytes_per_record
        record_num = start_record_num + i

        # 签名检查
        if rec[0] != _FILE_SIG[0] or rec[1] != _FILE_SIG[1] or \
           rec[2] != _FILE_SIG[2] or rec[3] != _FILE_SIG[3]:
            continue

        # ---------- USA Fixup（原地） ----------
        usa_offset = _read_u16(rec + 0x04)
        usa_count = _read_u16(rec + 0x06)
        if usa_count >= 2 and usa_offset + usa_count * 2 <= n:
            check_value = _read_u16(rec + usa_offset)
            for j in range(1, usa_count):
                pos = j * bytes_per_sector - 2
                if pos + 2 > n:
                    break
                current = _read_u16(rec + pos)
                if current != check_value:
                    continue
                replace_value = _read_u16(rec + usa_offset + j * 2)
                rec[pos] = <unsigned char>(replace_value & 0xFF)
                rec[pos + 1] = <unsigned char>(replace_value >> 8)

        # ---------- 记录标志 ----------
        first_attr_offset = _read_u16(rec + 0x14)
        flags = _read_u16(rec + 0x16)
        is_in_use = (flags & FILE_RECORD_IN_USE) != 0
        is_dir = (flags & FILE_RECORD_IS_DIRECTORY) != 0
        if not is_in_use:
            continue

        # ---------- 遍历属性链表 ----------
        pos = first_attr_offset
        has_file_name = False
        has_data = False
        is_reparse = False
        # 用于选最佳文件名
        best_name_raw = None
        best_parent = 0
        best_ns = NAMESPACE_DOS
        # $DATA 属性信息
        is_non_resident_data = False
        data_content_offset = 0
        data_content_length = 0
        data_real_size = 0

        while pos + 0x18 <= n:
            attr_type = _read_u32(rec + pos)
            if attr_type == ATTR_END:
                break
            attr_len = _read_u32(rec + pos + 4)
            # M10 修复:attr_len 下界校验(驻留头 0x18/非驻留 0x40),
            # 损坏记录 attr_len 过小时原代码越界读(静默脏数据)
            if attr_len < 0x18 or attr_len > n:
                break
            if pos + attr_len > n:
                break

            if attr_type == ATTR_FILE_NAME:
                non_resident = rec[pos + 8]
                if non_resident == 0:
                    content_offset = _read_u16(rec + pos + 0x14)
                    # 解析 $FILE_NAME 内容
                    if pos + content_offset + 0x42 <= n:
                        parent_ref = _read_u64(rec + pos + content_offset)
                        name_len_chars = rec[pos + content_offset + 0x40]
                        namespace = rec[pos + content_offset + 0x41]
                        name_bytes = name_len_chars * 2
                        name_start = pos + content_offset + 0x42
                        if name_start + name_bytes <= n:
                            cur_name_raw = rec[name_start:name_start + name_bytes]
                            cur_parent = parent_ref
                            cur_ns = namespace
                            # 选择最佳命名空间：Win32(1) > POSIX(0) > Win32&DOS(3) > DOS(2)
                            if best_name_raw is None:
                                best_name_raw = cur_name_raw
                                best_parent = cur_parent
                                best_ns = cur_ns
                            elif cur_ns == NAMESPACE_WIN32 and best_ns != NAMESPACE_WIN32:
                                best_name_raw = cur_name_raw
                                best_parent = cur_parent
                                best_ns = cur_ns
                            elif cur_ns == NAMESPACE_POSIX and best_ns == NAMESPACE_DOS:
                                best_name_raw = cur_name_raw
                                best_parent = cur_parent
                                best_ns = cur_ns
                            has_file_name = True
            elif attr_type == ATTR_DATA and not is_dir:
                non_resident = rec[pos + 8]
                if non_resident == 0:
                    data_content_length = _read_u32(rec + pos + 0x10)
                    data_content_offset = _read_u16(rec + pos + 0x14)
                    is_non_resident_data = False
                else:
                    # 非驻留属性头至少 0x40 字节(读 pos+0x30 需 pos+0x38<=n)
                    if attr_len < 0x40:
                        break
                    data_real_size = _read_u64(rec + pos + 0x30)
                    is_non_resident_data = True
                has_data = True
            elif attr_type == ATTR_REPARSE_POINT:
                is_reparse = True

            pos += attr_len

        if not has_file_name:
            continue

        # ---------- 组装结果 ----------
        size = 0
        if has_data and not is_dir:
            if is_non_resident_data:
                size = data_real_size
            else:
                size = data_content_length

        name = best_name_raw.decode("utf-16-le", errors="replace")

        results.append({
            "record_num": record_num,
            "name": name,
            "parent_ref": best_parent & 0x0000FFFFFFFFFFFF,  # 低 48 位
            "is_dir": bool(is_dir),
            "is_in_use": True,
            "size": size,
            "is_reparse": bool(is_reparse),
        })

    return results
