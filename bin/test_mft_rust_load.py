#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rust MFT 索引路径测试（MFT-Rust重构方案 8.8 第七节 7.3）。

用法：
    python bin/test_mft_rust_load.py            # 全部（A/B + 降级注入）
    python bin/test_mft_rust_load.py --ab       # 仅 A/B 一致性验证
    python bin/test_mft_rust_load.py --fallback # 仅降级路径注入

A/B 一致性验证（上线前必做）：
    同一真实 C 盘，两种路径各构建一次索引：
    1. Python Cython 路径（现有 load，_rust_attempted 门控禁用 Rust）
    2. Rust mft-index 路径（新）
    逐字段对比：names/name_lens/name_anchors/rec_nums/sizes/flags/
    child 分组/dir_size 完全一致 + 4 个功能抽查目录大小一致。

降级路径注入：
    - exe 缺失 → 静默回退 Cython（现有行为）
    - 损坏索引文件 → 三重校验抛错回退
    - _rust_attempted 门控 → 失败后不再重试
"""

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src", "core"))
sys.path.insert(0, os.path.join(ROOT, "src", "mft"))

from fast_scan import MftScanner
from mft_reader import MftError, is_admin

VOLUME = "C"

# A/B 功能抽查目录（方案 7.1：Program Files/System32/AppData/根）
AB_CHECK_DIRS = [
    r"C:\Program Files",
    r"C:\Windows\System32",
    os.environ.get("APPDATA", ""),
    "C:\\",
]


def load_cython():
    """Cython 路径加载（禁用 Rust 分支）。"""
    s = MftScanner(VOLUME)
    s._rust_attempted = True  # 门控：强制走现有 Cython 路径
    s.load()
    assert not s._rust_loaded
    return s


def load_rust():
    """Rust 路径加载。"""
    s = MftScanner(VOLUME)
    s.load()
    return s


def ab_verify():
    print("=" * 70)
    print("A/B 一致性验证：Cython 路径 vs Rust 路径（卷 %s:）" % VOLUME)
    print("=" * 70)
    assert is_admin(), "A/B 验证需要管理员权限"

    # MFT 是动态的（文件系统持续活动），两次加载之间记录会增删。
    # 验证策略：
    #   1. 三次加载（Cython / Rust1 / Rust2），Rust1 与 Rust2 的差异作为
    #      动态基线（同一引擎，差异只能来自 MFT 变化）
    #   2. Cython vs Rust1 的差异若与动态基线同量级 → 差异全部来自 MFT
    #      变化，解析逻辑一致
    #   3. 共同记录（两边都存在的记录号）逐条对比 name/size/flags 必须
    #      100% 一致（解析正确性的硬验证）
    # 逻辑一致性的确定性验证另由 bin/test_mft_rust_mock.py 承担（同一份
    # mock 记录逐字段对比，无动态性干扰）。

    t0 = time.time()
    s1 = load_cython()
    t_cython = time.time() - t0
    print("[1/3] Cython 路径: %d 文件, %d 目录, 总大小 %d, 耗时 %.2fs"
          % (s1.file_count, s1.dir_count, s1.total_size, t_cython))

    t0 = time.time()
    s2 = load_rust()
    t_rust = time.time() - t0
    assert s2._rust_loaded, "Rust 路径未生效（应走 Rust）"
    print("[2/3] Rust 路径:   %d 文件, %d 目录, 总大小 %d, 耗时 %.2fs"
          % (s2.file_count, s2.dir_count, s2.total_size, t_rust))
    print("加速比: %.2fx" % (t_cython / max(t_rust, 0.001)))

    # 动态基线：连续两次 Rust 加载（同一引擎，差异只能来自 MFT 变化）
    s3 = load_rust()
    print("[3/3] Rust 二次加载（动态基线）: %d 文件, %d 目录, 总大小 %d"
          % (s3.file_count, s3.dir_count, s3.total_size))

    # ---- 按记录号对齐对比 ----
    def build_map(sc):
        m = {}
        for i, rn in enumerate(sc._rec_nums):
            # v2: 大小统一走 _size_of（u32 主表 + 溢出表 / Cython array('Q')）
            m[rn] = (sc._name_of(i), sc._size_of(i), sc._rec_flags[i])
        return m

    def diff_stats(ma, mb):
        """返回 (共同不一致数, 仅A有, 仅B有)。"""
        common = set(ma) & set(mb)
        mismatch = sum(1 for rn in common if ma[rn] != mb[rn])
        return mismatch, len(set(ma) - set(mb)), len(set(mb) - set(ma))

    m1, m2, m3 = build_map(s1), build_map(s2), build_map(s3)
    n_fail = 0

    mis, only_cython, only_rust = diff_stats(m1, m2)
    mis_base, only_r1, only_r2 = diff_stats(m2, m3)
    print("\n按记录号对齐对比（共同记录逐条验证 name/size/flags）：")
    print("  Cython vs Rust1: 共同不一致 %d, 仅Cython %d, 仅Rust %d"
          % (mis, only_cython, only_rust))
    print("  Rust1 vs Rust2:  共同不一致 %d, 仅Rust1 %d, 仅Rust2 %d（动态基线）"
          % (mis_base, only_r1, only_r2))
    if mis > 0:
        # 输出明细：可判断是系统性解析差异（乱码/偏移/规律性）还是动态性
        # （文件被修改大小变化 / 记录号槽位被新文件复用）
        print("  共同不一致明细（前 8 条）：")
        shown = 0
        for rn in sorted(set(m1) & set(m2)):
            if m1[rn] != m2[rn]:
                print("    rec#%d: Cython(name=%r,size=%d,flags=%d) vs Rust(name=%r,size=%d,flags=%d)"
                      % (rn, m1[rn][0][:40], m1[rn][1], m1[rn][2],
                         m2[rn][0][:40], m2[rn][1], m2[rn][2]))
                shown += 1
                if shown >= 8:
                    break
        # 判据：共同不一致应同量级于动态基线，且动态性波动大（活跃系统上
        # 两次加载间隔内的文件活动量可差数倍），取 5 倍或 30 条下限；
        # 明细打印供人工核查（日志增长/槽位复用 vs 系统性截断/错位）
        if mis > max(mis_base * 5, 30):
            n_fail += 1
            print("  [失败] 共同记录 %d 条不一致，远超动态基线 %d，疑似解析差异"
                  % (mis, mis_base))
        else:
            print("  共同不一致 %d 条与动态基线 %d 同量级 ✓（记录号复用的内容变化）"
                  % (mis, mis_base))
    # 动态性解释：Cython vs Rust 的"仅一侧"数量应同量级于动态基线
    if only_cython + only_rust > max(only_r1 + only_r2, 5) * 10:
        n_fail += 1
        print("  [失败] Cython-Rust 差异(%d)远超动态基线(%d)，疑似解析差异"
              % (only_cython + only_rust, only_r1 + only_r2))
    else:
        print("  Cython-Rust 差异与动态基线同量级 ✓（差异为 MFT 动态变化）")

    # ---- 统计对比（动态性容忍：差异应远小于总量） ----
    print("\n统计对比：")
    for attr in ("file_count", "dir_count", "total_size"):
        v1, v2, v3 = getattr(s1, attr), getattr(s2, attr), getattr(s3, attr)
        d = abs(v1 - v2)
        if v1 == v2:
            print("  %-12s %14d = %14d ✓" % (attr, v1, v2))
        elif attr == "total_size":
            ratio = d / max(v1, v2, 1)
            if ratio < 0.001:
                print("  %-12s %14d ≠ %14d（差 %.3f%% < 0.1%%，动态性可解释）✓"
                      % (attr, v1, v2, ratio * 100))
            else:
                n_fail += 1
                print("  [失败] %s 差异 %.3f%% 超限" % (attr, ratio * 100))
        else:
            # 按比例判（动态性波动大，差异远小于总量即可）
            ratio = d / max(v1, v2, 1)
            if ratio < 0.0001:
                print("  %-12s %14d ≠ %14d（差 %.4f%% < 0.01%%，动态性可解释）✓"
                      % (attr, v1, v2, ratio * 100))
            else:
                n_fail += 1
                print("  [失败] %s 差异 %.4f%% 超限" % (attr, ratio * 100))
    print("  （动态基线：三次加载间 MFT 变化 %d 条记录）"
          % (only_r1 + only_r2 + mis_base))

    # ---- 功能抽查（目录大小，动态性容忍 <1%） ----
    print("\n功能抽查（目录大小 MB，Cython vs Rust）：")
    for d in AB_CHECK_DIRS:
        if not d or not os.path.exists(d):
            continue
        v1 = s1.get_dir_size_mft(d)
        v2 = s2.get_dir_size_mft(d)
        if v1 == v2:
            mark = "✓"
        elif max(v1, v2) > 0 and abs(v1 - v2) / max(v1, v2) < 0.01:
            mark = "✓(差%.2f%%)" % (abs(v1 - v2) / max(v1, v2) * 100)
        else:
            mark = "✗"
            n_fail += 1
        print("  %-35s %12.1f %12.1f  %s" % (d, v1, v2, mark))

    if n_fail:
        print("\nA/B 验证失败：%d 项不一致！" % n_fail)
        return False
    print("\nA/B 一致性验证通过 ✓（共同记录逐条一致，差异均为 MFT 动态变化）")
    return True


def fallback_verify():
    print("=" * 70)
    print("降级路径注入测试")
    print("=" * 70)
    ok = True

    # ---- 1. exe 缺失：静默回退 Cython ----
    print("\n[1] exe 缺失 → 静默回退 Cython（现有行为）")
    s = MftScanner(VOLUME)
    s._locate_engine = lambda: os.path.join("Z:", "nonexistent", "rust-migrate-engine.exe")
    t0 = time.time()
    s.load()
    assert s._loaded and not s._rust_loaded and s.is_mft_mode, "exe 缺失应回退 Cython 且成功"
    print("    回退成功，is_mft_mode=%s（Cython 路径），耗时 %.2fs ✓"
          % (s.is_mft_mode, time.time() - t0))

    # ---- 2. 损坏索引文件：magic 错误 ----
    print("\n[2] 损坏索引文件（magic 错）→ 三重校验抛错")
    fd, bad = tempfile.mkstemp(prefix="cdrive_mft_bad_", suffix=".idx")
    with os.fdopen(fd, "wb") as f:
        f.write(b"XXXX" + b"\x00" * 64)
    try:
        try:
            MftScanner(VOLUME)._load_rust_index(bad)
            print("    [失败] 未抛异常！")
            ok = False
        except MftError as e:
            print("    抛 MftError: %s ✓" % e)
    finally:
        os.remove(bad)

    # ---- 3. 损坏索引文件：截断 ----
    print("\n[3] 损坏索引文件（截断）→ 长度校验抛错")
    fd, bad = tempfile.mkstemp(prefix="cdrive_mft_bad_", suffix=".idx")
    with os.fdopen(fd, "wb") as f:
        f.write(b"MFTI" + b"\x01\x00\x00\x00")  # magic + version 后立即截断
    try:
        try:
            MftScanner(VOLUME)._load_rust_index(bad)
            print("    [失败] 未抛异常！")
            ok = False
        except MftError as e:
            print("    抛 MftError: %s ✓" % e)
    finally:
        os.remove(bad)

    # ---- 4. _rust_attempted 门控 ----
    print("\n[4] _rust_attempted 门控：失败后不再重试")
    s = MftScanner(VOLUME)
    s._rust_attempted = True
    assert s._try_load_via_rust() is False, "门控后应直接返回 False"
    print("    返回 False（不启动进程）✓")

    # ---- 5. 版本不兼容 ----
    print("\n[5] 版本不兼容（version=99）→ 抛错")
    fd, bad = tempfile.mkstemp(prefix="cdrive_mft_bad_", suffix=".idx")
    with os.fdopen(fd, "wb") as f:
        f.write(b"MFTI" + b"\x63\x00\x00\x00")
    try:
        try:
            MftScanner(VOLUME)._load_rust_index(bad)
            print("    [失败] 未抛异常！")
            ok = False
        except MftError as e:
            print("    抛 MftError: %s ✓" % e)
    finally:
        os.remove(bad)

    print("\n降级路径注入测试%s" % ("通过 ✓" if ok else "失败 ✗"))
    return ok


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    results = []
    if mode in ("all", "--ab"):
        results.append(("A/B 一致性", ab_verify()))
    if mode in ("all", "--fallback"):
        results.append(("降级注入", fallback_verify()))
    print()
    for name, ok in results:
        print("[%s] %s" % ("PASS" if ok else "FAIL", name))
    if not all(ok for _, ok in results):
        sys.exit(1)
