# -*- coding: utf-8 -*-
"""MftScanner 紧凑存储 mock 逻辑测试（不碰真实磁盘）"""
import sys, os
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "mft"))  # mft_reader 所在目录

import fast_scan
from fast_scan import MftScanner

# ===== 模拟 MftReader: 构造小目录树 =====
# 结构:
#   root(5)
#   ├── dir1(100)
#   │   ├── file1.txt(101)  size=100
#   │   └── subdir(102)
#   │       └── file2.bin(103)  size=200
#   ├── dir2(110)  [reparse]
#   │   └── file3(111)  size=300   (reparse 目录不展开)
#   └── top.txt(120)  size=50
class FakeReader:
    total_records = 8
    def __init__(self, volume): pass
    def open(self): pass
    def close(self): pass
    def enum_all_records(self):
        yield {"record_num": 5,   "name": ".",        "parent_ref": 5,   "is_dir": True,  "size": 0,   "is_reparse": False}
        yield {"record_num": 100, "name": "dir1",     "parent_ref": 5,   "is_dir": True,  "size": 0,   "is_reparse": False}
        yield {"record_num": 101, "name": "file1.txt","parent_ref": 100, "is_dir": False, "size": 100, "is_reparse": False}
        yield {"record_num": 102, "name": "subdir",   "parent_ref": 100, "is_dir": True,  "size": 0,   "is_reparse": False}
        yield {"record_num": 103, "name": "file2.bin","parent_ref": 102, "is_dir": False, "size": 200, "is_reparse": False}
        yield {"record_num": 110, "name": "dir2",     "parent_ref": 5,   "is_dir": True,  "size": 0,   "is_reparse": True}
        yield {"record_num": 111, "name": "file3",    "parent_ref": 110, "is_dir": False, "size": 300, "is_reparse": False}
        yield {"record_num": 120, "name": "top.txt",  "parent_ref": 5,   "is_dir": False, "size": 50,  "is_reparse": False}

fast_scan.MftReader = FakeReader  # 注入 mock
fast_scan.is_admin = lambda: True

s = MftScanner("C")
s.load()

MB = 1024 * 1024

# 1. 目录大小
d1 = s.get_dir_size_mft(r"C:\dir1")
assert d1 == round(300 / MB, 6), d1
# reparse 目录: 自身大小=直接子文件(300), 与旧实现一致(不展开子目录、不向父贡献)
d2 = s.get_dir_size_mft(r"C:\dir2")
assert d2 == round(300 / MB, 6), d2
root = s.get_dir_size_mft("C:\\")  # dir1(300) + dir2 不贡献 + top(50) = 350
assert root == round(350 / MB, 6), root
print("1. 目录大小计算正确 (reparse 语义与原版一致)")

# 2. 子目录列表
subs = s.list_subdirs_fast("C:\\")
names = [x["name"] for x in subs]
assert "dir1" in names and "dir2" not in names and "top.txt" not in names, names
print("2. list_subdirs_fast 正确:", names)

# 3. 搜索
r = s.search_files("file1*", "C:\\", 100)
assert len(r) == 1 and r[0]["path"].endswith("file1.txt") and r[0]["size"] == 100, r
r2 = s.search_files(".bin", "C:\\", 100)
assert len(r2) == 1 and r2[0]["path"].endswith("file2.bin"), r2
print("3. search_files 正确:", r, r2)

# 4. 统计
assert s.file_count == 4 and s.dir_count == 4, (s.file_count, s.dir_count)
assert s.total_size == 650, s.total_size  # 100+200+300+50
print("4. 统计正确: files=%d dirs=%d total=%d" % (s.file_count, s.dir_count, s.total_size))

# 5. _iter_children 惰性迭代 + 空 (返回 child 记录 index)
#    dir1(记录号100) 的子项: file1(101)→idx2, subdir(102)→idx3
assert list(s._iter_children(100)) == [2, 3], list(s._iter_children(100))
assert list(s._iter_children(9999)) == []
print("5. _iter_children 正确")

# 6. _resolve_path 缓存 + 未找到 (subdir 枚举 index=3)
assert s._resolve_path(r"C:\dir1\subdir") == 3, s._resolve_path(r"C:\dir1\subdir")
assert s._resolve_path(r"C:\notexist") is None
assert s._resolve_path("C:\\") == s._root_index
print("6. _resolve_path 正确")

# 7. 大样本压力: 10 万条记录构造
import random, time
random.seed(42)
N = 100000
class BigReader:
    total_records = N
    def __init__(self, volume): pass
    def open(self): pass
    def close(self): pass
    def enum_all_records(self):
        # 根 + 1000 个目录(每条 100 文件) + 根上 1000 文件
        yield {"record_num": 5, "name": ".", "parent_ref": 5, "is_dir": True, "size": 0, "is_reparse": False}
        for d in range(1, 1001):
            dn = d * 10
            yield {"record_num": dn, "name": "dir%d" % d, "parent_ref": 5, "is_dir": True, "size": 0, "is_reparse": False}
            for f in range(100):
                fn = dn * 1000 + f
                yield {"record_num": fn, "name": "file%d_%d.dat" % (d, f),
                       "parent_ref": dn, "is_dir": False,
                       "size": (d * 1000 + f) % 5000, "is_reparse": False}
        for i in range(1000):
            yield {"record_num": 1000000 + i, "name": "top%d.tmp" % i,
                   "parent_ref": 5, "is_dir": False, "size": i, "is_reparse": False}

fast_scan.MftReader = BigReader
s2 = MftScanner("C")
t0 = time.time()
s2.load()
load_sec = time.time() - t0
print("7. 10 万条加载耗时: %.2f 秒" % load_sec)
# root(1) + 目录(1000) + 文件(1000*100) + top(1000) = 102001
assert s2._count() == 102001, s2._count()
# 一个目录的大小（不触发 os.walk 兜底：目录在预计算缓存中）
sz = s2.get_dir_size_mft("C:\\dir500")
expect = sum((500 * 1000 + f) % 5000 for f in range(100))
assert sz == round(expect / MB, 6), (sz, expect)
# root 不在预计算缓存（自引用跳过，与原版一致）→ 直接查内部结构验证
assert s2._dir_size_bytes(s2._root_index) == 0
print("7. 10 万条功能验证通过 (目录大小正确, root 自引用跳过与原版一致)")
# 搜索
r3 = s2.search_files("file777_42*", "C:\\", 10)
assert len(r3) >= 1 and r3[0]["path"].endswith("file777_42.dat"), r3
print("7. 搜索验证通过")

print("\n全部 MftScanner 测试通过 ✅")
