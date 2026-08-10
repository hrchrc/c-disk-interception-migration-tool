#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高性能扫描封装 — 基于 MFT 的极速目录扫描。

对接主项目接口：
  - get_dir_size_mft(path)        替代 get_dir_size_fast（返回 MB）
  - list_subdirs_fast(base_path)  列出一级子目录及大小
  - scan_six_dirs(progress_cb)    扫描六个监控目录（兼容 scan_appdata 子集）
  - search_files(pattern, path)   Everything 式文件名搜索

设计要点：
  1. 启动时一次性加载全量 MFT 到内存，构建 record_num→info 和 parent→children 索引
  2. 目录大小计算 = 在内存索引上做拓扑求和，零磁盘 I/O，任意大小目录 < 0.1 秒
  3. 路径→记录号解析：从根目录（record 5）逐级匹配目录名
  4. 符号链接/junction 跳过：用 MFT 标志位（零磁盘 I/O），walk 模式用 lstat
  5. MFT 读取失败时自动回退 os.walk（fallback）

内存占用（8.8 优化，替代原 dict 方案）：
  原实现 3 个 dict（records/children/dir_size_cache）250 万条 Python 对象
  占用 700-800MB（用户实测常态 800MB）。现改为原生数组紧凑存储，
  每条记录固定开销：name 偏移 4B + 记录号 4B + size 8B + flags 1B
  + 子项对 8B + 名字 UTF-8 平均 ~12B ≈ 37B/条 → 250 万条约 90-110MB。
