"""界面文案国际化支持。

设计要点：
- 语言包为 JSON：key=界面原始文案，value=目标语言译文。存量中文文案
  无需改为 key 调用，运行时按原文查表翻译（对现有代码零侵入）。
- tr(text) 三级策略：精确查表 → 含中文长文本片段替换（长 key 优先，
  处理拼接句）→ 返回原文（无中文或含路径的文本不翻）。
- 语言切换：重载语言包后遍历控件树刷新可见文案；动态创建的控件在
  创建处调 tr() 或切换后再次遍历。
- 翻译完整性：tools/extract_i18n.py 从源码提取中文串并校验语言包缺项，
  缺 key 会输出报告（防止漏翻静默回退原文）。
- 作用域：只翻译用户可见界面文案；日志/识别描述/内部消息不翻。
"""

import json
import logging
import re
import sys
import threading
from pathlib import Path

log = logging.getLogger('CDriveRelocator')

# 模板归一化：数字（含小数/千分位）与盘符路径 → ~
_TPL_RE = re.compile(r'\d+(?:[.,]\d+)*|[A-Za-z]:\\[^\s,;）)]*')
# 中文检测（安全片段替换用）
_ZH_RE = re.compile(r'[\u4e00-\u9fff]')

# 语言包目录：打包模式优先 exe 内 _MEIPASS/i18n（打进 exe），缺失回退 exe 同级 i18n/；源码模式 = 项目根/i18n
if getattr(sys, "frozen", False):
    _meipass = getattr(sys, "_MEIPASS", None)
    if _meipass and (Path(_meipass) / "i18n").is_dir():
        _I18N_DIR = Path(_meipass) / "i18n"
    else:
        _I18N_DIR = Path(sys.executable).resolve().parent / "i18n"
else:
    _I18N_DIR = Path(__file__).resolve().parent.parent.parent / "i18n"

# 全局状态（UI 主线程使用，加锁防御多线程切换竞争）
_lock = threading.Lock()
_pack = {}            # 当前语言包 {原文: 译文}
_rev_pack = {}        # 反向索引 {译文: 原文}（切回中文时还原控件文本）
_pack_code = "zh_cn"  # 当前语言码
_cache = {}           # tr 结果缓存（LRU，上限 2048 防膨胀）
_frag_keys = None     # 安全片段替换用：含中文的 key，按长度降序（懒构建）


def available_languages():
    """可用语言列表 [(code, label)]。

    只接受语言码格式文件名（如 en_us / zh_cn），防止工具生成的临时
    分片文件（translate_part*.json 等）被误当成语言包混入下拉框。
    """
    result = []
    for f in sorted(_I18N_DIR.glob("*.json")):
        code = f.stem
        if not re.fullmatch(r"[a-z]{2,3}([-_][A-Za-z]{2,4})?", code):
            continue  # 非语言码格式（分片/临时文件）跳过
        label = code
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            label = data.get("_label", code)
        except Exception:
            pass
        result.append((code, label))
    return result


def current_language():
    """当前语言码。"""
    return _pack_code


def load_language(code):
    """加载语言包并重建缓存。中文或加载失败时回退原文（空包）。

    _rev_pack（译文→原文反向索引）切换语言时保留——切回中文时
    apply_language 需要用它把英文控件文本还原成中文原文。
    """
    global _pack, _rev_pack, _pack_code, _cache, _frag_keys
    with _lock:
        _pack_code = code or "zh_cn"
        _pack = {}
        _cache = {}
        _frag_keys = None
        if _pack_code != "zh_cn":
            path = _I18N_DIR / (_pack_code + ".json")
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                trans = data.get("translations", data)
                if isinstance(trans, dict):
                    # 只收字符串值（损坏/异常语言包的非字符串 value 会导致
                    # str.replace 类型错误，加载时过滤）
                    _pack = {k: v for k, v in trans.items()
                             if isinstance(k, str) and isinstance(v, str)
                             and not k.startswith("_")}
                    # 反向索引（切回中文时还原控件文本）
                    _rev_pack = {v: k for k, v in _pack.items()}
            except Exception as e:
                log.warning("[i18n] 语言包加载失败 %s: %s", path, e)
    return _pack_code


