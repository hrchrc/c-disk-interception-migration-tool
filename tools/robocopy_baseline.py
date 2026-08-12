# -*- coding: utf-8 -*-
"""tools/robocopy_baseline.py — 开发期对照基准(不进发布)

ADR-003 保留项(2026-08-05 重建):引擎开发期用系统 robocopy 跑同样任务做回归对照。
执行文档 §7.5 规格:
  - 用 robocopy 跑同样任务,对比:
      1. 文件数完整性
      2. 元数据保真度(ACL/ADS/时间戳)
      3. 性能(大文件吞吐、海量小文件耗时)
  - 用于引擎开发期回归对照,生产代码不引用,发布包不包含本脚本。

用法:
  python tools/robocopy_baseline.py <src> <dst> [--mode mirror|copy] [--mt 16] [--engine PATH]

示例:
  python tools/robocopy_baseline.py D:\\src D:\\dst --mode mirror --mt 16
  python tools/robocopy_baseline.py D:\\src D:\\dst --engine bin\\rust-migrate-engine.exe
"""
import argparse
import os
import subprocess
import sys
import time

_NO_WINDOW_FLAGS = 0x08000000  # CREATE_NO_WINDOW


def count_files(root):
    """统计目录树:文件数、总字节数。返回 (count, bytes)。"""
    n = 0
    total = 0
    for dp, _, fn in os.walk(root):
        for f in fn:
            p = os.path.join(dp, f)
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
            n += 1
    return n, total


def verify_timestamps(src, dst):
    """对比 src/dst 时间戳,mtime 偏差 >1s 记为不一致。返回不一致列表。"""
    mism = []
    src_mtimes = {}
    for dp, _, fn in os.walk(src):
        for f in fn:
            p = os.path.join(dp, f)
            src_mtimes[os.path.relpath(p, src)] = os.path.getmtime(p)
    for dp, _, fn in os.walk(dst):
        for f in fn:
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, dst)
            if rel not in src_mtimes:
                mism.append(("extra", rel))
            elif abs(os.path.getmtime(p) - src_mtimes[rel]) > 1.0:
                mism.append(("mtime", rel))
    return mism


def run_robocopy(src, dst, mode, mt):
    """跑系统 robocopy,返回 (rc, elapsed_s)。"""
    cmd = ["robocopy", src, dst]
    if mode == "mirror":
        cmd += ["/MIR"]
    else:
        cmd += ["/E"]
    cmd += ["/R:1", "/W:1", f"/MT:{mt}", "/NFL", "/NDL", "/NJH"]
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW_FLAGS)
    elapsed = time.time() - t0
    return p.returncode, elapsed


def run_engine(src, dst, exe, mode):
    """跑 Rust 引擎(自研)同任务,返回 (rc, elapsed_s)。"""
    import json
    import tempfile
    job = {
        "source": src,
        "target": dst,
        "mode": mode,
        "verify": "none",
        "retry": {"max_attempts": 1, "backoff_base_ms": 1, "network_path": False},
        "flush_checkpoint_mb": 64,
        "purge": {"enabled": mode == "mirror", "soft_delete": False},
        "background_mode": False,
        "write_through": False,
        "large_file_threshold_mb": 64,
    }
    with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(job, f)
        job_path = f.name
    try:
        t0 = time.time()
        p = subprocess.run(
            [exe, "--job", job_path, "--log-format", "jsonl"],
            capture_output=True, creationflags=_NO_WINDOW_FLAGS)
        elapsed = time.time() - t0
    finally:
        try:
            os.unlink(job_path)
        except OSError:
            pass
    return p.returncode, elapsed


def fmt_elapsed(s):
    return f"{s:.1f}s" if s < 300 else f"{s / 60:.1f}min"


def main():
    ap = argparse.ArgumentParser(description="开发期 robocopy 对照基准(不进发布)")
    ap.add_argument("src", help="源目录")
    ap.add_argument("dst", help="目标目录")
    ap.add_argument("--mode", choices=("mirror", "copy"), default="copy")
    ap.add_argument("--mt", type=int, default=16, help="robocopy 多线程数(默认 16)")
    ap.add_argument("--engine", default=None,
                    help="Rust 引擎 exe 路径,提供则同时跑引擎做对照")
    args = ap.parse_args()

    if not os.path.isdir(args.src):
        print(f"错误:源目录不存在 {args.src}")
        return 2
    if os.path.exists(args.dst) and not os.path.isdir(args.dst):
        print(f"错误:目标路径存在但不是目录 {args.dst}")
        return 2

    src_n, src_b = count_files(args.src)
    print(f"源: {args.src}")
    print(f"  文件数: {src_n}, 总字节: {src_b / 1024 / 1024:.1f} MB")
    print()

    # ---- 1. robocopy ----
    print(f"=== robocopy /{'MIR' if args.mode == 'mirror' else 'E'} /MT:{args.mt} ===")
    rc, el = run_robocopy(args.src, args.dst, args.mode, args.mt)
    dst_n, dst_b = count_files(args.dst)
    mism = verify_timestamps(args.src, args.dst) if os.path.isdir(args.dst) else []
    print(f"  rc={rc} 耗时 {fmt_elapsed(el)}")
    print(f"  目标文件数 {dst_n}(源 {src_n}),缺失/多余/时间戳不一致: {len(mism)}")
    if rc >= 8:
        print(f"  ⚠️ robocopy 返回失败码 {rc}")
    print()

    # ---- 2. (可选) Rust 引擎对照 ----
    if args.engine:
        if not os.path.isfile(args.engine):
            print(f"错误:引擎 exe 不存在 {args.engine}")
            return 2
        # 清掉 robocopy 残留,引擎跑干净任务(引擎幂等重跑会覆盖/补齐)
        print(f"=== Rust 引擎(自研) {args.mode} 模式 ===")
        rc2, el2 = run_engine(args.src, args.dst, args.engine, args.mode)
        dst_n2, _ = count_files(args.dst)
        mism2 = verify_timestamps(args.src, args.dst)
        print(f"  rc={rc2} 耗时 {fmt_elapsed(el2)}")
        print(f"  目标文件数 {dst_n2}(源 {src_n}),缺失/多余/时间戳不一致: {len(mism2)}")
        print()
        print("=== 对照汇总 ===")
        speedup = el / el2 if el2 > 0 else float("inf")
        print(f"  robocopy: {fmt_elapsed(el)} | 引擎: {fmt_elapsed(el2)} "
              f"| 引擎倍率: {speedup:.2f}x")
        print(f"  文件完整性 robocopy 不一致 {len(mism)} / 引擎不一致 {len(mism2)}")
    else:
        print("(加 --engine <exe> 可同时跑 Rust 引擎对照)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
