#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""白名单管理 Handler（从 main.py 抽出）

包含：
- manage_whitelist：白名单管理对话框（添加/删除/恢复默认/从拦截日志添加）
- _add_wl_row：向白名单表格添加一行
"""
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QInputDialog, QMessageBox, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QGroupBox, QLineEdit, QPushButton, QScrollArea,
)

from config import save_all
from ui_widgets import WideEditorDelegate, NoElideDelegate

log = logging.getLogger('CDriveRelocator')


class WhitelistHandler:
    """白名单管理相关方法 Handler"""

    def manage_whitelist(self):
        """白名单管理：带说明的表格，支持添加/删除/恢复默认/从拦截日志添加"""
        dlg = QDialog(self)
        dlg.setWindowTitle("白名单管理")
        dlg.resize(780, 720)
        layout = QVBoxLayout(dlg)

        # === 说明文字（可滚动，避免长文本挤压表格空间）===
        help_text = (
            "【白名单的作用】\n"
            "  当 C盘拦迁器 检测到某个系统级安装器（winget/choco/scoop/msiexec 等）\n"
            "  在安装软件时，会暂停它并弹窗问你。如果你把这个进程加入了白名单，\n"
            "  下次它再安装东西就不再拦你。\n\n"
            "【关键词匹配规则】\n"
            "  关键词在进程名中出现即放行，不区分大小写。\n"
            "  例：填 'winget' → 以后 winget 装任何包都不再问你\n"
            "      填 'msiexec' → 所有 msiexec 安装包都放行\n\n"
            "【两层白名单】\n"
            "  • 默认（灰色）：系统内置白名单，始终生效，可删（删了重启不恢复），可「恢复默认」还原\n"
            "  • 用户：你手动添加的，可删除/编辑说明\n\n"
            "【操作说明】\n"
            "  • 关键词列不可编辑（改了会让匹配失效），要改请删除后重新添加\n"
            "  • 用户白名单的说明列可双击编辑\n"
            "  • 添加按钮支持手动输入和一键添加常用安装器\n"
            "  • 从拦截日志添加：选之前被拦过的进程，直接按进程名加入白名单"
        )
        help_label = QLabel(help_text)
        help_label.setWordWrap(True)
        help_label.setStyleSheet("QLabel{color:#424242;padding:4px;background:#F5F5F5;border:1px solid #E0E0E0;border-radius:4px;}")
        scroll = QScrollArea()
        scroll.setWidget(help_label)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(140)  # 固定高度，超出滚动
        scroll.setFrameShape(QScrollArea.NoFrame)
        layout.addWidget(scroll)

        # === 默认白名单表格（系统内置，灰色只读，可删可恢复）===
        layout.addWidget(QLabel(
            "<b>系统默认白名单</b>（灰色：系统更新/杀毒/辅助进程，始终生效；可删，删后重启不恢复）"
        ))
        self._wl_default_table = QTableWidget(0, 2)
        self._wl_default_table.setHorizontalHeaderLabels(["关键词", "说明"])
        self._wl_default_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._wl_default_table.horizontalHeader().setMinimumSectionSize(100)
        self._wl_default_table.horizontalHeader().resizeSection(0, 200)
        self._wl_default_table.horizontalHeader().resizeSection(1, 400)
        self._wl_default_table.setTextElideMode(Qt.ElideNone)
        self._wl_default_table.setItemDelegate(NoElideDelegate(self._wl_default_table))
        self._wl_default_table.setWordWrap(False)
        self._wl_default_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._wl_default_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self._wl_default_table.setAlternatingRowColors(True)
        self._wl_default_table.setEditTriggers(QTableWidget.NoEditTriggers)  # 默认白名单全部只读
        layout.addWidget(self._wl_default_table)

        # === 用户白名单表格（用户自定义，可编辑）===
        layout.addWidget(QLabel(
            "<b>用户白名单</b>（你手动添加的，可删除/编辑说明）"
        ))
        self._wl_table = QTableWidget(0, 2)
        self._wl_table.setHorizontalHeaderLabels(["关键词", "说明"])
        self._wl_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._wl_table.horizontalHeader().setMinimumSectionSize(100)
        self._wl_table.horizontalHeader().resizeSection(0, 200)
        self._wl_table.horizontalHeader().resizeSection(1, 400)
        self._wl_table.setTextElideMode(Qt.ElideNone)
        self._wl_table.setItemDelegate(NoElideDelegate(self._wl_table))
        self._wl_table.setWordWrap(False)
        self._wl_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._wl_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self._wl_table.setAlternatingRowColors(True)
        # 关键词列只读，说明列可编辑
        self._wl_table.setEditTriggers(QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed)
        self._wl_table.setItemDelegateForColumn(1, WideEditorDelegate(self._wl_table))
        layout.addWidget(self._wl_table)

        # 加载默认白名单（排除用户已删除的）
        default_wl = self.monitor_worker.DEFAULT_WHITELIST if self.monitor_worker else []
        removed_default_kws = set(
            kw.lower() for kw in self.cfg.get("removed_default_whitelist", []) or []
        )
        for item in default_wl:
            if isinstance(item, dict):
                kw = item.get("keyword", "")
                desc = item.get("desc", "")
            else:
                kw = str(item)
                desc = ""
            if kw.lower() in removed_default_kws:
                continue
            self._add_wl_row(kw, desc, source="default")
        # 加载用户白名单
        user_wl = self.cfg.get("whitelist", []) or []
        for item in user_wl:
            if isinstance(item, dict):
                kw = item.get("keyword", "")
                desc = item.get("desc", "")
            else:
                kw = str(item)
                desc = ""
            # 跳过与默认白名单重复的关键词
            if any(d.get("keyword", "").lower() == kw.lower() for d in default_wl if isinstance(d, dict)):
                continue
            self._add_wl_row(kw, desc, source="user")
        # 按钮区
        btn_row = QHBoxLayout()
        btn_add = QPushButton("添加")
        btn_add_from_log = QPushButton("从拦截日志添加")
        btn_del = QPushButton("删除选中")
        btn_reset = QPushButton("恢复默认")
        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_add_from_log)
        btn_row.addWidget(btn_del)
        btn_row.addWidget(btn_reset)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        # 确定/取消
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        layout.addWidget(bb)

        def do_add():
            """添加白名单 - 支持 手动输入 / 快捷常用软件 两种方式"""
            add_dlg = QDialog(dlg)
            add_dlg.setWindowTitle("添加白名单")
            add_dlg.resize(450, 380)
            add_layout = QVBoxLayout(add_dlg)

            # 说明
            add_layout.addWidget(QLabel(
                "关键词匹配规则：关键词在进程名中出现即放行，不区分大小写\n"
                "例：填 'winget' → 以后 winget 装任何包都不再问你\n"
                "    填 'wechat' → 进程名含 wechat 的都放行"
            ))

            # ===== 方式1：手动输入 =====
            grp_manual = QGroupBox("方式1：手动输入关键词")
            grp_manual_layout = QVBoxLayout(grp_manual)
            grp_manual_layout.addWidget(QLabel("关键词:"))
            kw_input = QLineEdit()
            kw_input.setPlaceholderText("如: winget, choco, msiexec, wechat")
            grp_manual_layout.addWidget(kw_input)
            grp_manual_layout.addWidget(QLabel("说明（可选）:"))
            desc_input = QLineEdit()
            desc_input.setPlaceholderText("如: 微信更新服务 / winget 包管理器放行")
            grp_manual_layout.addWidget(desc_input)
            add_layout.addWidget(grp_manual)

            # ===== 方式2：快捷常用软件 =====
            grp_quick = QGroupBox("方式2：一键添加常用软件（点击直接加入白名单）")
            grp_quick_layout = QVBoxLayout(grp_quick)
            quick_grid_layout = QHBoxLayout()

            # 常用软件列表（关键词, 说明）—— 系统级安装器 + 常见应用安装器
            # 开发工具（node/python/git 等）不在系统级安装器拦截范围，无需放行
            quick_items = [
                ("winget", "winget包管理器"), ("choco", "Chocolatey包管理器"),
                ("scoop", "Scoop包管理器"), ("msiexec", "Windows Installer"),
                ("wechat", "微信"), ("qq", "QQ"),
                ("wechatupdate", "微信更新"), ("chrome", "Chrome浏览器"),
                ("firefox", "Firefox浏览器"), ("steam", "Steam"),
            ]
            left_col = QVBoxLayout()
            right_col = QVBoxLayout()
            for i, (kw, desc) in enumerate(quick_items):
                btn = QPushButton(f"{desc} ({kw})")
                btn.setToolTip(f"点击添加关键词: {kw}")
                def _add_quick(_checked, k=kw, d=desc):
                    # 检查重复（同时检查默认和用户两个表格）
                    for table in (self._wl_default_table, self._wl_table):
                        for j in range(table.rowCount()):
                            if table.item(j, 0).text().lower() == k.lower():
                                QMessageBox.information(dlg, "提示", f"'{k}' 已在白名单中")
                                return
                    self._add_wl_row(k, d, source="user")
                    QMessageBox.information(dlg, "已添加", f"已添加: {d} ({k})")
                btn.clicked.connect(_add_quick)
                if i % 2 == 0:
                    left_col.addWidget(btn)
                else:
                    right_col.addWidget(btn)
            quick_grid_layout.addLayout(left_col)
            quick_grid_layout.addLayout(right_col)
            grp_quick_layout.addLayout(quick_grid_layout)
            add_layout.addWidget(grp_quick)

            # 确定/取消（只对方式1生效，方式2已直接添加）
            bb2 = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            add_layout.addWidget(bb2)
            bb2.accepted.connect(add_dlg.accept)
            bb2.rejected.connect(add_dlg.reject)
            if add_dlg.exec() == QDialog.Accepted:
                kw = kw_input.text().strip().lower()
                desc = desc_input.text().strip()
                if not kw:
                    return
                # 检查重复（同时检查默认和用户两个表格）
                for table in (self._wl_default_table, self._wl_table):
                    for i in range(table.rowCount()):
                        if table.item(i, 0).text().lower() == kw.lower():
                            QMessageBox.information(dlg, "提示", f"关键词 '{kw}' 已存在")
                            return
                self._add_wl_row(kw, desc, source="user")

        def do_add_from_log():
            """从拦截日志中选择进程添加到白名单 - 直接按进程名作为关键词
            数据来源：进程检测(_kill_installer) 和 文件系统监控(_on_dir_created)
            （脚本拦截已回退，日志里只剩系统级安装器记录，统一按进程名放行）
            """
            blocked = self.cfg.get("blocked_processes", [])
            if not blocked:
                QMessageBox.information(dlg, "提示", "暂无检测记录。\n\n当检测到系统级安装器或新目录创建时会自动记录，届时可从此处选择添加到白名单。")
                return
            # 获取当前白名单关键词集合（去重用，同时检查默认和用户两个表格）
            existing_kws = set()
            for table in (self._wl_default_table, self._wl_table):
                for i in range(table.rowCount()):
                    existing_kws.add(table.item(i, 0).text().lower())

            sel_dlg = QDialog(dlg)
            sel_dlg.setWindowTitle("选择要放行的进程/目录")
            sel_dlg.resize(850, 560)
            sel_layout = QVBoxLayout(sel_dlg)
            sel_layout.addWidget(QLabel(
                "以下是曾被 C盘拦迁器 检测到安装行为的进程或目录（已加入白名单的不再显示）。\n"
                "选中要放行的条目后点「确定」，将按进程名自动加入白名单。\n"
                "提示：表格列宽可以拖动调整，命令行那一列可以拉宽看完整内容。"
            ))

            # 用表格代替列表，列宽可拖动调整
            tbl = QTableWidget()
            tbl.setSelectionMode(QTableWidget.ExtendedSelection)
            tbl.setSelectionBehavior(QTableWidget.SelectRows)  # 整行选中
            tbl.setColumnCount(4)
            tbl.setHorizontalHeaderLabels(["进程名", "命令行（实际输入的命令）", "拦截时间", "类型"])
            tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)  # 列宽可拖动
            tbl.horizontalHeader().setStretchLastSection(False)
            # 初始列宽
            tbl.setColumnWidth(0, 120)
            tbl.setColumnWidth(1, 500)
            tbl.setColumnWidth(2, 150)
            tbl.setColumnWidth(3, 100)

            # 记录每条对应的完整信息：(name, exe, cmdline, time, source)
            shown_items = []
            # 类型说明字典（大白话解释 source 字段）
            source_desc = {
                "process_detected": "进程检测",
                "script_intercept": "安装器拦截",
                "installer_detected_no_intercept": "安装器检测",
                "script_detected_no_intercept": "安装器检测",
                "dir_created": "新目录创建",
            }
            for b in reversed(blocked[-100:]):  # 最近的在前
                name = b.get("name", "")
                exe = b.get("exe", "")
                cmdline = b.get("cmdline", "")
                btime = b.get("time", "")
                source = b.get("source", "process_detected")
                # 去重：按 (name, cmdline) 组合去重
                dedup_key = (name.lower(), cmdline.lower()[:80])
                if any((s[0].lower(), s[2].lower()[:80]) == dedup_key for s in shown_items):
                    continue
                shown_items.append((name, exe, cmdline, btime, source))
                row = tbl.rowCount()
                tbl.insertRow(row)
                tbl.setItem(row, 0, QTableWidgetItem(name))
                # 命令行：空的话显示"（无命令行，文件系统检测）"
                cmd_display = cmdline if cmdline else "（无命令行 - 文件系统检测到新目录）"
                tbl.setItem(row, 1, QTableWidgetItem(cmd_display))
                tbl.setItem(row, 2, QTableWidgetItem(btime))
                tbl.setItem(row, 3, QTableWidgetItem(source_desc.get(source, source)))
            tbl.verticalHeader().setVisible(False)
            sel_layout.addWidget(tbl)
            if not shown_items:
                sel_layout.addWidget(QLabel("所有被拦截的进程都已在白名单中。"))
            bb3 = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            sel_layout.addWidget(bb3)
            bb3.accepted.connect(sel_dlg.accept)
            bb3.rejected.connect(sel_dlg.reject)
            if sel_dlg.exec() != QDialog.Accepted:
                return

            # 收集选中的条目（兼容整行选中和单元格选中两种模式）
            selected_entries = []
            selected_rows = set()
            sm = tbl.selectionModel()
            if sm:
                for idx in sm.selectedRows():
                    selected_rows.add(idx.row())
                # 兜底：如果 selectedRows() 为空，用 selectedIndexes() 提取行号
                if not selected_rows:
                    for idx in sm.selectedIndexes():
                        selected_rows.add(idx.row())
            for row in sorted(selected_rows):
                if 0 <= row < len(shown_items):
                    selected_entries.append(shown_items[row])

            if not selected_entries:
                QMessageBox.information(dlg, "提示", "请先在表格中选择至少一行再点确定")
                return

            # 直接按进程名加入白名单（系统级安装器统一按进程名放行）
            added_count = 0
            for name, exe, cmdline, btime, source in selected_entries:
                kw = name.lower()
                if not kw:
                    continue
                # 检查重复
                if kw in existing_kws:
                    continue
                existing_kws.add(kw)
                self._add_wl_row(kw, f"从拦截日志添加(按进程名)", source="user")
                added_count += 1
            if added_count:
                QMessageBox.information(dlg, "已添加",
                    f"已添加 {added_count} 条白名单记录（按进程名放行）。\n"
                    f"点「确定」保存白名单后生效。")

        def do_del():
            """删除选中的白名单行（两个表格都支持删，默认白名单删后可「恢复默认」还原）"""
            for table in (self._wl_default_table, self._wl_table):
                rows = sorted(set(idx.row() for idx in table.selectedIndexes()), reverse=True)
                for r in rows:
                    table.removeRow(r)
        def do_reset():
            """恢复默认：只重新加载默认白名单，不影响用户白名单"""
            self._wl_default_table.setRowCount(0)
            default_wl = self.monitor_worker.DEFAULT_WHITELIST if self.monitor_worker else []
            for item in default_wl:
                if isinstance(item, dict):
                    self._add_wl_row(item.get("keyword", ""), item.get("desc", ""), source="default")
            # 清空 removed_default_whitelist，让所有默认白名单恢复生效
            self.cfg["removed_default_whitelist"] = []
            # 注意：不清空 _wl_table，保留用户白名单
            QMessageBox.information(dlg, "已恢复默认",
                "系统默认白名单已全部恢复。\n用户白名单保持不变。")
        def do_ok():
            """保存白名单：从用户表格收集用户白名单 + 从默认表格计算被删的默认关键词"""
            new_wl = []
            # 用户表格：收集用户白名单
            for i in range(self._wl_table.rowCount()):
                kw_item = self._wl_table.item(i, 0)
                desc_item = self._wl_table.item(i, 1)
                kw = kw_item.text() if kw_item else ""
                desc = desc_item.text() if desc_item else ""
                if kw:
                    new_wl.append({"keyword": kw, "desc": desc})
            # 默认表格：收集仍存在的默认白名单关键词
            remaining_default_kws = set()
            for i in range(self._wl_default_table.rowCount()):
                kw_item = self._wl_default_table.item(i, 0)
                if kw_item:
                    remaining_default_kws.add(kw_item.text().lower())
            # 计算被用户删除的默认白名单关键词
            full_default_kws = set(
                d.get("keyword", "").lower()
                for d in (self.monitor_worker.DEFAULT_WHITELIST if self.monitor_worker else [])
                if isinstance(d, dict)
            )
            removed_default_kws = list(full_default_kws - remaining_default_kws)
            # 弹出确认对话框
            reply = QMessageBox.question(dlg, "确认保存白名单",
                f"即将保存 {len(new_wl)} 条用户白名单。\n"
                f"系统默认白名单 {len(full_default_kws)} 条，"
                f"当前生效 {len(remaining_default_kws)} 条"
                f"{'（已删除 ' + str(len(removed_default_kws)) + ' 条）' if removed_default_kws else ''}\n\n"
                f"确定保存？",
                QMessageBox.Yes | QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
            self.cfg["whitelist"] = new_wl
            self.cfg["removed_default_whitelist"] = removed_default_kws
            save_all(self.cfg)
            if self.monitor_worker:
                # 运行时合并：排除被删的默认白名单 + 用户白名单
                removed_set = set(kw.lower() for kw in removed_default_kws)
                effective_default = [
                    w for w in self.monitor_worker.DEFAULT_WHITELIST
                    if (w.get("keyword", "").lower() not in removed_set)
                ]
                self.monitor_worker.whitelist = list(effective_default) + list(new_wl)
            log.info(f"白名单已更新: 用户 {len(new_wl)} 条 + 默认 {len(remaining_default_kws)}/{len(full_default_kws)} 条")
            self.status_label.setText(f"白名单已更新: 用户 {len(new_wl)} 条 + 默认 {len(remaining_default_kws)}/{len(full_default_kws)} 条")
            dlg.accept()

        btn_add.clicked.connect(do_add)
        btn_add_from_log.clicked.connect(do_add_from_log)
        btn_del.clicked.connect(do_del)
        btn_reset.clicked.connect(do_reset)
        bb.accepted.connect(do_ok)
        bb.rejected.connect(dlg.reject)
        dlg.exec()

    def _add_wl_row(self, kw, desc, source="user"):
        """向白名单表格添加一行

        :param source: "default"=加到默认白名单表格（灰色只读） / "user"=加到用户白名单表格（可编辑）
        """
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QColor, QBrush
        # 根据来源选择目标表格
        if source == "default":
            table = self._wl_default_table
            text_color = QColor("#616161")  # 灰色
        else:
            table = self._wl_table
            text_color = QColor("#263238")  # 深色（正常）
        row = table.rowCount()
        table.insertRow(row)
        # 关键词列（只读）
        kw_item = QTableWidgetItem(kw)
        kw_item.setToolTip(kw + "（关键词不可编辑，如需修改请删除后重新添加）")
        kw_item.setFlags(kw_item.flags() & ~_Qt.ItemIsEditable)
        kw_item.setForeground(QBrush(text_color))
        table.setItem(row, 0, kw_item)
        # 说明列
        desc_item = QTableWidgetItem(desc)
        desc_item.setToolTip(desc if desc else kw)
        desc_item.setForeground(QBrush(text_color))
        if source == "default":
            # 默认白名单说明列只读
            desc_item.setFlags(desc_item.flags() & ~_Qt.ItemIsEditable)
        table.setItem(row, 1, desc_item)
