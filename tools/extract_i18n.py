#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面文案提取与语言包完整性校验。

用法：
    python tools/extract_i18n.py              # 提取 + 校验 + 报告缺 key
    python tools/extract_i18n.py --update     # 提取并把新 key 追加进 en_us.json 骨架

工作方式：
1. 扫描 src/ 下所有 .py，用正则提取字符串字面量中含中文的文案
2. 与 i18n/en_us.json 的 translations 对比，报告缺失/多余 key
3. --update 时把缺失 key 追加进语言包（译文占位=原文，供人工翻译）
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "src"
I18N_DIR = ROOT / "i18n"

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")
# 连续中文片段（含常见中文标点/数字后缀，如"个失败项"）
_FRAG_RE = re.compile(r"[\u4e00-\u9fff][\u4e00-\u9fff，。！？、（）：“”‘’·—…]+")

# 排除：纯代码标识符/路径/URL 等（提取后人工确认）
_EXCLUDE_PREFIX = ("http", "file://", "C:\\", "\\\\.\\")


def extract_strings():
    """提取文案：ast 解析字符串字面量（精确完整串）+ f-string 中文片段。

    返回 (full, frags)：
      full  = 非 f-string 的完整中文串（精确查表路径）
      frags = f-string 内连续中文片段（子串替换路径）
    """
    full = {}
    frags = {}
    for py in sorted(SRC_DIR.rglob("*.py")):
        if "test" in py.name or "__pycache__" in str(py):
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except Exception:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                s = node.value
                if len(s) >= 2 and _ZH_RE.search(s) and not s.startswith(_EXCLUDE_PREFIX):
                    full[s] = full.get(s, 0) + 1
            elif isinstance(node, ast.JoinedStr):
                # f-string：只取其中的中文片段（格式化后的 {expr} 无法预知）
                for v in node.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        for frag in _FRAG_RE.findall(v.value):
                            if len(frag) >= 2:
                                frags[frag] = frags.get(frag, 0) + 1
    return full, frags


def load_pack(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("translations", data)
    except Exception:
        return {}


def check_quality(path):
    """翻译质量检查：标记差译文（供重新翻译）。

    返回 (untranslated, mixed, placeholder_mismatch)：
      untranslated        — 译文 == 原文（占位未翻）
      mixed               — 译文含中文字符（中英混杂/漏翻片段）
      placeholder_mismatch — 模板 key（含 ~）译文里 ~ 数量与原文不符
    """
    pack = load_pack(path)
    untranslated, mixed, mismatch = [], [], []
    for k, v in pack.items():
        if k.startswith("_"):
            continue
        if v == k:
            untranslated.append(k)
            continue
        if _ZH_RE.search(v):
            mixed.append((k, v))
        if "~" in k and v.count("~") != k.count("~"):
            mismatch.append((k, v))
    return untranslated, mixed, mismatch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="把缺失 key 追加进 en_us.json")
    ap.add_argument("--check-quality", action="store_true",
                    help="质量检查：标记未翻译/混杂/占位符不符的译文")
    args = ap.parse_args()

    I18N_DIR.mkdir(exist_ok=True)
    en_path = I18N_DIR / "en_us.json"

    if args.check_quality:
        if not en_path.exists():
            print("语言包不存在: %s" % en_path)
            return 1
        untranslated, mixed, mismatch = check_quality(en_path)
        print("=== 翻译质量检查: %s ===" % en_path.name)
        print("未翻译（译文=原文占位）: %d 条" % len(untranslated))
        print("混杂（译文含中文）:     %d 条" % len(mixed))
        print("模板 ~ 数量不符:        %d 条" % len(mismatch))
        if mixed:
            print("\n混杂样例（前 15 条，需重新翻译）：")
            for k, v in sorted(mixed)[:15]:
                print("  中: %s" % k[:60])
                print("  英: %s" % v[:60])
        if mismatch:
            print("\n模板不符样例（前 10 条，需重新翻译）：")
            for k, v in sorted(mismatch)[:10]:
                print("  中: %s" % k[:60])
                print("  英: %s" % v[:60])
        return 0

    full, frags = extract_strings()
    pack = load_pack(en_path) if en_path.exists() else {}
    pack = {k: v for k, v in pack.items() if not k.startswith("_")}

    missing_full = [k for k in full if k not in pack]
    missing_frag = [k for k in frags if k not in pack]
    missing = sorted(set(missing_full) | set(missing_frag))
    extra = [k for k in pack if k not in full and k not in frags]

    print("源码完整中文串: %d 条（去重）" % len(full))
    print("f-string 中文片段: %d 条（去重）" % len(frags))
    print("语言包已有:   %d 条" % len(pack))
    print("缺失（未翻译）: %d 条（完整串 %d + 片段 %d）"
          % (len(missing), len(missing_full), len(missing_frag)))
    if extra:
        print("多余（源码已无此文案）: %d 条" % len(extra))
        for k in extra[:10]:
            print("   - %s" % k[:60])

    if missing:
        print("\n缺失样例（前 15 条）：")
        for k in sorted(missing)[:15]:
            print("   + %s" % k[:70])

    if args.update and missing:
        new_pack = dict(pack)
        for k in missing:
            new_pack[k] = k  # 占位：译文=原文，待人工翻译
        out = {"_label": "English", "translations": new_pack}
        en_path.write_text(
            json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8")
        print("\n已追加 %d 条到 %s（译文占位=原文，请人工翻译）"
              % (len(missing), en_path))
    elif not missing:
        print("\n语言包完整，无缺失 ✓")

    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