def render(text):
    """按当前语言渲染文案：英文模式正向翻译，中文模式反向还原。

    apply_language 遍历控件树刷新时使用——控件文本可能是中文原文
    （首次）或英文译文（切换后），两种模式都要能正确落到目标语言。
    """
    if not text:
        return text
    raw = str(text)
    if _pack_code == "zh_cn":
        return _rev_pack.get(raw, raw)
    return tr(raw)




def _cache_set(raw, val):
    if len(_cache) >= 2048:
        _cache.clear()
    _cache[raw] = val


def _normalize_template(text):
    """把消息中的数字/盘符路径归一化为 ~，返回 (归一化文本, 占位值列表)。

    用于 f-string 模板匹配：提取工具生成的语言包 key 用 ~ 标记变量位置，
    运行时消息归一化后查表，命中则把译文里的 ~ 按序还原为原值。
    """
    vals = []
    out = []
    last = 0
    for m in _TPL_RE.finditer(text):
        out.append(text[last:m.start()])
        out.append("~")
        vals.append(m.group())
        last = m.end()
    out.append(text[last:])
    return "".join(out), vals


def _restore_template(tpl, vals):
    """把译文模板中的 ~ 按序还原为占位值。"""
    it = iter(vals)
    return re.sub("~", lambda m: next(it, "~"), tpl)


def tr(text):
    """翻译界面文案。四级策略，保证输出纯英文或纯中文（不混杂）：
    1. 精确查表（完整原文在语言包）
    2. 模板匹配（f-string 类消息：数字/路径归一化后查模板表，命中还原）
    3. 安全片段替换（拼接消息：按 key 长度降序替换，**替换后仍有中文
       残留则整体放弃返回原文**——全有或全无，绝不产生中英混杂句）
    4. 未命中返回原文
    结果带缓存。"""
    if not text:
        return text
    raw = str(text)
    if not _pack:
        return raw
    cached = _cache.get(raw)
    if cached is not None:
        return cached
    exact = _pack.get(raw)
    if exact is not None:
        _cache_set(raw, str(exact))
        return str(exact)
    # 模板匹配：归一化后查表（含路径/数字的日志消息在此命中）
    norm, vals = _normalize_template(raw)
    if norm != raw:
        tpl = _pack.get(norm)
        if tpl is not None:
            out = _restore_template(str(tpl), vals)
            _cache_set(raw, out)
            return out
    # 安全片段替换：仅当替换后整句无中文残留才采用（防中英混杂）
    if _ZH_RE.search(raw):
        out = _safe_fragment_replace(raw)
        if out is not None:
            _cache_set(raw, out)
            return out
    return raw


def _fragment_keys():
    """安全片段替换用的 key 列表：含中文、已翻译、按长度降序（懒构建）。"""
    global _frag_keys
    if _frag_keys is None:
        _frag_keys = sorted(
            (k for k, v in _pack.items()
             if len(k) >= 2 and _ZH_RE.search(k) and isinstance(v, str) and v != k),
            key=len, reverse=True)
    return _frag_keys


def _safe_fragment_replace(text):
    """安全片段替换：按 key 长度降序替换消息中的已知中文片段。

    替换完成后若仍有中文残留（说明存在语言包外的未知片段/变量），
    返回 None（调用方保持原文）——保证输出要么纯英文要么纯中文。
    """
    out = text
    for key in _fragment_keys():
        if len(key) <= len(out) and key in out:
            val = _pack[key]
            if isinstance(val, str):
                out = out.replace(key, val)
    if _ZH_RE.search(out):
        return None
    return out


def patch_input_dialogs():
    """包装 QInputDialog：标题/标签自动过 tr()（瞬态对话框不参与控件树遍历）。"""
    from PySide6.QtWidgets import QInputDialog
    for name in ("getText", "getItem", "getInt", "getDouble", "getMultiLineText"):
        orig = getattr(QInputDialog, name, None)
        if orig is None:
            continue

        def _make_wrapper(orig_fn):
            def wrapper(parent, title, label, *args, **kwargs):
                try:
                    return orig_fn(parent, tr(title), tr(label), *args, **kwargs)
                except Exception:
                    return orig_fn(parent, title, label, *args, **kwargs)
            wrapper.__name__ = orig_fn.__name__
            wrapper.__doc__ = orig_fn.__doc__
            return wrapper

        setattr(QInputDialog, name, _make_wrapper(orig))


