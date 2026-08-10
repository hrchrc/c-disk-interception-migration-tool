# -*- coding: utf-8 -*-
"""真实 C 盘 MFT 加载 + 内存测量（新紧凑存储 vs 旧 dict 方案）"""
import sys, os, time, gc
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mft"))

import psutil
from fast_scan import MftScanner

proc = psutil.Process(os.getpid())

def rss_mb():
    gc.collect()
    return proc.memory_info().rss / 1024 / 1024

print("=== MftScanner 紧凑存储 · 真实 C 盘内存测量 ===")
base = rss_mb()
print("加载前基线 RSS: %.1f MB" % base)

t0 = time.time()
scanner = MftScanner("C")
scanner.load()
load_sec = time.time() - t0

after = rss_mb()
print("加载后 RSS: %.1f MB (耗时 %.1f 秒)" % (after, load_sec))
print("MFT 索引内存增量: %.1f MB" % (after - base))

# 数组明细
print("\n--- 数组明细 ---")
detail = [
    ("记录数", scanner._count()),
    ("_rec_names (字节池)", len(scanner._rec_names)),
    ("_name_lens", len(scanner._name_lens) * 2),
    ("_name_anchors", len(scanner._name_anchors) * 4),
    ("_rec_nums", len(scanner._rec_nums) * 4),
    ("_rec_sizes", len(scanner._rec_sizes) * (4 if scanner._size_ovf_idx is not None else 8)),  # v2: Rust 路径 u32 主表
    ("_rec_flags", len(scanner._rec_flags)),
    ("_child_data", len(scanner._child_data) * 4),
    ("_dir_entries_p+start", (len(scanner._dir_entries_p) + len(scanner._dir_entries_start)) * 4),
    ("_dir_size_idx", len(scanner._dir_size_idx) * 4),
    ("_dir_size_val", len(scanner._dir_size_val) * 8),
]
raw_total = 0
for name, size in detail:
    print("  %-22s %10.2f MB" % (name, size / 1024 / 1024))
    raw_total += size
print("  原生数组合计: %.1f MB (不含 Python 对象开销)" % (raw_total / 1024 / 1024))

print("\n--- 功能抽查 ---")
tests = [r"C:\Program Files", r"C:\ProgramData", r"C:\Windows\System32"]
if os.environ.get("LOCALAPPDATA"):
    tests.append(os.environ["LOCALAPPDATA"])
for d in tests:
    if not os.path.exists(d):
        continue
    t1 = time.time()
    sz = scanner.get_dir_size_mft(d)
    print("  %-40s %10.1f MB  (%.3f 秒)" % (d, sz, time.time() - t1))

t2 = time.time()
subs = scanner.list_subdirs_fast(r"C:\Program Files")
print("  Program Files 子目录: %d 个 (%.3f 秒)" % (len(subs), time.time() - t2))

t3 = time.time()
res = scanner.search_files("*.exe", r"C:\Program Files", 20)
print("  搜索 *.exe 前 %d 条 (%.3f 秒)" % (len(res), time.time() - t3))

scanner.close()
gc.collect()
print("\nclose 后 RSS: %.1f MB" % rss_mb())
