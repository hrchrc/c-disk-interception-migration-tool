#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提取"用户可见"文案子集（弹窗 + 监控日志）并分成翻译分片。

输出 i18n/translate_part1.json / part2.json / part3.json，
每个分片 {"translations": {中文原文: ""}}，译文由翻译流程填充后合并回
en_us.json。

来源：
1. QMessageBox.question/information/warning/critical 调用的 title/text
2. monitor.py / ui_monitor_log.py 的 log.* 消息（含 f-string 中文片段）
"""

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OUT = ROOT / "i18n"

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")
_FRAG_RE = re.compile(r"[\u4e00-\u9fff][\u4e00-\u9fff，。！？、（）：“”‘’·—…]+")
# % 格式化占位符（%s %d %r %f %(name)s 等；%% 是转义字面 %）
_PCT_RE = re.compile(r"%(?:\([^)]*\))?[sdifrx]")


def pct_template(s):
    """% 格式化模板 → ~ 模板：'引擎超时(%s 秒)' → '引擎超时(~ 秒)'。"""
    tpl = _PCT_RE.sub("~", s).replace("%%", "%")
    return tpl

visible = {}


def add(s):
    if isinstance(s, str) and len(s) >= 2 and _ZH_RE.search(s):
        visible[s] = visible.get(s, 0) + 1


def walk_msgbox(tree):
    """QMessageBox.* 调用的 title/text 参数。"""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("question", "information", "warning", "critical"):
            continue
        if not (isinstance(node.func.value, ast.Name)
                and node.func.value.id == "QMessageBox"):
            continue
        # 位置参数：parent, title, text, ...（跳过 parent）
        for arg in node.args[1:3]:
            if isinstance(arg, ast.Constant):
                add(arg.value)
        for kw in node.keywords:
            if kw.arg in ("title", "text") and isinstance(kw.value, ast.Constant):
                add(kw.value.value)


def joinedstr_template(node):
    """f-string 重建为模板：变量位置用 ~ 标记。

    运行时消息归一化（数字/路径 → ~）后可精确匹配此模板（日志翻译
    的核心：模板 key 命中后把英文模板的 ~ 还原为实际值）。
    如 f'迁移完成: {n} 个文件' → '迁移完成: ~ 个文件'。
    """
    parts = []
    for v in node.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(v.value)
        else:
            parts.append("~")
    return "".join(parts)


def walk_log_calls(tree):
    """log.* 消息 + on_monitor_log 的文本参数。

    静态字符串 → 完整 key；f-string → 模板 key（~ 标记变量）+ 中文片段。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in ("info", "warning", "error", "debug", "exception"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant):
                        add(arg.value)
                        # % 格式化模板（log.warning("...%s...", arg)）：
                        # 占位符归一化为 ~，运行时数字/路径归一化后可命中
                        if "%" in arg.value:
                            tpl = pct_template(arg.value)
                            if "~" in tpl and _ZH_RE.search(tpl):
                                add(tpl)
                    elif isinstance(arg, ast.JoinedStr):
                        add(joinedstr_template(arg))
                        for v in arg.values:
                            if isinstance(v, ast.Constant):
                                for frag in _FRAG_RE.findall(v.value):
                                    add(frag)
            elif attr == "on_monitor_log":
                for arg in node.args[1:]:
                    if isinstance(arg, ast.Constant):
                        add(arg.value)
                        if "%" in arg.value:
                            tpl = pct_template(arg.value)
                            if "~" in tpl and _ZH_RE.search(tpl):
                                add(tpl)
                    elif isinstance(arg, ast.JoinedStr):
                        add(joinedstr_template(arg))
                        for v in arg.values:
                            if isinstance(v, ast.Constant):
                                for frag in _FRAG_RE.findall(v.value):
                                    add(frag)


def walk_ui_strings(tree):
    """UI 层所有中文字符串常量（控件文本/按钮/标签/状态栏/setText 等）。

    ui 层与 main.py 的字符串基本全部用户可见——控件文本（按钮/标签/
    占位/表头/菜单）、弹窗、状态栏、日志消息全覆盖。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            add(node.value)
        elif isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.Constant):
                    for frag in _FRAG_RE.findall(v.value):
                        add(frag)


def main():
    # 1. UI 层全部中文字符串（main.py + 全部 ui 模块）——控件文本/弹窗/状态栏
    for py in [SRC / "main.py"] + sorted((SRC / "ui").glob("*.py")):
        try:
            walk_ui_strings(ast.parse(py.read_text(encoding="utf-8")))
        except (SyntaxError, OSError):
            pass
    # 2. 监控日志文案：core 的 log.* 消息（监控日志窗口读 app.log，
    #    Python logging 输出均用户可见）+ monitor.py 的 on_monitor_log
    for py in sorted((SRC / "core").glob("*.py")):
        if py.name.startswith("test_") or py.name.startswith("__"):
            continue
        try:
            walk_log_calls(ast.parse(py.read_text(encoding="utf-8")))
        except (SyntaxError, OSError):
            pass

    keys = sorted(visible)
    print("可见文案子集: %d 条" % len(keys))
    if not keys:
        return 1

    # 按字符数均衡分 3 片（每片目标字符量 ≈ 总量/3）
    total_chars = sum(len(k) for k in keys)
    per = total_chars / 3
    parts = [[], [], []]
    cur = 0
    acc = 0
    for k in keys:
        parts[cur].append(k)
        acc += len(k)
        if acc >= per and cur < 2:
            cur += 1
            acc = 0

    OUT.mkdir(exist_ok=True)
    for i, part in enumerate(parts, 1):
        path = OUT / ("translate_part%d.json" % i)
        data = {"_label": "", "translations": {k: "" for k in part}}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        print("  分片 %d: %d 条 (%d 字符)" % (i, len(part), sum(len(k) for k in part)))
    print("分片输出到 i18n/translate_part*.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
