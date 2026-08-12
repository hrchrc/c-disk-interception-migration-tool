#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 识别 Handler（从 main.py 抽出）

包含：
- _online_search_descriptions：联网搜索补全空/敷衍说明
- _ai_recognize_descriptions：AI 大模型智能识别说明
- _open_ai_settings / _open_ai_settings_impl：AI 设置对话框
"""
import os
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QMessageBox, QDialog, QVBoxLayout, QFormLayout, QHBoxLayout,
    QComboBox, QLineEdit, QCheckBox, QSpinBox,
    QPushButton, QLabel, QGroupBox, QTextBrowser
)
from PySide6.QtGui import QColor

from config import (
    save_all, log_error_with_reason,
    load_ai_keys, save_ai_keys,
)

log = logging.getLogger('CDriveRelocator')


class AIHandler:
    """AI 识别相关方法 Handler"""

    def _online_search_descriptions(self):
        """联网搜索补全待迁移表中说明为空或敷衍的条目
        基于路径匹配而非行号匹配，允许与刷新同时进行（刷新重建表格后仍能正确更新）
        点击时显示进度条和当前搜索的软件名，非弹窗友好提示
        """
        # 防止重复启动：如果上一个联网线程还在运行，直接返回
        if hasattr(self, '_online_thread') and self._online_thread and self._online_thread.isRunning():
            self.status_label.setText("联网搜索正在进行中，请稍候...")
            return
        # 收集说明为空或敷衍的条目（路径+名称，不依赖行号）
        empty_items = []
        for row in range(self.table_scan.rowCount()):
            desc_item = self.table_scan.item(row, 4)
            name_item = self.table_scan.item(row, 3)
            path_item = self.table_scan.item(row, 0)
            desc_text = desc_item.text() if desc_item else ""
            # 收集：空描述 + 笼统描述 + 低置信度描述（含[?][~]标记）
            desc_clean = desc_text.replace('[?] ','').replace('[~] ','').replace('[系统] ','').strip()
            if (self._is_vague_desc(desc_text) or self._is_vague_desc(desc_clean)
                or '[?]' in desc_text or '[~]' in desc_text):
                name = name_item.text() if name_item else ""
                path = path_item.text() if path_item else ""
                if name:
                    empty_items.append((path, name))
        if not empty_items:
            self.status_label.setText("没有需要补全的条目（空或敷衍说明）")
            return
        # 只禁用联网按钮（不禁用刷新按钮，允许同时刷新）
        self.btn_online_search.setEnabled(False)
        # 显示进度条（非弹窗友好提示）
        total = len(empty_items)
        self.progress.setVisible(True)
        self.progress.setRange(0, total)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat(f"联网搜索中 0/{total} (%p%)")
        self.status_label.setText(f"联网搜索中... 共 {total} 个条目待补全")

        class OnlineSearchThread(QThread):
            progress = Signal(int, int, str, str)  # current, total, desc, name
            finished_search = Signal(dict)    # {path: desc}
            error_signal = Signal(str)
            def __init__(self, items):
                super().__init__()
                self.items = items
            def run(self):
                # 后台线程使用 win32com 必须初始化COM，否则可能segfault
                import pythoncom
                try:
                    pythoncom.CoInitialize()
                except Exception as e:
                    log.error(f"[联网补全] CoInitialize 失败: {e}")
                try:
                    from software_detect import search_online_description
                    from concurrent.futures import ThreadPoolExecutor, as_completed
                    import threading
                    results = {}
                    total = len(self.items)
                    completed_count = [0]  # 用list包装以便闭包修改
                    lock = threading.Lock()
                    # 并发搜索（10 并发，网络 IO 密集不会卡 CPU）
                    def search_one(item):
                        path, name = item
                        desc = search_online_description(name, path)
                        with lock:
                            completed_count[0] += 1
                            results[path] = desc
                        self.progress.emit(completed_count[0], total, desc or "", name)
                    with ThreadPoolExecutor(max_workers=10) as executor:
                        list(executor.map(search_one, self.items))
                    self.finished_search.emit(results)
                except BaseException as e:
                    import traceback
                    log.error(f"[联网补全] 线程异常: {e}\n{traceback.format_exc()}")
                    self.error_signal.emit(str(e))
                finally:
                    try:
                        pythoncom.CoUninitialize()
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
        def on_progress(cur, total, desc, name):
            # 进度条+状态栏双重友好提示（非弹窗）
            self.progress.setValue(cur)
            self.progress.setFormat(f"联网搜索 {cur}/{total} - {name} (%p%)")
            if desc:
                self.status_label.setText(f"已找到: {name} - {desc[:30]}{'...' if len(desc) > 30 else ''}")
            else:
                self.status_label.setText(f"搜索中: {name} ({cur}/{total})")
        def on_finished(results):
            updated = 0
            failed = 0
            from utils import is_system_path
            # 阻塞itemChanged信号，避免setText时反复触发_on_scan_item_changed导致重复保存
            self.table_scan.blockSignals(True)
            # 基于路径匹配查找表格行（即使表格被刷新重建也能正确找到）
            for path, desc in results.items():
                if not desc:
                    failed += 1
                    continue
                # 纯说明（通用去掉所有 [xxx] 前缀），用于存入scan_cache
                import re
                pure_desc = re.sub(r'^\[[^\]]*\]\s*', '', desc).strip()
                is_sys = is_system_path(path)
                for row in range(self.table_scan.rowCount()):
                    path_item = self.table_scan.item(row, 0)
                    if path_item and path_item.text() == path:
                        desc_item = self.table_scan.item(row, 4)
                        if desc_item:
                            # 系统文件保留[系统]前缀
                            display_text = ("[系统] " + pure_desc) if is_sys else pure_desc
                            desc_item.setText(display_text)
                            if is_sys:
                                desc_item.setToolTip("⚠ 系统重要文件，迁移可能导致系统异常\n" + pure_desc)
                            else:
                                desc_item.setToolTip(pure_desc)
                            # 标记为联网获取（紫色）
                            desc_item.setForeground(QColor("#6A1B9A"))
                            updated += 1
                        break
                # 同步更新scan_cache（只存纯说明）
                for s in self.cfg.get("scan_cache", []):
                    if s.get("path") == path:
                        s["desc"] = pure_desc
                        break
            self.table_scan.blockSignals(False)
            if results:
                save_all(self.cfg)
            self.progress.setVisible(False)
            # 状态栏显示成功+失败数
            self.status_label.setText(
                f"联网搜索完成: 成功 {updated} 个，失败 {failed} 个（双击说明列可手动编辑）")
            # 写入监控日志
            self.on_monitor_log("install", f"联网补全说明完成: 成功 {updated} 个，失败 {failed} 个")
            # 清理线程引用
            if self._online_thread:
                self._online_thread.quit()
                self._online_thread.wait(2000)
                self._online_thread = None
            self.btn_online_search.setEnabled(True)
        def on_error(err):
            # 英文原始错误保留（精确），错误日志同时记录
            log_error_with_reason("联网搜索失败", err, "OnlineSearchThread.run")
            self.progress.setVisible(False)
            # 状态栏显示英文错误（精确）
            self.status_label.setText(f"联网搜索失败: {err}")
            # 监控日志显示英文错误（精确）
            self.on_monitor_log("error", f"联网补全说明失败: {err}")
            if self._online_thread:
                self._online_thread.quit()
                self._online_thread.wait(2000)
                self._online_thread = None
            self.btn_online_search.setEnabled(True)
        self._online_thread = OnlineSearchThread(empty_items)
        self._online_thread.progress.connect(on_progress, Qt.QueuedConnection)
        self._online_thread.finished_search.connect(on_finished, Qt.QueuedConnection)
        self._online_thread.error_signal.connect(on_error, Qt.QueuedConnection)
        self._online_thread.start()

    def _ai_recognize_descriptions(self):
        """AI 智能识别补全说明（多平台支持，批量调用）
        收集所有 [~] [?] 空描述条目，批量发给大模型识别
        """
        # 防止重复启动
        if hasattr(self, '_ai_thread') and self._ai_thread and self._ai_thread.isRunning():
            self.status_label.setText("AI识别正在进行中，请稍候...")
            return

        # 检查配置
        ai_cfg = self.cfg.get("ai_recognize", {})
        if not ai_cfg.get("enabled"):
            QMessageBox.warning(self, "未启用", "请先在设置页启用 AI 识别并填入 API Key。")
            return
        # 从独立文件 ai_keys.json 取当前平台的 key（与 config.json 分离存储）
        cur_platform = ai_cfg.get("platform", "zhipu")
        api_keys = load_ai_keys()
        api_key = api_keys.get(cur_platform, "")
        if not api_key:
            QMessageBox.warning(self, "缺少Key", f"请先在设置页为 {cur_platform} 平台填入 API Key。")
            return

        # 收集需要识别的条目（[~] [?] 空 无法识别 模糊 都收集；[系统] 不收集）
        # 同时采集目录下的文件列表，一起发给 AI（像上传文件那样）
        empty_items = []
        for row in range(self.table_scan.rowCount()):
            desc_item = self.table_scan.item(row, 4)
            name_item = self.table_scan.item(row, 3)
            path_item = self.table_scan.item(row, 0)
            desc_text = desc_item.text() if desc_item else ""
            # 跳过系统文件
            path = path_item.text() if path_item else ""
            if path:
                from utils import is_system_path
                if is_system_path(path):
                    continue
            # 收集：空 + [~] + [?] + 无法识别 + 厂商容器 + 模糊描述
            desc_clean = desc_text.replace('[?] ', '').replace('[~] ', '').replace('[系统] ', '').strip()
            need_ai = (
                not desc_clean
                or '[?]' in desc_text
                or '[~]' in desc_text
                or desc_clean.startswith('无法识别')
                or '厂商容器目录' in desc_clean
                or '建议进入子目录' in desc_clean
                or self._is_vague_desc(desc_text)
                or self._is_vague_desc(desc_clean)
            )
            if need_ai:
                name = name_item.text() if name_item else ""
                dir_name = os.path.basename(path) if path else name
                if dir_name:
                    # 中危-8：os.scandir 文件列表采集移至 AI 线程内部执行
                    # 主线程只收集 (dir_name, path)，避免大量目录扫描卡顿 UI
                    # AI 线程 run() 开头会扫描文件列表构造三元组
                    empty_items.append((dir_name, path))

        if not empty_items:
            self.status_label.setText("没有需要AI识别的条目（空/[~]/[?] 说明）")
            return

        # 禁用按钮
        self.btn_ai_recognize.setEnabled(False)
        total = len(empty_items)
        self.progress.setVisible(True)
        self.progress.setRange(0, total)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat(f"AI识别中 0/{total} (%p%)")
        self.status_label.setText(f"AI识别中... 共 {total} 个条目，分批调用大模型")

        class AIRecognizeThread(QThread):
            progress = Signal(int, int, str, dict)  # current, total, batch_info, usage_info
            finished_search = Signal(dict, dict)     # {path: {name, desc, type}}, total_usage
            error_signal = Signal(str)
            _cancel = False  # 取消标志：清空缓存时设置为 True，线程下一批开始前退出

            def __init__(self, items, platform, api_key, batch_size, config_dir):
                super().__init__()
                # items 是三元组列表: [(dir_name, full_path, files_list), ...]
                self.items = items
                self.platform = platform
                self.api_key = api_key
                self.batch_size = batch_size
                self.config_dir = config_dir

            def cancel(self):
                """请求线程安全退出（下一批开始前检查）"""
                self._cancel = True

            def run(self):
                try:
                    from ai_recognizer import AIRecognizer
                    rec = AIRecognizer(
                        platform=self.platform,
                        api_key=self.api_key,
                        config_dir=self.config_dir,
                    )
                    # 覆盖默认批量大小
                    if self.batch_size and 5 <= self.batch_size <= 100:
                        rec.batch_size = self.batch_size

                    # 中危-8：在后台线程扫描每个目录的文件列表（最多 15 个）
                    # 主线程只传 (dir_name, path) 二元组，这里转成三元组
                    items = []
                    for entry in self.items:
                        if len(entry) == 3:
                            # 兼容旧调用方（已是三元组）
                            items.append(entry)
                        else:
                            dir_name, path = entry
                            files = []
                            try:
                                if path and os.path.isdir(path):
                                    for fe in os.scandir(path):
                                        if fe.is_file():
                                            files.append(fe.name)
                                            if len(files) >= 15:
                                                break
                            except Exception as e:
                                log.debug("忽略异常: %s", e)
                            items.append((dir_name, path, files))
                    self.items = items

                    # 分批识别，每批完成回调进度
                    all_results = {}
                    # 构造 dir_name → full_path 映射（用于结果回填）
                    path_map = {d: p for d, p, _ in self.items}

                    def progress_cb(cur, tot, batch_result, usage_info):
                        # usage_info: {prompt_tokens, completion_tokens, total_tokens, api_calls}
                        self.progress.emit(cur, tot, f"已识别 {cur}/{tot}", usage_info or {})

                    # 分批调用，支持 cancel
                    normalized = rec._normalize_items(self.items) if hasattr(rec, '_normalize_items') else self.items
                    todo, cached_results = rec._split_cache(normalized) if hasattr(rec, '_split_cache') else (normalized, {})
                    all_results.update(cached_results)
                    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "api_calls": 0}

                    total = len(todo)
                    for i in range(0, total, rec.batch_size):
                        # 检查取消标志
                        if self._cancel:
                            log.info(f"[AI识别] 收到取消信号，已完成 {i}/{total}，退出")
                            break
                        batch = todo[i : i + rec.batch_size]
                        try:
                            batch_result, batch_usage = rec._identify_batch(batch)
                            all_results.update(batch_result)
                            with rec._cache_lock:
                                rec._cache.update(batch_result)
                            rec._save_cache()
                        except Exception as e:
                            err_msg = f"API调用失败: {type(e).__name__}: {str(e)[:100]}"
                            for d, _, _ in batch:
                                all_results[d] = {"name": "", "desc": "", "type": "未知", "error": err_msg}
                        total_usage["prompt_tokens"] += batch_usage.get("prompt_tokens", 0)
                        total_usage["completion_tokens"] += batch_usage.get("completion_tokens", 0)
                        total_usage["total_tokens"] += batch_usage.get("total_tokens", 0)
                        total_usage["api_calls"] += 1
                        if progress_cb:
                            progress_cb(min(i + rec.batch_size, total), total, {}, total_usage)
                        # 礼貌间隔
                        if i + rec.batch_size < total and not self._cancel:
                            import time as _time
                            _time.sleep(max(2.0, rec.rate_limit_ms / 1000.0))

                    # 按路径整理结果
                    final_results = {}
                    for d, info in all_results.items():
                        p = path_map.get(d)
                        if p:
                            final_results[p] = info
                    self.finished_search.emit(final_results, total_usage)
                except BaseException as e:
                    import traceback
                    log.error(f"[AI识别] 线程异常: {e}\n{traceback.format_exc()}")
                    self.error_signal.emit(str(e))

        def on_progress(cur, total, info, usage_info):
            # 如果线程已被取消，不更新 UI
            if not getattr(self, '_ai_thread', None):
                return
            self.progress.setValue(cur)
            self.progress.setFormat(f"AI识别 {cur}/{total} (%p%)")
            # 动态显示当前累计 token 消耗
            tok_total = usage_info.get("total_tokens", 0) if usage_info else 0
            tok_prompt = usage_info.get("prompt_tokens", 0) if usage_info else 0
            tok_comp = usage_info.get("completion_tokens", 0) if usage_info else 0
            api_calls = usage_info.get("api_calls", 0) if usage_info else 0
            self.status_label.setText(
                f"AI识别中 {cur}/{total} - {info} | 已调用API {api_calls}次 | "
                f"累计 tokens: {tok_total:,}（输入{tok_prompt:,} + 输出{tok_comp:,}）"
            )

        def on_finished(results, total_usage):
            # 检查线程是否已被取消（清空缓存时会设置 _ai_thread = None）
            # 如果已被取消，丢弃这次结果，不更新表格
            if not getattr(self, '_ai_thread', None):
                log.info("[AI识别] 线程已被取消（清空缓存），丢弃结果")
                self.progress.setVisible(False)
                self.btn_ai_recognize.setEnabled(True)
                return
            updated = 0
            failed = 0
            from utils import is_system_path
            self.table_scan.blockSignals(True)
            for path, info in results.items():
                desc = info.get("desc", "").strip()
                if not desc or info.get("type") == "未知":
                    failed += 1
                    continue
                # 拼接：软件名 + 用途说明（如果 desc 已含软件名就不重复）
                name = info.get("name", "").strip()
                pure_desc = desc
                # 系统文件保留前缀
                is_sys = is_system_path(path)
                for row in range(self.table_scan.rowCount()):
                    path_item = self.table_scan.item(row, 0)
                    if path_item and path_item.text() == path:
                        desc_item = self.table_scan.item(row, 4)
                        if desc_item:
                            display_text = ("[系统] " + pure_desc) if is_sys else pure_desc
                            desc_item.setText(display_text)
                            tip = pure_desc
                            if info.get("type") and info["type"] != "未知":
                                tip = f"[{info['type']}] {pure_desc}"
                            desc_item.setToolTip(tip)
                            # 标记为AI识别（蓝色）
                            desc_item.setForeground(QColor("#1565C0"))
                            updated += 1
                        break
                # 同步更新 scan_cache
                for s in self.cfg.get("scan_cache", []):
                    if s.get("path") == path:
                        s["desc"] = pure_desc
                        break
            self.table_scan.blockSignals(False)
            if results:
                save_all(self.cfg)
            self.progress.setVisible(False)
            # 最终状态栏显示：包含 token 统计
            tok_total = total_usage.get("total_tokens", 0)
            tok_prompt = total_usage.get("prompt_tokens", 0)
            tok_comp = total_usage.get("completion_tokens", 0)
            api_calls = total_usage.get("api_calls", 0)
            # 缓存命中数 = 总数 - API 调用次数 × batch_size（粗略）
            cache_hit = max(0, len(results) - updated - failed) if (updated + failed) > 0 else 0
            self.status_label.setText(
                f"AI识别完成: 成功 {updated} 个，未识别 {failed} 个 | "
                f"API调用 {api_calls} 次 | 总消耗 {tok_total:,} tokens（输入{tok_prompt:,} + 输出{tok_comp:,}）"
            )
            self.on_monitor_log(
                "install",
                f"AI智能识别完成: 成功{updated}个，未识别{failed}个，API调用{api_calls}次，消耗{tok_total}tokens"
            )
            if hasattr(self, '_ai_thread') and self._ai_thread:
                self._ai_thread.quit()
                self._ai_thread.wait(2000)
                self._ai_thread = None
            self.btn_ai_recognize.setEnabled(True)

        def on_error(err):
            log_error_with_reason("AI识别失败", err, "AIRecognizeThread.run")
            self.progress.setVisible(False)
            self.status_label.setText(f"AI识别失败: {err}")
            self.on_monitor_log("error", f"AI智能识别失败: {err}")
            if hasattr(self, '_ai_thread') and self._ai_thread:
                self._ai_thread.quit()
                self._ai_thread.wait(2000)
                self._ai_thread = None
            self.btn_ai_recognize.setEnabled(True)

        # 本文件位于 src/ui/ui_ai.py，向上3级到项目根目录
        config_dir = str(Path(__file__).parent.parent.parent)
        batch_size = ai_cfg.get("batch_size", 20)
        self._ai_thread = AIRecognizeThread(
            empty_items, cur_platform,
            api_key, batch_size, config_dir
        )
        self._ai_thread.progress.connect(on_progress, Qt.QueuedConnection)
        self._ai_thread.finished_search.connect(on_finished, Qt.QueuedConnection)
        self._ai_thread.error_signal.connect(on_error, Qt.QueuedConnection)
        self._ai_thread.start()

    def _open_ai_settings(self):
        """打开 AI 识别设置对话框"""
        try:
            return self._open_ai_settings_impl()
        except BaseException as e:
            import traceback
            log.error(f"[AI设置] 打开失败: {e}\n{traceback.format_exc()}")
            QMessageBox.critical(self, "打开失败", f"AI设置对话框打开失败：\n\n{type(e).__name__}: {e}\n\n请查看日志文件获取详细信息。")

    def _open_ai_settings_impl(self):
        """AI 识别设置对话框实现"""
        from ai_recognizer import PLATFORMS, AIRecognizer

        dlg = QDialog(self)
        dlg.setWindowTitle("AI 识别设置")
        dlg.setMinimumWidth(520)
        layout = QVBoxLayout(dlg)

        ai_cfg = self.cfg.get("ai_recognize", {})

        # 从独立文件 ai_keys.json 读取所有平台的 key（update_info 闭包会用到，必须提前定义）
        api_keys = load_ai_keys()

        # 平台选择
        platform_group = QGroupBox("选择 AI 平台")
        platform_layout = QVBoxLayout(platform_group)
        platform_combo = QComboBox()
        for pid, p in PLATFORMS.items():
            # 只显示平台名，不加任何后缀/宣传词（禁止擅自推荐）
            platform_combo.addItem(p["name"], pid)
        # 选中当前平台
        cur_platform = ai_cfg.get("platform", "zhipu")
        for i in range(platform_combo.count()):
            if platform_combo.itemData(i) == cur_platform:
                platform_combo.setCurrentIndex(i)
                break
        platform_layout.addWidget(platform_combo)

        # 平台详情
        info_label = QLabel()
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #546E7A; font-size: 11px; padding: 4px;")
        platform_layout.addWidget(info_label)

        # 先创建 key_input（update_info 闭包会用到，必须提前创建）
        key_input = QLineEdit()
        key_input.setEchoMode(QLineEdit.Password)
        key_input.setPlaceholderText("粘贴你的 API Key（仅保存在本地 ai_keys.json，清空缓存不会丢失）")
        # 加载当前平台的 key
        cur_platform_key = api_keys.get(cur_platform, "")
        key_input.setText(cur_platform_key)

        def update_info(idx):
            pid = platform_combo.itemData(idx)
            p = PLATFORMS[pid]
            proxy_tag = "（需翻墙）" if p["need_proxy"] else ""
            info_label.setText(
                f"模型: {p['model']}\n"
                f"注册地址: {p['signup_url']}\n"
                f"文档: {p['doc_url']}"
                + (f"\n{proxy_tag}" if proxy_tag else "")
            )
            # 切换平台时自动加载该平台保存的 key
            saved_key = api_keys.get(pid, "")
            key_input.setText(saved_key)
        platform_combo.currentIndexChanged.connect(update_info)
        update_info(platform_combo.currentIndex())
        layout.addWidget(platform_group)

        # API Key（每个平台独立保存）
        key_group = QGroupBox("API Key（每个平台独立保存，切换平台不丢失）")
        key_layout = QVBoxLayout(key_group)
        key_layout.addWidget(key_input)
        key_row = QHBoxLayout()
        show_btn = QPushButton("显示/隐藏")
        show_btn.setCheckable(True)
        def toggle_show(checked):
            key_input.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        show_btn.toggled.connect(toggle_show)
        key_row.addWidget(show_btn)
        open_signup_btn = QPushButton("去注册获取 Key")
        def open_signup():
            pid = platform_combo.currentData()
            url = PLATFORMS[pid]["signup_url"]
            import webbrowser
            webbrowser.open(url)
        open_signup_btn.clicked.connect(open_signup)
        key_row.addWidget(open_signup_btn)
        test_btn = QPushButton("测试连接")
        def test_conn():
            pid = platform_combo.currentData()
            key = key_input.text().strip()
            if not key:
                QMessageBox.warning(dlg, "缺Key", "请先填入 API Key")
                return
            test_btn.setEnabled(False)
            test_btn.setText("测试中...")
            dlg.repaint()
            try:
                # 本文件位于 src/ui/ui_ai.py，向上3级到项目根目录
                rec = AIRecognizer(platform=pid, api_key=key, config_dir=str(Path(__file__).parent.parent.parent))
                ok, msg = rec.test_connection()
                if ok:
                    QMessageBox.information(dlg, "成功", f"连接成功！\n\n模型回复: {msg}")
                else:
                    QMessageBox.warning(dlg, "失败", f"连接失败：\n\n{msg}")
            except Exception as e:
                QMessageBox.warning(dlg, "异常", f"{type(e).__name__}: {e}")
            finally:
                test_btn.setEnabled(True)
                test_btn.setText("测试连接")
        test_btn.clicked.connect(test_conn)
        key_row.addWidget(test_btn)
        key_row.addStretch(1)
        key_layout.addLayout(key_row)
        layout.addWidget(key_group)

        # 选项
        opt_group = QGroupBox("识别选项")
        opt_layout = QFormLayout(opt_group)
        enable_check = QCheckBox("启用 AI 识别（不启用则按钮无效）")
        enable_check.setChecked(ai_cfg.get("enabled", False))
        opt_layout.addRow(enable_check)
        auto_fill_check = QCheckBox("扫描时自动补全空说明（首扫会变慢，默认关闭）")
        auto_fill_check.setChecked(ai_cfg.get("auto_fill_on_scan", False))
        opt_layout.addRow(auto_fill_check)
        batch_spin = QSpinBox()
        batch_spin.setRange(5, 100)
        batch_spin.setValue(ai_cfg.get("batch_size", 20))
        batch_spin.setToolTip("每批发送给大模型的目录数量，建议 10-50")
        opt_layout.addRow("每批识别数量:", batch_spin)
        layout.addWidget(opt_group)

        # 说明
        tip = QLabel(
            "说明：AI 识别会批量调用大模型 API，每次扫描后点击 \"AI智能识别\" 按钮补全空说明。\n"
            "API Key 只保存在本地 config.json，不会上传到任何服务器。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #1565C0; font-size: 11px; padding: 6px;")
        layout.addWidget(tip)

        # 按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(dlg.reject)
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        if dlg.exec() == QDialog.Accepted:
            # 读取控件值（在对话框销毁前先取出来）
            saved_platform = platform_combo.currentData()
            saved_key = key_input.text().strip()
            saved_enabled = enable_check.isChecked()
            saved_auto_fill = auto_fill_check.isChecked()
            saved_batch = batch_spin.value()

            # 保存配置（API Key 独立存到 ai_keys.json，其他配置存 config.json）
            if "ai_recognize" not in self.cfg or not isinstance(self.cfg.get("ai_recognize"), dict):
                self.cfg["ai_recognize"] = {}
            # 更新当前平台的 key 到独立文件（其他平台的 key 保留不动）
            all_keys = load_ai_keys()
            all_keys[saved_platform] = saved_key
            save_ai_keys(all_keys)
            # 其他配置（非敏感）存到 config.json
            self.cfg["ai_recognize"]["platform"] = saved_platform
            self.cfg["ai_recognize"]["enabled"] = saved_enabled
            self.cfg["ai_recognize"]["auto_fill_on_scan"] = saved_auto_fill
            self.cfg["ai_recognize"]["batch_size"] = saved_batch
            self.cfg["ai_recognize"]["last_used_at"] = ""

            # 立即写 config.json（非敏感配置）
            save_all(self.cfg)

            # 读回 ai_keys.json 验证 API Key 是否真的写入了
            verify_ok = False
            verify_msg = ""
            try:
                verify_keys = load_ai_keys()
                if verify_keys.get(saved_platform) == saved_key:
                    verify_ok = True
                else:
                    verify_msg = f"写入后读回不符：期望 key={'***' if saved_key else '(空)'}, 实际 key={'***' if verify_keys.get(saved_platform) else '(空)'}"
            except Exception as e:
                verify_msg = f"读回验证失败: {type(e).__name__}: {e}"

            if verify_ok:
                # 统计已配置 key 的平台数
                configured_count = sum(1 for v in all_keys.values() if v)
                QMessageBox.information(
                    self, "保存成功",
                    f"AI 设置已保存\n\n"
                    f"API Key 存到: ai_keys.json（清空缓存不会丢失）\n"
                    f"其他配置存到: config.json\n\n"
                    f"当前平台: {PLATFORMS[saved_platform]['name']}\n"
                    f"API Key: {'已填入(' + str(len(saved_key)) + '字符)' if saved_key else '未填'}\n"
                    f"启用: {'是' if saved_enabled else '否'}\n"
                    f"每批数量: {saved_batch}\n"
                    f"已配置 Key 的平台数: {configured_count}/7"
                )
                self.status_label.setText(
                    f"AI 设置已保存：{PLATFORMS[saved_platform]['name']}"
                    f"（{'已启用' if saved_enabled else '未启用'}）"
                )
                self.on_monitor_log(
                    "install",
                    f"AI识别设置已保存: 平台={saved_platform}, 启用={saved_enabled}, key长度={len(saved_key)}"
                )
            else:
                QMessageBox.critical(
                    self, "保存失败",
                    f"保存 API Key 到 ai_keys.json 失败！\n\n"
                    f"原因: {verify_msg}\n\n"
                    f"可能原因：\n"
                    f"1. ai_keys.json 被其他程序占用\n"
                    f"2. 磁盘空间不足\n"
                    f"3. 权限不足\n"
                    f"请关闭其他可能占用 ai_keys.json 的程序后重试。"
                )
                log_error_with_reason(
                    "AI设置保存失败", verify_msg,
                    f"_open_ai_settings: platform={saved_platform}, key_len={len(saved_key)}"
                )
