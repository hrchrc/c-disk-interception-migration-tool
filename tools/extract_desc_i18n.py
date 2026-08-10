#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提取"识别描述层"文案：类型×位置矩阵 + 词典/内嵌描述 + AI type + 拼接短片段。

输出 i18n/translate_partN.json（每片 250 条），翻译后合并回 en_us.json。

来源：
1. _TYPE_POSITION_MATRIX（utils.py，85 类型 × 6 位置 ≈ 425 条模板，含 {sw}）
   以及矩阵的 type 键名（"浏览器"/"通讯软件"等，78-85 个）
2. software_dict.json 的中文描述值
3. software_detect.py 内嵌描述字典值（ast 提取）
4. ai_recognizer.py 的 AI type 列表（ast 提取）
5. en_us.json 中未翻译的短片段（2-12 字，拼接消息主力，安全片段替换用）
"""

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
I18N_DIR = ROOT / "i18n"

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")

desc_keys = set()


def add(s):
    if isinstance(s, str) and len(s) >= 2 and _ZH_RE.search(s):
        desc_keys.add(s)


def extract_matrix(src_path):
    """ast 提取 _TYPE_POSITION_MATRIX 的模板值与 type 键名。"""
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_TYPE_POSITION_MATRIX" \
                        and isinstance(node.value, ast.Dict):
                    for key_node, val_node in zip(node.value.keys, node.value.values):
                        if isinstance(key_node, ast.Constant):
                            add(key_node.value)  # type 名（"浏览器"）
                        if isinstance(val_node, ast.Dict):
                            for v in val_node.values:
                                if isinstance(v, ast.Constant):
                                    add(v.value)  # "{sw} 浏览器主程序（64 位）"


def extract_dict_values(src_path, dict_names):
    """ast 提取指定 dict 名的所有字符串值（软件描述字典）。"""
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in dict_names \
                        and isinstance(node.value, ast.Dict):
                    for v in node.value.values:
                        if isinstance(v, ast.Constant):
                            add(v.value)
                        elif isinstance(v, ast.Dict):  # 嵌套（如位置模板 dict）
                            for vv in v.values:
                                if isinstance(vv, ast.Constant):
                                    add(vv.value)


def extract_ai_types(src_path):
    """ast 提取 ai_recognizer.py 的中文 type 列表（列表/集合字面量）。"""
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
            for el in node.elts:
                if isinstance(el, ast.Constant) and isinstance(el.value, str) \
                        and _ZH_RE.search(el.value) and len(el.value) <= 8:
                    add(el.value)


def main():
    src_ui = ROOT / "src" / "core"
    # 1. 矩阵模板 + type 名
    extract_matrix(src_ui / "utils.py")
    # 2. 内嵌描述字典（缓存短路/位置后缀/兜底等）
    extract_dict_values(src_ui / "software_detect.py",
                        {"_CACHE_DIR_SHORTCUT", "_LOCATION_SUFFIX_KEYWORDS",
                         "_SPECIFIC_FUNCTION_KEYWORDS"})
    extract_dict_values(src_ui / "utils.py",
                        {"_POSITION_TEMPLATES", "_LOCATION_SUFFIX_KEYWORDS"})
    # 3. AI type 列表
    extract_ai_types(src_ui / "ai_recognizer.py")
    # 4. software_dict.json 描述值
    sd = ROOT / "src" / "data" / "software_dict.json"
    if sd.exists():
        try:
            data = json.loads(sd.read_text(encoding="utf-8"))
            for v in data.values():
                if isinstance(v, str):
                    add(v)
                elif isinstance(v, dict):
                    for vv in v.values():
                        if isinstance(vv, str):
                            add(vv)
        except Exception:
            pass
    # 5. 未翻译短片段（en_us 占位中 2-12 字）
    en_path = I18N_DIR / "en_us.json"
    if en_path.exists():
        t = json.loads(en_path.read_text(encoding="utf-8"))["translations"]
        for k, v in t.items():
            if v == k and 2 <= len(k) <= 12 and _ZH_RE.search(k):
                add(k)

    keys = sorted(desc_keys)
    print("识别描述层文案: %d 条" % len(keys))
    if not keys:
        return 1

    # 每片 250 条
    parts = [keys[i:i + 250] for i in range(0, len(keys), 250)]
    I18N_DIR.mkdir(exist_ok=True)
    # 清理旧分片
    for f in I18N_DIR.glob("translate_part*.json"):
        f.unlink()
    for i, part in enumerate(parts, 1):
        p = I18N_DIR / ("translate_part%d.json" % i)
        p.write_text(json.dumps({"_label": "", "translations": {k: "" for k in part}},
                                ensure_ascii=False, indent=1), encoding="utf-8")
        print("  分片%d: %d 条" % (i, len(part)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
