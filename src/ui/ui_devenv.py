#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开发环境迁移功能 Handler(从 main.py 抽出)

包含 39 个方法,涵盖:
- 开发工具表格填充/刷新/缓存
- 状态检测与可视化
- 配置应用/撤销/回滚
- 数据迁移/还原
- 右键菜单与下载菜单
- 双击编辑路径
- 配置记录管理

这些方法原属 MainWindow,抽取为 Handler 以降低 main.py 体量。
方法内通过 self 访问 MainWindow 的属性和其他方法,运行时由 MainWindow 提供。
"""
import os
import sys
import json
import shutil
import subprocess
import logging
import traceback
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QThread, QTimer, QUrl
from PySide6.QtWidgets import (QMessageBox, QMenu, QFileDialog, QInputDialog,
                                QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                                QPushButton, QTableWidget, QTableWidgetItem,
                                QHeaderView, QCheckBox, QProgressBar, QDialogButtonBox,
                                QTextEdit, QWidgetAction)
from PySide6.QtGui import QColor, QAction, QDesktopServices, QIcon, QFont, QBrush, QPixmap

from config import (log_error_with_reason, save_config, save_state, save_all,
                    log_link_operation)
from ui_widgets import _format_size, _apply_size_item_color
from dev_env_migrate import (
    TOOLS as DEV_TOOLS, get_tool_status as dev_get_tool_status,
    apply_tool as dev_apply_tool, get_suggest_path as dev_get_suggest_path,
    unconfigure_tool as dev_unconfigure_tool,
    unapply_tool as dev_unapply_tool,
    get_tool_data_info as dev_get_tool_data_info,
    migrate_tool_data as dev_migrate_tool_data,
    get_tool_default_c_path as dev_get_tool_default_c_path,
    cleanup_bad_env_vars as dev_cleanup_bad_env_vars,
    GITHUB_URLS as DEV_GITHUB_URLS,
)
from migrator import Migrator
from ui_workers import (DevEnvApplyWorker, DevEnvRefreshWorker,
                        DevToolDownloadWorker, _DEV_TOOL_DOWNLOAD_APIS)
import dev_env_snapshot as dev_snapshot

log = logging.getLogger('CDriveRelocator')


class DevEnvHandler:
    """开发环境迁移功能 Handler"""

    def _prefill_dev_env_skeleton(self):
        """启动时立即填充骨架表格（解决二次打开白屏问题）

        在 __init__ 创建 table_dev_env 后立即调用：
        - 用 DEV_TOOLS 静态信息填充 26 行×9 列
        - 工具名/类别/C盘原位置用真实值（无需检测）
        - 当前路径/占用空间/状态/提示显示"加载中..."
        - 建议新路径用 dev_get_suggest_path 算出（无需检测）
        - 勾选框默认不勾（避免用户在数据加载完前误操作）

        后续缓存命中或后台检测完成时，通过 _update_single_dev_env_row
        增量更新对应行，不会清空整表重建。
        """
        try:
            from dev_env_migrate import (get_tool_default_c_path as dev_get_tool_default_c_path,
                                         get_suggest_path as dev_get_suggest_path)
            target_drive = self.dev_target_drive.currentText() if hasattr(self, 'dev_target_drive') else "D"

            self.table_dev_env.setSortingEnabled(False)
            self.table_dev_env.blockSignals(True)
            self.table_dev_env.setRowCount(0)

            for tool in DEV_TOOLS:
                row = self.table_dev_env.rowCount()
                self.table_dev_env.insertRow(row)

                # 第0列：勾选框（加载中不勾选）
                chk_item = QTableWidgetItem()
                chk_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                chk_item.setCheckState(Qt.Unchecked)
                chk_item.setData(Qt.UserRole, 0)
                self.table_dev_env.setItem(row, 0, chk_item)

                # 第1列：工具名（UserRole 存 tool_id 供反查）
                name_item = QTableWidgetItem(tool["name"])
                name_item.setToolTip(tool["name"])
                name_item.setData(Qt.UserRole, tool["id"])
                self.table_dev_env.setItem(row, 1, name_item)

                # 第2列：类别
                self.table_dev_env.setItem(row, 2, QTableWidgetItem(tool["category"]))

                # 第3列：当前路径（加载中）
                cur_item = QTableWidgetItem("加载中...")
                cur_item.setForeground(QColor("#9E9E9E"))
                self.table_dev_env.setItem(row, 3, cur_item)

                # 第4列：C盘原位置（静态默认路径，无需检测）
                c_original = ""
                c_orig_tooltip = ""
                default_c = dev_get_tool_default_c_path(tool)
                if default_c:
                    c_original = default_c.replace("\\\\?\\", "")
                    c_orig_tooltip = f"C 盘默认路径：{c_original}"
                if not c_original:
                    c_original = "—"
                    c_orig_tooltip = "此工具无固定 C 盘默认路径"
                c_orig_item = QTableWidgetItem(c_original)
                c_orig_item.setToolTip(c_orig_tooltip)
                c_orig_item.setForeground(QColor("#9E9E9E"))
                c_orig_item.setFlags(c_orig_item.flags() & ~Qt.ItemIsEditable)
                self.table_dev_env.setItem(row, 4, c_orig_item)

                # 第5列：占用空间（加载中）
                size_item = QTableWidgetItem("...")
                size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                size_item.setForeground(QColor("#9E9E9E"))
                size_item.setFlags(size_item.flags() & ~Qt.ItemIsEditable)
                self.table_dev_env.setItem(row, 5, size_item)

                # 第6列：建议新路径（可静态计算）
                suggest_path = dev_get_suggest_path(tool, target_drive)
                suggest_item = QTableWidgetItem(suggest_path)
                suggest_item.setToolTip(suggest_path + "\n（双击可修改路径）")
                self.table_dev_env.setItem(row, 6, suggest_item)

                # 第7列：状态（加载中）
                status_item = QTableWidgetItem("加载中...")
                status_item.setForeground(QColor("#9E9E9E"))
                self.table_dev_env.setItem(row, 7, status_item)

                # 第8列：提示（加载中）
                tip_item = QTableWidgetItem("正在检测...")
                tip_item.setForeground(QColor("#9E9E9E"))
                self.table_dev_env.setItem(row, 8, tip_item)

            self.table_dev_env.setSortingEnabled(True)
            self.table_dev_env.blockSignals(False)
            self.stat_dev_env.setText(
                f"共{len(DEV_TOOLS)}项 | 正在加载状态...")
        except Exception as e:
            log.error(f"填充骨架表格失败: {e}")

    def _find_dev_env_row_by_id(self, tool_id):
        """根据 tool_id 查找表格行号（用于增量更新）

        遍历表格第1列的 UserRole 数据匹配 tool_id。
        :return: 行号（int），找不到返回 -1
        """
        for row in range(self.table_dev_env.rowCount()):
            name_item = self.table_dev_env.item(row, 1)
            if name_item and name_item.data(Qt.UserRole) == tool_id:
                return row
        return -1

    def _update_single_dev_env_row(self, tool, status, target_drive, skip_io_checks=False):
        """单行增量更新（流式刷新和缓存填充共用）

        不清空整表，只更新指定 tool_id 对应的行。
        与 _populate_dev_env_table 的单行逻辑等价，但只更新一个工具。

        :param tool: 工具定义 dict
        :param status: 状态 dict {installed, current_path, on_c, configured, size_mb, ...}
        :param target_drive: 目标盘符
        :param skip_io_checks: True 跳过 is_symlink/os.path.exists 同步 IO（缓存命中时用）
        :return: True 表示找到行并更新成功；False 表示未找到行
        """
        from dev_env_migrate import (get_tool_default_c_path as dev_get_tool_default_c_path,
                                     get_suggest_path as dev_get_suggest_path)
        from utils import is_symlink as _is_symlink
        # _format_size 来自 ui_widgets.py

        tool_id = tool["id"]
        row = self._find_dev_env_row_by_id(tool_id)
        if row < 0:
            return False

        # 加载 dev_env_configured 中的 source_path（判断数据是否已迁移）
        dev_env_cfg = self.cfg.get("dev_env_configured") or {}
        cfg_info = dev_env_cfg.get(tool_id) or {}
        source_path = cfg_info.get("source_path", "")

        migrated_srcs = set()
        for m in (self.cfg.get("migrated") or []):
            s = (m.get("src") or "").replace("\\\\?\\", "").lower().rstrip("\\")
            if s:
                migrated_srcs.add(s)

        data_migrated = False
        if not skip_io_checks:
            if source_path:
                sp = source_path.replace("\\\\?\\", "")
                if _is_symlink(sp):
                    data_migrated = True
                elif sp.replace("\\\\?\\", "").lower().rstrip("\\") in migrated_srcs:
                    data_migrated = True
            if not data_migrated:
                default_c = dev_get_tool_default_c_path(tool)
                if default_c:
                    dc = default_c.replace("\\\\?\\", "")
                    if _is_symlink(dc):
                        data_migrated = True
                    elif dc.lower().rstrip("\\") in migrated_srcs:
                        data_migrated = True
        if not data_migrated and status.get("is_symlink"):
            data_migrated = True

        # 关闭排序和信号，避免更新过程中触发排序/信号
        was_sorting_enabled = self.table_dev_env.isSortingEnabled()
        self.table_dev_env.setSortingEnabled(False)
        self.table_dev_env.blockSignals(True)

        try:
            # 第0列：勾选框
            # M11：增量更新时保留用户已勾选的状态，不重置为 Unchecked
            # 否则流式刷新（每 100-200ms 更新一行）会清掉用户刚勾选的工具
            chk_item = self.table_dev_env.item(row, 0)
            if chk_item:
                # 仅当勾选状态从未初始化过时才设为 Unchecked（新行首次填充）
                if chk_item.data(Qt.UserRole) is None:
                    chk_item.setCheckState(Qt.Unchecked)
                    chk_item.setData(Qt.UserRole, 0)

            # 第3列：当前路径
            cur_path = (status["current_path"] or "").replace("\\\\?\\", "").replace("/", "\\") \
                       or ("未安装" if not status["installed"] else "（无）")
            cur_item = self.table_dev_env.item(row, 3)
            if cur_item:
                cur_item.setText(cur_path)
                cur_item.setToolTip(cur_path)
                # 重置颜色
                cur_item.setForeground(QColor("#424242"))
                if not status["installed"]:
                    cur_item.setForeground(QColor("#9E9E9E"))
                elif status["on_c"]:
                    cur_item.setForeground(QColor("#E53935"))

            # 第4列：C盘原位置
            c_original = ""
            c_orig_tooltip = ""
            c_orig_color = "#9E9E9E"
            c_orig_mark = False
            if source_path and source_path[1:2] == ":" and source_path[0].upper() == "C":
                c_original = source_path.replace("\\\\?\\", "")
                c_orig_color = "#616161"
                c_orig_tooltip = f"配置前 C 盘原始路径：\n{c_original}"
            else:
                default_c = dev_get_tool_default_c_path(tool)
                if default_c:
                    c_original = default_c.replace("\\\\?\\", "")
                    if (status["installed"] and not status["on_c"]
                            and (skip_io_checks or not os.path.exists(c_original))):
                        if skip_io_checks:
                            c_orig_color = "#9E9E9E"
                            c_orig_tooltip = f"C 盘默认路径：{c_original}"
                        else:
                            c_orig_mark = True
                            c_orig_color = "#9E9E9E"
                            c_orig_tooltip = (f"C 盘默认路径：{c_original}\n"
                                              f"ⓘ 此工具最初就装在其他盘，C 盘从未有过数据")
                    else:
                        c_orig_color = "#9E9E9E"
                        c_orig_tooltip = f"C 盘默认路径：{c_original}"
            if not c_original:
                c_original = "—"
                c_orig_tooltip = "此工具无固定 C 盘默认路径"
            c_orig_item = self.table_dev_env.item(row, 4)
            if c_orig_item:
                c_orig_item.setText(c_original + ("  ⓘ" if c_orig_mark else ""))
                c_orig_item.setToolTip(c_orig_tooltip)
                c_orig_item.setForeground(QColor(c_orig_color))

            # 第5列：占用空间
            size_mb = status.get("size_mb", 0)
            if not status["installed"]:
                size_text, size_color = "—", "#9E9E9E"
            elif size_mb == -1:
                size_text, size_color = "已迁移", "#2E7D32"
            elif size_mb == -2:
                size_text, size_color = "未生成", "#9E9E9E"
            elif size_mb == -3:
                size_text, size_color = "—", "#9E9E9E"
            elif size_mb > 0:
                size_text = _format_size(size_mb)
                size_color = "#E65100" if size_mb >= 500 else "#1565C0"
            else:
                size_text, size_color = "0 MB", "#9E9E9E"
            size_item = self.table_dev_env.item(row, 5)
            if size_item:
                size_item.setText(size_text)
                size_item.setForeground(QColor(size_color))
                if data_migrated:
                    migrate_drive = cfg_info.get("target_drive", "?")
                    size_item.setToolTip(f"数据已迁移到 {migrate_drive}: 盘")
                elif size_mb > 0:
                    size_item.setToolTip(f"{size_mb:.1f} MB")
                else:
                    size_item.setToolTip(size_text)

            # 第6列：建议新路径（盘符切换时也要更新）
            # 通用：当前路径已在非 C 盘（如 D/E/F 盘）且不是符号链接时，不显示默认建议路径
            # 用户仍可双击修改（后悔药：把数据迁到另一个盘/目录）
            # 例外：已通过待迁移区迁移（is_symlink=True）时，建议路径也置空
            cur_path_str = (status.get("current_path") or "").replace("\\\\?\\", "")
            is_sym = status.get("is_symlink", False)
            on_c = status.get("on_c", False)
            if is_sym or (cur_path_str and not on_c):
                # 已迁移（符号链接）或当前在非 C 盘 → 建议路径置空
                suggest_path = ""
                suggest_tip = "数据已不在 C 盘，如需迁移请双击选择新路径"
            else:
                suggest_path = dev_get_suggest_path(tool, target_drive)
                suggest_tip = suggest_path + "\n（双击可修改路径）"
            suggest_item = self.table_dev_env.item(row, 6)
            if suggest_item:
                suggest_item.setText(suggest_path)
                suggest_item.setToolTip(suggest_tip)

            # 第7列：状态
            status_font = None
            if not status["installed"]:
                status_text = "未安装（单击下载）"
                status_color = "#1565C0"
                status_font = QFont()
                status_font.setUnderline(True)
            elif data_migrated:
                cur_path = status.get("current_path", "") or status.get("symlink_target", "")
                p = cur_path.replace("\\\\?\\", "").replace("/", "\\")
                migrate_drive = p[0].upper() if len(p) >= 2 and p[1] == ':' else cfg_info.get("target_drive", target_drive)
                status_text = f"✓ 已迁移到{migrate_drive}:盘"
                status_color = "#2E7D32"
            elif status["configured"]:
                status_text = f"✓ 已配置到{target_drive}:盘"
                status_color = "#43A047"
            elif status["on_c"]:
                status_text = "⚠️ 装在C盘"
                status_color = "#FB8C00"
            else:
                cur_path = status["current_path"] or ""
                p = cur_path.replace("\\\\?\\", "").replace("/", "\\")
                drive_letter = p[0].upper() if len(p) >= 2 and p[1] == ':' else ""
                if drive_letter:
                    status_text = f"已装在{drive_letter}:盘"
                else:
                    status_text = "已装(路径未知)"
                status_color = "#1565C0"
            status_item = self.table_dev_env.item(row, 7)
            if status_item:
                status_item.setText(status_text)
                status_item.setForeground(QColor(status_color))
                if status_font:
                    status_item.setFont(status_font)
                    status_item.setToolTip("单击此单元格可弹出下载菜单")
                else:
                    status_item.setToolTip("")

            # 第8列：提示
            special = tool["special"]
            if special == "pip":
                tip_text, tip_color = "⚠️ pip 装到 site-packages，需 Python 装到 D 盘", "#E53935"
                tip_tooltip = tool["clean_guide"]
            elif special == "docker":
                tip_text, tip_color = "⚠️ 需用 wsl --export/import 迁移", "#FB8C00"
                tip_tooltip = tool["clean_guide"]
            elif special == "wsl":
                tip_text, tip_color = "⚠️ 需用 wsl --export/import 迁移", "#FB8C00"
                tip_tooltip = tool["clean_guide"]
            elif special == "vs":
                tip_text, tip_color = "⚠️ 需用 VS Installer 改路径", "#FB8C00"
                tip_tooltip = tool["clean_guide"]
            elif not status["installed"]:
                tip_text, tip_color = "未检测到此工具", "#9E9E9E"
                tip_tooltip = tool["clean_guide"]
            elif data_migrated:
                cur_path = status.get("current_path", "") or status.get("symlink_target", "")
                p = cur_path.replace("\\\\?\\", "").replace("/", "\\")
                migrate_drive = p[0].upper() if len(p) >= 2 and p[1] == ':' else cfg_info.get("target_drive", target_drive)
                if status["configured"]:
                    tip_text = f"环境变量已指向{migrate_drive}:盘，C盘数据已迁移（符号链接）"
                else:
                    tip_text = f"C盘数据已迁移到{migrate_drive}:盘（符号链接），环境变量未改但可正常工作"
                tip_color = "#2E7D32"
                tip_tooltip = tip_text
            elif status["configured"]:
                tip_text, tip_color = "环境变量已指向目标盘，新装的包会去那里", "#43A047"
                tip_tooltip = tool["clean_guide"]
            elif status["on_c"]:
                tip_text, tip_color = "当前装在C盘，可配置环境变量迁移", "#1565C0"
                tip_tooltip = tool["clean_guide"]
            else:
                cur_path = status["current_path"] or ""
                p = cur_path.replace("\\\\?\\", "").replace("/", "\\")
                drive_letter = p[0].upper() if len(p) >= 2 and p[1] == ':' else ""
                if drive_letter and drive_letter != target_drive.upper():
                    tip_text = f"已装在{drive_letter}:盘，无需配置（与目标盘不同）"
                    tip_color = "#5D4037"
                elif drive_letter:
                    tip_text = f"已装在{drive_letter}:盘（即目标盘），无需配置"
                    tip_color = "#43A047"
                else:
                    tip_text, tip_color = "—", "#424242"
                tip_tooltip = tool["clean_guide"]
            tip_item = self.table_dev_env.item(row, 8)
            if tip_item:
                tip_item.setText(tip_text)
                tip_item.setForeground(QColor(tip_color))
                tip_item.setToolTip(tip_tooltip)

            # 整行背景色：勾选状态优先（浅蓝），其次特殊状态色，最后默认
            # （QColor() 会渲染成黑色，必须用 QBrush() 默认构造 = 无填充，
            # 让 QTableWidget 的 alternatingRowColors 白/灰交替生效）
            is_checked = chk_item and chk_item.checkState() == Qt.Checked
            if is_checked:
                bg = QBrush(QColor("#90CAF9"))
            elif data_migrated:
                bg = QBrush(QColor("#E8F5E9"))
            elif special == "pip":
                bg = QBrush(QColor("#FFEBEE"))
            elif special in ("docker", "wsl", "vs"):
                bg = QBrush(QColor("#FFF3E0"))
            else:
                bg = QBrush()  # 默认无填充（透明），保留 alternatingRowColors 效果
            for c in range(9):
                item = self.table_dev_env.item(row, c)
                if item:
                    item.setBackground(bg)
        finally:
            self.table_dev_env.setSortingEnabled(was_sorting_enabled)
            self.table_dev_env.blockSignals(False)

        return True

    def _preload_dev_env_table(self):
        """程序启动时预加载开发环境迁移表格（后台，不等用户点击 Tab）

        目的：用户切到该 Tab 时能立即看到数据，不用等加载。
        流程：先从 state.json 缓存秒开（增量更新骨架行，不重建整表）→ 后台静默刷新最新数据。
        """
        if not hasattr(self, 'table_dev_env'):
            return
        # 骨架已在 __init__ 中填充，这里只尝试缓存命中（增量更新每行）
        if self._load_dev_env_cache():
            # 缓存命中：表格已秒开，后台静默刷新一次（流式更新覆盖缓存值）
            QTimer.singleShot(200, lambda: self._refresh_dev_env_table(silent=True))
        else:
            # 无缓存：骨架已显示"加载中..."，立即后台静默刷新（流式逐行更新）
            QTimer.singleShot(100, lambda: self._refresh_dev_env_table(silent=True))

    def _on_dev_env_tab_changed(self, index):
        """切到开发环境迁移 Tab 时，如表格还没加载过则触发加载"""
        if index == 4 and hasattr(self, 'table_dev_env') and self.table_dev_env.rowCount() == 0:
            # 表格为空（预加载还没跑完或没预加载）→ 立即加载
            if self._load_dev_env_cache():
                # 缓存命中：后台静默刷新
                QTimer.singleShot(500, lambda: self._refresh_dev_env_table(silent=True))
            else:
                # 无缓存：立即刷新
                self._refresh_dev_env_table()

    def _save_dev_env_cache(self, rows_data, target_drive):
        """保存开发环境工具状态到 state.json 缓存
        下次启动时可直接加载，避免每次重新检测（检测要调 subprocess，慢）
        """
        try:
            cache = {
                "target_drive": target_drive,
                "saved_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "rows": []
            }
            for tool, status in rows_data:
                cache["rows"].append({
                    "tool_id": tool["id"],
                    "tool_name": tool["name"],
                    "category": tool["category"],
                    "special": tool["special"],
                    "clean_guide": tool["clean_guide"],
                    "status": status,  # {installed, current_path, on_c, configured}
                    "suggest_path": dev_get_suggest_path(tool, target_drive),
                })
            self.cfg["dev_env_status_cache"] = cache
            save_all(self.cfg)
        except Exception as e:
            log.error(f"保存开发环境状态缓存失败: {e}")

    def _load_dev_env_cache(self):
        """从 state.json 加载开发环境工具状态缓存（增量更新，不重建整表）

        骨架表格已在 __init__ 中预填充，本方法遍历缓存，
        对每个工具调 _update_single_dev_env_row 增量更新对应行。
        避免清表重建造成的视觉闪烁。

        :return: True 表示缓存命中并已更新表格；False 表示无缓存或缓存无效
        """
        cache = self.cfg.get("dev_env_status_cache")
        if not cache or not isinstance(cache, dict):
            return False
        rows = cache.get("rows")
        if not rows or not isinstance(rows, list):
            return False
        target_drive = cache.get("target_drive", "D")
        # 校验缓存里的工具 id 和当前 DEV_TOOLS 一致（防止工具列表更新后缓存失效）
        current_ids = {t["id"] for t in DEV_TOOLS}
        cached_ids = {r.get("tool_id") for r in rows}
        if current_ids != cached_ids:
            log.info("开发环境缓存工具列表已变更，丢弃旧缓存")
            return False
        # 同步盘符到下拉框（防止上次保存的盘符与当前下拉框不一致）
        if hasattr(self, 'dev_target_drive'):
            idx = self.dev_target_drive.findText(target_drive)
            if idx >= 0:
                self.dev_target_drive.setCurrentIndex(idx)
        # 增量更新每行（不清表重建）
        updated = 0
        for r in rows:
            tool_id = r.get("tool_id")
            tool = next((t for t in DEV_TOOLS if t["id"] == tool_id), None)
            if not tool:
                continue
            status = r.get("status", {})
            # 补全 status 字段（防止旧缓存缺字段）
            status.setdefault("installed", False)
            status.setdefault("current_path", "")
            status.setdefault("on_c", False)
            status.setdefault("configured", False)
            status.setdefault("size_mb", 0)
            status.setdefault("is_symlink", False)
            status.setdefault("symlink_target", "")
            status.setdefault("original_path", status.get("current_path", ""))
            # 修复坏数据：current_path 不是有效路径（如旧 bug 写入的 "0 MB"）时清空
            cp = status.get("current_path", "")
            if cp and (len(cp) < 3 or cp[1:2] != ":" or
                       " MB" in cp or " GB" in cp or " KB" in cp):
                log.warning(f"缓存中 {tool_id} 的 current_path 异常: {cp!r}，已清空")
                status["current_path"] = ""
                status["original_path"] = ""
            try:
                # skip_io_checks=True：缓存命中模式，跳过 is_symlink/os.path.exists 同步 IO
                # 静默刷新完成后会带新数据（含实时 IO 检查）覆盖
                if self._update_single_dev_env_row(tool, status, target_drive, skip_io_checks=True):
                    updated += 1
            except Exception as e:
                log.error(f"缓存增量更新 {tool_id} 失败: {e}")
        if updated == 0:
            return False
        try:
            saved_time = cache.get("saved_time", "?")
            # 统计缓存中的工具状态
            installed = sum(1 for r in rows if r.get("status", {}).get("installed"))
            on_c = sum(1 for r in rows if r.get("status", {}).get("on_c"))
            configured = sum(1 for r in rows if r.get("status", {}).get("configured"))
            self.stat_dev_env.setText(
                f"共{updated}项 | 已装{installed}项 | "
                f"在C盘待配置{on_c}项 | 已配置到目标盘{configured}项"
                f"（缓存，{saved_time}）")
            self.status_label.setText(
                f"（缓存数据，最后更新: {saved_time}）后台正在刷新...")
            log.info(f"开发环境缓存命中，已增量更新 {updated} 项（保存于 {saved_time}）")
            return True
        except Exception as e:
            log.error(f"加载开发环境缓存失败: {e}")
            return False

    def _safe_cancel_dev_env_worker(self, attr_name, wait_ms=3000):
        """安全取消后台 dev_env worker（DevEnvSizeWorker / DevEnvRefreshWorker /
        DevEnvApplyWorker / _RestoreDataWorker）

        关键修复：取消后不能立即把属性设为 None，否则若 worker 仍在 os.walk 深层目录
        （如 Android SDK 1.5GB 几千个文件，wait 超时未退出），Python 端引用丢失会触发
        GC，QThread C++ 对象在线程仍运行时被销毁 → segfault（程序闪退，无 Python 日志）。

        本方法把 worker 移到 self._old_dev_env_workers 列表保留引用，并连接 finished
        信号到 _cleanup_old_worker，等线程真正退出后再 deleteLater。
        兼容没有 cancel() 方法的 worker（如 DevEnvApplyWorker / _RestoreDataWorker）。
        """
        try:
            worker = getattr(self, attr_name, None)
            if worker is None:
                return False
            # cancel 是可选的（DevEnvSizeWorker/DevEnvRefreshWorker 有，其他 worker 可能没有）
            if hasattr(worker, 'cancel'):
                worker.cancel()
            worker.quit()
            worker.wait(wait_ms)
            # 不设 None：保留引用到 _old_dev_env_workers，防止 GC 引发 segfault
            # worker 真正 finished 后由 _cleanup_old_worker 清理
            def _cleanup_old_worker(_w=worker):
                try:
                    if _w in self._old_dev_env_workers:
                        self._old_dev_env_workers.remove(_w)
                    _w.deleteLater()
                except Exception as e:
                    log.debug("忽略异常: %s", e)
            worker.finished.connect(_cleanup_old_worker)
            self._old_dev_env_workers.append(worker)
            setattr(self, attr_name, None)
            return True
        except Exception as e:
            log.error(f"安全取消 {attr_name} 失败: {e}")
            return False

    def _on_dev_target_drive_changed(self, new_drive):
        """盘符变化时只更新"建议新路径"列（毫秒级，不触发完整刷新）

        建议新路径是纯字符串拼接（{D} → E:），不需要重新检测工具状态。
        完整刷新（检测工具状态+大小）由"刷新检测"按钮触发。
        通用：当前路径已在非 C 盘的行，建议路径保持空（不显示默认值）。
        """
        try:
            new_drive = (new_drive or "").strip().upper()
            if not new_drive:
                return
            # 遍历表格所有行，从 name_item 的 UserRole 取 tool_id，查 DEV_TOOLS 重算建议路径
            for row in range(self.table_dev_env.rowCount()):
                name_item = self.table_dev_env.item(row, 1)
                if not name_item:
                    continue
                tool_id = name_item.data(Qt.UserRole)
                if not tool_id:
                    continue
                # 从 DEV_TOOLS 查找工具定义
                tool = None
                for t in DEV_TOOLS:
                    if t.get("id") == tool_id:
                        tool = t
                        break
                if not tool:
                    continue
                # 通用：检查当前路径列，如果已在非 C 盘或已是符号链接，建议路径保持空
                cur_item = self.table_dev_env.item(row, 3)
                cur_path_str = (cur_item.text() if cur_item else "").replace("\\\\?\\", "")
                # 状态列含"已迁移"或"已装在X:盘"（X 非 C）时，建议路径置空
                status_item = self.table_dev_env.item(row, 7)
                status_text = (status_item.text() if status_item else "") or ""
                already_migrated = ("已迁移到" in status_text) or ("已装在" in status_text and "C盘" not in status_text)
                if already_migrated or (cur_path_str and not cur_path_str.lower().startswith("c:")):
                    suggest_path = ""
                    suggest_tip = "数据已不在 C 盘，如需迁移请双击选择新路径"
                else:
                    suggest_path = dev_get_suggest_path(tool, new_drive)
                    suggest_tip = suggest_path + "\n（双击可修改路径）"
                suggest_item = self.table_dev_env.item(row, 6)
                if suggest_item:
                    suggest_item.setText(suggest_path)
                    suggest_item.setToolTip(suggest_tip)
            log.info(f"盘符切换到 {new_drive}:，已更新建议新路径列（{self.table_dev_env.rowCount()} 行）")
        except Exception as e:
            log.error(f"更新建议新路径列失败: {e}")

    def _refresh_dev_env_table(self, silent=False):
        """刷新开发环境工具检测表（后台线程，避免卡 UI）

        :param silent: True=静默刷新（缓存命中后的后台更新）：
                       不禁用按钮、不显示"正在检测..."、保留缓存表格显示
                       直到新数据就绪才替换；False=用户主动刷新（默认）
        """
        try:
            # 防重入：正在刷新时直接跳过，但给用户提示
            if getattr(self, '_dev_env_refreshing', False):
                self.status_label.setText("后台刷新中，请稍候...")
                return
            target_drive = self.dev_target_drive.currentText()
            self._dev_env_refreshing = True
            if not silent:
                self.btn_refresh_dev.setEnabled(False)
                self.status_label.setText(f"正在检测开发工具状态（目标盘 {target_drive}:）...")
            else:
                self.status_label.setText(f"（缓存已加载）后台刷新中...")
            self.on_monitor_log("dev_env", f"开始检测开发工具状态（目标盘 {target_drive}:）...")

            # 创建 Worker，保存引用避免被 GC
            worker = DevEnvRefreshWorker(DEV_TOOLS, target_drive, config=self.cfg)
            self._dev_env_refresh_worker = worker
            # 流式刷新计数器（用于显示进度）
            self._dev_env_stream_count = 0
            self._dev_env_stream_total = len(DEV_TOOLS)

            def _on_row_ready(tool, status, drive):
                """主线程：单个工具检测完成，立即增量更新该行（不用等全部完成）"""
                try:
                    self._update_single_dev_env_row(tool, status, drive)
                    self._dev_env_stream_count += 1
                    # 实时更新统计栏（让用户看到进度）
                    if self._dev_env_stream_count % 5 == 0 or self._dev_env_stream_count >= self._dev_env_stream_total:
                        self.stat_dev_env.setText(
                            f"正在检测... {self._dev_env_stream_count}/{self._dev_env_stream_total} 项完成")
                except Exception as e:
                    log.error(f"流式更新单行失败 ({tool.get('id')}): {e}")

            def _on_done(rows_data, drive):
                """主线程：全部完成，最终统计 + 保存缓存"""
                from utils import is_symlink  # 本闭包内使用（721 行）
                try:
                    # 流式刷新已经把每行更新好了，这里只做最终统计 + 缓存
                    # 不再调 _populate_dev_env_table（避免清表重建造成视觉闪烁）
                    # 但如果骨架未填充或行数不匹配，仍走全量填充兜底
                    if self.table_dev_env.rowCount() != len(rows_data):
                        log.info("表格行数与检测结果不匹配，走全量填充兜底")
                        self._populate_dev_env_table(rows_data, drive)
                except Exception as e:
                    log.error(f"填充开发环境表格失败: {e}")
                    self.status_label.setText(f"填充表格失败: {e}")
                if not silent:
                    self.btn_refresh_dev.setEnabled(True)
                self._dev_env_refreshing = False
                installed = sum(1 for _, s in rows_data if s["installed"])
                on_c = sum(1 for _, s in rows_data if s["on_c"])
                configured = sum(1 for _, s in rows_data if s["configured"])
                migrated = 0
                try:
                    dev_env_cfg = self.cfg.get("dev_env_configured") or {}
                    for tool, status in rows_data:
                        cfg_info = dev_env_cfg.get(tool["id"]) or {}
                        sp = (cfg_info.get("source_path") or "").replace("\\\\?\\", "")
                        if sp and is_symlink(sp):
                            migrated += 1
                except Exception as e:
                    log.debug("忽略异常: %s", e)
                self.stat_dev_env.setText(
                    f"共{len(rows_data)}项 | 已装{installed}项 | "
                    f"在C盘待配置{on_c}项 | 已配置到目标盘{configured}项"
                    f" | 数据已迁移{migrated}项")
                if silent:
                    self.status_label.setText(
                        f"已更新到最新（共{len(rows_data)}项，已装{installed}项，"
                        f"在C盘{on_c}项，已配置到{drive}:盘{configured}项）")
                else:
                    self.status_label.setText(f"开发工具检测完成（目标盘 {drive}:）")
                self.on_monitor_log("dev_env",
                    f"开发工具检测完成：共{len(rows_data)}项，已装{installed}项，"
                    f"在C盘{on_c}项，已配置到{drive}:盘{configured}项")
                # 保存到缓存（state.json），下次启动可快速加载
                self._save_dev_env_cache(rows_data, drive)

            def _on_error(err):
                self._dev_env_refreshing = False
                if not silent:
                    self.btn_refresh_dev.setEnabled(True)
                self.status_label.setText(f"开发工具检测失败: {err}")
                self.on_monitor_log("dev_env", f"开发工具检测失败: {err}")
                log.error(f"开发工具检测失败: {err}")

            worker.row_ready_signal.connect(_on_row_ready)
            worker.finished_signal.connect(_on_done)
            worker.error_signal.connect(_on_error)
            worker.start()
        except Exception as e:
            # 顶层兜底：防止刷新表格时异常导致闪退
            self._dev_env_refreshing = False
            if not silent:
                self.btn_refresh_dev.setEnabled(True)
            log.error(f"_refresh_dev_env_table 异常: {e}")
            self.on_monitor_log("error", f"刷新开发环境表格异常: {e}")

    def _partial_refresh_dev_env_rows(self, tool_ids, reason=""):
        """局部刷新：只重新检测并更新指定工具的行（不全表刷新）

        迁移/还原后调用，避免全表刷新 26+ 个工具很慢。
        :param tool_ids: 要刷新的工具 id 列表
        :param reason: 刷新原因（用于日志）
        """
        if not tool_ids:
            return
        try:
            target_drive = self.dev_target_drive.currentText()
            # 筛选出要刷新的工具
            tools_to_refresh = [t for t in DEV_TOOLS if t["id"] in set(tool_ids)]
            if not tools_to_refresh:
                return

            self.on_monitor_log("dev_env",
                f"局部刷新 {len(tools_to_refresh)} 个工具: {reason}")

            # 清空 detect/path/size 缓存（迁移/还原后路径和大小都变了，必须重新检测）
            try:
                from dev_env_migrate import clear_detect_path_cache as _clear_cache, clear_size_cache as _clear_size
                _clear_cache()
                _clear_size()
            except Exception as e:
                log.error(f"清空 detect/path/size 缓存失败: {e}")

            worker = DevEnvRefreshWorker(tools_to_refresh, target_drive, config=self.cfg)
            # 保存引用避免 GC（用列表收集旧 worker，等待 finished 后自动 deleteLater）
            self._old_dev_env_workers.append(worker)
            worker.finished.connect(lambda w=worker: self._cleanup_old_worker(w))

            def _on_partial_done(rows_data, drive):
                """局部刷新完成：只更新对应行，不清空表格"""
                try:
                    self._partial_update_dev_env_rows(rows_data, drive)
                except Exception as e:
                    log.error(f"局部更新表格失败: {e}")
                # 更新统计
                try:
                    installed = sum(1 for _, s in rows_data if s["installed"])
                    on_c = sum(1 for _, s in rows_data if s["on_c"])
                    configured = sum(1 for _, s in rows_data if s["configured"])
                    self.on_monitor_log("dev_env",
                        f"局部刷新完成（{len(rows_data)} 项：已装{installed}，"
                        f"在C盘{on_c}，已配置{configured}）")
                except Exception as e:
                    log.debug("忽略异常: %s", e)

            worker.finished_signal.connect(_on_partial_done)
            worker.error_signal.connect(
                lambda e: self.on_monitor_log("error", f"局部刷新失败: {e}"))
            worker.start()
        except Exception as e:
            log.error(f"_partial_refresh_dev_env_rows 异常: {e}")
            # 兜底：全表刷新
            try:
                self._refresh_dev_env_table(silent=True)
            except Exception as e:
                log.debug("忽略异常: %s", e)

    def _cleanup_old_worker(self, worker):
        """清理已完成的旧 worker（从 _old_dev_env_workers 列表移除）"""
        try:
            if worker in self._old_dev_env_workers:
                self._old_dev_env_workers.remove(worker)
            worker.deleteLater()
        except Exception as e:
            log.debug("忽略异常: %s", e)

    def _partial_update_dev_env_rows(self, rows_data, target_drive):
        """局部更新表格行：只更新 rows_data 中的工具，保留其他行

        与 _populate_dev_env_table 不同，不清空表格，只更新对应行。
        """
        from dev_env_migrate import (get_tool_default_c_path as dev_get_tool_default_c_path,
                                     get_suggest_path as dev_get_suggest_path)
        from utils import is_symlink as _is_symlink
        # _format_size 来自 ui_widgets.py

        # rows_data → {tool_id: (tool, status)}
        rows_map = {tool["id"]: (tool, status) for tool, status in rows_data}
        if not rows_map:
            return

        # 诊断日志：打印每个工具的 status 值（排查回滚后状态不更新问题）
        for tid, (t, s) in rows_map.items():
            log.info(f"[PARTIAL_REFRESH] {tid}: installed={s.get('installed')}, "
                     f"current_path={s.get('current_path')!r}, on_c={s.get('on_c')}, "
                     f"configured={s.get('configured')}, is_symlink={s.get('is_symlink')}")

        # 加载 dev_env_configured（用于 source_path 和 data_migrated 判断）
        dev_env_cfg = self.cfg.get("dev_env_configured") or {}
        migrated_srcs = set()
        for m in (self.cfg.get("migrated") or []):
            s = (m.get("src") or "").replace("\\\\?\\", "").lower().rstrip("\\")
            if s:
                migrated_srcs.add(s)

        self.table_dev_env.setSortingEnabled(False)
        self.table_dev_env.blockSignals(True)

        try:
            for row in range(self.table_dev_env.rowCount()):
                name_item = self.table_dev_env.item(row, 1)
                if not name_item:
                    continue
                tid = name_item.data(Qt.UserRole)
                if tid not in rows_map:
                    continue  # 不是要刷新的行，跳过

                tool, status = rows_map[tid]
                tool_id = tid
                cfg_info = dev_env_cfg.get(tool_id) or {}
                source_path = cfg_info.get("source_path", "")

                # 检查数据是否已迁移
                data_migrated = False
                if source_path:
                    sp = source_path.replace("\\\\?\\", "")
                    if _is_symlink(sp):
                        data_migrated = True
                    elif sp.replace("\\\\?\\", "").lower().rstrip("\\") in migrated_srcs:
                        data_migrated = True
                if not data_migrated:
                    default_c = dev_get_tool_default_c_path(tool)
                    if default_c:
                        dc = default_c.replace("\\\\?\\", "")
                        if _is_symlink(dc):
                            data_migrated = True
                if not data_migrated and status.get("is_symlink"):
                    data_migrated = True

                # ===== 更新第3列：当前路径 =====
                cur_path = (status["current_path"] or "").replace("\\\\?\\", "").replace("/", "\\") \
                           or ("未安装" if not status["installed"] else "（无）")
                cur_item = self.table_dev_env.item(row, 3)
                if cur_item:
                    cur_item.setText(cur_path)
                    cur_item.setToolTip(cur_path)
                    if not status["installed"]:
                        cur_item.setForeground(QColor("#9E9E9E"))
                    elif status["on_c"]:
                        cur_item.setForeground(QColor("#E53935"))
                    else:
                        cur_item.setForeground(QColor("#424242"))

                # ===== 更新第5列：占用空间 =====
                size_mb = status.get("size_mb", 0)
                if not status["installed"]:
                    size_text, size_color = "—", "#9E9E9E"
                elif size_mb == -1:
                    size_text, size_color = "已迁移", "#2E7D32"
                elif size_mb == -2:
                    size_text, size_color = "未生成", "#9E9E9E"
                elif size_mb == -3:
                    size_text, size_color = "—", "#9E9E9E"
                elif size_mb > 0:
                    size_text = _format_size(size_mb)
                    size_color = "#E65100" if size_mb >= 500 else "#1565C0"
                else:
                    size_text, size_color = "0 MB", "#9E9E9E"
                size_item = self.table_dev_env.item(row, 5)
                if size_item:
                    size_item.setText(size_text)
                    size_item.setForeground(QColor(size_color))
                    if data_migrated:
                        mig_d = cfg_info.get("target_drive", "?")
                        size_item.setToolTip(f"数据已迁移到 {mig_d}: 盘")
                    elif size_mb > 0:
                        size_item.setToolTip(f"{size_mb:.1f} MB")
                    else:
                        size_item.setToolTip(size_text)

                # ===== 更新第7列：状态 =====
                if not status["installed"]:
                    status_text = "未安装（单击下载）"
                    status_color = "#1565C0"
                    status_font = QFont()
                    status_font.setUnderline(True)
                elif data_migrated:
                    cp = status.get("current_path", "") or status.get("symlink_target", "")
                    p = cp.replace("\\\\?\\", "").replace("/", "\\")
                    mig_d = p[0].upper() if (len(p) >= 2 and p[1] == ':') else cfg_info.get("target_drive", target_drive)
                    status_text = f"✓ 已迁移到{mig_d}:盘"
                    status_color = "#2E7D32"
                    status_font = None
                elif status["configured"]:
                    status_text = f"✓ 已配置到{target_drive}:盘"
                    status_color = "#43A047"
                    status_font = None
                elif status["on_c"]:
                    status_text = "⚠️ 装在C盘"
                    status_color = "#FB8C00"
                    status_font = None
                else:
                    cp = status["current_path"] or ""
                    p = cp.replace("\\\\?\\", "").replace("/", "\\")
                    dl = p[0].upper() if (len(p) >= 2 and p[1] == ':') else ""
                    status_text = f"已装在{dl}:盘" if dl else "已装(路径未知)"
                    status_color = "#1565C0"
                    status_font = None
                status_item = self.table_dev_env.item(row, 7)
                if status_item:
                    status_item.setText(status_text)
                    status_item.setForeground(QColor(status_color))
                    if status_font:
                        status_item.setFont(status_font)
                        status_item.setToolTip("单击此单元格可弹出下载菜单（最新版/LTS版/访问官网）")
                    else:
                        # 恢复默认字体（去掉下划线）
                        status_item.setFont(QFont())
                        status_item.setToolTip(status_text)

                # ===== 更新第8列：提示 =====
                special = tool["special"]
                if special == "pip":
                    tip_text = "⚠️ pip 装到 site-packages，需 Python 装到 D 盘"
                    tip_color = "#E53935"
                elif special == "docker":
                    tip_text = "⚠️ 需用 wsl --export/import 迁移"
                    tip_color = "#FB8C00"
                elif special == "wsl":
                    tip_text = "⚠️ 需用 wsl --export/import 迁移"
                    tip_color = "#FB8C00"
                elif special == "vs":
                    tip_text = "⚠️ 需用 VS Installer 改路径"
                    tip_color = "#FB8C00"
                elif not status["installed"]:
                    tip_text = "未检测到此工具"
                    tip_color = "#9E9E9E"
                elif data_migrated:
                    cp = status.get("current_path", "") or status.get("symlink_target", "")
                    p = cp.replace("\\\\?\\", "").replace("/", "\\")
                    mig_d = p[0].upper() if (len(p) >= 2 and p[1] == ':') else cfg_info.get("target_drive", target_drive)
                    if status["configured"]:
                        tip_text = f"环境变量已指向{mig_d}:盘，C盘数据已迁移（符号链接）"
                    else:
                        tip_text = f"C盘数据已迁移到{mig_d}:盘（符号链接），环境变量未改但可正常工作"
                    tip_color = "#2E7D32"
                elif status["configured"]:
                    tip_text = "环境变量已指向目标盘，新装的包会去那里"
                    tip_color = "#43A047"
                elif status["on_c"]:
                    tip_text = "当前装在C盘，可配置环境变量迁移"
                    tip_color = "#1565C0"
                else:
                    cp = status["current_path"] or ""
                    p = cp.replace("\\\\?\\", "").replace("/", "\\")
                    dl = p[0].upper() if (len(p) >= 2 and p[1] == ':') else ""
                    if dl and dl != target_drive.upper():
                        tip_text = f"已装在{dl}:盘，无需配置（与目标盘不同）"
                        tip_color = "#5D4037"
                    elif dl:
                        tip_text = f"已装在{dl}:盘（即目标盘），无需配置"
                        tip_color = "#43A047"
                    else:
                        tip_text = "—"
                        tip_color = "#424242"
                tip_item = self.table_dev_env.item(row, 8)
                if tip_item:
                    tip_item.setText(tip_text)
                    tip_item.setForeground(QColor(tip_color))

                # ===== 整行变色 =====
                if data_migrated:
                    bg = QColor("#E8F5E9")
                elif special in ("pip",):
                    bg = QColor("#FFEBEE")
                elif special in ("docker", "wsl", "vs"):
                    bg = QColor("#FFF3E0")
                else:
                    # 保留原色（避免覆盖用户勾选的浅蓝底）
                    bg = None
                if bg is not None:
                    for c in range(self.table_dev_env.columnCount()):
                        cell = self.table_dev_env.item(row, c)
                        if cell:
                            cell.setBackground(bg)
        finally:
            self.table_dev_env.setSortingEnabled(True)
            self.table_dev_env.blockSignals(False)

    def _populate_dev_env_table(self, rows_data, target_drive, skip_size_calc=False):
        """填充开发环境表格
        :param skip_size_calc: True 时跳过异步算 size（缓存命中时用，直接显示缓存值）
        """
        self.table_dev_env.setSortingEnabled(False)
        self.table_dev_env.setRowCount(0)
        # 填充时阻塞 itemChanged 信号，避免触发 _on_dev_env_item_changed
        self.table_dev_env.blockSignals(True)
        from utils import is_symlink  # 本函数内使用（721/1082/1092 行）
        installed_count = 0
        on_c_count = 0
        configured_count = 0
        migrated_count = 0

        # 加载 dev_env_configured 中的 source_path（用于检查数据是否已迁移）
        # source_path 是配置前捕获的 C 盘原始路径，若它已成为符号链接说明数据已迁移到 D 盘
        dev_env_cfg = self.cfg.get("dev_env_configured") or {}
        # 同时检查 self.cfg["migrated"] 中的记录（待迁移区也可能搬过同一目录）
        migrated_srcs = set()
        for m in (self.cfg.get("migrated") or []):
            s = (m.get("src") or "").replace("\\\\?\\", "").lower().rstrip("\\")
            if s:
                migrated_srcs.add(s)

        # 缓存命中模式（skip_size_calc=True）：跳过 is_symlink/os.path.exists 同步 IO 检查，
        # 只用缓存中的 is_symlink 标志判断 data_migrated，实现秒开。
        # 静默刷新完成后会带新数据（含实时 IO 检查）替换表格。
        skip_io_checks = skip_size_calc

        for tool, status in rows_data:
            # 检查此工具的 C 盘数据是否已迁移（通过符号链接或 migrated 记录）
            tool_id = tool["id"]
            cfg_info = dev_env_cfg.get(tool_id) or {}
            source_path = cfg_info.get("source_path", "")
            data_migrated = False
            if not skip_io_checks:
                # 完整模式：实时 IO 检查 is_symlink（用户主动刷新时走这里）
                if source_path:
                    sp = source_path.replace("\\\\?\\", "")
                    if is_symlink(sp):
                        data_migrated = True
                    elif sp.replace("\\\\?\\", "").lower().rstrip("\\") in migrated_srcs:
                        data_migrated = True
                # source_path 在 D 盘（env var 之前已设置）时，用默认 C 盘路径检查
                # 是否为符号链接（开发环境迁移区迁移后 C 盘默认路径会变成符号链接）
                if not data_migrated:
                    default_c = dev_get_tool_default_c_path(tool)
                    if default_c:
                        dc = default_c.replace("\\\\?\\", "")
                        if is_symlink(dc):
                            data_migrated = True
                        elif dc.lower().rstrip("\\") in migrated_srcs:
                            data_migrated = True
            # 缓存模式 + 完整模式都用：status 中的 is_symlink 标志
            # （缓存命中时这是唯一的 data_migrated 判断依据）
            if not data_migrated and status.get("is_symlink"):
                data_migrated = True
            row = self.table_dev_env.rowCount()
            self.table_dev_env.insertRow(row)

            # 第0列：勾选框（默认不勾选，用户自行勾选要配置的工具）
            chk_item = QTableWidgetItem()
            # ItemIsSelectable 让点击勾选框列也能触发整行选中（和点其他列一样）
            chk_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk_item.setCheckState(Qt.Unchecked)
            # 设置 UserRole 数据，让点击表头排序时按勾选状态（0/1）而非文本
            chk_item.setData(Qt.UserRole, 0)
            self.table_dev_env.setItem(row, 0, chk_item)

            # 第1列：工具名（UserRole 存 tool_id 供反查）
            name_item = QTableWidgetItem(tool["name"])
            name_item.setToolTip(tool["name"])
            name_item.setData(Qt.UserRole, tool["id"])
            self.table_dev_env.setItem(row, 1, name_item)

            # 第2列：类别
            cat_item = QTableWidgetItem(tool["category"])
            self.table_dev_env.setItem(row, 2, cat_item)

            # 第3列：当前路径（统一反斜杠，兼容缓存中的旧混合斜杠路径）
            cur_path = (status["current_path"] or "").replace("\\\\?\\", "").replace("/", "\\") \
                       or ("未安装" if not status["installed"] else "（无）")
            cur_item = QTableWidgetItem(cur_path)
            cur_item.setToolTip(cur_path)
            if not status["installed"]:
                cur_item.setForeground(QColor("#9E9E9E"))  # 灰色
            elif status["on_c"]:
                cur_item.setForeground(QColor("#E53935"))  # 红色（在C盘）
            self.table_dev_env.setItem(row, 3, cur_item)

            # 第4列：C盘原位置（显示该工具的 C 盘默认路径，让用户知道数据"本该"在哪）
            # 优先级：source_path（配置时捕获的C盘路径） > dev_get_tool_default_c_path（静态默认）
            c_original = ""
            c_orig_tooltip = ""
            c_orig_color = "#9E9E9E"  # 默认灰色
            c_orig_mark = False  # 是否需要"ⓘ 最初安装在其他盘"标记
            if source_path and source_path[1:2] == ":" and source_path[0].upper() == "C":
                # 配置时捕获过 C 盘原始路径 → 显示这个
                c_original = source_path.replace("\\\\?\\", "")
                c_orig_color = "#616161"  # 深灰（有记录的真实路径）
                c_orig_tooltip = f"配置前 C 盘原始路径：\n{c_original}"
            else:
                # 没配置过 → 显示静态默认路径
                default_c = dev_get_tool_default_c_path(tool)
                if default_c:
                    c_original = default_c.replace("\\\\?\\", "")
                    # 判断是否"初始就装在其他盘"：工具已安装但当前路径不在 C 盘且 C 盘默认路径不存在
                    # 缓存命中时跳过 os.path.exists IO 检查（静默刷新后会补全）
                    if (status["installed"] and not status["on_c"]
                            and (skip_io_checks or not os.path.exists(c_original))):
                        if skip_io_checks:
                            # 缓存模式：无法实时检查，只标"可能"，等静默刷新确认
                            c_orig_color = "#9E9E9E"
                            c_orig_tooltip = f"C 盘默认路径：{c_original}"
                        else:
                            # 最初安装在其他盘，C 盘从未有过数据
                            c_orig_mark = True
                            c_orig_color = "#9E9E9E"  # 灰色
                            c_orig_tooltip = (f"C 盘默认路径：{c_original}\n"
                                              f"ⓘ 此工具最初就装在其他盘，C 盘从未有过数据")
                    else:
                        c_orig_color = "#9E9E9E"
                        c_orig_tooltip = f"C 盘默认路径：{c_original}"
            if not c_original:
                c_original = "—"
                c_orig_tooltip = "此工具无固定 C 盘默认路径（如 conda/pip_install）"
            c_orig_item = QTableWidgetItem(c_original)
            c_orig_item.setToolTip(c_orig_tooltip)
            c_orig_item.setForeground(QColor(c_orig_color))
            if c_orig_mark:
                # 在文本后追加标记
                c_orig_item.setText(c_original + "  ⓘ")
            c_orig_item.setFlags(c_orig_item.flags() & ~Qt.ItemIsEditable)
            self.table_dev_env.setItem(row, 4, c_orig_item)

            # 第5列：占用空间（已在 DevEnvRefreshWorker 中算好，无需异步计算）
            # size_mb 值约定：>0=大小, 0=空目录, -1=符号链接(已迁移), -2=路径不存在, -3=计算失败
            size_mb = status.get("size_mb", 0)
            if not status["installed"]:
                size_text = "—"
                size_color = "#9E9E9E"
            elif size_mb == -1:
                size_text = "已迁移"
                size_color = "#2E7D32"
            elif size_mb == -2:
                size_text = "未生成"
                size_color = "#9E9E9E"
            elif size_mb == -3:
                size_text = "—"
                size_color = "#9E9E9E"
            elif size_mb > 0:
                size_text = _format_size(size_mb)
                size_color = "#E65100" if size_mb >= 500 else "#1565C0"
            else:
                size_text = "0 MB"
                size_color = "#9E9E9E"
            size_item = QTableWidgetItem(size_text)
            size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            size_item.setForeground(QColor(size_color))
            # tooltip 补充迁移状态信息
            if data_migrated:
                migrate_drive = cfg_info.get("target_drive", "?")
                size_item.setToolTip(f"数据已迁移到 {migrate_drive}: 盘")
            elif size_mb > 0:
                size_item.setToolTip(f"{size_mb:.1f} MB")
            else:
                size_item.setToolTip(size_text)
            size_item.setFlags(size_item.flags() & ~Qt.ItemIsEditable)
            self.table_dev_env.setItem(row, 5, size_item)

            # 第6列：建议新路径（双击可修改）
            # 通用：当前路径已在非 C 盘（如 D/E/F 盘）且不是符号链接时，不显示默认建议路径
            # 用户仍可双击修改（后悔药：把数据迁到另一个盘/目录）
            cur_path_str = (status.get("current_path") or "").replace("\\\\?\\", "")
            is_sym = status.get("is_symlink", False)
            on_c = status.get("on_c", False)
            if is_sym or (cur_path_str and not on_c):
                suggest_path = ""
                suggest_tip = "数据已不在 C 盘，如需迁移请双击选择新路径"
            else:
                suggest_path = dev_get_suggest_path(tool, target_drive)
                suggest_tip = suggest_path + "\n（双击可修改路径）"
            suggest_item = QTableWidgetItem(suggest_path)
            suggest_item.setToolTip(suggest_tip)
            self.table_dev_env.setItem(row, 6, suggest_item)

            # 第6列：状态（明确显示装在哪个盘）
            # 优先级：未安装 > 数据已迁移 > 已配置到目标盘 > 装在C盘 > 装在其他盘
            status_font = None
            if not status["installed"]:
                # 未安装：蓝色+下划线，提示可单击下载
                status_text = "未安装（单击下载）"
                status_color = "#1565C0"
                status_font = QFont()
                status_font.setUnderline(True)
            elif data_migrated:
                # 数据已迁移（C盘是符号链接指向目标盘）
                # 从 current_path 或 symlink_target 提取实际盘符
                cur_path = status.get("current_path", "") or status.get("symlink_target", "")
                p = cur_path.replace("\\\\?\\", "").replace("/", "\\")
                migrate_drive = ""
                if len(p) >= 2 and p[1] == ':':
                    migrate_drive = p[0].upper()
                else:
                    migrate_drive = cfg_info.get("target_drive", target_drive)
                status_text = f"✓ 已迁移到{migrate_drive}:盘"
                status_color = "#2E7D32"
            elif status["configured"]:
                # 已配置到目标盘（环境变量已改，但数据可能还在C盘）
                status_text = f"✓ 已配置到{target_drive}:盘"
                status_color = "#43A047"
            elif status["on_c"]:
                status_text = "⚠️ 装在C盘"
                status_color = "#FB8C00"
            else:
                # 已装但不在 C 盘，从路径提取实际盘符
                cur_path = status["current_path"] or ""
                p = cur_path.replace("\\\\?\\", "").replace("/", "\\")
                drive_letter = ""
                if len(p) >= 2 and p[1] == ':':
                    drive_letter = p[0].upper()
                if drive_letter:
                    status_text = f"已装在{drive_letter}:盘"
                else:
                    # 路径为空但工具已安装：说明无法检测到具体路径
                    status_text = "已装(路径未知)"
                status_color = "#1565C0"
            status_item = QTableWidgetItem(status_text)
            status_item.setForeground(QColor(status_color))
            if status_font:
                status_item.setFont(status_font)
                status_item.setToolTip("单击此单元格可弹出下载菜单（最新版/LTS版/访问官网）")
            self.table_dev_env.setItem(row, 7, status_item)

            # 第8列：提示（详细补充）
            # 优先级与状态列一致：data_migrated 最优先，避免显示过时的 clean_guide
            special = tool["special"]
            if special == "pip":
                tip_text = "⚠️ pip 装到 site-packages，需 Python 装到 D 盘"
                tip_color = "#E53935"
                tip_tooltip = tool["clean_guide"]
            elif special == "docker":
                tip_text = "⚠️ 需用 wsl --export/import 迁移"
                tip_color = "#FB8C00"
                tip_tooltip = tool["clean_guide"]
            elif special == "wsl":
                tip_text = "⚠️ 需用 wsl --export/import 迁移"
                tip_color = "#FB8C00"
                tip_tooltip = tool["clean_guide"]
            elif special == "vs":
                tip_text = "⚠️ 需用 VS Installer 改路径"
                tip_color = "#FB8C00"
                tip_tooltip = tool["clean_guide"]
            elif not status["installed"]:
                tip_text = "未检测到此工具"
                tip_color = "#9E9E9E"
                tip_tooltip = tool["clean_guide"]
            elif data_migrated:
                # 数据已迁移：显示迁移完成提示，不显示过时的 clean_guide
                cur_path = status.get("current_path", "") or status.get("symlink_target", "")
                p = cur_path.replace("\\\\?\\", "").replace("/", "\\")
                migrate_drive = ""
                if len(p) >= 2 and p[1] == ':':
                    migrate_drive = p[0].upper()
                else:
                    migrate_drive = cfg_info.get("target_drive", target_drive)
                if status["configured"]:
                    tip_text = f"环境变量已指向{migrate_drive}:盘，C盘数据已迁移（符号链接）"
                else:
                    tip_text = f"C盘数据已迁移到{migrate_drive}:盘（符号链接），环境变量未改但可正常工作"
                tip_color = "#2E7D32"
                tip_tooltip = tip_text
            elif status["configured"]:
                tip_text = "环境变量已指向目标盘，新装的包会去那里"
                tip_color = "#43A047"
                tip_tooltip = tool["clean_guide"]
            elif status["on_c"]:
                tip_text = "当前装在C盘，可配置环境变量迁移"
                tip_color = "#1565C0"
                tip_tooltip = tool["clean_guide"]
            else:
                # 已装在其他盘，显示具体盘符
                cur_path = status["current_path"] or ""
                p = cur_path.replace("\\\\?\\", "").replace("/", "\\")
                drive_letter = ""
                if len(p) >= 2 and p[1] == ':':
                    drive_letter = p[0].upper()
                if drive_letter and drive_letter != target_drive.upper():
                    tip_text = f"已装在{drive_letter}:盘，无需配置（与目标盘不同）"
                    tip_color = "#5D4037"
                elif drive_letter:
                    tip_text = f"已装在{drive_letter}:盘（即目标盘），无需配置"
                    tip_color = "#43A047"
                else:
                    tip_text = "—"
                    tip_color = "#424242"
                tip_tooltip = tool["clean_guide"]
            tip_item = QTableWidgetItem(tip_text)
            tip_item.setForeground(QColor(tip_color))
            tip_item.setToolTip(tip_tooltip)
            self.table_dev_env.setItem(row, 8, tip_item)

            # 整行背景色：数据已迁移（绿色） > 特殊工具（红/橙） > 默认
            if data_migrated:
                # 绿色：环境已配置 + 数据已迁移到 D 盘（最佳状态）
                for c in range(9):
                    self.table_dev_env.item(row, c).setBackground(QColor("#E8F5E9"))
                migrated_count += 1
            elif special in ("pip",):
                for c in range(9):
                    self.table_dev_env.item(row, c).setBackground(QColor("#FFEBEE"))
            elif special in ("docker", "wsl", "vs"):
                for c in range(9):
                    self.table_dev_env.item(row, c).setBackground(QColor("#FFF3E0"))

            # 统计
            if status["installed"]:
                installed_count += 1
                if status["on_c"]:
                    on_c_count += 1
                if status["configured"]:
                    configured_count += 1

        self.table_dev_env.setSortingEnabled(True)
        # 解除信号阻塞
        self.table_dev_env.blockSignals(False)
        # 根据初始勾选状态着色（勾选=浅蓝底）
        # 延迟到下个事件循环执行，避免 setSortingEnabled 重置背景
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._apply_initial_check_colors)
        self.stat_dev_env.setText(
            f"共{len(rows_data)}项 | 已装{installed_count}项 | "
            f"在C盘待配置{on_c_count}项 | 已配置到目标盘{configured_count}项"
            f" | 数据已迁移{migrated_count}项")
        # 大小已在 DevEnvRefreshWorker._detect_one 中算好，无需异步计算

    def _on_dev_env_header_clicked(self, section):
        """点击表头：第0列切换全选/全不选（排除未安装），其他列保持默认排序行为"""
        if section != 0:
            return  # 其他列走默认排序，不处理
        # 收集可勾选的行（排除未安装：状态列(7)含"未安装"）
        checkable_rows = []
        for row in range(self.table_dev_env.rowCount()):
            status_item = self.table_dev_env.item(row, 7)
            status_text = status_item.text() if status_item else ""
            if "未安装" in status_text:
                continue  # 未安装的工具不参与全选
            checkable_rows.append(row)
        if not checkable_rows:
            return
        # 判断可勾选行是否全部已勾选 → 若是则全不选，否则全选
        all_checked = True
        for row in checkable_rows:
            chk = self.table_dev_env.item(row, 0)
            if chk and chk.checkState() != Qt.Checked:
                all_checked = False
                break
        new_state = Qt.Unchecked if all_checked else Qt.Checked
        # blockSignals 避免逐行触发 itemChanged 导致重复染色
        self.table_dev_env.blockSignals(True)
        for row in checkable_rows:
            chk = self.table_dev_env.item(row, 0)
            if chk:
                chk.setCheckState(new_state)
                chk.setData(Qt.UserRole, 1 if new_state == Qt.Checked else 0)
        self.table_dev_env.blockSignals(False)
        # 批量染色（只染参与全选的行）
        bg = QColor("#90CAF9") if new_state == Qt.Checked else QColor(255, 255, 255, 0)
        for row in checkable_rows:
            for col in range(self.table_dev_env.columnCount()):
                cell = self.table_dev_env.item(row, col)
                if cell:
                    cell.setBackground(bg)
        self.table_dev_env.viewport().update()
        self.status_label.setText(
            f"已{'全选' if new_state == Qt.Checked else '全不选'} {len(checkable_rows)} 项（已排除未安装）")

    def _on_dev_env_item_changed(self, item):
        """勾选状态变化时，同步 UserRole 数据 + 整行变色"""
        if item.column() != 0:
            return
        # 同步 UserRole：勾选=1，未勾选=0（保证表头排序按勾选状态）
        checked = item.checkState() == Qt.Checked
        # 用 blockSignals 防止 setData 触发 itemChanged 递归
        self.table_dev_env.blockSignals(True)
        item.setData(Qt.UserRole, 1 if checked else 0)
        self.table_dev_env.blockSignals(False)
        # 打勾=整行浅蓝底；取消打勾=整行白色背景
        row = item.row()
        bg = QColor("#90CAF9") if checked else QColor("#FFFFFF")
        for col in range(self.table_dev_env.columnCount()):
            cell = self.table_dev_env.item(row, col)
            if cell:
                cell.setBackground(bg)
        self.table_dev_env.viewport().update()

    def _apply_initial_check_colors(self):
        """填充表格后，根据初始勾选状态着色（勾选=浅蓝底）"""
        CHECKED_COLOR = QColor("#90CAF9")
        for row in range(self.table_dev_env.rowCount()):
            chk_item = self.table_dev_env.item(row, 0)
            if not chk_item or chk_item.checkState() != Qt.Checked:
                continue
            for col in range(self.table_dev_env.columnCount()):
                cell = self.table_dev_env.item(row, col)
                if cell:
                    cell.setBackground(CHECKED_COLOR)

    def _apply_migrated_tools_env(self):
        """一键配置已迁移工具的环境变量

        扫描表格中所有「数据已迁移（C盘是符号链接）但环境变量未配置」的工具，
        批量配置环境变量指向目标盘。不迁移数据（数据已迁移）。
        """
        from dev_env_migrate import TOOLS as DEV_TOOLS, get_tool_status, is_already_configured
        target_drive = self.dev_target_drive.currentText().strip()
        if not target_drive or len(target_drive) < 1:
            QMessageBox.warning(self, "错误", "请先选择目标盘符")
            return

        # 扫描所有工具，找出 data_migrated=True 但 configured=False 的
        tools_to_config = []
        for tool in DEV_TOOLS:
            if tool.get("special"):
                continue  # 跳过特殊工具（pip/docker/wsl/vs）
            try:
                status = get_tool_status(tool, target_drive,
                                          migrated_records=self.cfg.get("migrated", []))
                if status.get("is_symlink") and not status["configured"] and status["installed"]:
                    tools_to_config.append(tool)
            except Exception as e:
                log.debug("忽略异常: %s", e)

        if not tools_to_config:
            QMessageBox.information(self, "无需配置",
                "没有发现「已迁移但未配置环境变量」的工具。\n\n"
                "可能的情况：\n"
                "  • 所有已迁移的工具都已配置好环境变量\n"
                "  • 还没有迁移任何数据（请先在待迁移区迁移或在开发环境迁移区点「应用选中配置」）")
            return

        # 确认对话框
        tool_names = "\n".join(f"  • {t['name']}" for t in tools_to_config)
        reply = QMessageBox.question(self, "确认一键配置",
            f"检测到 {len(tools_to_config)} 个工具数据已迁移但环境变量未配置：\n\n"
            f"{tool_names}\n\n"
            f"将批量配置环境变量指向 {target_drive}: 盘（不迁移数据，数据已在目标盘）。\n"
            f"是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if reply != QMessageBox.Yes:
            return

        # 后台线程批量配置（不迁移数据）
        self.btn_apply_migrated.setEnabled(False)
        self.btn_apply_dev.setEnabled(False)
        self.status_label.setText(f"正在一键配置 {len(tools_to_config)} 个已迁移工具...")
        self.on_monitor_log("dev_env",
            f"一键配置已迁移工具: {len(tools_to_config)} 个工具配置环境变量到 {target_drive}: 盘: "
            + ", ".join(t["name"] for t in tools_to_config))

        # 大小计算已合并到 DevEnvRefreshWorker，无需取消 size worker

        worker = DevEnvApplyWorker(tools_to_config, target_drive,
                                   migrate_data=False, config=self.cfg)
        self._dev_env_apply_worker = worker

        # 显示进度条
        self.progress.setVisible(True)
        self.progress.setRange(0, len(tools_to_config))
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat(f"一键配置中 0/{len(tools_to_config)} (%p%)")

        def _on_apply_progress(current, total, msg):
            self.progress.setValue(current)
            self.progress.setFormat(f"一键配置 {current}/{total} (%p%)")
            self.status_label.setText(msg)

        def _on_migrated_done(worker_results, drive):
            self.btn_apply_migrated.setEnabled(True)
            self.btn_apply_dev.setEnabled(True)
            self.progress.setVisible(False)
            results = [(t["name"], ok, msg) for t, ok, msg, _ in worker_results]
            success_count = sum(1 for _, ok, _ in results if ok)
            fail_count = len(results) - success_count
            detail = "\n\n".join(
                f"{'✓' if ok else '✗'} {name}:\n{msg}"
                for name, ok, msg in results)
            if fail_count == 0:
                title = f"✓ 一键配置完成（{success_count} 个工具全部成功）"
            else:
                title = f"配置完成（成功 {success_count}，失败 {fail_count}）"
            QMessageBox.information(self, title,
                f"已配置 {success_count} 个工具的环境变量到 {drive}: 盘：\n\n{detail}")
            self.status_label.setText(f"一键配置完成：成功 {success_count}，失败 {fail_count}")
            # 局部刷新：只更新涉及的工具行，避免全表刷新很慢
            try:
                tids = [t["id"] for t, _, _, _ in worker_results]
                self._partial_refresh_dev_env_rows(tids, reason="一键配置完成")
            except Exception as e:
                log.error(f"局部刷新失败，回退全表刷新: {e}")
                self._refresh_dev_env_table()

        worker.finished_signal.connect(_on_migrated_done)
        worker.progress_signal.connect(_on_apply_progress)
        worker.verbose_log_sig.connect(self._log_monitor, Qt.QueuedConnection)
        worker.error_signal.connect(lambda e: (
            self.btn_apply_migrated.setEnabled(True),
            self.btn_apply_dev.setEnabled(True),
            self.progress.setVisible(False),
            QMessageBox.critical(self, "错误", f"一键配置失败: {e}")))
        worker.start()

    def _apply_dev_env_selected(self):
        """应用选中的开发工具配置"""
        target_drive = self.dev_target_drive.currentText()
        selected = []
        custom_paths = {}  # {tool_id: custom_path}（用户双击修改过的路径）
        for row in range(self.table_dev_env.rowCount()):
            chk = self.table_dev_env.item(row, 0)
            if chk and chk.checkState() == Qt.Checked:
                # 从 tool id 找到 tool 对象（按行号顺序对应 DEV_TOOLS 顺序）
                # 但排序后行号会变，用 tool name 反查
                name = self.table_dev_env.item(row, 1).text()
                for t in DEV_TOOLS:
                    if t["name"] == name:
                        selected.append(t)
                        # 检查用户是否双击修改了建议路径
                        suggest_item = self.table_dev_env.item(row, 6)
                        if suggest_item:
                            table_path = suggest_item.text().strip()
                            default_path = dev_get_suggest_path(t, target_drive)
                            if table_path and table_path.lower() != default_path.lower():
                                custom_paths[t["id"]] = table_path
                        break

        if not selected:
            QMessageBox.information(self, "提示", "请先勾选要配置的工具")
            return

        # 特殊工具警告
        special_tools = [t for t in selected if t["special"]]
        if special_tools:
            special_names = "\n".join(f"  • {t['name']}" for t in special_tools)
            QMessageBox.warning(self, "特殊工具无法自动配置",
                f"以下工具无法用环境变量自动配置，需手动处理：\n\n{special_names}\n\n"
                f"请取消勾选这些工具，或点「查看清理指引」查看手动迁移步骤。")
            return

        # 建议新路径为空检测：列出空值工具及其默认路径，让用户决定
        # 列为空时用默认路径也能跑，但用户可能没注意到，弹提示更友好
        empty_path_tools = []  # [(tool, default_path), ...]
        for t in selected:
            default_path = dev_get_suggest_path(t, target_drive)
            # 找到这个工具在表格中的行，读取"建议新路径"列(索引6)
            for row in range(self.table_dev_env.rowCount()):
                row_name = self.table_dev_env.item(row, 1).text() if self.table_dev_env.item(row, 1) else ""
                if row_name == t["name"]:
                    suggest_item = self.table_dev_env.item(row, 6)
                    table_path = suggest_item.text().strip() if suggest_item else ""
                    if not table_path:
                        empty_path_tools.append((t, default_path))
                    break

        if empty_path_tools:
            # 列出空值工具及其默认路径，问用户是用默认还是返回填
            lines = "\n".join(f"  • {t['name']}  →  默认: {dp}"
                              for t, dp in empty_path_tools)
            ret = QMessageBox.question(self, "建议新路径为空",
                f"检测到 {len(empty_path_tools)} 个工具的「建议新路径」列为空：\n\n"
                f"{lines}\n\n"
                f"将使用上述默认路径继续。如需自定义路径，请点「返回修改」，\n"
                f"然后双击对应行的「建议新路径」列输入路径后再点此按钮。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if ret != QMessageBox.Yes:
                # 用户选择返回修改，中止操作
                return

        # 确认对话框
        confirm_msg = f"即将配置 {len(selected)} 个工具到 {target_drive}: 盘：\n\n"
        for t in selected:
            confirm_msg += f"  • {t['name']}\n"
        confirm_msg += "\n这会修改用户环境变量和配置文件，是否继续？"
        if QMessageBox.question(self, "确认配置", confirm_msg,
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return

        # 弹窗询问：是否同时迁移 C 盘现有数据到 D 盘？
        # 先检测每个工具的 C 盘数据量，根据数据量给智能建议
        self.status_label.setText("正在检测 C 盘数据量...")
        from dev_env_migrate import get_tool_data_info as _get_info
        data_items = []  # [(name, size_mb, has_data, msg), ...]
        total_size = 0.0
        has_data_count = 0
        for t in selected:
            try:
                info = _get_info(t)
                if info["has_data"]:
                    has_data_count += 1
                    total_size += info["size_mb"]
                    data_items.append((t["name"], info["size_mb"], True, info["message"]))
                else:
                    data_items.append((t["name"], 0, False, info["message"]))
            except Exception as e:
                data_items.append((t["name"], 0, False, f"检测失败: {e}"))
        self.status_label.setText("")

        # 根据数据量生成智能建议
        if has_data_count == 0:
            # 没有数据可迁移 → 不弹框，直接只配置环境变量
            # 说明信息合并到配置完成后的"配置完成"弹窗中
            migrate_data = False
            self.status_label.setText(
                f"所选 {len(selected)} 个工具在 C 盘无数据，只配置环境变量...")
        else:
            # 有数据可迁移，弹窗显示详情并给建议
            # 与右键菜单「配置并迁移此工具」标准对齐：显示具体目标路径 + 三选一
            detail_lines = ""
            for name, size, has, msg in data_items:
                if has:
                    detail_lines += f"  • {name}: {_format_size(size)}\n"
                else:
                    detail_lines += f"  • {name}: 无数据\n"

            # 收集每个有数据工具的目标路径（与右键菜单一致：优先双击修改值，否则默认建议路径）
            target_lines = ""
            for t in selected:
                cp = custom_paths.get(t["id"])
                tp = cp if cp else dev_get_suggest_path(t, target_drive)
                target_lines += f"  • {t['name']}  →  {tp}\n"

            # 智能建议
            if total_size >= 500:
                advice = (f"💡 检测到 {_format_size(total_size)} 数据，建议同时迁移")
                default_btn = QMessageBox.Yes
            elif total_size >= 50:
                advice = (f"💡 检测到 {_format_size(total_size)} 数据，数据量中等，建议同时迁移")
                default_btn = QMessageBox.Yes
            else:
                advice = (f"💡 检测到仅 {_format_size(total_size)} 数据，数据量少，可只配置不迁移")
                default_btn = QMessageBox.No

            # 三选一弹窗：Yes=配置+迁移 / No=只配置 / Cancel=取消（与右键菜单一致）
            reply = QMessageBox.question(self, "选择操作方式",
                f"检测到 {has_data_count} 个工具在 C 盘有数据：\n\n"
                f"{detail_lines}\n"
                f"{'─' * 40}\n"
                f"总计: {_format_size(total_size)}\n\n"
                f"目标路径：\n{target_lines}\n"
                f"{advice}\n\n"
                f"  • 『Yes』配置 + 迁移数据（复制 + 符号链接）\n"
                f"     数据搬到上述目标路径，原位置变符号链接指向新路径\n\n"
                f"  • 『No』只配置环境变量\n"
                f"     现有数据不动，以后新装的去上述目标路径\n\n"
                f"  • 『Cancel』取消，不做任何操作",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, default_btn)
            if reply == QMessageBox.Yes:
                migrate_data = True
            elif reply == QMessageBox.No:
                migrate_data = False
            else:
                # Cancel 或关闭按钮 → 退出操作
                self.status_label.setText("批量配置已取消")
                return

        # 后台线程应用配置
        self.btn_apply_dev.setEnabled(False)
        self.status_label.setText(f"正在配置 {len(selected)} 个工具"
            + ("（含数据迁移）..." if migrate_data else "..."))
        self.on_monitor_log("dev_env",
            f"开始批量配置 {len(selected)} 个开发工具到 {target_drive}: 盘"
            + ("（含数据迁移）" if migrate_data else "") + ": "
            + ", ".join(t["name"] for t in selected))

        # 创建 Worker，保存引用避免被 GC
        # 如果用户双击修改了建议路径，创建工具副本将自定义路径烘焙到 env_vars/config_commands
        import copy as _copy
        tools_to_apply = []
        for t in selected:
            cp = custom_paths.get(t["id"])
            if cp:
                ct = _copy.deepcopy(t)
                for ev in ct["env_vars"]:
                    ev["default_value_template"] = cp
                for cmd in ct["config_commands"]:
                    if len(cmd["cmd_template"]) > 1:
                        cmd["cmd_template"][-1] = cp
                tools_to_apply.append(ct)
            else:
                tools_to_apply.append(t)
        # 取消正在运行的后台扫描线程（迁移时会删除 C 盘目录，避免并发访问冲突）
        self._safe_cancel_dev_env_worker("_dev_env_refresh_worker")
        worker = DevEnvApplyWorker(tools_to_apply, target_drive,
                                   migrate_data=migrate_data, config=self.cfg)
        self._dev_env_apply_worker = worker

        # 显示进度条
        self.progress.setVisible(True)
        self.progress.setRange(0, len(tools_to_apply))
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        action_word = "配置+迁移" if migrate_data else "配置"
        self.progress.setFormat(f"{action_word}中 0/{len(tools_to_apply)} (%p%)")

        def _on_apply_progress2(current, total, msg):
            self.progress.setValue(current)
            self.progress.setFormat(f"{action_word} {current}/{total} (%p%)")
            self.status_label.setText(msg)

        def _on_done(worker_results, drive):
            """主线程：更新 UI、保存记录、弹结果
            worker_results 是 [(tool, ok, msg, source_path), ...] 列表
            """
            try:
                # 确保 worker 线程完全退出，避免模态对话框期间的竞态
                try:
                    worker.wait(500)
                except Exception as e:
                    log.debug("忽略异常: %s", e)
                self.progress.setVisible(False)
                self.btn_apply_dev.setEnabled(True)
                # 转成 [(name, ok, msg), ...] 格式（丢弃 source_path，下面单独取）
                results = [(t["name"], ok, msg) for t, ok, msg, _ in worker_results]
                # 汇总结果
                success_count = sum(1 for _, ok, _ in results if ok)
                fail_count = len(results) - success_count
                detail = "\n\n".join(
                    f"{'✓' if ok else '✗'} {name}:\n{msg}"
                    for name, ok, msg in results
                )
                # 保存成功配置的记录到 state（方便以后卸载时找到 D 盘真实目录）
                if success_count > 0:
                    try:
                        configured = self.cfg.setdefault("dev_env_configured", {})
                        for tool in selected:
                            # 只记录成功的
                            for rname, rok, _ in results:
                                if rname == tool["name"] and rok:
                                    # 取出 worker 捕获的 C 盘源路径（配置前）
                                    source_path = ""
                                    for t, ok, msg, sp in worker_results:
                                        if t["name"] == tool["name"] and ok:
                                            source_path = sp
                                            break
                                    configured[tool["id"]] = {
                                        "name": tool["name"],
                                        "category": tool["category"],
                                        "target_drive": drive,
                                        "target_path": custom_paths.get(tool["id"],
                                            dev_get_suggest_path(tool, drive)),
                                        "env_vars": [ev["name"] for ev in tool["env_vars"]],
                                        "configured_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        "clean_guide": tool["clean_guide"],
                                        "source_path": source_path,
                                    }
                                    break
                        save_all(self.cfg)
                    except Exception as e:
                        log.error(f"保存开发环境配置记录失败: {e}")
                self.status_label.setText(
                    f"开发环境配置完成：成功 {success_count} 个，失败 {fail_count} 个")
                # 记录每个工具的配置结果到监控日志
                for name, ok, msg in results:
                    mark = "✓" if ok else "✗"
                    self.on_monitor_log("dev_env",
                        f"{mark} {name} 配置{'成功' if ok else '失败'}")
                self.on_monitor_log("dev_env",
                    f"批量配置完成：成功 {success_count} 个，失败 {fail_count} 个")
                QMessageBox.information(self, "配置结果",
                    f"成功 {success_count} 个，失败 {fail_count} 个\n\n{detail}\n\n"
                    f"⚠️ 请重新打开终端/编辑器让新环境变量生效。\n"
                    f"配置记录已保存，以后卸载工具时可从右键菜单「查看已配置记录」找到 D 盘真实目录。\n\n"
                    + (
                        f"✅ 三区状态变化：\n"
                        f"  • 开发环境迁移区：已配置的工具行变绿色（数据已迁移）\n"
                        f"  • 待迁移区：已迁移的目录不再显示（C 盘已是符号链接）\n"
                        f"  • 已迁移区：新增 {success_count} 条迁移记录"
                        if migrate_data else
                        (
                            f"ℹ️ 所选工具在 C 盘无已装包/库，已只配置环境变量\n"
                            f"   以后新装的包会直接去 {drive}: 盘\n\n"
                            f"📋 三区状态变化：\n"
                            f"  • 开发环境迁移区：已配置的工具行变绿色（环境已配置）\n"
                            f"  • 待迁移区/已迁移区：无变化（C 盘本就无数据）"
                            if has_data_count == 0 else
                            f"📋 三区状态变化：\n"
                            f"  • 开发环境迁移区：已配置的工具行变绿色（环境已配置）\n"
                            f"  • 待迁移区：对应目录标橙色「[已配置]」（数据还在 C 盘）\n"
                            f"  • 建议去待迁移区点「迁移」按钮把数据搬到 D 盘"
                        )
                    ))
                # 局部刷新开发环境表格：只更新涉及的工具行，避免全表刷新很慢
                try:
                    tids = [t["id"] for t, _, _, _ in worker_results]
                    self._partial_refresh_dev_env_rows(tids, reason="批量配置完成")
                except Exception as e:
                    log.error(f"局部刷新失败，回退全表刷新: {e}")
                    try:
                        self._refresh_dev_env_table()
                    except Exception as e2:
                        log.error(f"全表刷新也失败: {e2}")
                # 数据迁移成功后，同步刷新待迁移区和已迁移区
                # （C 盘已变符号链接，待迁移区该条目应消失，已迁移区应新增记录）
                if migrate_data and success_count > 0:
                    try:
                        self._refresh_migrated_only()
                    except Exception as e:
                        log.error(f"刷新已迁移区失败: {e}")
                    try:
                        # 待迁移区用轻量刷新（不重新全盘扫描，只更新现有行的链接状态）
                        self._light_refresh_scan_table()
                    except Exception as e:
                        log.error(f"刷新待迁移区失败: {e}")
            except Exception as e:
                # 顶层兜底：防止 Qt 槽函数崩溃导致程序闪退
                import traceback
                err_detail = f"批量_on_done 异常: {e}\n{traceback.format_exc()[-500:]}"
                log.error(err_detail)
                self.on_monitor_log("error", err_detail)
                try:
                    self.progress.setVisible(False)
                    self.btn_apply_dev.setEnabled(True)
                    QMessageBox.critical(self, "内部错误",
                        f"批量配置完成但刷新界面时出错：\n{e}\n\n配置已保存，请手动刷新表格。")
                except Exception as e:
                    log.debug("忽略异常: %s", e)

        def _on_error(err):
            try:
                self.btn_apply_dev.setEnabled(True)
                self.progress.setVisible(False)
                self.status_label.setText(f"配置失败: {err}")
                self.on_monitor_log("dev_env", f"批量配置失败: {err}")
                log.error(f"批量配置失败: {err}")
                QMessageBox.critical(self, "配置失败", f"批量配置失败: {err}")
            except Exception as e:
                log.error(f"批量_on_error 槽异常: {e}")

        worker.finished_signal.connect(_on_done)
        worker.progress_signal.connect(_on_apply_progress2)
        worker.verbose_log_sig.connect(self._log_monitor, Qt.QueuedConnection)
        worker.error_signal.connect(_on_error)
        worker.start()

    def _show_dev_clean_guide(self):
        """显示所有工具的清理指引"""
        guide = "【开发工具清理指引】\n\n"
        for tool in DEV_TOOLS:
            guide += f"━━━ {tool['name']}（{tool['category']}）━━━\n"
            guide += f"{tool['clean_guide']}\n\n"
        dlg = QDialog(self)
        dlg.setWindowTitle("开发工具清理指引")
        dlg.resize(700, 600)
        lay = QVBoxLayout(dlg)
        edit = QTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText(guide)
        lay.addWidget(edit)
        btn = QPushButton("关闭")
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn)
        dlg.exec()

    def _on_dev_env_double_click(self, row, col):
        """开发环境表格双击处理
        - 第0列（勾选框列）：不响应双击，避免误弹框
        - 第6列（建议新路径）：弹目录浏览对话框让用户修改路径
        - 第7列（状态列）：未安装时弹下载菜单（与单击相同，双击也触发）
        - 其他列：显示清理指引
        列号：0勾选 1工具 2类别 3当前路径 4C盘原位置 5占用空间 6建议新路径 7状态 8提示
        """
        if col == 0:
            # 勾选框列：双击不做任何操作（避免误触弹框）
            return
        if col == 6:
            # 双击建议新路径列 → 弹浏览对话框
            self._edit_dev_env_suggest_path(row)
            return
        if col == 7:
            # 双击状态列：未安装时弹下载菜单（与单击一致）
            status_item = self.table_dev_env.item(row, 7)
            status_text = status_item.text() if status_item else ""
            if "未安装" in status_text:
                self._show_dev_env_download_menu(row)
            else:
                self._show_dev_clean_guide_for_row(row)
            return
        # 其他列：显示清理指引
        self._show_dev_clean_guide_for_row(row)

    def _make_github_icon(self, size=32):
        """加载 GitHub 图标文件（ico/github.ico）"""
        import os
        # 图标目录：打包模式优先 exe 内 _MEIPASS/ico（打进 exe），缺失回退 exe 同级 ico/；源码模式 = 项目根/ico
        if getattr(sys, "frozen", False):
            _meipass = getattr(sys, "_MEIPASS", None)
            if _meipass and os.path.isdir(os.path.join(_meipass, "ico")):
                base = _meipass
            else:
                base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        ico_path = os.path.join(base, "ico", "github.ico")
        if os.path.exists(ico_path):
            return QIcon(ico_path)
        # 兜底：文件不存在时返回空图标
        return QIcon()

    def _show_dev_env_download_menu(self, row):
        """未安装工具的下载菜单：最新版/最热门版/访问官网/GitHub仓库"""
        try:
            # 获取工具信息
            tool = self._get_dev_env_tool_by_row(row)
            if not tool:
                return
            download_url = tool.get("download_url", "")
            tool_id = tool["id"]
            tool_name = tool["name"]

            # 弹出下载菜单
            menu = QMenu(self)
            menu.setWindowTitle(f"下载 {tool_name}")

            # 对有 API 的工具，提供"下载最新稳定版"和"下载最热门版（LTS/推荐版）"
            api_info = _DEV_TOOL_DOWNLOAD_APIS.get(tool_id)
            if api_info:
                act_latest = menu.addAction("⬇ 下载最新稳定版")
                act_latest.setToolTip("自动获取最新版本并下载安装包到本地")
                act_popular = menu.addAction("⬇ 下载最热门版（LTS/推荐）")
                act_popular.setToolTip("获取最多人使用的稳定版本（LTS 或推荐版）")
                menu.addSeparator()
            else:
                act_latest = None
                act_popular = None

            if download_url:
                act_website = menu.addAction("🌐 访问官网下载")
                act_website.setToolTip(f"打开浏览器: {download_url}")
            else:
                act_website = None

            # GitHub 仓库选项（查看源码、提 issue、克隆编译）
            github_url = DEV_GITHUB_URLS.get(tool_id)
            if github_url:
                menu.addSeparator()
                act_github = menu.addAction("GitHub 仓库")
                act_github.setIcon(self._make_github_icon(16))
                act_github.setToolTip(f"打开浏览器查看源码/提 issue:\n{github_url}")
            else:
                act_github = None

            if not api_info and not download_url and not github_url:
                QMessageBox.information(self, "暂无下载源",
                    f"未配置 {tool_name} 的自动下载源。\n"
                    f"请手动搜索下载。")
                return

            # 菜单弹出在状态列(7)中心（用户点击的就是这一列）
            # 列号：0勾选 1工具 2类别 3当前路径 4C盘原位置 5占用空间 6建议新路径 7状态 8提示
            action = menu.exec(self.table_dev_env.viewport().mapToGlobal(
                self.table_dev_env.visualRect(self.table_dev_env.model().index(row, 7)).center()))

            if action is None:
                return
            if act_latest and action == act_latest:
                self._download_dev_tool(tool_id, "latest")
            elif act_popular and action == act_popular:
                self._download_dev_tool(tool_id, "popular")
            elif act_website and action == act_website:
                QDesktopServices.openUrl(QUrl(download_url))
            elif act_github and action == act_github:
                QDesktopServices.openUrl(QUrl(github_url))
        except Exception as e:
            import traceback
            log.error(f"[DEV_ENV] 下载菜单异常: {e}\n{traceback.format_exc()}")
            QMessageBox.critical(self, "错误", f"弹出下载菜单失败:\n{e}")

    def _download_dev_tool(self, tool_id, version_type):
        """下载开发工具安装包到本地（不自动安装）
        version_type: "latest"=最新稳定版, "popular"=LTS/推荐版
        """
        api_info = _DEV_TOOL_DOWNLOAD_APIS.get(tool_id)
        if not api_info:
            QMessageBox.warning(self, "无API", f"未配置 {tool_id} 的下载API")
            return
        # 选择保存位置
        default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        suggested_name = f"{tool_id}_{version_type}.exe"
        save_path, _ = QFileDialog.getSaveFileName(
            self, f"保存 {tool_id} 安装包",
            os.path.join(default_dir, suggested_name),
            "安装包 (*.exe *.msi *.zip);;所有文件 (*)"
        )
        if not save_path:
            return
        # 启动后台下载线程
        worker = DevToolDownloadWorker(tool_id, version_type, save_path, api_info)
        worker.progress_signal.connect(self._on_dev_tool_download_progress)
        worker.finished_signal.connect(self._on_dev_tool_download_finished)
        worker.error_signal.connect(self._on_dev_tool_download_error)
        self._dev_tool_download_worker = worker  # 防止被GC
        worker.start()
        self.statusBar().showMessage(f"正在获取 {tool_id} 下载链接...")

    def _on_dev_tool_download_progress(self, tool_id, percent, msg):
        """下载进度回调"""
        self.statusBar().showMessage(f"下载 {tool_id}: {percent}% - {msg}")

    def _on_dev_tool_download_finished(self, tool_id, save_path):
        """下载完成回调"""
        self.statusBar().showMessage(f"下载完成: {save_path}", 5000)
        QMessageBox.information(self, "下载完成",
            f"已下载到:\n{save_path}\n\n"
            f"请手动运行安装包进行安装。\n"
            f"安装时建议选择 D 盘作为安装路径。")

    def _on_dev_tool_download_error(self, tool_id, error_msg):
        """下载失败回调"""
        self.statusBar().showMessage(f"下载失败: {tool_id}", 5000)
        # 获取官网链接作为兜底
        tool = None
        for t in DEV_TOOLS:
            if t["id"] == tool_id:
                tool = t
                break
        download_url = tool.get("download_url", "") if tool else ""
        msg = f"下载失败: {error_msg}"
        if download_url:
            msg += f"\n\n建议访问官网手动下载:\n{download_url}"
        QMessageBox.warning(self, "下载失败", msg)

    def _get_dev_env_tool_by_row(self, row):
        """根据表格行获取工具 dict"""
        if row < 0 or row >= self.table_dev_env.rowCount():
            return None
        # 从第1列（工具名列）的 UserRole 获取 tool_id
        name_item = self.table_dev_env.item(row, 1)
        if not name_item:
            return None
        tool_id = name_item.data(Qt.UserRole)
        if tool_id:
            for t in DEV_TOOLS:
                if t["id"] == tool_id:
                    return t
        # 兜底：从名称反查
        name = name_item.text()
        for t in DEV_TOOLS:
            if t["name"] == name:
                return t
        return None

    def _edit_dev_env_suggest_path(self, row):
        """双击修改建议新路径（弹目录浏览对话框）"""
        if row < 0 or row >= self.table_dev_env.rowCount():
            return
        cur_suggest = self.table_dev_env.item(row, 6).text() if self.table_dev_env.item(row, 6) else ""
        # 默认目录：取当前建议路径的父目录
        start_dir = os.path.dirname(cur_suggest) if cur_suggest else ""
        from PySide6.QtWidgets import QFileDialog
        new_path = QFileDialog.getExistingDirectory(
            self, f"选择新的目标路径", start_dir)
        if new_path:
            # 规范化路径：
            # 1. 去掉可能的 \\?\ 前缀
            # 2. QFileDialog 在 Windows 上默认返回正斜杠，统一转为反斜杠
            #    （Windows 路径规范是反斜杠，正斜杠在 cmd/注册表/环境变量中可能出问题）
            new_path = new_path.replace("\\\\?\\", "").replace("/", "\\")
            # 去掉末尾的反斜杠（避免显示为 "D:\path\\"，与表格其他路径风格一致）
            new_path = new_path.rstrip("\\")
            item = self.table_dev_env.item(row, 6)
            if item:
                item.setText(new_path)
                item.setToolTip(new_path + "\n（双击可修改路径）")
            self.on_monitor_log("dev_env",
                f"修改建议路径: {cur_suggest} → {new_path}")

    def _show_dev_clean_guide_for_row(self, row):
        """显示某一行的清理指引"""
        if row < 0 or row >= self.table_dev_env.rowCount():
            return
        name = self.table_dev_env.item(row, 1).text()
        tool = None
        for t in DEV_TOOLS:
            if t["name"] == name:
                tool = t
                break
        if not tool:
            return
        tip = self.table_dev_env.item(row, 8).text() if self.table_dev_env.item(row, 8) else ""
        QMessageBox.information(self, f"{tool['name']} - 清理指引",
            f"工具：{tool['name']}（{tool['category']}）\n"
            f"状态提示：{tip}\n\n"
            f"清理/迁移指引：\n{tool['clean_guide']}")

    def _dev_env_context_menu(self, pos):
        """开发环境表格右键菜单"""
        row = self.table_dev_env.rowAt(pos.y())
        if row < 0 or row >= self.table_dev_env.rowCount():
            return
        name = self.table_dev_env.item(row, 1).text()
        tool = None
        for t in DEV_TOOLS:
            if t["name"] == name:
                tool = t
                break
        if not tool:
            return

        # 收集所有选中行对应的工具（支持多选还原）
        # 还原条件：状态为「✓ 已迁移到X:盘」或「✓ 已配置到X:盘」
        # （已迁移 = 数据在D盘+符号链接；已配置 = 仅环境变量指向D盘，数据可能还在C盘）
        # 两者都需要支持还原，因为用户可能只想撤销配置不迁移数据
        selected_rows = sorted({idx.row() for idx in self.table_dev_env.selectedIndexes()})
        if not selected_rows:
            selected_rows = [row]
        selected_tools = []
        for r in selected_rows:
            r_name_item = self.table_dev_env.item(r, 1)
            if not r_name_item:
                continue
            r_name = r_name_item.text()
            r_tool = next((t for t in DEV_TOOLS if t["name"] == r_name), None)
            if r_tool:
                # 收集状态为「已迁移」或「已配置」的工具（都指向 D 盘）
                # 列号：0勾选 1工具 2类别 3当前路径 4C盘原位置 5占用空间 6建议新路径 7状态 8提示
                r_status_item = self.table_dev_env.item(r, 7)
                r_status = r_status_item.text() if r_status_item else ""
                if r_status.startswith("✓ 已迁移到") or r_status.startswith("✓ 已配置到"):
                    selected_tools.append(r_tool)

        target_drive = self.dev_target_drive.currentText()
        suggest_path = dev_get_suggest_path(tool, target_drive)
        cur_path = self.table_dev_env.item(row, 3).text()
        status_text = self.table_dev_env.item(row, 7).text() if self.table_dev_env.item(row, 7) else ""

        # ⚠️ 修复"打开目标路径"误判不存在：
        # suggest_path 是默认模板路径（如 D:\dev\android\sdk），但用户可能已迁移到
        # 自定义路径（如 D:\测试目录）。优先级：
        # 1. 工具已迁移/已配置 → 用表格第 6 列"建议新路径"的当前值（用户可双击修改），
        #    该列在迁移完成后会更新为实际目标路径
        # 2. 第 6 列为空或等于默认模板 → 用 cur_path（当前路径，已迁移时就是目标路径）
        # 3. 兜底用 suggest_path（默认模板）
        actual_target_path = suggest_path  # 默认值
        suggest_item = self.table_dev_env.item(row, 6)
        suggest_in_table = suggest_item.text().replace("\\\\?\\", "") if suggest_item else ""
        if suggest_in_table:
            # 表格第 6 列有值（可能是用户自定义路径或默认值）
            actual_target_path = suggest_in_table
        elif ("已迁移到" in status_text or "已配置到" in status_text) and cur_path:
            # 工具已迁移/已配置，但第 6 列为空 → 用当前路径（已迁移时即目标路径）
            cur_path_clean = cur_path.replace("\\\\?\\", "")
            if cur_path_clean and cur_path_clean[1:2] == ":" and cur_path_clean[0].upper() != "C":
                actual_target_path = cur_path_clean

        menu = QMenu(self)
        # 1. 配置并迁移此工具（设置环境变量 + 可选把数据搬到新路径）
        act_apply = menu.addAction("配置并迁移此工具到新路径")
        act_apply.setToolTip(
            "设置环境变量指向新路径，并可选把 C 盘数据迁移过去\n"
            "流程：1. 配置环境变量  2. 可选复制数据到新路径  3. C 盘原位置变符号链接")
        # 2. 打开当前路径目录（蓝色字体）
        # 注：QAction 自身不支持 per-item 文字颜色（QSS 的 QMenu::item 只能改选中态）。
        #    Qt 官方推荐方案：QWidgetAction + QPushButton，通过 QPushButton.setStyleSheet 改文字色。
        #    高度对齐：用 menu.font() 同步字体，padding 6px 上下与 QAction 默认 padding 一致。
        #    ⚠️ 关键修复：QPushButton.clicked 不会让 menu.exec() 返回该 QWidgetAction，
        #       所以不能依赖 action==act_open_cur 判断。改为直接在 clicked 信号里
        #       调用处理函数并 menu.close()，exec 返回 None 时也不影响后续逻辑。
        act_open_cur = QWidgetAction(self)
        btn_cur = QPushButton("  打开当前路径")
        btn_cur.setFlat(True)
        btn_cur.setFont(menu.font())
        btn_cur.setStyleSheet(
            "QPushButton { color: #1976D2; text-align: left; padding: 6px 16px;"
            " border: none; background: transparent; }"
            "QPushButton:hover { background: #E3F2FD; }")
        btn_cur.setToolTip(f"打开: {cur_path}")
        act_open_cur.setDefaultWidget(btn_cur)
        menu.addAction(act_open_cur)
        # 3. 打开目标路径目录（目标盘新路径，绿色字体）
        act_open_target = QWidgetAction(self)
        btn_target = QPushButton("  打开目标路径")
        btn_target.setFlat(True)
        btn_target.setFont(menu.font())
        btn_target.setStyleSheet(
            "QPushButton { color: #43A047; text-align: left; padding: 6px 16px;"
            " border: none; background: transparent; }"
            "QPushButton:hover { background: #E8F5E9; }")
        btn_target.setToolTip(f"打开: {actual_target_path}")
        act_open_target.setDefaultWidget(btn_target)
        menu.addAction(act_open_target)
        # 4. 查看清理/卸载指引
        act_guide = menu.addAction("查看清理/卸载指引")
        # 5. 查看已配置记录
        act_records = menu.addAction("查看此工具的配置记录")
        # 6. 访问官网下载（未安装时尤其有用）
        download_url = tool.get("download_url", "")
        if download_url:
            act_download = menu.addAction("🌐 访问官网下载")
            act_download.setToolTip(f"打开浏览器: {download_url}")
        else:
            act_download = None
        menu.addSeparator()
        # 7. 还原数据（全自动：撤销配置 + 数据搬回 C 盘，含 unapply_tool 的所有操作）
        # 注：原"一键还原（回滚所有配置）"已合并到此选项，避免功能重合
        #     单独只撤销配置不还原数据的入口已移到"还原配置（历史快照）"对话框
        act_restore_data = None
        if selected_tools:
            if len(selected_tools) == 1:
                act_restore_data = menu.addAction("还原此工具数据到 C 盘")
                act_restore_data.setToolTip(
                    "全自动还原：撤销环境变量配置 + 数据从目标盘搬回 C 盘\n"
                    "包括：删环境变量、撤销 npm/pip/go 配置、还原 settings.xml、复制数据回 C 盘")
            else:
                act_restore_data = menu.addAction(
                    f"批量还原 {len(selected_tools)} 个工具数据到 C 盘")
                act_restore_data.setToolTip(
                    "依次全自动还原选中工具：撤销配置 + 数据搬回 C 盘\n"
                    "包括：删环境变量、撤销配置命令、还原配置文件、复制数据回 C 盘")

        # ⚠️ 关键修复：QPushButton.clicked 不会让 menu.exec() 返回该 QWidgetAction。
        #    因此把"打开当前路径"和"打开目标路径"的逻辑直接挂到按钮 clicked 信号上，
        #    在槽函数里 menu.close() 关闭菜单。menu.exec() 会因菜单关闭而返回 None，
        #    后续 if/elif 分支不会命中 act_open_cur/act_open_target，但保留分支以防
        #    未来改成 QAction 方案时能直接复用。
        def _do_open_cur():
            menu.close()
            # 打开当前路径（去 \\?\ 前缀）
            p = cur_path.replace("\\\\?\\", "").replace("\\\\.", "")
            if p and p not in ("未安装", "（无）"):
                self._open_path(p)
                return
            # 路径为空时，尝试用 shutil.which() 找可执行文件所在目录
            import shutil as _shutil
            exe_map = {
                "npm_global": "npm", "npm_cache": "npm",
                "yarn_global": "yarn", "pnpm_global": "pnpm",
                "pip_install": "pip", "pip_cache": "pip",
                "conda": "conda", "cargo_home": "cargo",
                "rustup_home": "rustup", "gopath": "go",
                "gocache": "go", "gomodcache": "go",
                "dotnet_tools": "dotnet", "gradle_home": "gradle",
                "maven_repo": "mvn", "gem_home": "gem",
                "julia_depot": "julia", "composer_cache": "composer",
                "pub_cache": "dart", "terraform_cache": "terraform",
                "conan_home": "conan", "vcpkg_root": "vcpkg",
                "bazel_output": "bazel",
            }
            exe_name = exe_map.get(tool["id"])
            found = _shutil.which(exe_name) if exe_name else None
            if found:
                found_dir = os.path.dirname(found)
                if os.path.exists(found_dir):
                    QMessageBox.information(self, "提示",
                        f"未检测到数据目录，但找到 {exe_name} 可执行文件位于：\n"
                        f"{found_dir}\n\n即将打开此目录（包含可执行文件本身）")
                    self._open_path(found_dir)
                    return
            QMessageBox.information(self, "提示",
                f"未检测到 {tool['name']} 的数据目录路径。\n\n"
                f"可能原因：\n"
                f"  1. 工具刚安装，尚未生成数据目录\n"
                f"  2. 工具是非标准安装，路径检测失败\n"
                f"  3. 工具是特殊工具（如 Docker/WSL/VS），需手动查找\n\n"
                f"建议：右键\"查看清理/卸载指引\"查看该工具的默认路径说明")

        def _do_open_target():
            menu.close()
            # 打开目标路径：不存在时询问用户是否创建（不自动创建）
            # ⚠️ 用 actual_target_path（实际迁移目标），不用 suggest_path（默认模板）
            target_p = actual_target_path.replace("\\\\?\\", "").replace("\\\\.", "")
            if not target_p:
                QMessageBox.information(self, "提示", "此工具无目标路径")
                return
            if os.path.exists(target_p):
                self._open_path(target_p)
                return
            # 路径不存在，询问用户是否创建
            reply = QMessageBox.question(self, "目标路径不存在",
                f"目标路径尚未创建：\n{target_p}\n\n"
                f"是否现在创建此目录？\n"
                f"（正式迁移配置时会自动创建，此处可跳过）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                try:
                    os.makedirs(target_p, exist_ok=True)
                    self._open_path(target_p)
                except Exception as e:
                    QMessageBox.warning(self, "创建失败", f"创建目录失败: {e}")

        btn_cur.clicked.connect(_do_open_cur)
        btn_target.clicked.connect(_do_open_target)

        action = menu.exec(self.table_dev_env.viewport().mapToGlobal(pos))

        if action == act_apply:
            # 单独应用这一个工具
            self._apply_single_dev_tool(tool)
        elif action == act_guide:
            self._show_dev_clean_guide_for_row(row)
        elif action == act_records:
            self._show_dev_env_configured_records()
        elif act_download is not None and action == act_download:
            # 访问官网下载页（用系统默认浏览器打开）
            QDesktopServices.openUrl(QUrl(download_url))
        elif act_restore_data is not None and action == act_restore_data:
            # 还原选中工具的数据到 C 盘（仅数据，不动环境变量）
            self._restore_dev_tools_data(selected_tools)

    def _find_dev_tool_migrated_src(self, tool):
        """查找工具对应的迁移记录 src 路径（通用逻辑，不针对特定工具）

        优先级：
        1. dev_env_configured[tool_id].source_path（配置前捕获的 C 盘原始路径）
           - 若 source_path 在 D 盘（env var 之前已设置），改用 get_tool_default_c_path
             定位 C 盘原始符号链接位置
        2. 在 config["migrated"] 中精确匹配候选 C: 路径
        3. C: 路径本身是符号链接 → 允许还原（orphan symlink）
        4. 在 migrated 中按候选路径的 basename 做模糊匹配（兜底）
        5. 【新增】无迁移记录但环境变量指向其他盘且该盘有数据 → 返回 (None, None, d盘路径)
           支持把"手动放到 D 盘"的数据复制回 C 盘默认路径
        :return: (src_path, migrated_record) 或 (None, None, d盘数据路径) 或 (None, None)
        """
        from utils import is_symlink
        tool_id = tool.get("id", "")

        # 候选 C: 路径列表（用于在 migrated 记录和符号链接中匹配）
        candidate_c_paths = []
        # 1. 优先用配置时捕获的 source_path（仅当在 C 盘时才用）
        dev_env_cfg = self.cfg.get("dev_env_configured") or {}
        cfg_info = dev_env_cfg.get(tool_id) or {}
        src_path = cfg_info.get("source_path", "").replace("\\\\?\\", "")
        if src_path and src_path[1:2] == ":" and src_path[0].upper() == "C":
            # source_path 在 C 盘 → 直接用作候选
            candidate_c_paths.append(src_path)
        # 2. 用 get_tool_default_c_path 兜底（不读工具 env var，纯默认路径）
        default_c = dev_get_tool_default_c_path(tool)
        if default_c:
            default_c = default_c.replace("\\\\?\\", "")
            if default_c not in candidate_c_paths:
                candidate_c_paths.append(default_c)

        # 在 migrated 记录中精确匹配候选路径
        for cand in candidate_c_paths:
            for m in self.cfg.get("migrated", []):
                m_src = (m.get("src") or "").replace("\\\\?\\", "")
                if m_src and m_src.lower() == cand.lower():
                    return cand, m

        # 通用：在 migrated 记录中按父目录匹配（工具路径是迁移源路径的子目录）
        # 场景：用户在普通迁移区迁移了父目录（如 C:\...\Android），
        # 开发环境迁移区的工具路径是子目录（如 C:\...\Android\Sdk）
        # 还原时需要识别出父目录被迁移了，返回父目录作为 src
        for cand in candidate_c_paths:
            if not cand:
                continue
            cand_lower = cand.lower()
            for m in self.cfg.get("migrated", []):
                m_src = (m.get("src") or "").replace("\\\\?\\", "")
                if not m_src:
                    continue
                m_src_lower = m_src.lower()
                # cand 是 m_src 的子目录（如 cand=C:\...\Android\Sdk, m_src=C:\...\Android）
                if cand_lower.startswith(m_src_lower + "\\"):
                    return m_src, m

        # migrated 中无记录，但 C: 路径本身是符号链接 → 允许还原（orphan symlink）
        for cand in candidate_c_paths:
            if cand and os.path.exists(cand) and is_symlink(cand):
                return cand, None

        # 通用：C: 路径本身不是符号链接，但祖先目录是符号链接 → 返回祖先符号链接路径
        # 场景：用户在普通迁移区迁移了父目录（如 C:\...\Android），
        # 工具路径是子目录（如 C:\...\Android\Sdk），子目录本身不是符号链接，
        # 但通过父目录符号链接访问。还原时需要还原整个父目录符号链接。
        for cand in candidate_c_paths:
            if not cand:
                continue
            # 从 cand 向上逐级检查祖先目录是否是符号链接
            # 如 cand = C:\Users\aaa\AppData\Local\Android\Sdk
            # 依次检查 C:\...\Android, C:\...\Local, ... 直到根
            cur = cand
            for _ in range(10):  # 最多向上 10 级，避免无限循环
                parent = os.path.dirname(cur)
                if not parent or parent == cur:
                    break
                try:
                    if os.path.exists(parent) and is_symlink(parent):
                        # 找到祖先符号链接，返回祖先路径
                        # 注意：这里返回祖先路径，还原时会还原整个祖先目录
                        log.info(f"  {tool.get('name','')}: 候选路径 {cand} 的祖先 {parent} 是符号链接")
                        return parent, None
                except Exception as e:
                    log.debug("忽略异常: %s", e)
                cur = parent

        # 兜底：在 migrated 中按候选路径的 basename 模糊匹配
        # （从候选路径提取末尾片段，不针对任何特定工具）
        basenames = set()
        for cand in candidate_c_paths:
            if not cand:
                continue
            # 提取最后 1-2 级路径片段作为模糊匹配特征
            norm = cand.replace("/", "\\").rstrip("\\")
            parts = [p for p in norm.split("\\") if p]
            if parts:
                basenames.add(parts[-1].lower())
            if len(parts) >= 2:
                basenames.add("\\" + parts[-1].lower())
                basenames.add("\\" + parts[-2].lower() + "\\" + parts[-1].lower())
        for m in self.cfg.get("migrated", []):
            m_src = (m.get("src") or "").replace("\\\\?\\", "")
            if not m_src:
                continue
            m_src_lower = m_src.lower()
            for frag in basenames:
                if frag in m_src_lower:
                    return m_src, m

        # 【新增分支】无迁移记录 + C 盘无符号链接，但环境变量指向其他盘且该盘有数据
        # 场景：用户手动把数据放到 D 盘并设了环境变量（未通过本工具迁移）
        # 还原时应该把 D 盘数据复制回 C 盘默认路径
        # 返回三元组 (None, None, d盘数据路径) 让 worker 做反向复制
        try:
            current_path_fn = tool.get("current_path_fn")
            if current_path_fn:
                from dev_env_migrate import CURRENT_PATH_FUNCS
                fn = CURRENT_PATH_FUNCS.get(current_path_fn)
                if fn:
                    cur_path = (fn() or "").replace("\\\\?\\", "")
                    # 当前路径在其他盘（非 C 盘）且存在且有数据
                    if (cur_path and len(cur_path) > 1 and cur_path[1] == ":"
                            and cur_path[0].upper() != "C" and os.path.exists(cur_path)):
                        # 确认该目录有内容（避免把空目录当数据）
                        try:
                            has_content = any(os.scandir(cur_path))
                        except Exception:
                            has_content = False
                        if has_content:
                            # C 盘默认路径应为空或不存在（否则数据冲突）
                            default_c_path = candidate_c_paths[0] if candidate_c_paths else ""
                            if default_c_path and not os.path.exists(default_c_path):
                                log.info(f"  {tool.get('name','')}: 无迁移记录但 D 盘有数据，"
                                         f"反向复制 {cur_path} → {default_c_path}")
                                return None, None, cur_path
        except Exception as e:
            log.error(f"查找 D 盘数据路径失败: {e}")

        return None, None

    def _restore_dev_tools_data(self, tools):
        """全自动还原选中工具到 C 盘（数据 + 配置都还原）

        后台线程批量执行（每个工具依次）：
        1. 撤销环境变量配置（unapply_tool）
        2. 若 C 盘有符号链接 → 删除符号链接 → 复制数据回 C 盘 → 删除迁移记录
           若 C 盘无符号链接（仅配置未迁移数据）→ 跳过数据还原，只撤销配置
        全自动，不需要用户额外操作。
        :param tools: list of tool dict
        """
        try:
            if not tools:
                QMessageBox.information(self, "提示",
                    "没有可还原的工具（仅状态为「✓ 已迁移到X:盘」或「✓ 已配置到X:盘」的工具才能还原）")
                return
            log.info(f"开始全自动还原 {len(tools)} 个工具: {[t.get('name','') for t in tools]}")
            # 清空 detect/path 缓存 + size 缓存（还原后 C 盘不再是符号链接，大小也变了）
            try:
                from dev_env_migrate import clear_detect_path_cache as _clear_cache, clear_size_cache as _clear_size
                _clear_cache()
                _clear_size()
            except Exception as e:
                log.error(f"清空 detect/path/size 缓存失败: {e}")
            # 收集每个工具的还原信息
            # tool_pairs: [(tool, src_path or None, d盘数据路径 or None), ...]
            # - src_path: C 盘符号链接路径（有迁移记录或 C 盘是符号链接时）
            # - d盘数据路径: 无迁移记录但 D 盘有数据时，需要反向复制回 C 盘
            tool_pairs = []
            for tool in tools:
                try:
                    result = self._find_dev_tool_migrated_src(tool)
                    # 兼容二元组和三元组返回
                    if len(result) == 3:
                        src, record, d_data = result
                    else:
                        src, record = result
                        d_data = None
                    tool_pairs.append((tool, src, d_data))
                    log.info(f"  {tool.get('name','')} src_path={src}, d_data={d_data}")
                except Exception as e:
                    import traceback
                    err = f"查找 {tool.get('name','')} 迁移记录失败: {e}\n{traceback.format_exc()[-300:]}"
                    log.error(err)
                    self.on_monitor_log("error", err)
                    tool_pairs.append((tool, None, None))
            # 二次确认
            # 通用：当 src 是父目录符号链接时，提示用户会还原整个父目录
            def _format_restore_info(t, src, dd):
                if src:
                    # 检查 src 是否是工具路径的父目录（祖先符号链接场景）
                    from dev_env_migrate import get_tool_default_c_path as _get_c
                    tool_c = _get_c(t)
                    if tool_c and src.lower() != tool_c.lower():
                        return f"  • {t['name']}  (符号链接: {src})\n      ⚠️ 这是父目录符号链接，将还原整个父目录（含所有子目录）"
                    return f"  • {t['name']}  (符号链接: {src})"
                elif dd:
                    return f"  • {t['name']}  (反向搬数据: {dd} → C 盘)"
                else:
                    return f"  • {t['name']}  (仅配置，无数据迁移)"
            tool_list_text = "\n".join(_format_restore_info(t, src, dd) for t, src, dd in tool_pairs)
            reply = QMessageBox.question(self, "确认全自动还原",
                f"即将全自动还原以下 {len(tool_pairs)} 个工具到 C 盘：\n"
                f"{tool_list_text}\n\n"
                f"将自动执行：\n"
                f"  1. 撤销环境变量配置（如 ANDROID_SDK_HOME、GRADLE_USER_HOME 等）\n"
                f"  2. 撤销配置命令（如 npm config delete、go env -u 等）\n"
                f"  3. 还原 Maven settings.xml 等配置文件\n"
                f"  4. 若 C 盘有符号链接 → 删除链接 → 复制数据从其他盘搬回 C 盘\n"
                f"  5. 若无迁移记录但其他盘有数据 → 复制数据搬回 C 盘默认路径\n"
                f"  6. 删除迁移记录\n\n"
                f"全自动完成，无需额外操作。\n"
                f"确定继续吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return

            # 后台线程批量还原（避免卡 UI）
            self.btn_apply_dev.setEnabled(False)
            self.status_label.setText(f"正在全自动还原 {len(tool_pairs)} 个工具...")
            self.on_monitor_log("dev_env",
                f"开始全自动还原: " + ", ".join(t["name"] for t, _, _ in tool_pairs))

            # 设置还原中标志位，让 monitor 跳过这些 src 的自动修复
            # （避免 monitor 把正在 复制引擎回 C 盘的数据再次迁回 D 盘）
            restoring_list = self.cfg.setdefault("restoring_in_progress", [])
            for _, src, _dd in tool_pairs:
                if src and src not in restoring_list:
                    restoring_list.append(src)
            from config import save_config as _save_cfg
            _save_cfg(self.cfg)

            # 取消正在运行的 DevEnvRefreshWorker（避免它检测工具状态时访问被删除的目录）
            # 注意：大小计算已合并到 DevEnvRefreshWorker，不再有独立的 size worker
            self._safe_cancel_dev_env_worker("_dev_env_refresh_worker", wait_ms=2000)

            from PySide6.QtCore import QThread, Signal
            class _RestoreDataWorker(QThread):
                finished_sig = Signal(list)
                progress_sig = Signal(int, int, str)  # (current, total, msg)
                error_sig = Signal(str)  # 致命错误（顶层异常）
                verbose_log_sig = Signal(str, str)  # migrator 阶段日志（开发工具区专用）
                def __init__(self, pairs, migrator, config):
                    super().__init__()
                    self.pairs = pairs  # [(tool, src_path or None, d盘数据路径 or None), ...]
                    self.migrator = migrator
                    self.config = config
                    self._orig_log_callback = None  # 保存原 callback，run 结束后恢复
                def run(self):
                    # 顶层 try/except：捕获所有未预期异常，防止 QThread 崩溃导致程序闪退且无日志
                    # 临时替换 migrator.log_callback，让 migrator 的阶段日志通过 verbose_log_sig
                    # 发到主线程（仅开发工具迁移区需要，普通迁移区 migrator 调用不经过此 worker）
                    self._orig_log_callback = self.migrator.log_callback
                    self.migrator.log_callback = lambda et, msg: self.verbose_log_sig.emit(et, msg)
                    try:
                        results = []
                        total = len(self.pairs)
                        for i, (tool, src, d_data) in enumerate(self.pairs):
                            detail_msgs = []
                            data_ok = True       # 数据还原是否成功（决定整体成败）
                            config_warn = False  # 配置撤销失败（只警告，不决定整体成败）
                            try:
                                # 进度反馈：开始处理
                                tool_name = tool.get("name", f"工具{i+1}")
                                if src:
                                    self.progress_sig.emit(i, total,
                                        f"[{i+1}/{total}] {tool_name}: 还原数据+撤销配置...")
                                elif d_data:
                                    self.progress_sig.emit(i, total,
                                        f"[{i+1}/{total}] {tool_name}: 反向搬数据回C盘+撤销配置...")
                                else:
                                    self.progress_sig.emit(i, total,
                                        f"[{i+1}/{total}] {tool_name}: 撤销配置中...")
                                # 步骤1：撤销环境变量配置（不抛异常，失败只警告）
                                try:
                                    from dev_env_migrate import unapply_tool
                                    # 从 dev_env_configured 读取该工具配置时的目标盘符
                                    dev_env_cfg = self.config.get("dev_env_configured") or {}
                                    cfg_info = dev_env_cfg.get(tool.get("id", "")) or {}
                                    tgt_drive = cfg_info.get("target_drive", "")
                                    if not tgt_drive:
                                        # 兜底1：从 src 路径推断目标盘符（如 D:\...）
                                        if src and len(src) > 1 and src[1] == ":":
                                            tgt_drive = src[0]
                                        # 兜底2：从 d_data 路径推断（反向数据复制场景）
                                        elif d_data and len(d_data) > 1 and d_data[1] == ":":
                                            tgt_drive = d_data[0]
                                        else:
                                            detail_msgs.append("[撤销配置] 未找到目标盘符，跳过撤销配置")
                                            config_warn = True
                                            tgt_drive = None
                                    if tgt_drive:
                                        # unapply_tool(tool, target_drive) 第二参数是盘符字符串如 "D"
                                        uok, umsg = unapply_tool(tool, tgt_drive)
                                        log.info(f"[RESTORE] unapply_tool({tool.get('id')}, {tgt_drive}) -> ok={uok}, msg={umsg[:200]}")
                                        detail_msgs.append(f"[撤销配置] {umsg}")
                                        if not uok:
                                            config_warn = True
                                except Exception as e:
                                    detail_msgs.append(f"[撤销配置] 异常: {e}")
                                    config_warn = True
                                # 步骤2：还原数据
                                if src:
                                    # 有迁移记录或 C 盘是符号链接 → 用 migrator.restore 还原
                                    from utils import is_symlink
                                    if is_symlink(src):
                                        try:
                                            dok, dmsg = self.migrator.restore(src)
                                            detail_msgs.append(f"[还原数据] {dmsg}")
                                            if not dok:
                                                data_ok = False
                                        except Exception as e:
                                            detail_msgs.append(f"[还原数据] 异常: {e}")
                                            data_ok = False
                                    else:
                                        # C 盘是真实目录（非符号链接）：数据未迁移，无需复制
                                        detail_msgs.append("[还原数据] C 盘为真实目录，数据未迁移，无需还原")
                                elif d_data:
                                    # 【反向搬数据】无迁移记录但 D 盘有数据 → 把数据搬回 C 盘默认路径
                                    # 场景：用户手动把数据放到 D 盘并设了环境变量，未通过本工具迁移
                                    # P6:收敛到 migrator.restore_dev_env_data —— 写 pending 事务
                                    # (断电可恢复) + 引擎复制(mirror+verify=hash) + 错误翻译 +
                                    # 进度上报,修复 UI 层直接调复制命令的架构违规
                                    try:
                                        from dev_env_migrate import get_tool_default_c_path as dev_get_tool_default_c_path
                                        default_c = dev_get_tool_default_c_path(tool)
                                        if not default_c:
                                            detail_msgs.append("[反向搬数据] 无法确定 C 盘默认路径，跳过")
                                            data_ok = False
                                        else:
                                            default_c = default_c.replace("\\\\?\\", "")
                                            detail_msgs.append(
                                                f"[反向搬数据] 引擎复制: {d_data} → {default_c}")
                                            dok, dmsg = self.migrator.restore_dev_env_data(
                                                d_data, default_c)
                                            detail_msgs.append(f"[反向搬数据] {dmsg}")
                                            if not dok:
                                                data_ok = False
                                    except Exception as e:
                                        import traceback
                                        detail_msgs.append(
                                            f"[反向搬数据] 异常: {e}\n{traceback.format_exc()[-300:]}")
                                        data_ok = False
                                else:
                                    detail_msgs.append("[还原数据] 无迁移记录，仅撤销配置")
                            except Exception as e:
                                # 单个工具处理异常：记录错误，继续处理下一个工具（不崩溃）
                                import traceback
                                detail_msgs.append(f"[处理异常] {e}\n{traceback.format_exc()[-300:]}")
                                data_ok = False
                            # 数据还原成功后，清理 dev_env_configured[tool_id] 记录
                            # （否则残留的 source_path 会让下次刷新误判为已配置）
                            if data_ok:
                                try:
                                    dev_env_cfg = self.config.get("dev_env_configured") or {}
                                    tid = tool.get("id", "")
                                    if tid and tid in dev_env_cfg:
                                        del dev_env_cfg[tid]
                                        self.config["dev_env_configured"] = dev_env_cfg
                                        from config import save_config
                                        save_config(self.config)
                                        detail_msgs.append("[清理配置] ✓ 已删除 dev_env_configured 记录")
                                except Exception as e:
                                    detail_msgs.append(f"[清理配置] ⚠ 删除 dev_env_configured 记录失败: {e}")
                            # 整体成败由数据还原决定，配置撤销失败只标记为警告
                            overall_ok = data_ok
                            results.append((tool, overall_ok, config_warn, "\n".join(detail_msgs), src))
                        # 进度反馈：全部完成
                        self.progress_sig.emit(total, total, f"全部 {total} 个工具还原完成")
                        self.finished_sig.emit(results)
                    except Exception as e:
                        # 顶层异常：发 error_sig，主线程弹窗显示，避免 QThread 静默崩溃
                        import traceback
                        err_msg = f"还原线程致命异常: {e}\n{traceback.format_exc()[-500:]}"
                        log.error(err_msg)
                        self.error_sig.emit(err_msg)
                    finally:
                        # 恢复 migrator.log_callback（避免影响后续普通迁移区调用）
                        self.migrator.log_callback = self._orig_log_callback

            restore_worker = _RestoreDataWorker(tool_pairs, self.migrator, self.cfg)
            self._dev_env_restore_worker = restore_worker
            # 开发工具迁移区专用：migrator 阶段日志转发到监控日志
            restore_worker.verbose_log_sig.connect(self._log_monitor, Qt.QueuedConnection)

            # 显示进度条
            self.progress.setVisible(True)
            self.progress.setRange(0, len(tool_pairs))
            self.progress.setValue(0)
            self.progress.setTextVisible(True)
            self.progress.setFormat(f"全自动还原中 0/{len(tool_pairs)} (%p%)")

            def _on_restore_progress(current, total, msg):
                self.progress.setValue(current)
                self.progress.setFormat(f"全自动还原 {current}/{total} (%p%)")
                self.status_label.setText(msg)
                self.on_monitor_log("dev_env", msg)

            def _on_restore_error(err_msg):
                self.btn_apply_dev.setEnabled(True)
                self.progress.setVisible(False)
                self.status_label.setText(f"还原失败: 致命异常")
                self.on_monitor_log("error", f"还原线程致命异常: {err_msg}")
                QMessageBox.critical(self, "还原失败",
                    f"还原过程中发生致命异常：\n\n{err_msg}\n\n"
                    f"请将此错误信息反馈给开发者。已完成的还原会在下次启动时自动续传。")

            def _on_restore_done(results):
                self.btn_apply_dev.setEnabled(True)
                self.progress.setVisible(False)
                # 清理还原中标志位
                try:
                    restoring_list = self.cfg.get("restoring_in_progress", [])
                    restored_srcs = {s for _, _, _, _, s in results}
                    restoring_list[:] = [s for s in restoring_list if s not in restored_srcs]
                    _save_cfg(self.cfg)
                except Exception as e:
                    log.error(f"清理还原标志位失败: {e}")
                # results 结构：(tool, data_ok, config_warn, msg, src)
                # data_ok 决定整体成败，config_warn 单独统计
                success_count = sum(1 for _, ok, _, _, _ in results if ok)
                warn_count = sum(1 for _, ok, cw, _, _ in results if ok and cw)
                fail_count = len(results) - success_count
                detail = "\n\n".join(
                    f"{'✓' if ok else '✗'} {t['name']}" + (" ⚠️" if (ok and cw) else "") + f":\n{msg}"
                    for t, ok, cw, msg, _ in results)
                status_text = f"全自动还原完成：成功 {success_count} 个"
                if warn_count > 0:
                    status_text += f"（其中 {warn_count} 个配置撤销有警告）"
                status_text += f"，失败 {fail_count} 个"
                self.status_label.setText(status_text)
                for t, ok, cw, msg, _ in results:
                    if ok:
                        mark = "✓" + ("⚠️" if cw else "")
                        status_word = "成功" + ("（配置撤销有警告）" if cw else "")
                    else:
                        mark = "✗"
                        status_word = "失败"
                    self.on_monitor_log("dev_env",
                        f"{mark} 全自动还原 {t['name']} {status_word}")
                self.on_monitor_log("dev_env",
                    f"全自动还原完成：成功 {success_count} 个"
                    + (f"（其中 {warn_count} 个配置撤销有警告）" if warn_count > 0 else "")
                    + f"，失败 {fail_count} 个")
                result_title = "还原完成"
                if fail_count > 0:
                    result_title = f"还原完成（{fail_count} 个失败）"
                elif warn_count > 0:
                    result_title = f"还原完成（{warn_count} 个配置撤销有警告）"
                # 构建提示文案：明确告知用户"做了什么"+"下一步建议"
                has_data_restore = any(s for _,_,_,_,s in results)
                msg_lines = [f"成功 {success_count} 个，失败 {fail_count} 个"]
                if warn_count > 0:
                    msg_lines.append(f"（其中 {warn_count} 个数据已还原，但环境变量撤销有警告，建议手动检查）")
                msg_lines.append("")  # 空行
                msg_lines.append("已完成的操作：")
                msg_lines.append("  ✓ 撤销环境变量配置（恢复到迁移前状态）")
                if has_data_restore:
                    msg_lines.append("  ✓ 数据从目标盘搬回 C 盘原位置")
                msg_lines.append("  ✓ 清理开发环境配置记录")
                msg_lines.append("  ✓ 自动刷新待迁移区、已迁移区、开发环境区")
                msg_lines.append("")
                msg_lines.append("详细结果：")
                msg_lines.append(detail)
                if fail_count > 0:
                    msg_lines.append("")
                    msg_lines.append("⚠ 失败的工具可查看上方监控日志了解原因，或重新尝试还原。")
                QMessageBox.information(self, result_title, "\n".join(msg_lines))
                # 局部刷新开发环境迁移区：只更新涉及的工具行，避免全表刷新很慢
                try:
                    tids = [t["id"] for t, _, _, _, _ in results]
                    self._partial_refresh_dev_env_rows(tids, reason="还原完成")
                except Exception as e:
                    log.error(f"局部刷新失败，回退全表刷新: {e}")
                    self._refresh_dev_env_table()
                # 刷新已迁移区（已迁移区有自己的局部刷新机制）
                try:
                    self._refresh_migrated_only()
                except Exception as e:
                    log.error(f"刷新已迁移区失败: {e}")
                # 还原后的目录需重新出现在待迁移区：触发轻量扫描添加新行
                try:
                    for t, ok, _, _, src in results:
                        if ok and src:
                            self._move_row_to_scan(src)
                except Exception as e:
                    log.error(f"待迁移区添加还原行失败: {e}")

            restore_worker.progress_sig.connect(_on_restore_progress)
            restore_worker.error_sig.connect(_on_restore_error)
            restore_worker.finished_sig.connect(_on_restore_done)
            restore_worker.start()
        except Exception as e:
            # 顶层兜底：防止 _restore_dev_tools_data 任何异常导致闪退
            import traceback
            err_detail = f"_restore_dev_tools_data 异常: {e}\n{traceback.format_exc()[-500:]}"
            log.error(err_detail)
            self.on_monitor_log("error", err_detail)
            try:
                self.btn_apply_dev.setEnabled(True)
                self.progress.setVisible(False)
                QMessageBox.critical(self, "内部错误",
                    f"启动还原操作时出错：\n{e}\n\n详情已记录到日志，请重试或反馈给开发者。")
            except Exception as e:
                log.debug("忽略异常: %s", e)

    def _apply_single_dev_tool(self, tool):
        """单独应用一个工具的配置（通用：支持 C→D / D→D / D→E / E→F 等任意盘间迁移）

        弹窗显示具体目标路径（不只是盘符），三选一：
          1. 配置 + 迁移数据（复制 + 符号链接）
          2. 只配置环境变量（数据不动）
          3. 取消（退出操作）

        通用迁移：无论数据当前在 C 盘还是非 C 盘，都允许迁移到目标路径。
        """
        target_drive = self.dev_target_drive.currentText()
        if tool["special"]:
            QMessageBox.warning(self, "无法自动配置",
                f"{tool['name']} 是特殊工具，无法用环境变量自动配置。\n"
                f"请查看清理指引手动处理。")
            return

        # 先算建议路径（用于在确认弹窗中显示具体目标路径，而不是宽泛的"X 盘"）
        # 优先读表格"建议新路径"列（用户可能双击修改过），否则用默认建议路径
        # 通用：当前路径非 C 盘时，表格"建议新路径"列可能为空，用户必须双击指定新路径才能迁移
        custom_path = ""
        for row in range(self.table_dev_env.rowCount()):
            name_item = self.table_dev_env.item(row, 1)
            if name_item and name_item.text() == tool["name"]:
                suggest_item = self.table_dev_env.item(row, 6)
                if suggest_item:
                    table_path = suggest_item.text().strip()
                    default_path = dev_get_suggest_path(tool, target_drive)
                    if table_path and table_path.lower() != default_path.lower():
                        custom_path = table_path
                break
        suggest = custom_path or dev_get_suggest_path(tool, target_drive)

        # 第一道确认：显示具体目标路径
        reply = QMessageBox.question(self, "确认配置",
            f"即将配置 {tool['name']}\n\n"
            f"  目标路径: {suggest}\n\n"
            f"是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        # 检测数据量（通用：无论数据在哪个盘都检测），给智能迁移建议
        self.status_label.setText(f"正在检测 {tool['name']} 数据量...")
        from dev_env_migrate import get_tool_data_info as _get_info
        migrate_data = False
        try:
            info = _get_info(tool)
        except Exception as e:
            info = {"has_data": False, "size_mb": 0, "message": f"检测失败: {e}",
                    "on_c": False, "source_path": ""}
        self.status_label.setText("")

        if not info.get("has_data"):
            # 无数据 → 不弹框，直接只配置环境变量
            migrate_data = False
            self.status_label.setText(
                f"{tool['name']}：无现有数据，只配置环境变量...")
        else:
            size_mb = info.get("size_mb", 0)
            on_c = info.get("on_c", False)
            source_path = info.get("source_path", "")
            # 通用文案：根据数据所在位置调整提示
            if on_c:
                data_loc_desc = f"C 盘有 {_format_size(size_mb)} 数据"
            else:
                data_loc_desc = f"当前路径有 {_format_size(size_mb)} 数据\n  当前路径: {source_path}"

            if size_mb >= 500:
                advice = f"💡 检测到 {_format_size(size_mb)} 数据，建议同时迁移"
                default_btn = QMessageBox.Yes
            elif size_mb >= 50:
                advice = f"💡 检测到 {_format_size(size_mb)} 数据，数据量中等，建议同时迁移"
                default_btn = QMessageBox.Yes
            else:
                advice = f"💡 检测到仅 {_format_size(size_mb)} 数据，数据量少，可只配置不迁移"
                default_btn = QMessageBox.No
            # 三选一弹窗：Yes=配置+迁移 / No=只配置 / Cancel=退出
            reply = QMessageBox.question(self, "选择操作方式",
                f"{tool['name']} {data_loc_desc}\n\n"
                f"目标路径: {suggest}\n\n"
                f"{advice}\n\n"
                f"  • 『Yes』配置 + 迁移数据（复制 + 符号链接）\n"
                f"     数据搬到 {suggest}\n"
                f"     迁移后原位置变符号链接指向新路径\n\n"
                f"  • 『No』只配置环境变量\n"
                f"     现有数据不动，以后新装的去 {suggest}\n\n"
                f"  • 『Cancel』取消，不做任何操作",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, default_btn)
            if reply == QMessageBox.Yes:
                migrate_data = True
            elif reply == QMessageBox.No:
                migrate_data = False
            else:
                # Cancel 或关闭按钮 → 退出操作
                self.status_label.setText(f"{tool['name']} 配置已取消")
                return

        self.status_label.setText(f"正在配置 {tool['name']}...")
        # custom_path 已在弹窗前算过，这里直接复用
        # 如果用户双击修改了建议路径，需要把 tool 的 env_vars / config_commands
        # 也改成用户指定的路径，再传给 Worker
        tool_to_apply = tool
        if custom_path:
            import copy as _copy
            tool_to_apply = _copy.deepcopy(tool)
            for ev in tool_to_apply["env_vars"]:
                ev["default_value_template"] = custom_path
            for cmd in tool_to_apply["config_commands"]:
                if len(cmd["cmd_template"]) > 1:
                    cmd["cmd_template"][-1] = custom_path
            suggest = custom_path
        else:
            suggest = dev_get_suggest_path(tool, target_drive)
        self.on_monitor_log("dev_env",
            f"开始配置 {tool['name']} → {suggest}")

        # 用 Worker 在后台线程执行，通过 Signal 回到主线程
        # 通用：传入 target_path_override 让 migrate_tool_data 用用户指定的目标路径
        # （而非默认模板路径），支持 D→D / D→E 等任意盘间迁移
        worker = DevEnvApplyWorker([tool_to_apply], target_drive,
                                   migrate_data=migrate_data, config=self.cfg,
                                   target_path_override=custom_path if custom_path else None)
        self._dev_env_apply_worker = worker

        def _on_done(worker_results, drive):
            # 顶层 try/except：防止任何异常传到 Qt C++ 层导致闪退且无日志
            try:
                # 确保 worker 线程完全退出，避免模态对话框期间的竞态
                # （QMessageBox.information 会启动嵌套事件循环，此时 QThread 可能在清理中）
                try:
                    worker.wait(500)
                except Exception as e:
                    log.debug("忽略异常: %s", e)
                # worker_results 是 [(tool, ok, msg, source_path), ...]，取第一个
                if not worker_results:
                    return
                _, ok, msg, source_path = worker_results[0]
                if ok:
                    # 保存配置记录
                    try:
                        configured = self.cfg.setdefault("dev_env_configured", {})
                        configured[tool["id"]] = {
                            "name": tool["name"],
                            "category": tool["category"],
                            "target_drive": drive,
                            "target_path": custom_path or dev_get_suggest_path(tool, drive),
                            "env_vars": [ev["name"] for ev in tool["env_vars"]],
                            "configured_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "clean_guide": tool["clean_guide"],
                            "source_path": source_path,
                        }
                        save_all(self.cfg)
                    except Exception as e:
                        log.error(f"保存配置记录失败: {e}")
                    self.status_label.setText(f"{tool['name']} 配置完成")
                    self.on_monitor_log("dev_env",
                        f"✓ {tool['name']} 配置成功 → {suggest}")
                    # 根据迁移情况生成说明（合并原"无需迁移数据"框的信息）
                    if migrate_data:
                        status_note = (
                            f"✅ 三区状态变化：\n"
                            f"  • 开发环境迁移区：此行变绿色（数据已迁移）\n"
                            f"  • 待迁移区：不再显示此目录（C 盘已是符号链接）\n"
                            f"  • 已迁移区：新增 1 条迁移记录"
                        )
                    elif not info.get("has_data"):
                        # 原"无需迁移数据"框的内容合并到这里
                        status_note = (
                            f"ℹ️ C 盘无已装包/库，已只配置环境变量\n"
                            f"   以后新装的包会直接去 {drive}: 盘（不在 C 盘创建软连接）\n\n"
                            f"📋 三区状态变化：\n"
                            f"  • 开发环境迁移区：此行变绿色（环境已配置）\n"
                            f"  • 待迁移区/已迁移区：无变化（C 盘本就无数据）\n\n"
                            f"💡 提示：主程序（如 Node.js/Python/Git 解释器本身）"
                            f"如果装在 C 盘，可去「待迁移区」迁移（用软连接），"
                            f"或卸载后重装到目标盘（最干净）。"
                        )
                    else:
                        status_note = (
                            f"📋 三区状态变化：\n"
                            f"  • 开发环境迁移区：此行变绿色（环境已配置）\n"
                            f"  • 待迁移区：对应目录标橙色「[已配置]」\n"
                            f"  • 建议去待迁移区点「迁移」按钮把数据搬到 D 盘"
                        )
                    QMessageBox.information(self, "配置成功",
                        f"✓ {tool['name']} 配置完成\n\n{msg}\n\n"
                        f"⚠️ 请重新打开终端/编辑器让新环境变量生效。\n\n"
                        f"{status_note}")
                    # 刷新表格（加 try/except 防止刷新失败导致闪退）
                    try:
                        self._refresh_dev_env_table()
                    except Exception as e:
                        log.error(f"刷新开发环境表格失败: {e}")
                    # 数据迁移成功后，同步刷新待迁移区和已迁移区
                    if migrate_data:
                        try:
                            self._refresh_migrated_only()
                        except Exception as e:
                            log.error(f"刷新已迁移区失败: {e}")
                        try:
                            self._light_refresh_scan_table()
                        except Exception as e:
                            log.error(f"刷新待迁移区失败: {e}")
                else:
                    self.status_label.setText(f"{tool['name']} 配置失败")
                    self.on_monitor_log("dev_env",
                        f"✗ {tool['name']} 配置失败: {msg}")
                    QMessageBox.warning(self, "配置失败",
                        f"✗ {tool['name']} 配置失败\n\n{msg}")
            except Exception as e:
                # 顶层兜底：记录错误，防止 Qt 槽函数崩溃导致程序闪退
                import traceback
                err_detail = f"_on_done 异常: {e}\n{traceback.format_exc()[-500:]}"
                log.error(err_detail)
                self.on_monitor_log("error", err_detail)
                try:
                    QMessageBox.critical(self, "内部错误",
                        f"配置完成但刷新界面时出错：\n{e}\n\n配置已保存，请手动刷新表格。")
                except Exception as e:
                    log.debug("忽略异常: %s", e)
                # 异常时也刷新表格（避免 on_monitor_log 等非关键异常导致表格不更新）
                try:
                    self._refresh_dev_env_table()
                except Exception as e2:
                    log.error(f"异常后刷新表格失败: {e2}")

        def _on_error(err):
            try:
                self.status_label.setText(f"{tool['name']} 配置失败: {err}")
                self.on_monitor_log("dev_env", f"✗ {tool['name']} 配置失败: {err}")
                log.error(f"单工具配置失败: {err}")
                QMessageBox.critical(self, "配置失败",
                    f"✗ {tool['name']} 配置失败: {err}")
            except Exception as e:
                log.error(f"_on_error 槽异常: {e}")

        worker.finished_signal.connect(_on_done)
        worker.progress_signal.connect(lambda c, t, m: self.on_monitor_log("dev_env", m))
        worker.verbose_log_sig.connect(self._log_monitor, Qt.QueuedConnection)
        worker.error_signal.connect(_on_error)
        worker.start()

    def _show_dev_env_configured_records(self):
        """显示已配置工具记录（卸载辅助）
        帮用户找到 D 盘真实目录，方便卸载开发工具时清理
        """
        configured = self.cfg.get("dev_env_configured", {})
        if not configured:
            QMessageBox.information(self, "已配置记录",
                "暂无配置记录。\n\n"
                "配置过开发工具后，这里会记录每个工具的 D 盘真实路径，\n"
                "方便你以后卸载工具时找到并清理 D 盘的残留文件。")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("已配置工具的记录详情")
        dlg.resize(900, 600)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "【卸载辅助说明】\n"
            "  这里记录了你配置过环境变量的所有开发工具和它们的目标盘真实路径。\n"
            "  当你卸载某个开发工具（如卸载 cargo、go、node.js）时，\n"
            "  系统卸载程序通常只清理 C 盘的安装目录，不会清理目标盘的包/缓存目录。\n"
            "  你可以在这里找到目标盘真实路径，手动删除或用「清空目标盘目录」按钮清理。\n\n"
            "  ⚠️ 清空前请确认该工具确实已卸载或不再需要，清空后无法恢复（目录本身保留）。"
        ))

        # 记录表格
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView
        tbl = QTableWidget()
        tbl.setColumnCount(6)
        tbl.setHorizontalHeaderLabels(
            ["工具", "类别", "目标盘", "目标盘真实路径", "配置时间", "环境变量"])
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        tbl.horizontalHeader().setStretchLastSection(False)
        # 列宽适配 900px 对话框：工具/类别/目标盘缩窄，腾出空间给路径和环境变量
        tbl.setColumnWidth(0, 130)
        tbl.setColumnWidth(1, 70)
        tbl.setColumnWidth(2, 50)
        tbl.setColumnWidth(3, 260)
        tbl.setColumnWidth(4, 130)
        tbl.setColumnWidth(5, 260)
        tbl.setSelectionBehavior(QTableWidget.SelectRows)
        tbl.setSelectionMode(QTableWidget.SingleSelection)
        tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        tbl.setAlternatingRowColors(True)
        tbl.verticalHeader().setVisible(False)

        records = list(configured.values())
        for rec in records:
            row = tbl.rowCount()
            tbl.insertRow(row)
            tbl.setItem(row, 0, QTableWidgetItem(rec.get("name", "")))
            tbl.setItem(row, 1, QTableWidgetItem(rec.get("category", "")))
            tbl.setItem(row, 2, QTableWidgetItem(rec.get("target_drive", "")))
            path_item = QTableWidgetItem(rec.get("target_path", ""))
            path_item.setToolTip(rec.get("target_path", ""))
            tbl.setItem(row, 3, path_item)
            tbl.setItem(row, 4, QTableWidgetItem(rec.get("configured_time", "")))
            env_str = ", ".join(rec.get("env_vars", []))
            env_item = QTableWidgetItem(env_str)
            env_item.setToolTip(env_str)
            tbl.setItem(row, 5, env_item)

        lay.addWidget(tbl, stretch=1)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_open = QPushButton("打开目标盘目录")
        btn_open.clicked.connect(lambda: self._open_configured_dir(tbl, records))
        btn_row.addWidget(btn_open)

        btn_del_dir = QPushButton("清空目标盘目录（卸载清理）")
        btn_del_dir.setStyleSheet(
            "QPushButton { background-color: #F44336; color: white; font-weight: bold; }"
            "QPushButton:hover { background-color: #D32F2F; }")
        btn_del_dir.clicked.connect(lambda: self._delete_configured_dir(tbl, records))
        btn_row.addWidget(btn_del_dir)

        btn_unconfig = QPushButton("取消环境变量配置")
        btn_unconfig.setToolTip("删除该工具的环境变量配置，但保留目标盘文件")
        btn_unconfig.clicked.connect(lambda: self._unconfig_from_records(tbl, records, configured))
        btn_row.addWidget(btn_unconfig)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)

        dlg.exec()

    def _open_configured_dir(self, tbl, records):
        """打开已配置记录中选中行的 D 盘目录"""
        rows = {idx.row() for idx in tbl.selectedIndexes()}
        if not rows:
            QMessageBox.information(self, "提示", "请先选择一行")
            return
        row = sorted(rows)[0]
        if row >= len(records):
            return
        path = records[row].get("target_path", "")
        if not path:
            return
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            log.debug("忽略异常: %s", e)
        self._open_path(path)

    def _delete_configured_dir(self, tbl, records):
        """删除已配置记录中选中行的 D 盘目录（卸载清理用）"""
        rows = {idx.row() for idx in tbl.selectedIndexes()}
        if not rows:
            QMessageBox.information(self, "提示", "请先选择一行")
            return
        row = sorted(rows)[0]
        if row >= len(records):
            return
        rec = records[row]
        path = rec.get("target_path", "")
        name = rec.get("name", "")
        if not path or not os.path.exists(path):
            QMessageBox.information(self, "提示", f"目录不存在：{path}")
            return

        # 计算目录大小
        try:
            total_size = 0
            for dirpath, dirnames, filenames in os.walk(path):
                for f in filenames:
                    try:
                        fp = os.path.join(dirpath, f)
                        total_size += os.path.getsize(fp)
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
            size_mb = total_size / (1024 * 1024)
            size_str = f"{size_mb:.2f} MB"
        except Exception:
            size_str = "（无法计算大小）"

        # 二次确认（红色警告）
        confirm = QMessageBox(self)
        confirm.setWindowTitle("⚠️ 危险操作确认")
        confirm.setIcon(QMessageBox.Critical)
        confirm.setText(
            f"⚠️ 即将清空以下目录内的所有内容（保留目录本身）：\n\n"
            f"工具：{name}\n"
            f"路径：{path}\n"
            f"大小：{size_str}\n\n"
            f"此操作不可恢复！请确认：\n"
            f"  1. 该开发工具已卸载或不再需要\n"
            f"  2. 目录里的包/缓存都不再需要\n"
            f"  3. 没有其他工具依赖此目录\n\n"
            f"注意：只会清空目录内的文件和子目录，目录本身会保留。\n"
            f"确定要清空吗？")
        btn_yes = confirm.addButton("确认清空", QMessageBox.AcceptRole)
        confirm.addButton("取消", QMessageBox.RejectRole)
        confirm.setDefaultButton(confirm.button(QMessageBox.RejectRole))
        if confirm.exec() != QMessageBox.AcceptRole:
            return

        # 执行清空（保留目录本身，只删里面的内容，避免误删父目录结构）
        try:
            ok, err = self.migrator._cleanup_dir_contents(path)
            if ok:
                self.on_monitor_log("dev_env",
                    f"🗑️ 已清空 {name} 的目标盘目录内容：{path}（{size_str}），目录本身保留")
                QMessageBox.information(self, "清空成功",
                    f"已清空目录内容：{path}\n大小：{size_str}\n"
                    f"目录本身已保留（空目录）。\n\n"
                    f"如需同时删除环境变量配置，请点「取消环境变量配置」按钮。")
            else:
                self.on_monitor_log("dev_env",
                    f"⚠️ 清空 {name} 目标盘目录部分失败：{err}")
                QMessageBox.warning(self, "部分清空失败",
                    f"目录内容已部分清空，但有残留：\n{err}\n\n"
                    f"可能是某些文件被占用，请关闭相关程序后重试。")
        except Exception as e:
            self.on_monitor_log("dev_env",
                f"✗ 清空 {name} 目标盘目录失败：{e}")
            QMessageBox.critical(self, "清空失败",
                f"清空失败：{e}\n\n"
                f"可能是目录被占用，请先关闭相关程序（如编辑器、终端）再试。")

    def _unconfig_from_records(self, tbl, records, configured_dict):
        """从已配置记录中一键还原（完整回滚）"""
        rows = {idx.row() for idx in tbl.selectedIndexes()}
        if not rows:
            QMessageBox.information(self, "提示", "请先选择一行")
            return
        row = sorted(rows)[0]
        if row >= len(records):
            return
        rec = records[row]
        tool_id = None
        tool = None
        # 通过 name 反查 tool 对象
        for t in DEV_TOOLS:
            if t["name"] == rec.get("name"):
                tool = t
                tool_id = t["id"]
                break
        if not tool:
            QMessageBox.warning(self, "错误", "找不到对应工具定义")
            return

        target_drive = rec.get("target_drive", "D")
        # 列出将要执行的回滚操作
        rollback_items = []
        for ev in tool["env_vars"]:
            rollback_items.append(f"  • 删除环境变量 {ev['name']}")
        for cmd_info in tool.get("unconfig_commands", []):
            rollback_items.append(f"  • {cmd_info['desc']}")
        if tool["id"] == "maven_repo":
            rollback_items.append("  • 还原 Maven settings.xml（移除 localRepository）")
        if tool["id"] == "bazel_output":
            rollback_items.append("  • 还原 .bazelrc（移除 output_user_root）")

        if QMessageBox.question(self, "确认一键还原",
            f"即将回滚 {tool['name']} 的所有配置：\n"
            + "\n".join(rollback_items) +
            f"\n\nD 盘数据目录保留不动（用户数据无价）。\n是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return

        ok, msg = dev_unapply_tool(tool, target_drive)
        if ok:
            # 从记录中删除
            if tool_id in configured_dict:
                del configured_dict[tool_id]
                try:
                    save_all(self.cfg)
                except Exception as e:
                    log.error(f"保存配置失败: {e}")
            self.on_monitor_log("dev_env", f"✓ 回滚 {tool['name']} 成功")
            QMessageBox.information(self, "还原成功",
                f"✓ {tool['name']} 已还原到默认配置\n\n{msg}\n\n"
                f"⚠️ 请重新打开终端/编辑器让变更生效。\n"
                f"目标盘数据目录保留，如需清空请用「清空目标盘目录」按钮。")
            # 关闭对话框重新打开（刷新记录）
            self.sender().parent().accept()
            self._show_dev_env_configured_records()
        else:
            self.on_monitor_log("dev_env",
                f"✗ 回滚 {tool['name']} 失败：{msg}")
            QMessageBox.warning(self, "还原失败", msg)

    def _unconfigure_dev_tool(self, tool):
        """从主表格右键：一键还原单个工具配置（完整回滚）
        包括：删除环境变量 + 撤销配置命令（npm config delete 等）+ 还原 Maven/Bazel 配置文件
        """
        # 查配置记录获取当初的目标盘符
        configured = self.cfg.get("dev_env_configured", {})
        rec = configured.get(tool["id"], {})
        target_drive = rec.get("target_drive", self.dev_target_drive.currentText())

        # 列出将要执行的回滚操作
        rollback_items = []
        for ev in tool["env_vars"]:
            rollback_items.append(f"  • 删除环境变量 {ev['name']}")
        for cmd_info in tool.get("unconfig_commands", []):
            rollback_items.append(f"  • {cmd_info['desc']}")
        if tool["id"] == "maven_repo":
            rollback_items.append("  • 还原 Maven settings.xml（移除 localRepository）")
        if tool["id"] == "bazel_output":
            rollback_items.append("  • 还原 .bazelrc（移除 output_user_root）")
        if not rollback_items:
            QMessageBox.information(self, "提示",
                f"{tool['name']} 没有可回滚的配置（可能是特殊工具或未配置过）。")
            return

        if QMessageBox.question(self, "确认一键还原",
            f"即将回滚 {tool['name']} 的所有配置：\n"
            + "\n".join(rollback_items) +
            f"\n\nD 盘数据目录保留不动（用户数据无价）。\n是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return

        self.status_label.setText(f"正在回滚 {tool['name']}...")
        self.on_monitor_log("dev_env", f"开始回滚 {tool['name']}（目标盘 {target_drive}:）")

        ok, msg = dev_unapply_tool(tool, target_drive)
        if ok:
            # 从配置记录中删除
            if tool["id"] in configured:
                del configured[tool["id"]]
                try:
                    save_all(self.cfg)
                except Exception as e:
                    log.error(f"保存配置失败: {e}")
            self.status_label.setText(f"{tool['name']} 回滚完成")
            self.on_monitor_log("dev_env", f"✓ 回滚 {tool['name']} 成功")
            QMessageBox.information(self, "还原成功",
                f"✓ {tool['name']} 已还原到默认配置\n\n{msg}\n\n"
                f"⚠️ 请重新打开终端/编辑器让变更生效。\n"
                f"目标盘数据目录保留，如需删除请到『已配置工具的记录详情』中操作。")
            self._refresh_dev_env_table()
        else:
            self.status_label.setText(f"{tool['name']} 回滚失败")
            self.on_monitor_log("dev_env", f"✗ 回滚 {tool['name']} 失败: {msg}")
            QMessageBox.warning(self, "还原失败", msg)

    def _rollback_all_dev_env(self):
        """一键还原所有已配置的开发环境（批量回滚）
        遍历 state.json 的 dev_env_configured 记录，逐个调用 unapply_tool
        """
        configured = self.cfg.get("dev_env_configured", {})
        if not configured:
            QMessageBox.information(self, "无配置可还原",
                "当前没有任何开发环境配置记录。\n\n"
                "只有通过『应用选中配置』或右键『配置并迁移此工具到新路径』配置过的工具才能还原。")
            return

        # 列出所有已配置工具
        tool_ids = list(configured.keys())
        tools_to_rollback = []
        for tid in tool_ids:
            tool = next((t for t in DEV_TOOLS if t["id"] == tid), None)
            if tool:
                tools_to_rollback.append(tool)

        if not tools_to_rollback:
            QMessageBox.information(self, "无配置可还原",
                "配置记录中的工具已不在列表中，可能是工具列表已更新。")
            return

        # 二次确认（危险操作）
        tool_list_text = "\n".join(f"  • {t['name']}" for t in tools_to_rollback)
        reply = QMessageBox.question(self, "⚠️ 确认一键还原所有配置",
            f"即将回滚以下 {len(tools_to_rollback)} 个工具的所有配置：\n"
            f"{tool_list_text}\n\n"
            f"每个工具将执行：\n"
            f"  • 删除环境变量\n"
            f"  • 撤销配置命令（npm config delete / go env -u 等）\n"
            f"  • 还原 Maven/Bazel 配置文件\n\n"
            f"⚠️ 此操作不可撤销！D 盘数据目录保留不动。\n"
            f"确认要还原所有配置吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        # 再次确认
        reply2 = QMessageBox.warning(self, "再次确认",
            f"即将还原 {len(tools_to_rollback)} 个工具的配置！\n\n"
            f"还原后所有开发工具将回到默认状态（装回 C 盘默认路径）。\n"
            f"D 盘的数据目录会保留（需手动删除）。\n\n"
            f"确定继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply2 != QMessageBox.Yes:
            return

        # 后台线程批量回滚（避免卡 UI）
        # 注：独立的"一键还原所有配置"按钮已合并到"📋 还原配置（历史快照）"对话框中
        # 这里改为可被对话框复用的内部方法
        self.btn_apply_dev.setEnabled(False)
        self.status_label.setText(f"正在批量回滚 {len(tools_to_rollback)} 个工具...")
        self.on_monitor_log("dev_env",
            f"开始批量回滚 {len(tools_to_rollback)} 个开发工具: "
            + ", ".join(t["name"] for t in tools_to_rollback))

        class _RollbackWorker(QThread):
            finished_sig = Signal(list)
            def __init__(self, tools, configured_records):
                super().__init__()
                self.tools = tools
                self.records = configured_records
            def run(self):
                results = []
                for tool in self.tools:
                    try:
                        drive = self.records.get(tool["id"], {}).get("target_drive", "D")
                        ok, msg = dev_unapply_tool(tool, drive)
                        results.append((tool, ok, msg))
                    except Exception as e:
                        results.append((tool, False, f"异常: {e}"))
                self.finished_sig.emit(results)

        rollback_worker = _RollbackWorker(tools_to_rollback, configured)
        self._dev_env_rollback_worker = rollback_worker

        def _on_rollback_done(results):
            self.btn_apply_dev.setEnabled(True)
            success_count = sum(1 for _, ok, _ in results if ok)
            fail_count = len(results) - success_count
            detail = "\n\n".join(
                f"{'✓' if ok else '✗'} {t['name']}:\n{msg}"
                for t, ok, msg in results
            )
            # 清空配置记录（成功回滚的）
            if success_count > 0:
                try:
                    new_configured = {}
                    for t, ok, _ in results:
                        if not ok and t["id"] in configured:
                            new_configured[t["id"]] = configured[t["id"]]
                    self.cfg["dev_env_configured"] = new_configured
                    save_all(self.cfg)
                except Exception as e:
                    log.error(f"清空配置记录失败: {e}")
            self.status_label.setText(
                f"批量回滚完成：成功 {success_count} 个，失败 {fail_count} 个")
            for t, ok, msg in results:
                mark = "✓" if ok else "✗"
                self.on_monitor_log("dev_env",
                    f"{mark} 回滚 {t['name']} {'成功' if ok else '失败'}")
            self.on_monitor_log("dev_env",
                f"批量回滚完成：成功 {success_count} 个，失败 {fail_count} 个")
            QMessageBox.information(self, "回滚结果",
                f"成功还原 {success_count} 个，失败 {fail_count} 个\n\n{detail}\n\n"
                f"⚠️ 请重新打开终端/编辑器让变更生效。\n"
                f"目标盘数据目录保留，如需删除请到『已配置工具的记录详情』中操作。")
            self._refresh_dev_env_table()

        rollback_worker.finished_sig.connect(_on_rollback_done)
        rollback_worker.start()

    # ========== 开发环境快照功能（仿 GitHub commit） ==========