"""

import os
import sys
import time
import ctypes
import gc
import json
import logging
import mmap
import struct
import subprocess
import tempfile
import threading
import weakref
import atexit
from array import array
from bisect import bisect_left
from pathlib import Path

from mft_reader import (
    MftReader, MftError, MftPermissionError, MftNotNtfsError,
    is_admin, _ref_number_to_mft_num,
)

# 真实 MftReader 引用：测试注入 mock（如 test_mft_compact.py 的 FakeReader）
# 替换 fast_scan.MftReader 时，Rust 分支必须跳过（避免启动真实引擎读卷
# 破坏 mock 语义）。
_REAL_MFT_READER = MftReader

# 与主项目共用日志通道（main.py 配置的 'CDriveRelocator' handler）
log = logging.getLogger('CDriveRelocator')

# ---- mmap 临时索引文件清理（v2）----
# 进程正常退出不执行 __del__，atexit 兜底：先关所有存活 scanner 的 mmap
# 映射（解除文件锁定）再删临时索引文件，杜绝 %TEMP% 残留。
_ACTIVE_SCANNERS = weakref.WeakSet()
_PENDING_TMP = set()


def _cleanup_mft_tmp_at_exit():
    for s in list(_ACTIVE_SCANNERS):
        try:
            s._close_rust_mm()
        except Exception:
            pass
    for p in list(_PENDING_TMP):
        try:
            os.remove(p)
        except OSError:
            pass


atexit.register(_cleanup_mft_tmp_at_exit)

# NTFS 根目录的 MFT 记录号固定为 5
ROOT_RECORD_NUM = 5

# reparse point 标记位（用于检测符号链接/junction）
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# 记录号上限保护：MFT 记录号超 32 位时无法用 array('I') 紧凑存储，
# 直接降级 os.walk（Windows 实际卷上记录号远小于 2^32）
_MAX_REC_NUM = 0xFFFFFFFF


def _is_reparse_point(path):
    """检测路径是否为重解析点（符号链接/junction）。"""
    try:
        if os.path.islink(path):
            return True
        st = os.lstat(path)
        if hasattr(st, "st_reparse_tag") and st.st_reparse_tag != 0:
            return True
        return False
    except Exception:
        return False


def _get_dir_size_walk(path):
    """os.walk 兜底实现（与主项目 get_dir_size_fast 一致）。"""
    return round(_get_dir_size_walk_bytes(path) / 1024 / 1024, 6)


def _get_dir_size_walk_bytes(path):
    """os.walk 兜底实现，返回字节数。用于 MFT 预计算为 0 时的二次校验。"""
    try:
        total = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except Exception:
                    pass
        return total
    except Exception:
        return 0


class MftScanner:
    """基于 MFT 的高性能扫描器。

    用法:
        scanner = MftScanner("C")       # 加载 MFT（约 3-5 秒）
        scanner.load()                   # 构建内存索引
        size = scanner.get_dir_size_mft(r"C:\\Program Files\\Adobe")
        subdirs = scanner.list_subdirs_fast(r"C:\\Program Files")
        results = scanner.scan_six_dirs()

    若 MFT 加载失败（非管理员/非 NTFS），自动降级为 os.walk 模式。

    内部紧凑存储（array 原生数组，下标即记录 index 0..N-1）：
      _rec_names      bytes          所有文件名 UTF-8 拼接（字节池）
      _name_lens      array('H')     [N] 每条 name 的 UTF-8 字节长度
      _name_anchors   array('I')     [N/16+1] 每 16 条一个起始偏移（含哨兵）
      _rec_nums       array('I')     每条对应的 MFT 记录号（稀疏）
      _rec_sizes      array('Q')     文件大小（字节）
      _rec_flags      array('B')     bit0=is_dir bit1=is_reparse
      _dir_entries_p  array('I')     目录记录号（升序，子项分组入口）
      _dir_entries_start array('I')  目录对应子项在 _child_data 的起点
      _child_data     array('I')     子项 index 顺序存储（按父分组）
      _dir_size_idx   array('I')     目录 index（升序）
      _dir_size_val   array('Q')     对应目录总大小（字节）
      _root_index     int            根目录（记录号 5）的 index
    """

    def __init__(self, volume="C"):
        self.volume = volume
        self._reader = None
        self._loaded = False
        self._fallback = False  # 是否降级为 os.walk

        # ---- 紧凑内存索引（array 原生数组）----
        self._rec_names = b""
        # 名字索引：2B 长度数组 + 每 16 条一个 4B 起始偏移锚点
        # （替代 4B/条 的完整偏移数组，省 ~40% 索引内存）
        self._name_lens = array('H')       # [N] 每条名字 UTF-8 字节长度
        self._name_anchors = array('I')    # [N/16+1] 每 16 条的起始偏移
        self._rec_nums = array('I')
        self._rec_sizes = array('Q')
        self._rec_flags = array('B')
        # 子项关系：目录入口数组 + 子项顺序数据（按 parent 记录号升序）
        self._dir_entries_p = array('I')       # 目录记录号
        self._dir_entries_start = array('I')   # 对应子项在 _child_data 的起点
        self._child_data = array('I')          # 子项 index 顺序存储
        self._dir_size_idx = array('I')
        self._dir_size_val = array('Q')
        self._root_index = -1
        # 路径→index 缓存（避免重复解析）
        self._path_cache = {}

        # ---- Rust 优先加载（MFT-Rust重构方案 8.8）----
        # _rust_attempted：本次会话 Rust 失败后不再重试（避免每次 load 启动失败进程）
        # _rust_loaded：索引由 Rust 路径加载（Rust 模式无 _reader 句柄）
        self._rust_attempted = False
        self._rust_loaded = False
        # ---- v2 内存优化（8.8）----
        # _rec_sizes 在 Rust 路径为 array('I')（u32 主表）+ 溢出表；
        # Cython 路径保持 array('Q') 不变（_size_ovf_idx 为 None 区分）
        self._size_ovf_idx = None
        self._size_ovf_val = array('Q')
        # names 字节池的 mmap 映射（Rust 路径持有，关闭扫描器时释放）
        self._mm = None
        # mmap 持有的 CRT 文件描述符（mm close 后释放表项）
        self._mm_fd = -1
        # mmap 打开期间删除失败的临时索引文件（mm 关闭后补删）
        self._pending_tmp = None
        _ACTIVE_SCANNERS.add(self)

        # 统计
        self.file_count = 0
        self.dir_count = 0
        self.total_size = 0  # 用户文件总大小（排除系统元数据）
        self.load_time = 0.0

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    def load(self, progress_cb=None):
        """加载 MFT 并构建内存索引。失败时降级为 os.walk 模式。"""
        if not is_admin():
            print("[MftScanner] 非管理员权限，降级为 os.walk 模式")
            self._fallback = True
            self._loaded = True
            return self

        t0 = time.time()
        # Rust 优先分支（方案 8.8）：失败自动回退现有 Cython 路径
        if self._try_load_via_rust(progress_cb):
            self.load_time = time.time() - t0
            self._loaded = True
            print("[MftScanner] Rust 索引构建完成: %d 文件, %d 目录, 总大小 %d 字节, 耗时 %.2f 秒"
                  % (self.file_count, self.dir_count, self.total_size, self.load_time))
            return self

        try:
            self._reader = MftReader(self.volume)
            self._reader.open()
        except (MftPermissionError, MftNotNtfsError, MftError) as e:
            print("[MftScanner] MFT 打开失败: %s，降级为 os.walk 模式" % e)
            self._reader = None
            self._fallback = True
            self._loaded = True
            return self

        # 枚举全部记录，直接构建紧凑数组（无中间 dict）
        print("[MftScanner] 加载 MFT 索引...")
        total = self._reader.total_records
        try:
            t_enum = time.time()
            enum_count = 0
            names_chunks = []
            # 子项收集：packed (parent_rec_num << 32) | child_index
            # 构建完成后排序转 array('I') 再释放（临时 ~90MB，峰值可控）
            child_packed = []
            for rec in self._reader.enum_all_records():
                rn = rec["record_num"]
                parent = rec["parent_ref"] & 0xFFFFFFFF
                if rn > _MAX_REC_NUM or parent > _MAX_REC_NUM:
                    # 记录号超 32 位：紧凑存储不可用，降级 os.walk
                    raise MftError("MFT 记录号超出紧凑存储上限 (%d)" % rn)
                name_b = rec["name"].encode("utf-8", errors="replace")
                if len(name_b) > 0xFFFF:
                    # 名字超 64KB（理论上不可能，防御）：截断为合法前缀
                    name_b = name_b[:0xFFFF]
                names_chunks.append(name_b)
                self._name_lens.append(len(name_b))
                self._rec_nums.append(rn)
                self._rec_sizes.append(rec["size"])
                is_dir = 1 if rec["is_dir"] else 0
                is_reparse = 1 if rec.get("is_reparse", False) else 0
                self._rec_flags.append(is_dir | (is_reparse << 1))
                child_packed.append((parent << 32) | enum_count)
                if rn == ROOT_RECORD_NUM:
                    self._root_index = enum_count
                if rec["is_dir"]:
                    self.dir_count += 1
                else:
                    self.file_count += 1
                    # 排除系统元数据文件（记录号 < 24，如 $MFT/$BadClus/$LogFile）
                    if rn >= 24:
                        self.total_size += rec["size"]
                enum_count += 1
                # 每 1 万条报告一次进度（枚举阶段映射到 0-85%）
                if progress_cb and enum_count % 10000 == 0:
                    cur85 = int(enum_count * 0.85)
                    progress_cb(cur85, total, "构建内存索引 %d/%d" % (enum_count, total))
            # 名字字节池 + 锚点索引（每 16 条一个起始偏移）
            self._rec_names = b"".join(names_chunks)
            names_chunks = None
            self._build_name_anchors()
            # 子项关系：按 parent 分组存储——目录入口数组 + 子项顺序数据
            # (parent 字段只存 44 万次目录而非 242 万次,省 ~5MB)
            child_packed.sort()
            child_data = array('I')
            dir_entries_p = array('I')       # 目录记录号（升序）
            dir_entries_start = array('I')   # 对应子项在 child_data 的起点
            i = 0
            n_packed = len(child_packed)
            while i < n_packed:
                p = child_packed[i] >> 32
                start = len(child_data)
                while i < n_packed and (child_packed[i] >> 32) == p:
                    child_data.append(child_packed[i] & 0xFFFFFFFF)
                    i += 1
                dir_entries_p.append(p)
                dir_entries_start.append(start)
            self._child_data = child_data
            self._dir_entries_p = dir_entries_p
            self._dir_entries_start = dir_entries_start
            child_packed = None
            gc.collect()
            t_enum_done = time.time()
            print("[MftScanner] 枚举+索引完成: %d 条记录, 耗时 %.2f 秒"
                  % (enum_count, t_enum_done - t_enum))
            if progress_cb:
                progress_cb(int(total * 0.85), total, "枚举完成，开始预计算目录大小")
        except Exception as e:
            print("[MftScanner] MFT 枚举失败: %s，降级为 os.walk 模式" % e)
            # 枚举阶段 reader 已 open，必须先关闭句柄再丢弃引用，避免卷句柄泄漏
            if self._reader is not None:
                try:
                    self._reader.close()
                except Exception:
                    pass
            self._reader = None
            self._reset_index()
            self._fallback = True
            self._loaded = True
            return self

        # 预计算所有目录大小（一次 O(N) 拓扑，之后查询 O(log N) 二分）
        # 预计算阶段映射到 85-100%
        self._precompute_dir_sizes(progress_cb, total)

        self.load_time = time.time() - t0
        self._loaded = True
        print("[MftScanner] 索引构建完成: %d 文件, %d 目录, 总大小 %d 字节, 耗时 %.2f 秒, 内存索引已就绪"
              % (self.file_count, self.dir_count, self.total_size, self.load_time))
        return self

    # ------------------------------------------------------------------
    # Rust 优先加载路径（MFT-Rust重构方案 8.8 第五/六节）
    # ------------------------------------------------------------------
    @staticmethod
    def _locate_engine():
        """定位 rust-migrate-engine.exe（复用 migrate_engine._locate_engine 同款逻辑）。"""
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            exe_dir = os.path.dirname(sys.executable)
            candidates = []
            if meipass:
                candidates.append(os.path.join(meipass, "bin", "rust-migrate-engine.exe"))
            candidates.append(os.path.join(exe_dir, "bin", "rust-migrate-engine.exe"))
            candidates.append(os.path.join(exe_dir, "rust-migrate-engine.exe"))
            for cand in candidates:
                if os.path.isfile(cand):
                    return cand
            return candidates[0]
        # 源码模式: src/core/fast_scan.py → 上两级 → 项目根 → bin/rust-migrate-engine.exe
        here = Path(__file__).resolve().parent  # src/core
        return str(here.parent.parent / "bin" / "rust-migrate-engine.exe")

    def _log_fallback(self, reason):
        """记录降级原因（[rust-engine] MFT 索引回退: <原因>）。"""
        log.warning("[rust-engine] MFT 索引回退: %s", reason)
        print("[MftScanner] Rust 索引路径不可用（%s），降级为现有 Cython 路径" % reason)

    @staticmethod
    def _cleanup_rust_tmp(tmp):
        """删除 Rust 索引临时文件（正常/异常路径都删，防残留）。

        返回是否删除成功（mmap 打开期间删除失败时调用方延迟到 mm 关闭后）。
        """
        if tmp:
            try:
                os.remove(tmp)
                return True
            except OSError:
                return False
        return True

    def _try_load_via_rust(self, progress_cb=None):
        """尝试用 Rust mft-index 路径构建索引。

        方案 8.8 第六节降级原则：任何一步失败 → 完整回退现有 Cython 路径，
        日志记录原因（[rust-engine] MFT 索引回退: <原因>），绝不部分加载。
        _rust_attempted 门控：本次会话失败后不再重试。
        """
        if self._rust_attempted:
            return False
        self._rust_attempted = True
        # mock 注入（测试场景）：MftReader 被替换时跳过 Rust 分支
        if MftReader is not _REAL_MFT_READER:
            return False
        exe = self._locate_engine()
        if not os.path.isfile(exe):
            return False  # exe 缺失:静默回退(现有行为)

        # 临时索引文件：加载完即删（finally 兜底异常路径）
        tmp = None
        proc = None
        timeout_flag = threading.Event()
        timeout_timer = None
        stderr_lines = []
        try:
            fd, tmp = tempfile.mkstemp(prefix="cdrive_mft_", suffix=".idx")
            os.close(fd)
            # CREATE_NO_WINDOW：引擎是 console 程序，不加会在桌面弹出黑框
            proc = subprocess.Popen(
                [exe, "--mft-index", "--volume", self.volume, "--out", tmp],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=1, text=True, encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as e:
            self._log_fallback("启动引擎失败: %s (%s)" % (exe, e))
            self._cleanup_rust_tmp(tmp)
            return False

        # N1 超时 watchdog：120s 后 force_kill（复用 migrate_engine 模式）
        def _on_timeout():
            timeout_flag.set()
            log.warning("[rust-engine] MFT 索引超时(120 秒),强制终止进程")
            if proc:
                try:
                    proc.kill()
                except OSError:
                    pass

        timeout_timer = threading.Timer(120, _on_timeout)
        timeout_timer.daemon = True
        timeout_timer.start()

        # stderr 排空线程（防管道阻塞）+ 收集诊断
        def _drain_stderr():
            if proc and proc.stderr:
                try:
                    for line in proc.stderr:
                        stderr_lines.append(line)
                except Exception:
                    pass

        t = threading.Thread(target=_drain_stderr, daemon=True)
        t.start()

        job_done = False
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("event") == "job_done":
                    job_done = True
                elif evt.get("event") == "info" and evt.get("key") == "mft_progress":
                    # 进度映射：留 5% 给 Python 加载阶段
                    value = evt.get("value", "")
                    if progress_cb and "/" in value:
                        try:
                            cur_i, total_i = map(int, value.split("/", 1))
                        except ValueError:
                            continue
                        if total_i > 0:
                            progress_cb(int(cur_i * 0.95), total_i,
                                        "构建内存索引 %s" % value)
        except Exception:
            pass  # stdout 读异常（进程被强杀）走退出码判定
        finally:
            if timeout_timer:
                timeout_timer.cancel()
            try:
                proc.wait()
            except Exception:
                pass
            t.join(timeout=5)

        rc = proc.returncode if proc.returncode is not None else -1
        stderr_text = "".join(stderr_lines)[:2000]

        if timeout_flag.is_set():
            self._log_fallback("引擎超时(120 秒),已强制终止")
            self._cleanup_rust_tmp(tmp)
            return False
        if rc != 0 or not job_done or not os.path.isfile(tmp):
            # 退出码非 0 / 未收到 job_done / 索引文件缺失 → 回退
            self._log_fallback("引擎异常退出 code=%s%s"
                               % (rc, " stderr=%s" % stderr_text if stderr_text else ""))
            self._cleanup_rust_tmp(tmp)
            return False

        # 加载二进制索引（magic/version/长度三重校验，失败 raise → 回退）
        ok = False
        try:
            self._load_rust_index(tmp)
            ok = True
        except Exception as e:
            self._log_fallback("索引加载失败: %s" % e)
            self._reset_index()
            return False
        finally:
            if not ok:
                # 失败：mm 已在 _load_rust_index 内部释放，可直接删除
                self._cleanup_rust_tmp(tmp)
            else:
                # 成功：mm 保持打开（查询需要 names 池视图）。共享删除语义下
                # 文件已可立即标记删除；失败则延迟到 _close_rust_mm 后补删，
                # 进程退出 atexit 最后兜底，杜绝残留
                if not self._cleanup_rust_tmp(tmp):
                    self._pending_tmp = tmp
                    _PENDING_TMP.add(tmp)
        self._rust_loaded = True
        return True

    @staticmethod
    def _open_mmap_share_delete(path):
        """以 FILE_SHARE_DELETE 共享模式打开并映射索引文件。

        mmap.mmap 内部持有文件句柄直到 close，且普通 open() 的 CRT 句柄
        不允许删除共享——映射期间 os.remove 必然失败（临时索引残留 %TEMP%）。
        这里用 CreateFileW 显式带 FILE_SHARE_DELETE 打开（Windows 共享删除
        语义）：映射期间 os.remove 立即成功（标记删除，数据在 mm close 后
        才真正释放），实现"即用即删"。

        返回 (mm, fd)；mm close 后调用方 os.close(fd) 释放 CRT fd 表项
        （句柄所有权已转移给 mm，mm close 时 CloseHandle）。
        """
        import msvcrt
        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        OPEN_EXISTING = 3
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        handle = k32.CreateFileW(
            path, GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None, OPEN_EXISTING, 0, None,
        )
        if not handle or handle == INVALID_HANDLE_VALUE:
            raise MftError("打开索引文件失败: %s" % path)
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        mm = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
        return mm, fd

    def _load_rust_index(self, path):
        """从 Rust 二进制索引文件（v2）加载紧凑数组。

        v2 内存优化：
          - sizes 主表 u32（4B/条）+ 溢出表（>4GB 大文件），省 ~10MB
          - names 字节池用 mmap 视图（零拷贝，按需分页）
          - array 段用普通文件读（走系统缓存，不触碰 mm 映射页，
            避免"映射页 + 拷贝"双份物理内存）
        每个数组长度校验 == 声明值（防截断/损坏），任一不符 raise → 回退。
        """
        # ---- 第一步：文件读构造所有 array（系统缓存路径）----
        with open(path, "rb") as f:
            head = f.read(4 + 4 + 8 * 5)
            if len(head) != 4 + 4 + 8 * 5:
                raise MftError("索引截断 (头部)")
            if head[0:4] != b"MFTI":
                raise MftError("索引格式不兼容 (magic=%r)" % head[0:4])
            version = struct.unpack_from("<I", head, 4)[0]
            if version != 2:
                raise MftError("索引版本不兼容 (version=%d)" % version)
            count, ndirs, nsized, novf, names_len = struct.unpack_from("<QQQQQ", head, 8)
            # 防御：计数超上限视为损坏（2500 万条远超真实卷）
            if count > 25_000_000 or ndirs > count or nsized > count or novf > count:
                raise MftError("索引计数异常 (count=%d ndirs=%d nsized=%d novf=%d)"
                               % (count, ndirs, nsized, novf))
            # 防御：names_len 上限 = 每条名字最多 64KB（Rust 侧同款截断），
            # 超限视为损坏，避免损坏文件声明巨量字节池引发巨量分配
            if names_len > count * 0x10000 + 1:
                raise MftError("索引 names_len 异常 (%d, count=%d)"
                               % (names_len, count))
            # 跳过 names 段（由 mmap 视图承载，不读入内存）
            f.seek(4 + 4 + 8 * 5 + names_len)

            def _load_array(typecode, n, elem_size, what):
                raw = f.read(n * elem_size)
                if len(raw) != n * elem_size:
                    raise MftError("索引截断 (%s %d/%d)" % (what, len(raw), n * elem_size))
                a = array(typecode)
                a.frombytes(raw)
                if len(a) != n:
                    raise MftError("索引损坏 (%s 元素 %d/%d)" % (what, len(a), n))
                return a

            self._name_lens = _load_array('H', count, 2, "name_lens")
            n_anchors = (count + 15) // 16 + 1  # 每 16 条一个锚点 + 哨兵
            self._name_anchors = _load_array('I', n_anchors, 4, "name_anchors")
            self._rec_nums = _load_array('I', count, 4, "rec_nums")
            self._rec_sizes = _load_array('I', count, 4, "sizes")  # v2: u32 主表
            self._rec_flags = _load_array('B', count, 1, "flags")
            self._size_ovf_idx = _load_array('I', novf, 4, "size_ovf_idx")
            self._size_ovf_val = _load_array('Q', novf, 8, "size_ovf_val")
            self._dir_entries_p = _load_array('I', ndirs, 4, "dir_entries_p")
            self._dir_entries_start = _load_array('I', ndirs, 4, "dir_entries_start")
            self._child_data = _load_array('I', count, 4, "child_data")
            self._dir_size_idx = _load_array('I', nsized, 4, "dir_size_idx")
            self._dir_size_val = _load_array('Q', nsized, 8, "dir_size_val")
            tail = f.read(28)
            if len(tail) != 28:
                raise MftError("索引截断 (统计)")
            root_raw = struct.unpack_from("<I", tail, 0)[0]
            file_count, dir_count, total_size = struct.unpack_from("<QQQ", tail, 4)

        # ---- 第二步：mmap 映射 names 段（视图零拷贝，按需分页）----
        mm, mm_fd = self._open_mmap_share_delete(path)
        try:
            names_start = 4 + 4 + 8 * 5
            mv = memoryview(mm)
            self._rec_names = mv[names_start:names_start + names_len]
            if len(self._rec_names) != names_len:
                raise MftError("索引截断 (names %d/%d)"
                               % (len(self._rec_names), names_len))
        except Exception:
            # 失败路径释放 mmap，避免句柄/映射泄漏
            try:
                mm.close()
            except Exception:
                pass
            try:
                os.close(mm_fd)
            except OSError:
                pass
            raise

        self._mm = mm
        self._mm_fd = mm_fd
        # root 未找到时 Rust 写 u32::MAX，转 -1（与 fast_scan 语义一致）
        self._root_index = -1 if root_raw == 0xFFFFFFFF else root_raw
        self.file_count = file_count
        self.dir_count = dir_count
        self.total_size = total_size

    def _close_rust_mm(self):
        """释放 names 池的 mmap 映射（加载失败/重置/关闭时调用）。"""
        mm = getattr(self, "_mm", None)
        if mm is not None:
            # 关键：_rec_names 是 memoryview，持有 mmap 的 buffer 引用——不先
            # 释放它，mm.close() 后映射视图仍存活，文件映射不销毁、删除失败。
            # 只在 Rust 模式清（Cython 路径 _mm 为 None，索引数据保留）
            self._rec_names = b""
            try:
                mm.close()
            except Exception:
                pass
            self._mm = None
        fd = getattr(self, "_mm_fd", -1)
        if fd >= 0:
            try:
                os.close(fd)  # 句柄已由 mm close 关闭，仅释放 CRT fd 表项
            except OSError:
                pass
            self._mm_fd = -1
        # 补删 mmap 打开期间未能删除的临时索引文件（映射关闭后解除锁定）
        pending = getattr(self, "_pending_tmp", None)
        if pending:
            self._pending_tmp = None
            if self._cleanup_rust_tmp(pending):
                _PENDING_TMP.discard(pending)
            else:
                _PENDING_TMP.add(pending)  # atexit 最后兜底

    def _size_of(self, idx):
        """按记录 index 取文件大小。

        v2：Rust 路径 _rec_sizes 为 u32 主表，>4GB 溢出文件查溢出表（二分，
        溢出记录极少）；Cython 路径 _size_ovf_idx 为 None，直读 array('Q')。
        """
        ovf = self._size_ovf_idx
        if ovf is None:
            return self._rec_sizes[idx]
        di = bisect_left(ovf, idx)
        if di < len(ovf) and ovf[di] == idx:
            return self._size_ovf_val[di]
        return self._rec_sizes[idx]

    def _build_name_anchors(self):
        """构建名字锚点索引：每 16 条记录一个起始偏移（4B），
        块内用 2B 长度数组求和定位。替代完整 4B 偏移数组，省 ~40% 索引内存。
        """
        anchors = array('I')
        total = 0
        n = len(self._name_lens)
        step = 16
        for i in range(0, n, step):
            anchors.append(total)
            total += sum(self._name_lens[i:i + step])
        anchors.append(total)  # 哨兵：名字总长
        self._name_anchors = anchors

    def _reset_index(self):
        """清空紧凑索引（降级 os.walk 时释放内存）。"""
        self._rec_names = b""
        self._name_lens = array('H')
        self._name_anchors = array('I')
        self._rec_nums = array('I')
        self._rec_sizes = array('Q')
        self._rec_flags = array('B')
        # 子项关系：目录入口数组 + 子项顺序数据（按 parent 记录号升序）
        self._dir_entries_p = array('I')       # 目录记录号
        self._dir_entries_start = array('I')   # 对应子项在 _child_data 的起点
        self._child_data = array('I')          # 子项 index 顺序存储
        self._dir_size_idx = array('I')
        self._dir_size_val = array('Q')
        self._root_index = -1
        self._path_cache.clear()
        self.file_count = 0
        self.dir_count = 0
        self.total_size = 0
        self._rust_loaded = False
        self._size_ovf_idx = None
        self._size_ovf_val = array('Q')
        self._close_rust_mm()
        gc.collect()

    # ------------------------------------------------------------------
    # 紧凑索引访问原语
    # ------------------------------------------------------------------
    def _count(self):
        return len(self._rec_nums)

    def _name_of(self, idx):
        """按记录 index 取文件名（2B 长度 + 锚点定位，块内 16 条求和 C 级）。

        Rust 路径 _rec_names 是 memoryview（mmap 视图），bytes() 包装统一
        转换（每次仅拷贝一条名字 ~12B，与原 bytes 切片开销相同）。
        """
        block = idx >> 4
        base = self._name_anchors[block]
        # 块内前缀和：array 切片 sum 为 C 级循环（最多 16 元素）
        if idx & 0xF:
            base += sum(self._name_lens[(block << 4):idx])
        end = base + self._name_lens[idx]
        return bytes(self._rec_names[base:end]).decode("utf-8", errors="replace")

    def _is_dir(self, idx):
        return bool(self._rec_flags[idx] & 0x01)

    def _is_reparse(self, idx):
        return bool(self._rec_flags[idx] & 0x02)

    def _iter_children(self, parent_rec_num):
        """生成某父目录（记录号）的所有子项 index（目录入口二分 + 区间切片）。

        注意:目录入口按 parent_rec_num 排序,与枚举顺序无关,
        因此不依赖 _rec_nums 有序(MFT 枚举顺序中记录号可能逆序)。
        """
        entries = self._dir_entries_p
        n = len(entries)
        # 二分下界
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if entries[mid] < parent_rec_num:
                lo = mid + 1
            else:
                hi = mid
        if lo >= n or entries[lo] != parent_rec_num:
            return
        start = self._dir_entries_start[lo]
        # 区间终点 = 下一个目录入口的起点（最后一个是数据总长）
        if lo + 1 < n:
            end = self._dir_entries_start[lo + 1]
        else:
            end = len(self._child_data)
        for j in range(start, end):
            yield self._child_data[j]

    def _dir_size_bytes(self, idx):
        """目录总大小（字节）。排序数组二分 O(log M)。"""
        di = bisect_left(self._dir_size_idx, idx)
        if di < len(self._dir_size_idx) and self._dir_size_idx[di] == idx:
            return self._dir_size_val[di]
        return 0

    # ------------------------------------------------------------------
    # 目录大小预计算（拓扑排序，结果与原 dict 版一致）
    # ------------------------------------------------------------------
    def _precompute_dir_sizes(self, progress_cb=None, enum_total=0):
        """O(N log N) 拓扑排序预计算所有目录大小。

        从叶子目录开始逐层向上；reparse 目录不展开（避免重复计算）。
        计算期间用临时 dict 存已算完的目录大小（~60MB，算完释放），
        最终转排序数组（~6MB/50 万目录）后二分查询。
        """
        from collections import deque

        t0 = time.time()
        # 目录映射: {dir_rec_num: (parent_rec_num, dir_index)}
        # 同时携带目录 index,避免"记录号→index"反查(MFT 枚举顺序中
        # 记录号不递增,无法用 bisect)
        dir_parent = {}
        entries_p = self._dir_entries_p
        entries_start = self._dir_entries_start
        child_data = self._child_data
        n_entries = len(entries_p)
        for e in range(n_entries):
            start = entries_start[e]
            end = entries_start[e + 1] if e + 1 < n_entries else len(child_data)
            for j in range(start, end):
                ci = child_data[j]
                if self._is_dir(ci):
                    dir_parent[self._rec_nums[ci]] = (entries_p[e], ci)

        # 1. 统计每个目录的"待处理普通子目录数"
        pending = {}
        for p in dir_parent:
            cnt = 0
            for ci in self._iter_children(p):
                if self._is_dir(ci) and not self._is_reparse(ci):
                    cnt += 1
            pending[p] = cnt

        total_dirs = len(pending)
        # 2. 从叶子目录开始（pending=0）
        queue = deque(p for p, c in pending.items() if c == 0)

        tmp = {}  # {dir_index: size} 预计算期临时表（转数组后释放）
        processed_count = 0
        progress_interval = 5000
        next_report = progress_interval
        base85 = int(enum_total * 0.85) if enum_total else 0
        span15 = max(1, int(enum_total * 0.15)) if enum_total else 1

        while queue:
            p = queue.popleft()
            # 计算本目录大小：累加所有普通子目录的缓存值 + 直接子文件大小
            total = 0
            for ci in self._iter_children(p):
                if self._is_dir(ci):
                    if self._is_reparse(ci):
                        continue
                    total += tmp.get(ci, 0)
                else:
                    total += self._rec_sizes[ci]
            pidx = dir_parent[p][1]  # 目录 index（构建时已记录）
            tmp[pidx] = total
            processed_count += 1

            if progress_cb and processed_count >= next_report:
                next_report += progress_interval
                ratio = min(1.0, processed_count / total_dirs) if total_dirs else 1.0
                cur = base85 + int(span15 * ratio)
                progress_cb(cur, enum_total, "预计算目录大小 %d/%d" % (processed_count, total_dirs))

            # 通知父目录：减少一个待处理子目录
            pp = dir_parent.get(p)
            if pp is not None and pp[0] in pending:
                pending[pp[0]] -= 1
                if pending[pp[0]] == 0:
                    queue.append(pp[0])

        # 转排序数组（二分查询），释放临时表
        items = sorted(tmp.items())
        self._dir_size_idx = array('I', (k for k, _ in items))
        self._dir_size_val = array('Q', (v for _, v in items))
        items = None
        tmp = None
        dir_parent = None
        pending = None
        gc.collect()

        skipped = total_dirs - len(self._dir_size_idx)
        print("[MftScanner] 目录大小预计算完成: %d/%d 个目录, 耗时 %.2f 秒%s"
              % (len(self._dir_size_idx), total_dirs, time.time() - t0,
                 "（跳过 %d 个疑似环目录）" % skipped if skipped else ""))

    # ------------------------------------------------------------------
    # 路径 → 记录 index 解析
    # ------------------------------------------------------------------
    def _resolve_path(self, path):
        """将文件系统路径解析为记录 index。

        从根目录（record 5）逐级匹配目录名（大小写不敏感）。
        返回 index 或 None。
        """
        if path in self._path_cache:
            return self._path_cache[path]

        # 规范化路径：去掉盘符和前缀
        clean = path.replace("/", "\\").strip("\\")
        if len(clean) >= 2 and clean[1] == ":":
            clean = clean[2:].lstrip("\\")

        if not clean:
            self._path_cache[path] = self._root_index
            return self._root_index

        segments = clean.split("\\")
        cur_rec_num = ROOT_RECORD_NUM
        cur_idx = self._root_index

        for seg in segments:
            if not seg:
                continue
            seg_lower = seg.lower()
            found = None
            for child_idx in self._iter_children(cur_rec_num):
                if self._name_of(child_idx).lower() == seg_lower:
                    found = child_idx
                    break
            if found is None:
                self._path_cache[path] = None
                return None
            cur_idx = found
            cur_rec_num = self._rec_nums[found]

        self._path_cache[path] = cur_idx
        return cur_idx

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------
    def get_dir_size_mft(self, path):
        """计算目录大小（MB）。MFT 模式下任意目录 < 0.1 秒。

        兼容主项目 get_dir_size_fast(path) 的签名和返回值（float MB）。
        """
        if self._fallback:
            return _get_dir_size_walk(path)

        idx = self._resolve_path(path)
        if idx is None:
            # 路径在 MFT 中找不到（可能已删除或为非 NTFS 路径），回退
            return _get_dir_size_walk(path)

        size_bytes = self._dir_size_bytes(idx)
        # 兜底：MFT 预计算为 0 但目录确实存在子文件时（WRP 系统文件等），
        # 回退到 os.walk 重新计算
        if size_bytes == 0 and os.path.isdir(path):
            size_bytes = _get_dir_size_walk_bytes(path)
        # 保留 6 位小数（最小到 1 字节），避免小目录被 round 成 0.0 显示为"0B"
        return round(size_bytes / 1024 / 1024, 6)

    def list_subdirs_fast(self, base_path):
        """列出指定目录下的一级子目录（含大小）。

        返回: list of {"path": str, "name": str, "size_mb": float}
        性能目标：单目录查询 < 0.1 秒（二分查预计算缓存，零磁盘 I/O）
        """
        if self._fallback:
            return self._list_subdirs_walk(base_path)

        idx = self._resolve_path(base_path)
        if idx is None:
            return self._list_subdirs_walk(base_path)

        results = []
        for child_idx in self._iter_children(self._rec_nums[idx]):
            if not self._is_dir(child_idx):
                continue
            # 跳过符号链接/junction（用 MFT 标志位，零磁盘 I/O）
            if self._is_reparse(child_idx):
                continue
            name = self._name_of(child_idx)
            full_path = os.path.join(base_path, name)
            size_bytes = self._dir_size_bytes(child_idx)
            # 兜底：MFT 预计算为 0 但目录确实存在子文件时（WRP 系统文件等）
            if size_bytes == 0 and os.path.isdir(full_path):
                size_bytes = _get_dir_size_walk_bytes(full_path)
            size_mb = round(size_bytes / 1024 / 1024, 6)
            results.append({
                "path": full_path,
                "name": name,
                "size_mb": size_mb,
            })
        results.sort(key=lambda x: x["size_mb"], reverse=True)
        return results

    def scan_six_dirs(self, progress_cb=None):
        """扫描六个监控目录的所有一级子目录。

        兼容主项目 scan_appdata 的返回格式（子集）：
            {"path", "name", "location", "size_mb"}

        性能目标：六个目录全部一级子目录 < 2 秒
        """
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        appdata = os.environ.get("APPDATA", "")
        scan_dirs = [
            (local_appdata, "Local"),
            (os.path.join(local_appdata, "Programs"), "Programs"),
            (appdata, "Roaming"),
            (r"C:\Program Files", "Program Files"),
            (r"C:\Program Files (x86)", "Program Files (x86)"),
            (r"C:\ProgramData", "ProgramData"),
        ]

        # 先收集所有候选（六个目录的一级子目录）
        all_candidates = []
        for base_path, label in scan_dirs:
            if not base_path or not os.path.exists(base_path):
                continue
            if self._fallback:
                # os.walk 模式：逐目录计算大小
                try:
                    for entry in os.listdir(base_path):
                        full_path = os.path.join(base_path, entry)
                        if not os.path.isdir(full_path):
                            continue
                        if _is_reparse_point(full_path):
                            continue
                        all_candidates.append((full_path, entry, label))
                except Exception:
                    pass
            else:
                # MFT 模式：通过索引获取子目录（零磁盘 I/O）
                idx = self._resolve_path(base_path)
                if idx is None:
                    continue
                for child_idx in self._iter_children(self._rec_nums[idx]):
                    if not self._is_dir(child_idx):
                        continue
                    # 跳过符号链接/junction（用 MFT 标志位）
                    if self._is_reparse(child_idx):
                        continue
                    name = self._name_of(child_idx)
                    full_path = os.path.join(base_path, name)
                    all_candidates.append((full_path, name, label))

        # 计算每个候选目录的大小
        total = len(all_candidates)
        results = []
        for i, (full_path, name, label) in enumerate(all_candidates):
            if self._fallback:
                size = _get_dir_size_walk(full_path)
            else:
                size = self.get_dir_size_mft(full_path)
            results.append({
                "path": full_path,
                "name": name,
                "location": label,
                "size_mb": size,
            })
            if progress_cb:
                progress_cb(i + 1, total, name)

        if progress_cb:
            progress_cb(total, total, "完成")

        results.sort(key=lambda x: x["size_mb"], reverse=True)
        return results

    def search_files(self, pattern, search_path=None, limit=1000):
        """像 Everything 一样按文件名搜索。

        pattern: 文件名匹配模式（支持 * 和 ? 通配符，大小写不敏感）
                 若不含通配符，自动加 * 前后缀（如 ".exe" → "*.exe*"）
        search_path: 限定搜索路径（None 表示全卷）
        limit: 最大返回结果数
        返回: list of {"path": str, "size": int, "is_dir": bool}
        """
        import fnmatch

        # 自动补全通配符：".exe" → "*.exe"，"test" → "*test*"
        if "*" not in pattern and "?" not in pattern:
            if pattern.startswith("."):
                pattern = "*" + pattern
            else:
                pattern = "*" + pattern + "*"

        # 检查搜索路径是否在当前卷上
        if search_path:
            search_path_normalized = search_path.replace("/", "\\").rstrip("\\")
            path_drive = search_path_normalized[:1].upper() if search_path_normalized else ""
            if path_drive and path_drive != self.volume.upper():
                # 路径在其他卷上，降级 os.walk
                return self._search_files_walk(pattern, search_path, limit)

        if self._fallback:
            return self._search_files_walk(pattern, search_path, limit)

        pattern_lower = pattern.lower()
        # 确定搜索范围的根 index
        root_idx = self._root_index
        if search_path:
            root_idx = self._resolve_path(search_path)
            if root_idx is None:
                return self._search_files_walk(pattern, search_path, limit)

        results = []

        # 收集搜索范围内的所有后代 index（BFS，用 deque 避免 pop(0) 的 O(n) 开销）
        from collections import deque
        scope = []
        visited = set()
        queue = deque([root_idx])
        while queue:
            idx = queue.popleft()
            if idx in visited:
                continue
            visited.add(idx)
            scope.append(idx)
            for child_idx in self._iter_children(self._rec_nums[idx]):
                if child_idx not in visited:
                    queue.append(child_idx)

        # 构建 index→路径的映射（从根开始）
        ref_to_path = {root_idx: search_path or (self.volume + ":\\")}
        queue2 = deque([root_idx])
        while queue2:
            idx = queue2.popleft()
            base_path = ref_to_path.get(idx)
            if base_path is None:
                continue
            for child_idx in self._iter_children(self._rec_nums[idx]):
                if child_idx in ref_to_path:
                    continue
                ref_to_path[child_idx] = os.path.join(base_path, self._name_of(child_idx))
                queue2.append(child_idx)

        # 遍历范围内所有记录，匹配文件名
        for idx in scope:
            if not self._is_dir(idx):
                name = self._name_of(idx)
                if fnmatch.fnmatch(name.lower(), pattern_lower):
                    path = ref_to_path.get(idx, name)
                    results.append({"path": path, "size": self._size_of(idx), "is_dir": False})
                    if len(results) >= limit:
                        break

        return results

    def count_files(self, path):
        """统计目录下文件总数（内存索引 BFS，零磁盘 I/O）。

        #23 优化:替代 rglob 全目录遍历(大目录如 Steam 10 万+ 文件,磁盘遍历
        数十秒,内存索引 BFS 毫秒级)。返回 -1 表示无法计算(非 MFT 模式/
        路径不在索引中),调用方应回退 rglob。

        审查修复:visited 去重防环——根目录在 MFT 中 parent_ref 指向自己
        (NTFS 惯例),不加 visited 时 count_files 对根/任意环路径死循环。
        """
        if self._fallback:
            return -1
        idx = self._resolve_path(path)
        if idx is None:
            return -1
        count = 0
        from collections import deque
        queue = deque([idx])
        # 环只能经目录形成(每个文件只有一个父,只会被 BFS 到达一次),
        # visited 只对目录去重:防 root 自引用/异常环,且省内存(44 万目录 vs 242 万节点)
        visited = set()
        while queue:
            i = queue.popleft()
            if self._is_dir(i):
                if i in visited:
                    continue
                visited.add(i)
                for ci in self._iter_children(self._rec_nums[i]):
                    queue.append(ci)
            else:
                count += 1
        return count

    def close(self):
        if self._reader:
            self._reader.close()
            self._reader = None
        self._close_rust_mm()

    def __del__(self):
        # 兜底：对象被回收时若 mm 未关闭则补关（含延迟删除的临时索引文件），
        # 防止调用方未 close 时映射句柄/临时文件泄漏
        try:
            self._close_rust_mm()
        except Exception:
            pass

    @property
    def is_mft_mode(self):
        # Rust 路径加载后无 _reader 句柄，用 _rust_loaded 标志
        return not self._fallback and (self._reader is not None or self._rust_loaded)

    # ------------------------------------------------------------------
    # os.walk 兜底实现
    # ------------------------------------------------------------------
    def _list_subdirs_walk(self, base_path):
        results = []
        try:
            for entry in os.listdir(base_path):
                full_path = os.path.join(base_path, entry)
                if not os.path.isdir(full_path):
                    continue
                if _is_reparse_point(full_path):
                    continue
                size_mb = _get_dir_size_walk(full_path)
                results.append({"path": full_path, "name": entry, "size_mb": size_mb})
        except Exception:
            pass
        results.sort(key=lambda x: x["size_mb"], reverse=True)
        return results

    def _search_files_walk(self, pattern, search_path, limit):
        import fnmatch
        results = []
        roots = [search_path] if search_path else [self.volume + ":\\"]
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                # 匹配文件
                for f in filenames:
                    if fnmatch.fnmatch(f.lower(), pattern.lower()):
                        full = os.path.join(dirpath, f)
                        try:
                            size = os.path.getsize(full)
                        except Exception:
                            size = 0
                        results.append({"path": full, "size": size, "is_dir": False})
                        if len(results) >= limit:
                            return results
                # 匹配子目录名
                for d in dirnames:
                    if fnmatch.fnmatch(d.lower(), pattern.lower()):
                        full = os.path.join(dirpath, d)
                        results.append({"path": full, "size": 0, "is_dir": True})
                        if len(results) >= limit:
                            return results
        return results


# ======================================================================
# 自测入口
# ======================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("MftScanner 自测")
    print("=" * 60)

    scanner = MftScanner("C")
    scanner.load()

    if scanner.is_mft_mode:
        print("\n[模式] MFT 高速模式")
    else:
        print("\n[模式] os.walk 兜底模式")

    # 测试1：单个目录大小
    test_dirs = [
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        r"C:\ProgramData",
        r"C:\Windows",
    ]
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        test_dirs.append(local_appdata)

    print("\n--- 测试1：单个目录大小计算 ---")
    for d in test_dirs:
        if not os.path.exists(d):
            continue
        t0 = time.time()
        size = scanner.get_dir_size_mft(d)
        t1 = time.time()
        print("  %-45s %10.1f MB  (%.3f 秒)" % (d, size, t1 - t0))

    # 测试2：列出一级子目录
    print("\n--- 测试2：C:\\Program Files 一级子目录（前10） ---")
    t0 = time.time()
    subdirs = scanner.list_subdirs_fast(r"C:\Program Files")
    t1 = time.time()
    print("  共 %d 个子目录，耗时 %.3f 秒" % (len(subdirs), t1 - t0))
    for s in subdirs[:10]:
        print("    %-40s %10.1f MB" % (s["name"], s["size_mb"]))

    # 测试3：六个目录扫描
    print("\n--- 测试3：六个监控目录扫描 ---")
    t0 = time.time()
    results = scanner.scan_six_dirs()
    t1 = time.time()
    print("  共 %d 个目录，耗时 %.3f 秒" % (len(results), t1 - t0))
    for r in results[:15]:
        print("    [%-13s] %-35s %10.1f MB" % (r["location"], r["name"], r["size_mb"]))

    scanner.close()