def patch_message_boxes():
    """包装 QMessageBox 静态方法：弹窗标题/正文自动过 tr() 翻译。

    在应用启动时调用一次；现有所有 QMessageBox.question/information/
    warning/critical 调用点无需改动即获得翻译。签名与原始方法一致，
    仅对 parent/title/text 前三个参数做翻译。
    """
    from PySide6.QtWidgets import QMessageBox
    for name in ("question", "information", "warning", "critical"):
        orig = getattr(QMessageBox, name, None)
        if orig is None:
            continue

        def _make_wrapper(orig_fn):
            def wrapper(parent, title, text, *args, **kwargs):
                try:
                    return orig_fn(parent, tr(title), tr(text), *args, **kwargs)
                except Exception:
                    # 翻译异常不阻断弹窗（回退原文）
                    return orig_fn(parent, title, text, *args, **kwargs)
            wrapper.__name__ = orig_fn.__name__
            wrapper.__doc__ = orig_fn.__doc__
            return wrapper

        setattr(QMessageBox, name, _make_wrapper(orig))


def apply_language(widget):
    """遍历控件树刷新可见文案（按钮/标签/输入框占位/表头/下拉项/菜单）。

    动态创建的控件：创建时用 tr() 包文案，或切换语言后对窗口整体再调
    本函数。返回刷新过的控件数。
    """
    from PySide6.QtWidgets import (
        QWidget, QAbstractButton, QLabel, QLineEdit, QComboBox,
        QTableWidget, QTabWidget, QGroupBox, QProgressBar, QMenu,
    )
    count = 0

    def _refresh(w):
        nonlocal count
        # 标记 i18n_skip 的控件不翻（如语言选择下拉框本身）
        if isinstance(w, QWidget) and w.property("i18n_skip"):
            return
        if isinstance(w, QAbstractButton):
            t = render(w.text())
            if t != w.text():
                w.setText(t)
                count += 1
        if isinstance(w, QLabel):
            t = render(w.text())
            if t != w.text():
                w.setText(t)
                count += 1
        if isinstance(w, QLineEdit):
            p = render(w.placeholderText())
            if p != w.placeholderText():
                w.setPlaceholderText(p)
                count += 1
        if isinstance(w, QComboBox):
            for i in range(w.count()):
                item = w.itemText(i)
                t = render(item)
                if t != item:
                    w.setItemText(i, t)
                    count += 1
        if isinstance(w, QTableWidget):
            for col in range(w.columnCount()):
                item = w.horizontalHeaderItem(col)
                if item is not None:
                    t = render(item.text())
                    if t != item.text():
                        item.setText(t)
                        count += 1
        if isinstance(w, QTabWidget):
            # tab 标题（如 待迁移/已迁移/监控日志）——切换语言后同步刷新
            for i in range(w.count()):
                t = render(w.tabText(i))
                if t != w.tabText(i):
                    w.setTabText(i, t)
                    count += 1
        if isinstance(w, QGroupBox):
            t = render(w.title())
            if t != w.title():
                w.setTitle(t)
                count += 1
        if isinstance(w, QProgressBar):
            t = render(w.format())
            if t != w.format():
                w.setFormat(t)
                count += 1
        if isinstance(w, QMenu):
            for act in w.actions():
                t = render(act.text())
                if t != act.text():
                    act.setText(t)
                    count += 1
        # toolTip（静态设置的中文提示，切换语言后一并刷新）
        if isinstance(w, QWidget):
            tip = w.toolTip()
            if tip:
                t = render(tip)
                if t != tip:
                    w.setToolTip(t)
                    count += 1

    # BFS 遍历控件树（QObject.children 含布局项；菜单是独立 QWidget，
    # 需用 findChildren 单独收集）
    stack = [widget]
    seen = set()
    while stack:
        w = stack.pop()
        if w is None or id(w) in seen:
            continue
        seen.add(id(w))
        _refresh(w)
        for child in w.children():
            stack.append(child)
    for menu in widget.findChildren(QMenu):
        _refresh(menu)
    return count
