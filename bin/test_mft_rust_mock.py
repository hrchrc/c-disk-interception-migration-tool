#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合成数据 A/B 对照（方案 8.8 §4.3 集成测试的 Python 侧）。

流程：
1. 读取 Rust 集成测试输出的 mft_mock_raw.bin（合成 MFT 记录字节）
2. Cython 路径：mft_fast.parse_records_bulk → 复刻 fast_scan 构建逻辑
   （枚举循环/名字池/锚点/子项分组/拓扑预计算）
3. Rust 路径：MftScanner._load_rust_index(mft_mock_out.idx) 直接加载
4. 逐字段对比——同一份输入，两路输出必须完全一致（确定性验证）

前置：
  cargo test --release --test integration_mft
  （在 rust-engine/crates/rust-migrate-engine/ 生成 mft_mock_raw.bin / mft_mock_out.idx）
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src", "core"))
sys.path.insert(0, os.path.join(ROOT, "src", "mft"))

from fast_scan import MftScanner
from mft_reader import _HAS_CYTHON
import mft_fast

CRATE = os.path.join(ROOT, "rust-engine", "crates", "rust-migrate-engine")
RAW_PATH = os.path.join(CRATE, "mft_mock_raw.bin")
IDX_PATH = os.path.join(CRATE, "mft_mock_out.idx")

BPR = 1024
BPS = 512


def build_like_fast_scan(records):
    """复刻 fast_scan.load() 的枚举+构建逻辑（Cython 路径原样：array('Q')）。

    输入为 Cython 解析出的记录列表。大小语义对比走 _size_of 全量验证
    （s1 直读 array('Q') 完整值；s2 走 u32 主表+溢出表），溢出表内部
    结构由 Rust 集成测试断言。
    """
    sc = MftScanner("C")
    names_chunks = []
    child_packed = []
    for idx, rec in enumerate(records):
        rn = rec["record_num"]
        parent = rec["parent_ref"] & 0xFFFFFFFF
        name_b = rec["name"].encode("utf-8", errors="replace")
        if len(name_b) > 0xFFFF:
            name_b = name_b[:0xFFFF]
        names_chunks.append(name_b)
        sc._name_lens.append(len(name_b))
        sc._rec_nums.append(rn)
        sc._rec_sizes.append(rec["size"])
        is_dir = 1 if rec["is_dir"] else 0
        is_reparse = 1 if rec.get("is_reparse", False) else 0
        sc._rec_flags.append(is_dir | (is_reparse << 1))
        child_packed.append((parent << 32) | idx)
        if rn == 5:
            sc._root_index = idx
        if rec["is_dir"]:
            sc.dir_count += 1
        else:
            sc.file_count += 1
            if rn >= 24:
                sc.total_size += rec["size"]
    sc._rec_names = b"".join(names_chunks)
    sc._build_name_anchors()
    # 子项分组（fast_scan.load 同款）
    child_packed.sort()
    child_data = []
    dir_entries_p = []
    dir_entries_start = []
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
    sc._child_data = array_of('I', child_data)
    sc._dir_entries_p = array_of('I', dir_entries_p)
    sc._dir_entries_start = array_of('I', dir_entries_start)
    sc._precompute_dir_sizes()
    return sc


def array_of(typecode, vals):
    from array import array
    a = array(typecode)
    a.extend(vals)
    return a


def main():
    assert os.path.isfile(RAW_PATH), "缺少 %s（先跑 cargo test --release --test integration_mft）" % RAW_PATH
    assert os.path.isfile(IDX_PATH), "缺少 %s" % IDX_PATH
    assert _HAS_CYTHON, "需要已编译的 mft_fast.pyd（src/setup.py build_ext --inplace）"

    print("=" * 70)
    print("合成数据 A/B 对照：Cython 路径 vs Rust 路径（同一份 mock 记录）")
    print("=" * 70)

    with open(RAW_PATH, "rb") as f:
        raw = f.read()
    assert len(raw) % BPR == 0
    print("mock 记录字节: %d 条 × %dB = %d 字节"
          % (len(raw) // BPR, BPR, len(raw)))

    # ---- Cython 路径（同一份数据） ----
    bulk = bytearray(raw)
    recs = mft_fast.parse_records_bulk(bulk, BPR, BPS, 24)
    print("Cython 解析: %d 条有效记录" % len(recs))
    s1 = build_like_fast_scan(recs)

    # ---- Rust 路径（加载 Rust 输出的索引文件） ----
    s2 = MftScanner("C")
    s2._load_rust_index(IDX_PATH)
    print("Rust  解析: %d 条有效记录（索引加载完成）" % len(s2._rec_nums))

    # ---- 逐字段对比 ----
    # _rec_sizes 类型不同（Cython array('Q') vs Rust u32 主表），大小语义
    # 由下方 _size_of 全量对比验证；溢出表内部结构由 Rust 集成测试断言
    fields = [
        ("_rec_names", "名字字节池"),
        ("_name_lens", "名字长度"),
        ("_name_anchors", "名字锚点"),
        ("_rec_nums", "记录号"),
        ("_rec_flags", "标志"),
        ("_dir_entries_p", "子项分组父"),
        ("_dir_entries_start", "子项分组起点"),
        ("_child_data", "子项数据"),
        ("_dir_size_idx", "目录大小 index"),
        ("_dir_size_val", "目录大小值"),
    ]
    n_fail = 0
    for attr, label in fields:
        a, b = getattr(s1, attr), getattr(s2, attr)
        if a != b:
            n_fail += 1
            print("  [不一致] %s (%s): Cython len=%d, Rust len=%d"
                  % (label, attr, len(a), len(b)))
            if hasattr(a, "__iter__") and hasattr(b, "__iter__"):
                for i, (x, y) in enumerate(zip(a, b)):
                    if x != y:
                        print("    首个差异 @%d: %r vs %r" % (i, x, y))
                        break
    # _size_of 全量抽查（溢出表路径验证）
    for i in range(len(s2._rec_nums)):
        if s1._size_of(i) != s2._size_of(i):
            n_fail += 1
            print("  [不一致] _size_of(%d): %r vs %r" % (i, s1._size_of(i), s2._size_of(i)))
            break
    if s1._root_index != s2._root_index:
        n_fail += 1
        print("  [不一致] root_index: %d vs %d" % (s1._root_index, s2._root_index))
    for attr in ("file_count", "dir_count", "total_size"):
        if getattr(s1, attr) != getattr(s2, attr):
            n_fail += 1
            print("  [不一致] %s: %d vs %d" % (attr, getattr(s1, attr), getattr(s2, attr)))

    if n_fail:
        print("\n合成数据 A/B 失败：%d 个字段不一致！" % n_fail)
        sys.exit(1)
    print("\n合成数据 A/B：11 个数组 + root_index + 3 个统计完全一致 ✓")
    print("（记录数 %d，file=%d dir=%d total=%d）"
          % (len(s2._rec_nums), s2.file_count, s2.dir_count, s2.total_size))


if __name__ == "__main__":
    main()
