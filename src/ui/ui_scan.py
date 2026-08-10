#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""待迁移/已迁移扫描与表格刷新 Handler（从 main.py 抽出）

包含 16 个方法：
- _async_fill_empty_desc: 异步填充空说明（含 DescFillThread 嵌套类）
- refresh: 全量刷新待迁移表
- _update_scan_progress: 更新扫描进度
- on_scan_finished: 扫描完成回调
- _start_migrated_size_calc: 启动已迁移大小计算（含 SizeCalcThread 嵌套类）
- _on_size_calculated: 大小计算完成回调
- _on_search_files: 搜索文件（含 SearchThread 嵌套类）
- on_scan_error: 扫描错误回调
- smart_refresh_scan: 智能刷新扫描
- _update_smart_progress: 更新智能扫描进度
- on_smart_scan_finished: 智能扫描完成回调
- on_smart_scan_error: 智能扫描错误回调
- _on_scan_item_changed: 待迁移表项变化处理
- _scan_context_menu: 待迁移表右键菜单
- _light_refresh_scan_table: 轻量刷新待迁移表
- _refresh_migrated_only: 仅刷新已迁移表（含 MigratedScanThread 嵌套类）

这些方法原属 MainWindow，抽取为 Handler 以降低 main.py 体量。
方法内通过 self 访问 MainWindow 的属性和其他方法，运行时由 MainWindow 提供。

