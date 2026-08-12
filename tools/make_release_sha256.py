#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成发布包 SHA256 校验文件

用法：
    python tools/make_release_sha256.py <文件路径...>
    python tools/make_release_sha256.py dist/C盘拦迁器.exe

对每个文件生成 `<文件>.sha256`（内容：`<sha256>  <文件名>`），
发布时随包提供，用户可运行 `certutil -hashfile <文件> SHA256` 校验完整性。
"""
import hashlib
import sys
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    for arg in argv:
        p = Path(arg)
        if not p.is_file():
            print(f"跳过（不是文件）: {p}")
            continue
        digest = sha256_file(p)
        out = p.with_name(p.name + ".sha256")
        out.write_text(f"{digest}  {p.name}\n", encoding="ascii")
        print(f"{digest}  {p.name}  ->  {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
