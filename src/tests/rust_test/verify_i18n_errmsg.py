# -*- coding: utf-8 -*-
"""验证:事故弹框消息在英文语言包下的翻译结果(与 patch_message_boxes 同路径)。
2026-08-09 新增:ERR_SOURCE_CHANGED 内部码翻译 + 片段条目。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))
import i18n

# 1. JSON 语法 + 条目数
for f in ("i18n/en_us.json", "i18n/zh_cn.json"):
    d = json.load(open(f, encoding="utf-8"))
    print(f, "OK,", len(d["translations"]), "entries")

# 2. 模拟事故弹框消息(修复后 _format_copy_fail 输出)
msg = (
    "迁移失败（返回码 8）。\n"
    "原因：源文件在复制期间被截断或发生变化（ERROR 0xE0000001）\n"
    "建议：文件可能正被其他程序写入（如安装器/更新器正在运行），请关闭相关软件后重新迁移该目录\n"
    "失败文件：C:\\Users\\aaa\\AppData\\Local\\updater\\installer.exe\n"
    "已记录未完成事务，目标盘已保留已复制的数据。\n"
    "下次启动程序会自动续传（引擎幂等重跑）。"
)

i18n.load_language("en_us")
out = i18n.tr(msg)
print("--- 英文模式 ---")
print(out)
zh_left = sum(1 for ch in out if "\u4e00" <= ch <= "\u9fff")
print("中文残留字符数:", zh_left)
assert zh_left == 0, "英文模式不应有中文残留"

# 3. 中文模式保持原文
i18n.load_language("zh_cn")
assert i18n.tr(msg) == msg, "中文模式应保持原文"
print("--- 中文模式保持原文: OK")
print("ALL PASS")