依赖的 MainWindow 属性：
- self.cfg                  配置字典
- self.migrator             Migrator 实例
- self.table_scan           待迁移表格控件
- self.table_migrated       已迁移表格控件
- self._refresh_migrated_only()  自身方法（递归调用）
"""
import os
import sys
import json
import time
import logging
import subprocess
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QThread, QTimer, QUrl
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QCheckBox, QLabel, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QMenu, QFileDialog, QProgressBar, QFrame,
)
from PySide6.QtGui import QColor, QAction, QDesktopServices

# 根据方法实际用到的 import 补充以下引用（按需）：
from config import (
    log_link_operation, log_error_with_reason, KNOWN_SOFTWARE_DIRS, save_all,
)
from utils import (
    is_symlink, get_symlink_target, get_dir_size_fast,
    get_exe_version_info, _read_lnk_target,
)
from software_detect import get_dir_description
from migrator import Migrator
from monitor import ScanWorker, SmartScanWorker
from ui_widgets import (
    NumericTableWidgetItem, WideEditorDelegate, _format_size, _apply_size_item_color,
)

log = logging.getLogger('CDriveRelocator')


class ScanHandler:
    """待迁移/已迁移扫描与表格刷新 Handler"""

    def _async_fill_empty_desc(self):
        """异步补全空desc（后台线程，不阻塞UI）

        策略：扫描完成后，对 desc 为空的目录调用 get_dir_description 识别，
        识别结果同时写入 scan_cache 和 desc_cache（持久化到 config.json）。
        下次扫描时 desc_cache 命中则直接返回，无需再识别。

        注意：识别失败（返回空字符串）的目录也写入 desc_cache（值为空字符串），
        作为"已尝试识别"标记，避免下次启动重复识别同样的无法识别目录。
        联网搜索按钮仍可手动触发对这些空 desc 的补全。
        """
        cache = self.cfg.get("scan_cache", [])
        desc_cache = self.cfg.get("desc_cache", {}) or {}
        # 只对 scan_cache 中 desc 为空 且 不在 desc_cache 中（未尝试过）的目录补全
        empty_paths = [item["path"] for item in cache
                       if not item.get("desc") and item["path"] not in desc_cache]
        if not empty_paths:
            return
        # 用独立 logger 写文件，确保即使全局 log 未配置 handler 也能落盘
        import logging as _logging
        _dbg = _logging.getLogger("CDriveRelocator.async_desc")
        if not _dbg.handlers:
            from config import LOG_FILE
            _fh = _logging.FileHandler(str(LOG_FILE), encoding="utf-8")
            _fh.setFormatter(_logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            _dbg.addHandler(_fh)
            _dbg.setLevel(_logging.INFO)  # INFO级别，进度信息用DEBUG不记录
            _dbg.propagate = False  # 防止消息冒泡到父 logger CDriveRelocator 导致重复打印
        # 防止并发崩溃：如果上一个 desc 补全线程还在运行，直接跳过本次
        # （desc_cache 已在累积，下次扫描命中更多，无需强行重跑；
        #  两个线程并发调用 win32com/ctypes 会引发 C 层 segfault）
        if hasattr(self, '_desc_fill_thread') and self._desc_fill_thread:
            if self._desc_fill_thread.isRunning():
                _dbg.info("[异步补全] 上一个线程还在运行，跳过本次补全")
                return
            self._desc_fill_thread = None
        _dbg.info(f"[异步补全] 启动线程，待识别 {len(empty_paths)} 个目录")
        from PySide6.QtCore import QThread, Signal
        class DescFillThread(QThread):
            filled = Signal(str, str)  # path, desc
            done = Signal(int)  # count
            error_signal = Signal(str, str)  # path, error
            log_signal = Signal(str)  # 窗口日志输出（实时显示进度）
            def __init__(self, paths):
                super().__init__()
                self.paths = paths
                self._cancel = False  # 取消标志（清空缓存时设 True，让 run() 主动退出）
            def cancel(self):
                """请求线程取消（清空缓存时调用，避免 wait 超时后回调访问已清空数据）"""
                self._cancel = True
            def run(self):
                import time as _time
                import threading as _threading
                _t0 = _time.perf_counter()
                _dbg.info("[异步补全] 线程 run() 进入")
                self.log_signal.emit(f"[异步补全] 启动：待识别 {len(self.paths)} 个目录（串行+5秒超时）")
                # 后台线程使用 win32com（lnk读取、WMI查询）必须初始化COM，否则segfault
                import pythoncom
                try:
                    pythoncom.CoInitialize()
                    _dbg.info("[异步补全] CoInitialize 成功")
                except BaseException as e:
                    _dbg.error(f"[异步补全] CoInitialize 失败: {e}")
                count = 0
                _succ = 0
                _total = len(self.paths)
                _max_dur = 0.0
                _max_path = ""
                _sum_dur = 0.0
                _timeout_count = 0
                try:
                    from software_detect import get_dir_description as _gdd

                    def _identify_with_timeout(path, timeout=5.0):
                        """单条识别 + 超时保护
                        串行下每条单独起子线程执行，join(timeout) 控制单条最长 5 秒
                        超时返回空字符串（这条留空，下次扫描时再识别或联网兜底）
                        """
                        result = {"desc": "", "error": None}
                        def _worker():
                            try:
                                result["desc"] = _gdd(path)
                            except BaseException as e:
                                result["error"] = e
                        t = _threading.Thread(target=_worker, daemon=True)
                        t.start()
                        t.join(timeout)
                        if t.is_alive():
                            return None  # 超时
                        if result["error"]:
                            raise result["error"]
                        return result["desc"]

                    for path in self.paths:
                        # 取消检查（清空缓存时设 _cancel=True，避免回调访问已清空数据）
                        if self._cancel:
                            self.log_signal.emit(f"[异步补全] 收到取消请求，剩余 {_total - count} 条跳过")
                            _dbg.info(f"[异步补全] 收到取消，已处理 {count}/{_total}")
                            break
                        _t_start = _time.perf_counter()
                        try:
                            desc = _identify_with_timeout(path, timeout=5.0)
                            _dur = _time.perf_counter() - _t_start
                            count += 1
                            _sum_dur += _dur
                            if _dur > _max_dur:
                                _max_dur = _dur
                                _max_path = path
                            if desc is None:
                                _timeout_count += 1
                                _dbg.warning(f"[异步补全] 单条超时 (>5s): {path}")
                                self.log_signal.emit(
                                    f"[异步补全] 超时跳过 ({count}/{_total}): {path}")
                            else:
                                if desc:
                                    # 再次检查取消（5秒超时期间可能已收到取消）
                                    if self._cancel:
                                        self.log_signal.emit(f"[异步补全] 取消，丢弃最后一条结果")
                                        break
                                    self.filled.emit(path, desc)
                                    _succ += 1
                                else:
                                    # 识别返回空字符串（无法识别）：
                                    # 也 emit filled 写入 desc_cache（空字符串作为"已尝试"标记），
                                    # 避免下次启动重复识别同样的无法识别目录
                                    if not self._cancel:
                                        self.filled.emit(path, "")
                            # 每 10 条或最后一条输出进度
                            if count % 10 == 0 or count == _total:
                                _avg = _sum_dur / count
                                self.log_signal.emit(
                                    f"[异步补全] 进度 {count}/{_total} - "
                                    f"累计 {_sum_dur:.1f}s 平均 {_avg*1000:.0f}ms/条"
                                    f" 最慢 {_max_dur*1000:.0f}ms"
                                    f" 超时 {_timeout_count}")
                        except BaseException as e:
                            import traceback
                            _dbg.error(f"[异步补全] 识别异常 {path}: {e}\n{traceback.format_exc()}")
                            self.error_signal.emit(path, str(e))
                            count += 1
                    _elapsed = _time.perf_counter() - _t0
                    _avg = _sum_dur / count if count else 0
                    _summary = (f"[异步补全] 完成：{_succ}/{_total} 成功 - "
                                f"总耗时 {_elapsed:.2f}s（串行墙钟）"
                                f" 累计 CPU {_sum_dur:.2f}s 平均 {_avg*1000:.0f}ms/条"
                                f" 最慢 {_max_dur*1000:.0f}ms"
                                f" 超时 {_timeout_count} 条")
                    self.log_signal.emit(_summary)
                    _dbg.info(f"[异步补全] 线程完成，成功 {_succ}/{_total}, 耗时 {_elapsed:.2f}s")
                    self.done.emit(count)
                except BaseException as e:
                    import traceback
                    _dbg.error(f"[异步补全] 线程级异常: {e}\n{traceback.format_exc()}")
                    self.done.emit(count)
                finally:
                    try:
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass
        def on_filled(path, desc):
            # 更新 scan_cache 和 desc_cache
            try:
                # desc_cache 始终写入（空字符串也写，作为"已尝试识别"标记）
                if "desc_cache" not in self.cfg:
                    self.cfg["desc_cache"] = {}
                self.cfg["desc_cache"][path] = desc
                # 空字符串不更新 scan_cache 和表格（保持空，留给联网搜索兜底）
                if not desc:
                    return
                for s in self.cfg.get("scan_cache", []):
                    if s.get("path") == path:
                        s["desc"] = desc
                        break
                # 更新表格（基于路径匹配，加防护避免表格被刷新重建时访问无效item）
                row_count = self.table_scan.rowCount()
                for row in range(row_count):
                    try:
                        path_item = self.table_scan.item(row, 0)
                        if path_item and path_item.text() == path:
                            desc_item = self.table_scan.item(row, 4)
                            if desc_item:
                                # 去掉所有 [xxx] 前缀后判断是否为空（[~] [?] [系统] 都视为待填充）
                                import re as _re_fill
                                cur_text = desc_item.text() or ""
                                cur_clean = _re_fill.sub(r'^\[[^\]]*\]\s*', '', cur_text).strip()
                                if not cur_clean:
                                    # 保留系统文件的 [系统] 前缀
                                    from utils import is_system_path
                                    if is_system_path(path):
                                        desc_item.setText("[系统] " + desc)
                                    else:
                                        desc_item.setText(desc)
                                    desc_item.setToolTip(desc)
                            break
                    except Exception:
                        break  # 表格正在被重建，放弃本次更新
            except Exception as e:
                _dbg.error(f"[异步补全] on_filled 异常 {path}: {e}")
        def on_done(count):
            try:
                save_all(self.cfg)
                self.on_monitor_log("init", f"异步补全完成: 处理 {count} 个目录（成功识别的已写入说明，无法识别的已标记跳过）")
                _dbg.info(f"[异步补全] on_done 完成，保存配置")
            except Exception as e:
                _dbg.error(f"[异步补全] on_done 异常: {e}")
        self._desc_fill_thread = DescFillThread(empty_paths)
        self._desc_fill_thread.filled.connect(on_filled, Qt.QueuedConnection)
        self._desc_fill_thread.done.connect(on_done, Qt.QueuedConnection)
        self._desc_fill_thread.log_signal.connect(
            lambda msg: self.on_monitor_log("init", msg), Qt.QueuedConnection)
        self._desc_fill_thread.start()

    def refresh(self):
        """异步刷新 - 不阻塞UI，真实进度条
        进度通过QTimer轮询ScanWorker共享变量，避免跨线程信号队列堆积导致进度条不动
        """
        # 用_busy标志位覆盖整个生命周期（启动→完成回调），防止连续点击绕过isRunning检查
        if getattr(self, '_scan_busy', False):
            return
        if self.scan_thread and self.scan_thread.isRunning():
            return  # 正在扫描中，跳过
        # 扫描线程之间互斥（全盘扫描和智能刷新不能同时进行），但允许与联网搜索同时进行
        if self.smart_scan_thread and self.smart_scan_thread.isRunning():
            return
        self._scan_busy = True
        # 禁用扫描按钮（不禁用联网搜索按钮，允许同时进行）
        self.btn_refresh.setEnabled(False)
        self.btn_refresh_scan.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)  # 确定进度模式
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("准备扫描... %p%")
        self.status_label.setText("扫描中...")

        # 记录本次扫描前 MFT 是否已加载（用于进度条映射）
        from utils import get_mft_scanner
        _scanner = get_mft_scanner()
        self._mft_loaded_this_scan = (_scanner is not None and _scanner._loaded)

        self.scan_thread = QThread()
        self.scan_worker = ScanWorker(self.migrator)
        self.scan_worker.moveToThread(self.scan_thread)
        self.scan_thread.started.connect(self.scan_worker.run)
        # 状态栏文字（低频）仍用信号，进度条用QTimer轮询
        self.scan_worker.progress_signal.connect(
            lambda msg: self.status_label.setText(msg), Qt.QueuedConnection)
        self.scan_worker.finished_signal.connect(self.on_scan_finished, Qt.QueuedConnection)
        self.scan_worker.error_signal.connect(self.on_scan_error, Qt.QueuedConnection)
        self.scan_thread.start()

        # QTimer轮询后台线程进度共享变量（主线程主动拉取，完全绕过跨线程信号队列）
        self._last_progress_pct = -1
        self._progress_timer = QTimer(self)
        self._progress_timer.timeout.connect(self._update_scan_progress)
        self._progress_timer.start(100)  # 每100ms轮询一次

    def _update_scan_progress(self):
        """QTimer回调 - 主线程轮询后台扫描进度，直接更新进度条"""
        if not self.scan_worker:
            return
        try:
            # MFT 加载阶段（首次刷新时加载 MFT 索引，占进度条 0-90%）
            if getattr(self.scan_worker, 'mft_loading', False):
                mft_current = self.scan_worker.mft_current
                mft_total = self.scan_worker.mft_total
                mft_message = self.scan_worker.mft_message or "加载 MFT 索引"
                if mft_total > 0:
                    pct = int(mft_current * 100 / mft_total)
                    display_pct = min(int(pct * 0.9), 90)
                    self.progress.setValue(display_pct)
                    self.progress.setFormat(f"{mft_message} {mft_current}/{mft_total} (%p%)")
                    self.status_label.setText(f"{mft_message} {mft_current}/{mft_total}")
                else:
                    self.progress.setFormat(f"{mft_message}... (%p%)")
                    self.status_label.setText(f"{mft_message}...")
                return

            # 扫描阶段（MFT 加载完后占 90-100%，未加载 MFT 时占 0-100%）
            current = self.scan_worker.current
            total = self.scan_worker.total
            dir_name = self.scan_worker.dir_name
            if total > 0:
                # 检测本次扫描是否走过 MFT 加载阶段
                if getattr(self, '_mft_loaded_this_scan', False):
                    pct = 90 + int(current * 10 / total)
                    pct = min(pct, 99)
                else:
                    pct = int(current * 100 / total)
                if pct != self._last_progress_pct:
                    self._last_progress_pct = pct
                    self.progress.setValue(pct)
                    self.progress.setFormat(f"扫描中 {current}/{total} - {dir_name} (%p%)")
                    self.status_label.setText(f"扫描中 {current}/{total} - {dir_name}")
        except Exception as e:
            log_error_with_reason("未知错误", str(e), "_update_scan_progress")
            log.error(f"_update_scan_progress异常: {e}")

    def on_scan_finished(self, migrated, scanned):
        """扫描完成回调（主线程）"""
        # 停止进度轮询
        if hasattr(self, '_progress_timer'):
            self._progress_timer.stop()
        # 读取扫描耗时（由 ScanWorker 记录）
        elapsed = getattr(self.scan_worker, 'scan_elapsed', 0) if self.scan_worker else 0
        # 填充前关闭排序，避免插入单元格时Qt重排行序导致数据错位
        self.table_migrated.setSortingEnabled(False)
        self.table_scan.setSortingEnabled(False)
        # 不使用setUpdatesEnabled(False)：会导致部分单元格内容不显示（Qt已知bug）
        # 改用blockSignals阻塞itemChanged信号
        self.table_migrated.blockSignals(True)
        self.table_scan.blockSignals(True)
        # 填充已迁移表
        self.table_migrated.setRowCount(0)
        # 状态映射：文本 + 颜色 + 说明
        status_map = {
            "OK":         ("正常",     "#2E7D32", "符号链接有效，数据在目标盘"),
            "BROKEN":     ("断链",     "#C62828", "C盘路径被软件覆盖为真实目录，点击右键修复"),
            "MISSING":    ("丢失",     "#EF6C00", "C盘路径不存在，点击右键修复（直接创建链接）"),
            "TARGET_GONE":("目标丢失", "#B71C1C", "目标盘数据不存在，需还原或重新迁移"),
        }
        for m in migrated:
            row = self.table_migrated.rowCount()
            self.table_migrated.insertRow(row)
            item0 = QTableWidgetItem(m["src"])
            item0.setToolTip(m["src"])
            self.table_migrated.setItem(row, 0, item0)
            item1 = QTableWidgetItem(m["dst"])
            item1.setToolTip(m["dst"])
            self.table_migrated.setItem(row, 1, item1)
            si = NumericTableWidgetItem(_format_size(m["size_mb"]))
            si.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            si.setData(Qt.UserRole, float(m["size_mb"]))
            _apply_size_item_color(si, m["size_mb"])
            self.table_migrated.setItem(row, 2, si)
            # 状态列：4种状态 + 颜色 + ToolTip说明
            status_text, status_color, status_tip = status_map.get(
                m["status"], ("未知", "#424242", ""))
            # i18n：状态词渲染时翻译（切换语言后下次表格刷新生效）
            from i18n import tr
            status_text = tr(status_text)
            status_tip = tr(status_tip)
            st = QTableWidgetItem(status_text)
            st.setForeground(QColor(status_color))
            st.setToolTip(status_tip)
            self.table_migrated.setItem(row, 3, st)
            # 链接目标列：显示符号链接实际指向（让用户看到链接是否建立）
            target = m.get("target", "")
            if target:
                # 去掉 \\?\ 前缀显示更清晰
                target_display = target.replace("\\\\?\\", "").replace("\\\\?\\UNC\\", "\\\\")
                tgt_item = QTableWidgetItem(target_display)
                tgt_item.setToolTip(target_display)
                # 若是符号链接且指向正确，用绿色；断链用红色
                if m.get("is_symlink"):
                    tgt_item.setForeground(QColor("#2E7D32"))
                else:
                    tgt_item.setForeground(QColor("#C62828"))
            else:
                tgt_item = QTableWidgetItem("（非符号链接）")
                tgt_item.setForeground(QColor("#9E9E9E"))
                tgt_item.setToolTip("C盘路径不是符号链接，可能是真实目录（被软件覆盖）")
            self.table_migrated.setItem(row, 4, tgt_item)
            # 说明列：复用软件识别
            desc_m = self._get_dir_description_safe(m["src"])
            item5 = QTableWidgetItem(desc_m)
            item5.setToolTip(desc_m if desc_m else os.path.basename(m["src"]))
            self.table_migrated.setItem(row, 5, item5)
            # 迁移时间列
            item6 = QTableWidgetItem(m["time"])
            item6.setToolTip(m["time"])
            self.table_migrated.setItem(row, 6, item6)

        # 填充待迁移表
        self.table_scan.setRowCount(0)
        for s in scanned:
            row = self.table_scan.rowCount()
            self.table_scan.insertRow(row)
            item0 = QTableWidgetItem(s["path"])
            item0.setToolTip(s["path"])
            item0.setFlags(item0.flags() & ~Qt.ItemIsEditable)
            self.table_scan.setItem(row, 0, item0)
            item1 = QTableWidgetItem(s["location"])
            item1.setToolTip(s["location"])
            item1.setFlags(item1.flags() & ~Qt.ItemIsEditable)
            self.table_scan.setItem(row, 1, item1)
            si = NumericTableWidgetItem(_format_size(s["size_mb"]))
            si.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            si.setData(Qt.UserRole, float(s["size_mb"]))
            _apply_size_item_color(si, s["size_mb"])
            si.setFlags(si.flags() & ~Qt.ItemIsEditable)
            self.table_scan.setItem(row, 2, si)
            item3 = QTableWidgetItem(s["name"])
            item3.setToolTip(s["name"])
            item3.setFlags(item3.flags() & ~Qt.ItemIsEditable)
            self.table_scan.setItem(row, 3, item3)
            desc_text = s.get("desc", "")
            item4 = QTableWidgetItem(desc_text)
            item4.setToolTip(desc_text if desc_text else s["name"])
            # 说明列保持可编辑（不调用setFlags去掉ItemIsEditable）
            self.table_scan.setItem(row, 4, item4)
            # === 颜色标记体系：根据描述质量给整行上色 ===
            from utils import is_system_path
            desc_quality = self._assess_desc_quality(desc_text, s.get("name", ""))
            row_color = None
            desc_prefix = ""
            desc_tooltip_extra = ""

            if is_system_path(s["path"]):
                row_color = QColor("#FFE0B2")  # 深橙色：系统文件
                desc_prefix = "[系统] "
                desc_tooltip_extra = "! 系统重要文件，迁移可能导致系统异常\n"
            elif s.get("dev_env_configured"):
                # 开发环境迁移区已配置此目录（环境变量已改到 D 盘，但数据还在 C 盘）
                # 用琥珀色提示用户：可在此区直接迁移数据，无需再去开发环境迁移区
                row_color = QColor("#FFE082")  # 琥珀色：开发环境已配置
                dev_name = s.get("dev_env_name", "")
                dev_drive = s.get("dev_env_drive", "")
                dev_target = s.get("dev_env_target", "")
                desc_prefix = "[已配置] "
                desc_tooltip_extra = (
                    f"! 此目录已被开发环境迁移区配置到 {dev_drive}: 盘\n"
                    f"  工具: {dev_name}\n"
                    f"  目标路径: {dev_target}\n"
                    f"  环境/配置已改到 D 盘，但 C 盘数据尚未迁移\n"
                    f"  建议: 可在此区直接迁移数据（复制+符号链接）\n")
            elif desc_quality == "wrong":
                row_color = QColor("#FFEBEE")  # 浅红色：描述大概率错误
                desc_prefix = "[?] "
                desc_tooltip_extra = "! 此说明可能不准确（联网搜索结果不相关），可双击编辑修正\n"
            elif desc_quality == "low":
                row_color = QColor("#E8EAF6")  # 浅靛蓝色：描述质量低
                desc_prefix = "[~] "
                desc_tooltip_extra = "! 说明质量较低，可双击编辑或点击联网补全说明重新识别\n"

            if row_color:
                for col in range(self.table_scan.columnCount()):
                    cell = self.table_scan.item(row, col)
                    if cell:
                        cell.setBackground(row_color)
                if desc_prefix:
                    item4.setText(desc_prefix + desc_text)
                if desc_tooltip_extra:
                    item4.setToolTip(desc_tooltip_extra + (desc_text if desc_text else s["name"]))

        total = sum(s["size_mb"] for s in scanned)
        # 状态栏显示扫描耗时（MFT 模式下通常 0.1-2 秒，os.walk 模式 30-60 秒）
        elapsed_text = f" (耗时 {elapsed:.2f} 秒)" if elapsed > 0 else ""
        self.status_label.setText(
            f"已迁移: {len(migrated)} 个 | 待迁移: {len(scanned)} 个 ({total:.0f} MB){elapsed_text}")
        self.progress.setVisible(False)
        self.btn_refresh.setEnabled(True)
        self.btn_refresh_scan.setEnabled(True)
        # 确保线程完全结束再清除引用（quit+wait，防止线程残留导致painter冲突）
        if self.scan_thread:
            self.scan_thread.quit()
            self.scan_thread.wait(2000)
            self.scan_thread = None
        self._scan_busy = False  # 清除忙状态
        # 恢复信号和排序
        self.table_migrated.blockSignals(False)
        self.table_scan.blockSignals(False)
        self.table_migrated.setSortingEnabled(True)
        self.table_scan.setSortingEnabled(True)
        # 强制刷新视图，确保所有单元格都正确显示
        self.table_migrated.viewport().update()
        self.table_scan.viewport().update()
        # 保存扫描结果到缓存
        self.cfg["scan_cache"] = scanned
        self.cfg["scan_cache_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_all(self.cfg)
        # 更新统计标签
        self._update_stats(migrated_count=len(migrated), scan_count=len(scanned))
        # 启动后台线程异步计算已迁移目录大小（不阻塞UI）
        self._start_migrated_size_calc(migrated)
        # 触发异步补全空 desc（后台线程识别软件描述，写入 desc_cache 持久化）
        # 首次扫描 desc 全空，5-15 秒内逐渐填满；二次扫描 desc_cache 命中无需补全
        QTimer.singleShot(500, self._async_fill_empty_desc)

    def _start_migrated_size_calc(self, migrated):
        """后台异步计算已迁移目录大小，计算完逐个更新表格

        说明：已迁移目录的目标在 D 盘等其他盘，MFT 扫描器只索引了 C 盘，
        所以这里会自动回退到 os.walk。若 pending 为空（所有 size_mb 已有值）则直接跳过。
        """
        # 收集需要计算大小的目录（size_mb == 0 的）
        pending = []
        for m in migrated:
            try:
                if float(m.get("size_mb", 0)) == 0:
                    dst = m.get("dst", "") or m.get("target", "")
                    if dst and os.path.exists(dst):
                        pending.append((m["src"], dst))
            except Exception:
                pass
        if not pending:
            return
        log.info(f"开始计算 {len(pending)} 个已迁移目录的大小（目标在其他盘，走 os.walk）")
        # 避免重复启动
        if hasattr(self, '_size_calc_thread') and self._size_calc_thread and self._size_calc_thread.isRunning():
            return

        class SizeCalcThread(QThread):
            size_signal = Signal(str, float)  # (src_path, size_mb)
            finished_signal = Signal()
            def __init__(self, items):
                super().__init__()
                self.items = items
                self._stop = False
            def stop(self):
                self._stop = True
            def run(self):
                import time
                from utils import get_dir_size_fast
                t0 = time.time()
                count = 0
                for src, dst in self.items:
                    if self._stop:
                        break
                    try:
                        size = get_dir_size_fast(dst)
                        self.size_signal.emit(src, size)
                        count += 1
                    except Exception:
                        pass
                log.info(f"已迁移目录大小计算完成: {count}/{len(self.items)} 个, 耗时 {time.time()-t0:.2f} 秒")
                self.finished_signal.emit()

        self._size_calc_thread = SizeCalcThread(pending)
        self._size_calc_thread.size_signal.connect(self._on_size_calculated)
        self._size_calc_thread.start()

    def _on_size_calculated(self, src_path, size_mb):
        """单个目录大小计算完成，更新已迁移表对应行"""
        # 通过src路径找到表格中的行
        for row in range(self.table_migrated.rowCount()):
            item = self.table_migrated.item(row, 0)
            if item and item.text().lower() == src_path.lower():
                # 更新大小列（第2列）
                size_item = self.table_migrated.item(row, 2)
                if size_item:
                    from PySide6.QtWidgets import QTableWidgetItem
                    from PySide6.QtCore import Qt as QtConst
                    new_item = NumericTableWidgetItem(_format_size(size_mb))
                    new_item.setTextAlignment(QtConst.AlignRight | QtConst.AlignVCenter)
                    new_item.setData(QtConst.UserRole, float(size_mb))
                    # 保持不可编辑
                    new_item.setFlags(new_item.flags() & ~QtConst.ItemIsEditable)
                    self.table_migrated.setItem(row, 2, new_item)
                break

    def _on_search_files(self):
        """文件搜索 - 基于 MFT 索引在 C 盘搜索文件名，弹窗显示结果和耗时"""
        pattern = self.edit_search.text().strip()
        if not pattern:
            QMessageBox.information(self, "搜索", "请输入文件名模式（如 *.exe 或 test）")
            return
        # 检查 MFT 是否已加载
        from utils import get_mft_scanner
        scanner = get_mft_scanner()
        if scanner is None or not scanner._loaded:
            QMessageBox.warning(self, "搜索",
                "MFT 索引未加载，请先点击'刷新'按钮完成一次扫描以加载 MFT 索引。")
            return
        if not scanner.is_mft_mode:
            QMessageBox.warning(self, "搜索",
                "MFT 模式未启用（可能非管理员权限），无法使用快速搜索。")
            return
        # 后台线程搜索，避免阻塞 UI
        self.btn_search.setEnabled(False)
        self.edit_search.setEnabled(False)
        self.status_label.setText(f"搜索中: {pattern} ...")

        class SearchThread(QThread):
            finished_signal = Signal(list, float, str)  # (results, elapsed, pattern)
            error_signal = Signal(str)

            def __init__(self, scanner, pattern):
                super().__init__()
                self.scanner = scanner
                self.pattern = pattern

            def run(self):
                import time
                try:
                    t0 = time.time()
                    results = self.scanner.search_files(self.pattern, None, 1000)
                    elapsed = time.time() - t0
                    self.finished_signal.emit(results, elapsed, self.pattern)
                except Exception as e:
                    self.error_signal.emit(str(e))

        def _on_search_done(results, elapsed, pat):
            self.btn_search.setEnabled(True)
            self.edit_search.setEnabled(True)
            self.status_label.setText(
                f"搜索完成: '{pat}' 找到 {len(results)} 条结果, 耗时 {elapsed:.2f} 秒")
            # 弹窗显示结果
            from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                            QLabel, QTableWidget, QTableWidgetItem,
                                            QHeaderView, QPushButton, QAbstractItemView)
            dlg = QDialog(self)
            dlg.setWindowTitle(f"搜索结果: '{pat}' - {len(results)} 条 (耗时 {elapsed:.2f} 秒)")
            dlg.resize(900, 500)
            dlg_layout = QVBoxLayout(dlg)
            # 顶部信息
            info = QLabel(f"模式: {pat}    结果: {len(results)} 条    耗时: {elapsed:.2f} 秒")
            info.setStyleSheet("color: #1565C0; font-weight: bold; padding: 4px;")
            dlg_layout.addWidget(info)
            # 结果表格
            tbl = QTableWidget()
            tbl.setColumnCount(4)
            tbl.setHorizontalHeaderLabels(["路径", "大小", "类型", "记录号"])
            tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
            tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
            tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
            tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
            tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
            tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
            for r in results:
                row = tbl.rowCount()
                tbl.insertRow(row)
                tbl.setItem(row, 0, QTableWidgetItem(r.get("path", "")))
                size_val = r.get("size", 0)
                size_text = _format_size(round(size_val / 1024 / 1024, 1)) if size_val else ""
                tbl.setItem(row, 1, QTableWidgetItem(size_text))
                tbl.setItem(row, 2, QTableWidgetItem("目录" if r.get("is_dir") else "文件"))
                tbl.setItem(row, 3, QTableWidgetItem(str(r.get("record_num", ""))))
            dlg_layout.addWidget(tbl, 1)
            # 关闭按钮
            btn_row = QHBoxLayout()
            btn_row.addStretch()
            btn_close = QPushButton("关闭")
            btn_close.clicked.connect(dlg.accept)
            btn_row.addWidget(btn_close)
            dlg_layout.addLayout(btn_row)
            dlg.exec()

        def _on_search_error(err):
            self.btn_search.setEnabled(True)
            self.edit_search.setEnabled(True)
            self.status_label.setText(f"搜索失败: {err}")
            QMessageBox.critical(self, "搜索失败", str(err))

        self._search_thread = SearchThread(scanner, pattern)
        self._search_thread.finished_signal.connect(_on_search_done, Qt.QueuedConnection)
        self._search_thread.error_signal.connect(_on_search_error, Qt.QueuedConnection)
        self._search_thread.start()

    def on_scan_error(self, err):
        # 停止进度轮询
        if hasattr(self, '_progress_timer'):
            self._progress_timer.stop()
        self.status_label.setText(f"扫描失败: {err}")
        self.progress.setVisible(False)
        self.btn_refresh.setEnabled(True)
        self.btn_refresh_scan.setEnabled(True)
        if self.scan_thread:
            self.scan_thread.quit()
            self.scan_thread.wait(2000)
            self.scan_thread = None
        self._scan_busy = False  # 清除忙状态
        log_error_with_reason("扫描失败", err, "MainWindow.refresh")
        log.error(f"扫描失败: {err}")

    # ===== 智能刷新待迁移表（仅扫描一级子目录，复用旧大小，仅新增目录计算）=====

    def smart_refresh_scan(self):
        """智能刷新待迁移表 - 异步QThread后台执行
        策略：listdir收集一级子目录，对比表格现有路径复用旧大小和说明，仅新增目录计算大小
        所有耗时操作在后台线程执行，主线程仅更新UI，不卡死
        """
        # 防止重复触发：用_busy标志位覆盖整个生命周期（启动→完成回调）
        # isRunning()在run()结束后立即返回False，但finished信号还在队列中等待处理
        # 此时用户点击会绕过isRunning检查，导致旧QThread被覆盖引发painter冲突
        if getattr(self, '_smart_scan_busy', False):
            return
        if self.smart_scan_thread and self.smart_scan_thread.isRunning():
            return
        if self.scan_thread and self.scan_thread.isRunning():
            return  # 全盘扫描进行中也不允许智能刷新
        # 防止 segfault：异步 desc 补全线程在运行时禁止智能刷新
        # smart_scan 与 DescFillThread 并发调用 win32com/WMI/ctypes 会引发 C 层 segfault
        if hasattr(self, '_desc_fill_thread') and self._desc_fill_thread:
            if self._desc_fill_thread.isRunning():
                self.status_label.setText("异步补全中，请稍候再刷新...")
                return
        # 允许与联网搜索同时进行（联网补全基于路径匹配，不依赖行号）

        self._smart_scan_busy = True  # 标记忙状态，直到完成回调才清除

        # 收集当前表格的旧数据作为缓存（路径规范化用正斜杠小写）
        # 同时从 scan_cache 读取 mtime，用于增量对比（mtime 未变化则不重算大小）
        scan_cache_map = {}
        for s in self.cfg.get("scan_cache", []):
            p = s.get("path", "")
            if p:
                norm_p = p.lower().replace("\\", "/").rstrip("/")
                scan_cache_map[norm_p] = s
        old_entries = {}
        for row in range(self.table_scan.rowCount()):
            try:
                path_item = self.table_scan.item(row, 0)
                size_item = self.table_scan.item(row, 2)
                desc_item = self.table_scan.item(row, 4)
                if not path_item:
                    continue
                orig_path = path_item.text()
                norm = orig_path.lower().replace("\\", "/").rstrip("/")
                # 优先从UserRole读原始数值（NumericTableWidgetItem已存）
                # 避免从文本"17421.8MB"解析float失败导致size_val=0
                size_val = 0
                if size_item:
                    user_val = size_item.data(Qt.UserRole)
                    if user_val is not None:
                        try:
                            size_val = float(user_val)
                        except Exception:
                            size_val = 0
                    else:
                        # fallback: 从文本解析（去掉MB/KB后缀）
                        import re as _re
                        size_str = size_item.text() or "0"
                        m = _re.match(r'([\d.]+)', size_str.strip())
                        if m:
                            try:
                                num = float(m.group(1))
                                if 'KB' in size_str.upper():
                                    size_val = num / 1024
                                else:
                                    size_val = num
                            except Exception:
                                size_val = 0
                desc_text = desc_item.text() if desc_item else ""
                # 通用去掉所有 [xxx] 前缀（[系统]、[扫描发现]等）
                import re as _re2
                desc_text = _re2.sub(r'^\[[^\]]*\]\s*', '', desc_text).strip()
                # 从 scan_cache 读取 mtime（上次扫描记录的目录修改时间）
                old_mtime = 0
                sc = scan_cache_map.get(norm)
                if sc:
                    try:
                        old_mtime = float(sc.get("mtime", 0))
                    except Exception:
                        old_mtime = 0
                old_entries[norm] = {
                    "size_mb": size_val,
                    "desc": desc_text,
                    "orig_path": orig_path,
                    "mtime": old_mtime,
                }
            except Exception:
                pass

        # 禁用扫描按钮（不禁用联网搜索按钮，允许同时进行）
        self.btn_refresh.setEnabled(False)
        self.btn_refresh_scan.setEnabled(False)
        # 智能刷新很快，不显示进度条，只用状态栏提示
        self.status_label.setText("智能扫描中...（复用旧数据，仅新增目录计算大小）")

        # 启动后台线程
        self.smart_scan_thread = QThread()
        self.smart_scan_worker = SmartScanWorker(self.migrator, old_entries)
        self.smart_scan_worker.moveToThread(self.smart_scan_thread)
        self.smart_scan_thread.started.connect(self.smart_scan_worker.run)
        # 跨线程信号必须显式指定Qt.QueuedConnection
        self.smart_scan_worker.progress_signal.connect(
            lambda msg: self.status_label.setText(msg), Qt.QueuedConnection)
        self.smart_scan_worker.finished_signal.connect(self.on_smart_scan_finished, Qt.QueuedConnection)
        self.smart_scan_worker.error_signal.connect(self.on_smart_scan_error, Qt.QueuedConnection)
        self.smart_scan_thread.start()

        # QTimer轮询智能扫描进度（与全盘扫描共用_update_scan_progress逻辑）
        self._last_progress_pct = -1
        self._progress_timer = QTimer(self)
        self._progress_timer.timeout.connect(self._update_smart_progress)
        self._progress_timer.start(100)

    def _update_smart_progress(self):
        """QTimer回调 - 主线程轮询智能扫描进度"""
        if not self.smart_scan_worker:
            return
        try:
            current = self.smart_scan_worker.current
            total = self.smart_scan_worker.total
            dir_name = self.smart_scan_worker.dir_name
            if total > 0:
                pct = int(current * 100 / total)
                if pct != self._last_progress_pct:
                    self._last_progress_pct = pct
                    self.status_label.setText(f"智能扫描 {current}/{total} - {dir_name}")
        except Exception as e:
            log_error_with_reason("未知错误", str(e), "_update_smart_progress")
            log.error(f"_update_smart_progress异常: {e}")

    def on_smart_scan_finished(self, scanned):
        """智能扫描完成回调（主线程）- 只更新待迁移表，不触碰已迁移表"""
        # 停止进度轮询
        if hasattr(self, '_progress_timer'):
            self._progress_timer.stop()
        # 保存排序状态（如果启用了排序）
        sort_enabled = self.table_scan.isSortingEnabled()
        self.table_scan.setSortingEnabled(False)
        # 不使用setUpdatesEnabled(False)：会导致部分单元格内容不显示（Qt已知bug）
        # 改用blockSignals阻塞itemChanged信号，避免填充时反复触发保存
        self.table_scan.blockSignals(True)
        self.table_scan.setRowCount(0)

        from utils import is_system_path
        for s in scanned:
            row = self.table_scan.rowCount()
            self.table_scan.insertRow(row)
            item0 = QTableWidgetItem(s["path"])
            item0.setToolTip(s["path"])
            item0.setFlags(item0.flags() & ~Qt.ItemIsEditable)
            self.table_scan.setItem(row, 0, item0)
            item1 = QTableWidgetItem(s["location"])
            item1.setToolTip(s["location"])
            item1.setFlags(item1.flags() & ~Qt.ItemIsEditable)
            self.table_scan.setItem(row, 1, item1)
            # 大小列：用NumericTableWidgetItem + UserRole存数值，确保按数值排序
            size_val = s.get("size_mb", 0)
            si = NumericTableWidgetItem(_format_size(size_val))
            si.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            si.setData(Qt.UserRole, float(size_val))
            _apply_size_item_color(si, size_val)
            si.setFlags(si.flags() & ~Qt.ItemIsEditable)
            self.table_scan.setItem(row, 2, si)
            item3 = QTableWidgetItem(s["name"])
            item3.setToolTip(s["name"])
            item3.setFlags(item3.flags() & ~Qt.ItemIsEditable)
            self.table_scan.setItem(row, 3, item3)
            desc_text = s.get("desc", "")
            item4 = QTableWidgetItem(desc_text)
            item4.setToolTip(desc_text if desc_text else s["name"])
            # 说明列保持可编辑
            self.table_scan.setItem(row, 4, item4)
            # 系统文件整行涂橙色 / 开发环境已配置涂琥珀色
            if is_system_path(s["path"]):
                sys_brush = QColor("#FFF3E0")
                for col in range(self.table_scan.columnCount()):
                    cell = self.table_scan.item(row, col)
                    if cell:
                        cell.setBackground(sys_brush)
                item4.setText("[系统] " + desc_text)
                item4.setToolTip("⚠ 系统重要文件，迁移可能导致系统异常\n" + (desc_text if desc_text else s["name"]))
            elif s.get("dev_env_configured"):
                dev_brush = QColor("#FFE082")  # 琥珀色：开发环境已配置
                for col in range(self.table_scan.columnCount()):
                    cell = self.table_scan.item(row, col)
                    if cell:
                        cell.setBackground(dev_brush)
                dev_name = s.get("dev_env_name", "")
                dev_drive = s.get("dev_env_drive", "")
                dev_target = s.get("dev_env_target", "")
                item4.setText("[已配置] " + desc_text)
                item4.setToolTip(
                    f"! 此目录已被开发环境迁移区配置到 {dev_drive}: 盘\n"
                    f"  工具: {dev_name}\n"
                    f"  目标路径: {dev_target}\n"
                    f"  环境/配置已改到 D 盘，但 C 盘数据尚未迁移\n"
                    f"  建议: 可在此区直接迁移数据（复制+符号链接）\n"
                    + (desc_text if desc_text else s["name"]))

        # 恢复信号和排序状态
        self.table_scan.blockSignals(False)
        if sort_enabled:
            self.table_scan.setSortingEnabled(True)
        # 强制刷新视图，确保所有单元格都正确显示
        self.table_scan.viewport().update()

        total = sum(s.get("size_mb", 0) for s in scanned)
        # 读取扫描耗时（由 SmartScanWorker 记录）
        smart_elapsed = getattr(self.smart_scan_worker, 'scan_elapsed', 0) if self.smart_scan_worker else 0
        elapsed_text = f" (耗时 {smart_elapsed:.2f} 秒)" if smart_elapsed > 0 else ""
        self.status_label.setText(
            f"待迁移表已刷新: {len(scanned)} 项 ({total:.0f} MB){elapsed_text}")
        self.btn_refresh.setEnabled(True)
        self.btn_refresh_scan.setEnabled(True)
        # 确保线程完全结束再清除引用（quit+wait，防止线程残留导致painter冲突）
        if self.smart_scan_thread:
            self.smart_scan_thread.quit()
            self.smart_scan_thread.wait(2000)
            self.smart_scan_thread = None
        self._smart_scan_busy = False  # 清除忙状态

        # 更新缓存和统计
        self.cfg["scan_cache"] = scanned
        self.cfg["scan_cache_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_all(self.cfg)
        self._update_stats(scan_count=len(scanned))
        # 触发异步补全空 desc（与全盘扫描相同机制）
        QTimer.singleShot(500, self._async_fill_empty_desc)

    def on_smart_scan_error(self, err):
        """智能扫描失败回调"""
        # 停止进度轮询
        if hasattr(self, '_progress_timer'):
            self._progress_timer.stop()
        self.status_label.setText(f"智能扫描失败: {err}")
        self.btn_refresh.setEnabled(True)
        self.btn_refresh_scan.setEnabled(True)
        if self.smart_scan_thread:
            self.smart_scan_thread.quit()
            self.smart_scan_thread.wait(2000)
            self.smart_scan_thread = None
        self._smart_scan_busy = False  # 清除忙状态
        log_error_with_reason("智能扫描失败", err, "MainWindow.smart_refresh_scan")
        log.error(f"智能扫描失败: {err}")

    def _on_scan_item_changed(self, item):
        """待迁移表itemChanged回调 - 说明列(第4列)编辑后保存到scan_cache"""
        try:
            if item.column() != 4:
                return
            row = item.row()
            path_item = self.table_scan.item(row, 0)
            if not path_item:
                return
            path = path_item.text()
            new_desc = item.text().strip()
            # 去掉系统标记前缀，避免存入scan_cache后下次刷新重复添加变成"[系统] [系统] xxx"
            if new_desc.startswith("[系统] "):
                new_desc = new_desc[len("[系统] "):].strip()
            # 同步tooltip（系统文件保留警告提示）
            from utils import is_system_path
            if is_system_path(path):
                item.setToolTip("⚠ 系统重要文件，迁移可能导致系统异常\n" + (new_desc if new_desc else path))
            else:
                item.setToolTip(new_desc if new_desc else path)
            # 更新scan_cache（只存纯说明，不存[系统]前缀）
            for s in self.cfg.get("scan_cache", []):
                if s.get("path") == path:
                    s["desc"] = new_desc
                    break
            save_all(self.cfg)
            # 状态栏友好提示（非弹窗）
            self.status_label.setText(f"已更新说明: {new_desc[:30]}{'...' if len(new_desc) > 30 else ''}")
        except Exception as e:
            log_error_with_reason("未知错误", str(e), f"_on_scan_item_changed: row={item.row()}")

    def _scan_context_menu(self, pos):
        """待迁移表右键菜单"""
        rows = sorted(set(idx.row() for idx in self.table_scan.selectedIndexes()))
        if not rows:
            return
        menu = QMenu(self)
        # 迁移到默认盘
        act_migrate_default = menu.addAction(f"迁移到 {self.cfg['g_root']}")
        menu.addSeparator()
        # 迁移到指定位置
        act_migrate_to = menu.addAction("迁移到指定位置...")
        act_open = menu.addAction("打开目录")
        menu.addSeparator()
        act_copy = menu.addAction("复制路径")
        action = menu.exec(self.table_scan.viewport().mapToGlobal(pos))
        if action == act_migrate_default:
            self._migrate_rows(rows)
        elif action == act_migrate_to:
            self._migrate_rows_to_custom(rows)
        elif action == act_open:
            for row in rows:
                path = self.table_scan.item(row, 0).text()
                if os.path.exists(path):
                    os.startfile(path)
        elif action == act_copy:
            paths = [self.table_scan.item(row, 0).text() for row in rows]
            QApplication.clipboard().setText("\n".join(paths))
            self.status_label.setText(f"已复制{len(paths)}个路径")

    def _light_refresh_scan_table(self):
        """轻量刷新待迁移表：删除已成为符号链接的行（数据已迁移走）

        开发环境迁移区完成数据迁移后调用，不重新全盘扫描，只检查现有行。
        """
        from utils import is_symlink
        rows_to_remove = []
        for row in range(self.table_scan.rowCount()):
            path_item = self.table_scan.item(row, 0)
            if not path_item:
                continue
            path = path_item.text().replace("\\\\?\\", "")
            if path and is_symlink(path):
                rows_to_remove.append(row)
        # 从后往前删，避免索引错乱
        for row in reversed(rows_to_remove):
            self.table_scan.removeRow(row)
        if rows_to_remove:
            self.on_monitor_log("dev_env",
                f"待迁移区已移除 {len(rows_to_remove)} 条已迁移条目（C盘已变符号链接）")

    def _refresh_migrated_only(self):
        """只刷新已迁移表（异步线程，关联进度条）

        用户主动点击"刷新已迁移"按钮时，会强制重算所有记录的真实占用空间
        （因为目标数据在其他盘，可能被软件增删文件，旧 size_mb 不准）。
        重算在后台线程执行，避免 UI 卡顿。
        """
        if hasattr(self, '_migrated_thread') and self._migrated_thread and self._migrated_thread.isRunning():
            self.status_label.setText("已迁移表刷新正在进行中...")
            return
        self.btn_refresh_migrated.setEnabled(False)
        self.status_label.setText("正在刷新已迁移表（重算真实占用空间）...")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # 不确定进度（滚动条样式）
        self.progress.setTextVisible(True)
        self.progress.setFormat("刷新已迁移表中（重算大小）...")

        from PySide6.QtCore import QThread, Signal
        class MigratedScanThread(QThread):
            finished_scan = Signal(list)
            error_signal = Signal(str)
            def __init__(self, migrator):
                super().__init__()
                self.migrator = migrator
            def run(self):
                try:
                    # force_recalc_size=True：强制重算所有记录的真实大小
                    # 目标在其他盘走 os.walk，68GB 大约几十秒，但能反映真实占用
                    migrated = self.migrator.scan_migrated(force_recalc_size=True)
                    self.finished_scan.emit(migrated)
                except Exception as e:
                    self.error_signal.emit(str(e))

        def on_finished(migrated):
            # 填充前关闭排序和信号
            self.table_migrated.setSortingEnabled(False)
            self.table_migrated.blockSignals(True)
            self.table_migrated.setRowCount(0)
            status_map = {
                "OK":         ("正常",     "#2E7D32", "符号链接有效，数据在目标盘"),
                "BROKEN":     ("断链",     "#C62828", "C盘路径被软件覆盖为真实目录，点击右键修复"),
                "MISSING":    ("丢失",     "#EF6C00", "C盘路径不存在，点击右键修复（直接创建链接）"),
                "TARGET_GONE":("目标丢失", "#B71C1C", "目标盘数据不存在，需还原或重新迁移"),
            }
            for m in migrated:
                row = self.table_migrated.rowCount()
                self.table_migrated.insertRow(row)
                item0 = QTableWidgetItem(m["src"]); item0.setToolTip(m["src"])
                self.table_migrated.setItem(row, 0, item0)
                item1 = QTableWidgetItem(m["dst"]); item1.setToolTip(m["dst"])
                self.table_migrated.setItem(row, 1, item1)
                size_val = m.get("size_mb", 0)
                si = NumericTableWidgetItem(_format_size(size_val))
                si.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                si.setData(Qt.UserRole, float(size_val))
                _apply_size_item_color(si, size_val)
                self.table_migrated.setItem(row, 2, si)
                status_text, status_color, status_tip = status_map.get(
                    m["status"], ("未知", "#424242", ""))
                # i18n：状态词渲染时翻译
                from i18n import tr
                status_text = tr(status_text)
                status_tip = tr(status_tip)
                st = QTableWidgetItem(status_text)
                st.setForeground(QColor(status_color)); st.setToolTip(status_tip)
                self.table_migrated.setItem(row, 3, st)
                target = m.get("target", "")
                if target:
                    target_display = target.replace("\\\\?\\", "").replace("\\\\?\\UNC\\", "\\\\")
                    tgt_item = QTableWidgetItem(target_display)
                    tgt_item.setToolTip(target_display)
                    tgt_item.setForeground(QColor("#2E7D32") if m.get("is_symlink") else QColor("#C62828"))
                else:
                    tgt_item = QTableWidgetItem("（非符号链接）")
                    tgt_item.setForeground(QColor("#9E9E9E"))
                    tgt_item.setToolTip("C盘路径不是符号链接，可能是真实目录（被软件覆盖）")
                self.table_migrated.setItem(row, 4, tgt_item)
                # 说明列：优先用迁移记录中的 desc，其次查 desc_cache（和待迁移区一致），最后用目录名
                # 不调用 get_dir_description 现场识别（可能返回不准的简短结果如 "sdk"）
                desc_m = m.get("desc", "")
                if not desc_m:
                    desc_cache = self.cfg.get("desc_cache", {})
                    desc_m = desc_cache.get(m["src"], "")
                    if not desc_m:
                        # 规范化路径匹配
                        norm_src = m["src"].replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
                        for k, v in desc_cache.items():
                            if k.replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\") == norm_src:
                                desc_m = v
                                break
                if not desc_m:
                    desc_m = os.path.basename(m["src"])
                item5 = QTableWidgetItem(desc_m); item5.setToolTip(desc_m)
                self.table_migrated.setItem(row, 5, item5)
                # 迁移时间列
                item6 = QTableWidgetItem(m["time"]); item6.setToolTip(m["time"])
                self.table_migrated.setItem(row, 6, item6)
            # 恢复信号和排序
            self.table_migrated.blockSignals(False)
            self.table_migrated.setSortingEnabled(True)
            self.table_migrated.viewport().update()
            self._update_stats(migrated_count=len(migrated))
            self.progress.setVisible(False)
            self.status_label.setText(f"已迁移表已刷新: {len(migrated)} 条记录（真实占用空间已重算）")
            self.btn_refresh_migrated.setEnabled(True)
            # scan_migrated(force_recalc_size=True) 已在后台线程重算大小
            # 这里把新大小回写到 config.json，下次启动直接用，无需再算
            try:
                existing_srcs = {r.get("src", "").lower(): r
                                 for r in self.cfg.get("migrated", [])}
                for m in migrated:
                    src = m.get("src", "")
                    new_size = m.get("size_mb", 0)
                    src_lower = src.lower()
                    if src_lower in existing_srcs:
                        # 已有记录：更新 size_mb
                        existing_srcs[src_lower]["size_mb"] = new_size
                    else:
                        # 额外记录（文件系统存在但 config 未记录的符号链接）：新增
                        new_record = {
                            "src": src,
                            "dst": m.get("dst", ""),
                            "time": m.get("time", ""),
                            "size_mb": new_size,
                        }
                        # 保留 scan_migrated 返回的其他字段（desc 等）
                        if m.get("desc"):
                            new_record["desc"] = m["desc"]
                        self.cfg.setdefault("migrated", []).append(new_record)
                        existing_srcs[src_lower] = new_record
                save_all(self.cfg)
            except Exception as e:
                log.error(f"回写已迁移大小失败: {e}")
            # 清理线程
            if self._migrated_thread:
                self._migrated_thread.quit()
                self._migrated_thread.wait(2000)
                self._migrated_thread = None

        def on_error(err):
            self.progress.setVisible(False)
            self.table_migrated.setSortingEnabled(True)
            self.status_label.setText(f"刷新已迁移表失败: {err}")
            self.btn_refresh_migrated.setEnabled(True)
            log_error_with_reason("刷新已迁移表失败", err, "MigratedScanThread.run")

        self._migrated_thread = MigratedScanThread(self.migrator)
        self._migrated_thread.finished_scan.connect(on_finished)
        self._migrated_thread.error_signal.connect(on_error)
        self._migrated_thread.start()
