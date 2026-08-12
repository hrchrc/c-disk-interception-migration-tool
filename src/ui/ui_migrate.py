#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""迁移/还原/修复符号链接/右键菜单 Handler（从 main.py 抽出）

包含 27 个方法：
- _start_async_recover: 异步恢复（含 _RecoverWorker 嵌套类）
- _show_recovery_results: 显示恢复结果
- _show_user_decision_dialog: 用户决策对话框
- _refresh_all_tables_safe: 安全刷新所有表格
- _show_pending_decisions_dialog: 待决策对话框
- _update_pending_decisions_button: 更新待决策按钮
- migrate_selected: 迁移选中项（含 _MigrateWorker/_BatchMigrateWorker 嵌套类）
- _auto_config_dev_env_after_migrate: 迁移后自动配置开发环境
- _auto_unconfig_dev_env_after_restore: 还原后自动撤销开发环境配置
- _move_row_to_scan: 移动行到待迁移表
- _move_rows_to_scan: 批量移动行到待迁移表
- _move_row_to_migrated: 移动行到已迁移表
- _migrated_context_menu: 已迁移表右键菜单
- _fix_selected_links: 修复选中链接
- _update_migrated_rows_status: 更新已迁移行状态
- _relink_single: 重建单个链接
- _delete_link_single: 删除单个链接
- _migrate_rows: 批量迁移（含 _BatchMigrateWorker 嵌套类）
- _move_rows_to_migrated: 批量移动到已迁移表
- _migrate_rows_to_custom: 迁移到自定义路径（含 _BatchMigrateCustomWorker 嵌套类）
- restore_selected: 还原选中项（含 _RestoreWorker 嵌套类）
- delete_link: 删除链接
- rebuild_link: 重建链接
- open_dir: 打开目录
- _open_path: 打开路径辅助
- browse_dir: 浏览目录
- _browse_migrate_dir: 浏览迁移目录（含 _BrowseMigrateWorker 嵌套类）

这些方法原属 MainWindow，抽取为 Handler 以降低 main.py 体量。
方法内通过 self 访问 MainWindow 的属性和其他方法，运行时由 MainWindow 提供。

依赖的 MainWindow 属性：
- self.cfg                  配置字典
- self.migrator             Migrator 实例
- self.table_scan           待迁移表格控件
- self.table_migrated       已迁移表格控件
- self._refresh_migrated_only()
- self._light_refresh_scan_table()
"""
import os
import sys
import json
import shutil
import subprocess
import logging
import ctypes
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QThread, QTimer, QUrl
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QCheckBox, QLabel, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QMessageBox, QMenu, QFileDialog, QInputDialog,
    QDialog, QDialogButtonBox, QProgressBar, QFrame,
    QProgressDialog,
)
from PySide6.QtGui import QColor, QAction, QDesktopServices

# 根据方法实际用到的 import 补充以下引用（按需）：
from config import (
    log_link_operation, log_error_with_reason, save_all, save_config,
    save_state, load_state, KNOWN_SOFTWARE_DIRS, COMBO_MAP,
)
from utils import (
    is_symlink, get_symlink_target, get_dir_size_fast,
    get_exe_version_info, _read_lnk_target,
)
from migrator import Migrator
from ui_widgets import (
    NumericTableWidgetItem, WideEditorDelegate,
    _format_size, _apply_size_item_color,
)

log = logging.getLogger('CDriveRelocator')


class MigrateHandler:
    """迁移/还原/修复符号链接/右键菜单 Handler"""

    def _start_async_recover(self, recover_type="both", on_done_callback=None):
        """异步执行事务恢复（避免复制引擎阻塞 UI）

        :param recover_type: "migration" / "restore" / "both"
        :param on_done_callback: 完成后的回调 fn(results)，None 则默认弹 _show_recovery_results

        启动前的用户提示：
        - 检测到 pending 列表非空时，弹一个明显的提示框告知用户
          "检测到上次有未完成的事务，正在帮你继续"
        - 同时把状态栏「待处理事务」按钮切到「处理中」状态（黄色禁用）
        """
        # 如果已有恢复线程在跑，不重复启动
        if self._recover_worker and self._recover_worker.isRunning():
            log.info("已有恢复线程在运行，跳过本次启动恢复")
            return

        # ===== 启动前主动提示用户（如果有未完成事务）=====
        try:
            pending_migrations = self.cfg.get("pending_migrations", []) or []
            pending_restores = self.cfg.get("pending_restores", []) or []
            total_pending = len(pending_migrations) + len(pending_restores)
            if total_pending > 0:
                # 收集每个事务的简要信息（src + stage），让用户知道在恢复什么
                detail_lines = []
                for p in pending_migrations[:5]:
                    p_src = os.path.basename(p.get("src", "")) or p.get("src", "")
                    p_stage = p.get("stage", "")
                    detail_lines.append(f"  • 迁移: {p_src}（阶段: {p_stage}）")
                for p in pending_restores[:5]:
                    p_src = os.path.basename(p.get("src", "")) or p.get("src", "")
                    p_stage = p.get("stage", "")
                    detail_lines.append(f"  • 还原: {p_src}（阶段: {p_stage}）")
                detail_text = "\n".join(detail_lines)
                if total_pending > 5:
                    detail_text += f"\n  ... 等共 {total_pending} 个事务"
                QMessageBox.information(
                    self, "检测到上次有未完成的事务",
                    f"检测到上次程序退出时有 {total_pending} 个未完成的迁移/还原事务，\n"
                    f"现在自动帮你继续完成（无需手动操作）。\n\n"
                    f"{detail_text}\n\n"
                    f"恢复过程在后台异步执行，可继续使用其他功能，进度显示在状态栏和监控日志。"
                )
                log.info(f"启动恢复前提示用户: 共 {total_pending} 个未完成事务")
        except Exception as e:
            log.error(f"启动恢复前提示异常（不影响恢复流程）: {e}")

        # ===== 把待处理事务按钮切到「处理中」状态 =====
        self._set_pending_button_processing(total_pending if total_pending > 0 else None)

        from PySide6.QtCore import QThread, Signal

        class _RecoverWorker(QThread):
            done_signal = Signal(list)
            log_signal = Signal(str, str)
            def __init__(self, migrator, rtype):
                super().__init__()
                self.migrator = migrator
                self.rtype = rtype
            def run(self):
                # 顶层 try/except 防止未捕获异常导致程序闪退
                try:
                    def _progress(event_type, message):
                        try:
                            self.log_signal.emit(event_type, message)
                        except Exception as e:
                            log.debug("忽略异常: %s", e)
                    self.migrator.log_callback = _progress
                    results = []
                    if self.rtype in ("migration", "both"):
                        results.extend(self.migrator.recover_pending_migrations())
                    if self.rtype in ("restore", "both"):
                        results.extend(self.migrator.recover_pending_restores())
                    self.done_signal.emit(results)
                except Exception as e:
                    log.error(f"异步恢复异常: {e}")
                    try:
                        self.done_signal.emit([("", "error", f"恢复异常: {e}")])
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                finally:
                    try:
                        self.migrator.log_callback = None
                    except Exception as e:
                        log.debug("忽略异常: %s", e)

        worker = _RecoverWorker(self.migrator, recover_type)
        self._recover_worker = worker  # 保存引用避免 GC

        def _on_log(event_type, message):
            try:
                self.status_label.setText(message[:200])
                self._log_monitor(event_type, message)
            except Exception as e:
                log.debug("忽略异常: %s", e)

        def _on_done(results):
            try:
                if on_done_callback:
                    on_done_callback(results)
                else:
                    if results:
                        self._show_recovery_results(results)
                self._update_pending_decisions_button()
            except Exception as e:
                log.error(f"恢复完成回调异常: {e}")
            finally:
                # 确保 worker 完全退出，避免竞态
                try:
                    worker.wait(500)
                except Exception as e:
                    log.debug("忽略异常: %s", e)

        worker.log_signal.connect(_on_log, Qt.QueuedConnection)
        worker.done_signal.connect(_on_done, Qt.QueuedConnection)
        worker.start()
        log.info(f"启动异步恢复线程 (type={recover_type})")

    def _set_pending_button_processing(self, total_count=None):
        """把状态栏「待处理事务」按钮切到「处理中」状态（黄色禁用）

        恢复线程跑的时候调用，让用户看到按钮颜色变化知道在处理。
        完成后由 _update_pending_decisions_button 恢复为正常状态（启用橙色或禁用灰色）。

        :param total_count: 未完成事务总数（用于文案），None 时只显示"处理中"
        """
        try:
            if not hasattr(self, 'btn_pending_decisions'):
                return
            if total_count and total_count > 0:
                self.btn_pending_decisions.setText(f"⏳ 处理中... ({total_count})")
            else:
                self.btn_pending_decisions.setText("⏳ 处理中...")
            self.btn_pending_decisions.setEnabled(False)
            self.btn_pending_decisions.setToolTip("正在恢复上次未完成的事务，请稍候...")
            # 黄色背景表示处理中（区别于橙色的待决策、灰色的无事务）
            self.btn_pending_decisions.setStyleSheet(
                "QPushButton { color: #FFFFFF; background-color: #F9A825; "
                "border: none; padding: 2px 10px; border-radius: 8px; font-weight: bold; }"
                "QPushButton:disabled { color: #FFFFFF; background-color: #FBC02D; }")
        except Exception as e:
            log.error(f"设置处理中按钮状态失败: {e}")

    def _show_recovery_results(self, results):
        """启动时弹出未完成事务的恢复结果

        对 user_decision_required 的事务单独弹出决策对话框，
        让用户选择：重试 / 放弃 / 暂不处理。
        """
        from PySide6.QtWidgets import QMessageBox
        if not results:
            return
        success = [r for r in results if r[1] in ("completed", "cleaned", "rollback", "completed_warn")]
        # user_decision_required 单独处理（累计失败 2 次，需要用户决策）
        user_decisions = [r for r in results if r[1] == "user_decision_required"]
        failed = [r for r in results
                  if r[1] not in ("completed", "cleaned", "rollback", "completed_warn",
                                   "user_decision_required")]

        # 先显示普通结果（成功 + 一般失败）
        normal = success + failed
        if normal:
            detail = "\n".join(
                f"{'✓' if r[1] in ('completed','cleaned','rollback','completed_warn') else '✗'} "
                f"{os.path.basename(r[0])}: {r[2]}"
                for r in normal[:20])
            title = f"启动恢复完成（成功 {len(success)}，失败 {len(failed)}）"
            msg = (f"检测到上次有 {len(normal)} 个未完成的迁移/还原事务，已自动处理：\n\n"
                   f"{detail}\n\n")
            if failed:
                msg += (f"⚠️ 有 {len(failed)} 个事务恢复失败，请关闭占用程序的软件后重启程序重试，\n"
                        f"   或以管理员身份运行本程序。")
            else:
                msg += "所有事务已恢复完成，无需额外操作。"
            QMessageBox.information(self, title, msg)

        # 再处理需要用户决策的事务（逐个询问）
        if user_decisions:
            for r in user_decisions:
                self._show_user_decision_dialog(r[0], r[2])

        self.on_monitor_log("init",
            f"启动恢复: 成功 {len(success)} 个，失败 {len(failed)} 个，"
            f"待用户决策 {len(user_decisions)} 个")
        # 更新状态栏「待处理事务」按钮可见性
        self._update_pending_decisions_button()

    def _show_user_decision_dialog(self, src, result_msg):
        """对累计失败 2 次的事务弹出决策对话框

        让用户选择：重试迁移（清零 fail_count）/ 放弃迁移 / 暂不处理
        并根据 last_error 给出建议。
        """
        from PySide6.QtWidgets import QMessageBox
        # 从 config.json 找到对应的 pending 事务详情
        is_restore = False
        pending_entry = None
        for p in self.cfg.get("pending_migrations", []):
            if p.get("src") == src:
                pending_entry = p
                break
        if pending_entry is None:
            for p in self.cfg.get("pending_restores", []):
                if p.get("src") == src:
                    pending_entry = p
                    is_restore = True
                    break
        if pending_entry is None:
            # 事务已不在 pending 中（可能被其他流程清理），仅提示
            QMessageBox.information(self, "事务已处理",
                f"事务 {src} 已不在 pending 列表中，可能已被处理。")
            return

        stage = pending_entry.get("stage", "")
        fail_count = pending_entry.get("fail_count", 0)
        last_error = pending_entry.get("last_error", "未知原因")
        last_fail_time = pending_entry.get("last_fail_time", "")
        dst = pending_entry.get("dst", "")
        action = "还原" if is_restore else "迁移"

        # 获取建议
        suggestion = self.migrator.get_failure_suggestion(last_error, stage, is_restore=is_restore)

        title = f"⚠️ {action}事务需要您决策 - 累计失败 {fail_count} 次"
        msg = (
            f"📋 事务信息\n"
            f"────────────────────────────────\n"
            f"类型: {action}\n"
            f"源路径 (C盘): {src}\n"
            f"目标路径 (D盘): {dst}\n"
            f"失败阶段: {stage}\n"
            f"累计失败次数: {fail_count}\n"
            f"上次失败时间: {last_fail_time}\n"
            f"上次错误: {last_error}\n\n"
            f"💡 处理建议\n"
            f"────────────────────────────────\n"
            f"{suggestion}\n\n"
            f"────────────────────────────────\n"
            f"请选择操作：\n"
            f"  • 是 = 重试{action}（清零失败计数，立即恢复）\n"
            f"  • 否 = 放弃{action}（删除 pending 记录，D 盘数据保留由您自行处理）\n"
            f"  • 取消 = 暂不处理（保留 pending 记录，下次启动不再自动尝试）"
        )

        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(msg)
        box.setIcon(QMessageBox.Warning)
        yes_btn = box.addButton(f"重试{action}", QMessageBox.YesRole)
        no_btn = box.addButton(f"放弃{action}", QMessageBox.NoRole)
        cancel_btn = box.addButton("暂不处理", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()

        if clicked is yes_btn:
            # 重试：清零 fail_count 并异步调用恢复（避免 复制引擎阻塞 UI）
            ok, msg2 = self.migrator.manual_retry_pending(src, is_restore=is_restore)
            if ok:
                self.on_monitor_log("migrate",
                    f"🔁 用户选择重试 {action} 事务: {os.path.basename(src)}")
                self.status_label.setText(f"正在重试{action}: {os.path.basename(src)}...")
                # 异步执行恢复，完成后弹窗显示结果
                rtype = "restore" if is_restore else "migration"

                def _on_retry_done(results):
                    try:
                        if results:
                            for r in results:
                                icon = "✓" if r[1] in ("completed", "cleaned", "rollback", "completed_warn") else "✗"
                                self.on_monitor_log("migrate",
                                    f"  {icon} 重试结果: {os.path.basename(r[0]) if r[0] else '?'} - {r[2]}")
                            detail = "\n".join(
                                f"{'✓' if r[1] in ('completed','cleaned','rollback','completed_warn') else '✗'} "
                                f"{os.path.basename(r[0]) if r[0] else '?'}: {r[2]}"
                                for r in results[:10])
                            QMessageBox.information(self, f"重试{action}结果", detail)
                        else:
                            QMessageBox.information(self, f"重试{action}结果", "无事务被处理")
                        # 刷新界面（在单独 try/except 中，防止刷新失败影响主流程）
                        try:
                            self._refresh_all_tables_safe()
                        except Exception as e:
                            log.error(f"重试后刷新界面失败: {e}")
                        self.status_label.setText(f"重试{action}完成")
                    except Exception as e:
                        log.error(f"重试结果回调异常: {e}")

                self._start_async_recover(recover_type=rtype, on_done_callback=_on_retry_done)
            else:
                QMessageBox.warning(self, f"重试{action}失败", msg2)
        elif clicked is no_btn:
            # 放弃：删除 pending 记录
            ok, msg2 = self.migrator.cancel_pending(src, is_restore=is_restore)
            if ok:
                self.on_monitor_log("migrate",
                    f"🗑️ 用户放弃 {action} 事务: {os.path.basename(src)}")
                QMessageBox.information(self, f"已放弃{action}",
                    f"{msg2}\n\nD 盘数据保留在: {dst}\n您可以自行处理（手动创建链接或删除数据）。")
            else:
                QMessageBox.warning(self, f"放弃{action}失败", msg2)
        else:
            # 暂不处理
            self.on_monitor_log("migrate",
                f"⏸️ 用户选择暂不处理 {action} 事务: {os.path.basename(src)}")
            QMessageBox.information(self, "已保留待处理",
                f"事务已保留，下次启动程序不再自动尝试。\n"
                f"如需处理，请点击状态栏左侧的「待处理事务」按钮查看。")

        # 决策完成后刷新按钮状态
        self._update_pending_decisions_button()

    def _refresh_all_tables_safe(self):
        """刷新所有表格（待迁移/已迁移/开发环境），用于事务操作后的联动更新

        注意：不调用 self.refresh()（全盘扫描），因为事务操作（重试/放弃/改迁/还原）
        不需要重新扫描待迁移表，只需移除已迁移走的行即可。全盘扫描会触发 MFT 索引
        加载（首次约 10-30 秒），与事务操作无关，会让用户误以为重试触发了 MFT 加载。
        """
        try:
            self._refresh_migrated_only()
        except Exception as e:
            log.error(f"刷新已迁移表失败: {e}")
        try:
            self._light_refresh_scan_table()
        except Exception as e:
            log.error(f"轻量刷新待迁移表失败: {e}")
        try:
            self._refresh_dev_env_table()
        except Exception as e:
            log.error(f"刷新开发环境表失败: {e}")

    def _show_pending_decisions_dialog(self):
        """手动触发：显示所有 fail_count >= 2 的 pending 事务，让用户决策

        通过状态栏的「待处理事务」按钮触发，启动后随时可查看。
        """
        try:
            items = self.migrator.get_pending_user_decisions()
            if not items:
                QMessageBox.information(self, "无待处理事务",
                    "当前没有需要您决策的未完成事务。")
                return
            # 列表选择对话框
            from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel,
                QListWidget, QListWidgetItem, QHBoxLayout, QPushButton,
                QAbstractItemView)
            dlg = QDialog(self)
            dlg.setWindowTitle(f"待处理事务（{len(items)} 个）")
            dlg.resize(700, 450)
            layout = QVBoxLayout(dlg)
            layout.addWidget(QLabel(
                f"以下 {len(items)} 个事务已累计失败 2 次以上，"
                f"需要您决策：\n（选中后点击下方按钮操作）"))
            list_widget = QListWidget()
            list_widget.setSelectionMode(QAbstractItemView.SingleSelection)
            for item in items:
                action = "还原" if item["type"] == "restore" else "迁移"
                text = (f"[{action}] {os.path.basename(item['src'])}\n"
                        f"  路径: {item['src']}\n"
                        f"  阶段: {item['stage']} | 失败 {item['fail_count']} 次\n"
                        f"  上次错误: {item['last_error'][:100]}")
                lw_item = QListWidgetItem(text)
                lw_item.setData(Qt.UserRole, item)
                list_widget.addItem(lw_item)
            layout.addWidget(list_widget)

            # 详情按钮区
            btn_row = QHBoxLayout()
            btn_retry = QPushButton(f"🔁 重试选中")
            btn_cancel = QPushButton(f"🗑️ 放弃选中")
            btn_detail = QPushButton("📋 查看详情/建议")
            btn_close = QPushButton("关闭")
            btn_row.addWidget(btn_retry)
            btn_row.addWidget(btn_cancel)
            btn_row.addWidget(btn_detail)
            btn_row.addStretch()
            btn_row.addWidget(btn_close)
            layout.addLayout(btn_row)

            def get_selected():
                items_sel = list_widget.selectedItems()
                if not items_sel:
                    QMessageBox.warning(dlg, "未选择", "请先选择一个事务")
                    return None
                return items_sel[0].data(Qt.UserRole)

            def do_retry():
                item = get_selected()
                if not item:
                    return
                is_restore = (item["type"] == "restore")
                ok, msg = self.migrator.manual_retry_pending(item["src"], is_restore=is_restore)
                if not ok:
                    QMessageBox.warning(dlg, "重试失败", msg)
                    return
                self.on_monitor_log("migrate",
                    f"🔁 用户手动重试 {('还原' if is_restore else '迁移')} 事务: "
                    f"{os.path.basename(item['src'])}")
                action = "还原" if is_restore else "迁移"
                self.status_label.setText(f"正在重试{action}: {os.path.basename(item['src'])}...")
                rtype = "restore" if is_restore else "migration"
                # 关闭选择对话框，异步执行恢复，完成后弹结果
                dlg.accept()

                def _on_retry_done(results):
                    try:
                        if results:
                            detail = "\n".join(
                                f"{'✓' if r[1] in ('completed','cleaned','rollback','completed_warn') else '✗'} "
                                f"{os.path.basename(r[0]) if r[0] else '?'}: {r[2]}"
                                for r in results[:10])
                        else:
                            detail = "无事务被处理"
                        QMessageBox.information(self, f"重试{action}结果", detail)
                        try:
                            self._refresh_all_tables_safe()
                        except Exception as e:
                            log.error(f"重试后刷新界面失败: {e}")
                        self.status_label.setText(f"重试{action}完成")
                    except Exception as e:
                        log.error(f"手动重试结果回调异常: {e}")

                self._start_async_recover(recover_type=rtype, on_done_callback=_on_retry_done)

            def do_cancel():
                item = get_selected()
                if not item:
                    return
                is_restore = (item["type"] == "restore")
                action = "还原" if is_restore else "迁移"
                # 二次确认
                ret = QMessageBox.question(dlg, f"确认放弃{action}",
                    f"确定要放弃此{action}事务吗？\n\n"
                    f"路径: {item['src']}\n\n"
                    f"放弃后 pending 记录将被删除，D 盘数据保留由您自行处理。",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if ret != QMessageBox.Yes:
                    return
                ok, msg = self.migrator.cancel_pending(item["src"], is_restore=is_restore)
                if ok:
                    self.on_monitor_log("migrate",
                        f"🗑️ 用户放弃 {action} 事务: {os.path.basename(item['src'])}")
                    QMessageBox.information(dlg, f"已放弃{action}", msg)
                    # 刷新列表
                    dlg.accept()
                    try:
                        self._refresh_all_tables_safe()
                    except Exception as e:
                        log.error(f"放弃后刷新界面失败: {e}")
                else:
                    QMessageBox.warning(dlg, f"放弃{action}失败", msg)

            def do_detail():
                item = get_selected()
                if not item:
                    return
                is_restore = (item["type"] == "restore")
                suggestion = self.migrator.get_failure_suggestion(
                    item["last_error"], item["stage"], is_restore=is_restore)
                action = "还原" if is_restore else "迁移"
                QMessageBox.information(dlg, f"{action}事务详情",
                    f"📋 事务信息\n{'─'*40}\n"
                    f"类型: {action}\n"
                    f"源路径 (C盘): {item['src']}\n"
                    f"目标路径 (D盘): {item['dst']}\n"
                    f"失败阶段: {item['stage']}\n"
                    f"累计失败次数: {item['fail_count']}\n"
                    f"上次失败时间: {item['last_fail_time']}\n"
                    f"上次错误: {item['last_error']}\n\n"
                    f"💡 处理建议\n{'─'*40}\n"
                    f"{suggestion}")

            btn_retry.clicked.connect(do_retry)
            btn_cancel.clicked.connect(do_cancel)
            btn_detail.clicked.connect(do_detail)
            btn_close.clicked.connect(dlg.reject)
            dlg.exec()
        except Exception as e:
            log.error(f"显示待处理事务对话框失败: {e}")
            QMessageBox.critical(self, "错误", f"打开待处理事务列表失败: {e}")

    def _update_pending_decisions_button(self):
        """更新状态栏「待处理事务」按钮的状态和文案

        三种状态：
        - 处理中（黄色禁用）：恢复线程跑时由 _set_pending_button_processing 设置，
          本方法不主动覆盖；恢复完成后由调用方先调用本方法切回正常状态
        - 待决策（橙色启用）：有 fail_count >= 2 的事务
        - 无事务（灰色禁用）：没有待决策事务
        按钮始终可见，让用户知道入口存在。
        """
        try:
            if not hasattr(self, 'btn_pending_decisions'):
                return
            # 如果恢复线程正在运行，保持「处理中」状态不被覆盖
            if (hasattr(self, '_recover_worker') and self._recover_worker
                    and self._recover_worker.isRunning()):
                return
            count = len(self.migrator.get_pending_user_decisions())
            if count > 0:
                self.btn_pending_decisions.setText(f"⚠️ 待处理事务 ({count})")
                self.btn_pending_decisions.setEnabled(True)
                self.btn_pending_decisions.setToolTip(
                    f"有 {count} 个事务累计失败 2 次以上，点击查看并处理")
                # 橙色（恢复处理中可能改过样式，这里显式恢复）
                self.btn_pending_decisions.setStyleSheet(
                    "QPushButton { color: #FFFFFF; background-color: #FF6F00; "
                    "border: none; padding: 2px 10px; border-radius: 8px; font-weight: bold; }"
                    "QPushButton:hover { background-color: #FF8F00; }")
            else:
                self.btn_pending_decisions.setText("待处理事务")
                self.btn_pending_decisions.setEnabled(False)
                self.btn_pending_decisions.setToolTip("当前没有需要处理的事务")
                # 灰色（恢复处理中可能改过样式，这里显式恢复）
                self.btn_pending_decisions.setStyleSheet(
                    "QPushButton { color: #757575; background-color: #E0E0E0; "
                    "border: none; padding: 2px 10px; border-radius: 8px; font-weight: bold; }"
                    "QPushButton:enabled { color: #fff; background-color: #FF6F00; }"
                    "QPushButton:enabled:hover { background-color: #FF8F00; }")
        except Exception as e:
            log.error(f"更新待处理事务按钮失败: {e}")

    def migrate_selected(self):
        row = self.table_scan.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先在'待迁移'标签页选择要迁移的目录")
            return
        # 防重入：如果上一个迁移线程还在运行，拒绝启动新的
        # 避免两个 复制进程同时操作同一目录导致数据损坏
        if hasattr(self, '_migrate_worker') and self._migrate_worker and self._migrate_worker.isRunning():
            QMessageBox.warning(self, "请稍候", "上一个迁移任务还在执行中，请等待完成后再试。")
            return
        src_path = self.table_scan.item(row, 0).text()
        # 系统文件警告
        from utils import is_system_path
        if is_system_path(src_path):
            QMessageBox.critical(self, "⚠ 系统文件警告",
                f"检测到系统重要文件/目录：\n\n{src_path}\n\n"
                f"迁移此目录可能导致Windows系统异常或无法启动！\n"
                f"如非必要，请勿迁移。\n\n"
                f"如确实需要迁移，请确保已创建系统还原点。")
            if QMessageBox.question(self, "确认强行迁移",
                f"仍要迁移此系统目录吗？\n\n{src_path}\n\n风险自负！",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
        if QMessageBox.question(self, "确认迁移",
            f"确定要将以下目录迁移到G盘吗？\n\n{src_path}\n\n"
            f"将执行：数据同步 -> 删除C盘原目录 -> 创建符号链接",
            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.status_label.setText(f"正在迁移 {src_path}...")
        self._log_monitor("install", f"开始迁移: {src_path}")

        # 后台线程执行迁移，避免 UI 卡死（用户可继续操作其他功能）
        class _MigrateWorker(QThread):
            done_signal = Signal(bool, str, str, int)  # (ok, msg, src_path, row)
            log_signal = Signal(str, str)  # (event_type, message) 复制进度
            def __init__(self, migrator, path, row):
                super().__init__()
                self.migrator = migrator
                self.path = path
                self.row = row
            def run(self):
                # 设置 log_callback：复制进度实时输出到状态栏 + 监控日志
                def _migrate_progress(event_type, message):
                    try:
                        self.log_signal.emit(event_type, message)
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                self.migrator.log_callback = _migrate_progress
                try:
                    ok, msg = self.migrator.migrate(self.path)
                    self.done_signal.emit(ok, msg, self.path, self.row)
                except Exception as e:
                    self.done_signal.emit(False, str(e), self.path, self.row)
                finally:
                    self.migrator.log_callback = None

        self._migrate_worker = _MigrateWorker(self.migrator, src_path, row)
        # 复制进度信号：更新状态栏 + 监控日志
        def _on_migrate_log(event_type, message):
            try:
                self.status_label.setText(message[:200])
                self._log_monitor(event_type, message)
            except Exception as e:
                log.debug("忽略异常: %s", e)
        self._migrate_worker.log_signal.connect(_on_migrate_log)

        def _on_migrate_done(ok, msg, path, done_row):
            # 程序正在退出时跳过弹窗和 UI 更新，避免退出后弹窗卡住
            if getattr(self, '_force_quit', False):
                self._log_monitor("install", f"程序退出中，迁移结果未弹窗: {path} ok={ok}")
                return
            # 处理目标非空警告：弹确认框，确认后用 force_overwrite=True 重试
            if not ok and msg.startswith("NEED_CONFIRM_OVERWRITE\n"):
                warning = msg[len("NEED_CONFIRM_OVERWRITE\n"):]
                ret = QMessageBox.warning(self, "⚠ 目标目录非空", warning,
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if ret == QMessageBox.Yes:
                    # 用户确认覆盖，重试迁移（force_overwrite=True 跳过非空检测）
                    self._log_monitor("install", f"用户确认覆盖目标目录，重试迁移: {path}")
                    # 复用原 worker 结构，传 force_overwrite=True
                    class _MigrateWorkerForce(QThread):
                        done_signal = Signal(bool, str, str, int)
                        log_signal = Signal(str, str)
                        def __init__(self, migrator, path, row):
                            super().__init__()
                            self.migrator = migrator
                            self.path = path
                            self.row = row
                        def run(self):
                            def _progress(event_type, message):
                                try:
                                    self.log_signal.emit(event_type, message)
                                except Exception as e:
                                    log.debug("忽略异常: %s", e)
                            self.migrator.log_callback = _progress
                            try:
                                ok2, msg2 = self.migrator.migrate(self.path, force_overwrite=True)
                                self.done_signal.emit(ok2, msg2, self.path, self.row)
                            except Exception as e:
                                self.done_signal.emit(False, str(e), self.path, self.row)
                            finally:
                                self.migrator.log_callback = None
                    self._migrate_worker = _MigrateWorkerForce(self.migrator, path, done_row)
                    self._migrate_worker.done_signal.connect(_on_migrate_done)
                    self._migrate_worker.log_signal.connect(
                        lambda et, m: (self.status_label.setText(m[:200]),
                                       self._log_monitor(et, m)))
                    self._migrate_worker.start()
                else:
                    self._log_monitor("install", f"用户取消覆盖目标目录，迁移中止: {path}")
                    self.status_label.setText(f"迁移已取消: {path}")
                return
            if ok:
                self._log_monitor("install", f"迁移成功: {msg}")
                # 数据迁移成功后，自动检测并配置对应开发工具的环境变量（全自动）
                self._auto_config_dev_env_after_migrate(path)
                # 清理目标盘符号链接残留（还原为真实空目录）
                try:
                    cleaned, scanned, _ = self.migrator.cleanup_symlink_residues()
                    if cleaned > 0:
                        self._log_monitor("install",
                            f"🔗 已清理目标盘 {cleaned} 个符号链接残留（还原为真实空目录）")
                except Exception as e:
                    log.error(f"迁移后清理符号链接残留失败: {e}")
                QMessageBox.information(self, "成功", msg)
                # 迁移成功后：不重新扫描，直接在表格间移动
                # 注意：row 可能已变化，按 src_path 查找当前行
                for r in range(self.table_scan.rowCount()):
                    if self.table_scan.item(r, 0) and self.table_scan.item(r, 0).text() == path:
                        done_row = r
                        break
                self._move_row_to_migrated(done_row)
                self.status_label.setText(f"迁移成功: {path}")
            else:
                self._log_monitor("error", f"迁移失败: {path} - 原因: {msg}")
                QMessageBox.critical(self, "失败", msg)
                self.status_label.setText(f"迁移失败: {path}")
            # 无论成功失败都刷新待处理事务按钮（失败可能已写入 pending）
            try:
                self._update_pending_decisions_button()
            except Exception as e:
                log.debug("忽略异常: %s", e)
        self._migrate_worker.done_signal.connect(_on_migrate_done)
        self._migrate_worker.start()

    def _auto_config_dev_env_after_migrate(self, src_path):
        """数据迁移成功后，自动检测并配置对应开发工具的环境变量（全自动）

        通用逻辑：
        1. 从迁移记录获取实际目标路径（完整路径，不只是盘符）
        2. 遍历开发工具，找出 C 盘默认路径匹配 src_path 的
        3. 计算工具在实际迁移目标中的路径（处理子目录关系）
        4. 调用 apply_tool 时传入 target_path_override，让环境变量指向实际路径
        不弹确认框，全自动执行，结果写入监控日志。
        """
        from dev_env_migrate import (TOOLS as DEV_TOOLS, CURRENT_PATH_FUNCS,
                                      apply_tool as dev_apply_tool,
                                      is_already_configured as dev_is_configured,
                                      get_migrated_tool_path as _get_migrated_path,
                                      get_tool_default_c_path as _get_default_c)
        # 从迁移记录中获取目标盘符和实际目标路径
        norm_src = src_path.replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
        target_drive = ""
        migrated_records = self.cfg.get("migrated", [])
        for m in migrated_records:
            m_src = m.get("src", "").replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
            if m_src == norm_src:
                dst = m.get("dst", "")
                if len(dst) >= 2 and dst[1] == ':':
                    target_drive = dst[0].upper()
                break
        if not target_drive:
            return  # 找不到迁移记录，跳过

        # 遍历开发工具，找出 C 盘默认路径匹配 src_path 的
        # 用 get_tool_default_c_path 静态查找（不读环境变量，避免已配置的工具误判）
        matched_tools = []
        for tool in DEV_TOOLS:
            if tool.get("special"):
                continue  # 跳过特殊工具（pip/docker/wsl/vs）
            try:
                tool_c_path = _get_default_c(tool)
                if not tool_c_path:
                    continue
                norm_tool = tool_c_path.replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
                # 精确匹配或工具 C 盘路径是 src_path 的子目录
                # （如工具路径 C:\...\Android\Sdk，src_path C:\...\Android）
                if norm_src == norm_tool or norm_tool.startswith(norm_src + "\\"):
                    # 检查是否已配置，避免重复配置
                    if not dev_is_configured(tool, target_drive):
                        matched_tools.append(tool)
            except Exception as e:
                log.debug("忽略异常: %s", e)

        if not matched_tools:
            return

        # 自动配置（不弹确认，全自动）
        # 关键：计算每个工具在实际迁移目标中的路径，传给 apply_tool 作为 target_path_override
        # 这样环境变量会指向用户实际迁移到的目录，而非默认模板路径
        configured = []
        for tool in matched_tools:
            try:
                # 计算工具在实际迁移目标中的路径
                # 如工具 C 盘路径 C:\...\Android\Sdk，迁移源 C:\...\Android，迁移目标 D:\xxx\appdata
                # → 工具实际路径 D:\xxx\appdata\Sdk
                tool_override = _get_migrated_path(tool, migrated_records)
                if not tool_override:
                    # 兜底：用迁移目标路径本身
                    for m in migrated_records:
                        if m.get("src", "").replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\") == norm_src:
                            tool_override = m.get("dst", "")
                            break

                ok, msg = dev_apply_tool(tool, target_drive,
                                          target_path_override=tool_override if tool_override else None)
                if ok:
                    configured.append(tool["name"])
                    # 记录实际目标路径到 dev_env_configured（供 unapply 和状态显示用）
                    # 只更新内存中的字典，循环结束后统一 save_all，避免频繁写盘
                    try:
                        dev_env_cfg = self.cfg.setdefault("dev_env_configured", {})
                        dev_env_cfg[tool["id"]] = {
                            "target_drive": target_drive,
                            "target_path": tool_override,
                            "source_path": src_path,
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "auto": True,
                        }
                    except Exception as e:
                        log.error(f"记录 dev_env_configured 失败: {e}")
                    self._log_monitor("dev_env",
                        f"自动配置 {tool['name']} 环境变量到 {tool_override or target_drive + ':盘'}"
                        f"（数据迁移后自动触发，使用实际迁移目标路径）")
                else:
                    self._log_monitor("error",
                        f"自动配置 {tool['name']} 失败: {msg}")
            except Exception as e:
                self._log_monitor("error", f"自动配置 {tool['name']} 异常: {e}")

        # 所有工具配置完成后统一保存一次，避免循环内频繁写盘影响性能
        if configured:
            try:
                save_all(self.cfg)
            except Exception as e:
                log.error(f"保存 dev_env_configured 失败: {e}")
            # 刷新开发环境迁移表，显示最新状态
            try:
                self._refresh_dev_env_table()
            except Exception as e:
                log.debug("忽略异常: %s", e)

    def _auto_unconfig_dev_env_after_restore(self, src_path):
        """数据还原回 C 盘后，自动撤销对应开发工具的环境变量配置

        如果 src_path 匹配某个开发工具的 C 盘路径，自动撤销环境变量配置。
        不弹确认框，全自动执行，结果写入监控日志。
        """
        from dev_env_migrate import (TOOLS as DEV_TOOLS, CURRENT_PATH_FUNCS,
                                      unapply_tool as dev_unapply_tool,
                                      is_already_configured as dev_is_configured,
                                      get_tool_default_c_path as dev_get_default_c)
        # 从 dev_env_configured 获取每个工具的目标盘符
        # 用静态 C 盘默认路径匹配（与 _auto_config 保持一致，不用环境变量当前值）
        dev_env_cfg = self.cfg.get("dev_env_configured", {})
        norm_src = src_path.replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")

        # 检查所有开发工具，找出路径匹配 src_path 且已配置的
        matched_tools = []
        for tool in DEV_TOOLS:
            if tool.get("special"):
                continue  # 跳过特殊工具
            try:
                tool_id = tool["id"]
                cfg_info = dev_env_cfg.get(tool_id) or {}
                target_drive = cfg_info.get("target_drive", "")
                if not target_drive:
                    continue
                # 用静态 C 盘默认路径匹配（与 _auto_config 保持一致）
                tool_c_path = dev_get_default_c(tool)
                if not tool_c_path:
                    continue
                norm_tool = tool_c_path.replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
                # 精确匹配或工具路径是 src_path 的子目录
                if norm_src == norm_tool or norm_tool.startswith(norm_src + "\\"):
                    # 检查是否已配置到该盘（用 dev_env_configured 判断）
                    if dev_is_configured(tool, target_drive):
                        matched_tools.append((tool, target_drive))
            except Exception as e:
                log.debug("忽略异常: %s", e)

        if not matched_tools:
            return

        # 自动撤销配置（不弹确认，全自动）
        unconfigured = []
        for tool, target_drive in matched_tools:
            try:
                ok, msg = dev_unapply_tool(tool, target_drive)
                if ok:
                    unconfigured.append(tool["name"])
                    # 删除 dev_env_configured 中的残留条目，避免状态不一致
                    try:
                        dev_env_cfg.pop(tool["id"], None)
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                    self._log_monitor("dev_env",
                        f"自动撤销 {tool['name']} 环境变量配置（数据还原后自动触发）")
                else:
                    self._log_monitor("error",
                        f"自动撤销 {tool['name']} 失败: {msg}")
            except Exception as e:
                self._log_monitor("error", f"自动撤销 {tool['name']} 异常: {e}")

        # 持久化 dev_env_configured 的变更（删除残留条目）
        if unconfigured:
            try:
                save_all(self.cfg)
            except Exception as e:
                log.error(f"保存 dev_env_configured 失败: {e}")

        if unconfigured:
            # 刷新开发环境迁移表，显示最新状态
            try:
                self._refresh_dev_env_table()
            except Exception as e:
                log.debug("忽略异常: %s", e)

    def _update_dev_env_target_path(self, src_path, new_target_path):
        """通用：将 dev_env_configured 中匹配 src_path 的工具 target_path 更新为新路径

        用于：改迁、重建链接、修复链接后，让开发环境区知道数据的新位置。
        """
        from dev_env_migrate import (TOOLS as DEV_TOOLS, get_tool_default_c_path as dev_get_default_c)
        dev_env_cfg = self.cfg.get("dev_env_configured", {})
        norm_src = src_path.replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
        updated = []
        for tool in DEV_TOOLS:
            if tool.get("special"):
                continue
            try:
                tool_c_path = dev_get_default_c(tool)
                if not tool_c_path:
                    continue
                norm_tool = tool_c_path.replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
                if norm_src == norm_tool or norm_tool.startswith(norm_src + "\\"):
                    cfg_info = dev_env_cfg.get(tool["id"], {})
                    if cfg_info and cfg_info.get("target_drive"):
                        cfg_info["target_path"] = new_target_path
                        updated.append(tool["name"])
                        log.info(f"更新开发环境配置 target_path: {tool['name']} → {new_target_path}")
            except Exception as e:
                log.debug("忽略异常: %s", e)
        if updated:
            self.cfg["dev_env_configured"] = dev_env_cfg
            save_all(self.cfg)
            try:
                self._refresh_dev_env_table()
            except Exception as e:
                log.debug("忽略异常: %s", e)

    def _unconfig_dev_env_for_path(self, src_path):
        """通用：撤销 src_path 对应的开发工具环境变量配置

        用于：删除链接后，撤掉环境变量防止指向无效路径。
        """
        from dev_env_migrate import (TOOLS as DEV_TOOLS, unapply_tool as dev_unapply_tool,
                                      get_tool_default_c_path as dev_get_default_c)
        dev_env_cfg = self.cfg.get("dev_env_configured", {})
        norm_src = src_path.replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
        unconfigured = []
        for tool in DEV_TOOLS:
            if tool.get("special"):
                continue
            try:
                tool_id = tool["id"]
                cfg_info = dev_env_cfg.get(tool_id) or {}
                target_drive = cfg_info.get("target_drive", "")
                if not target_drive:
                    continue
                tool_c_path = dev_get_default_c(tool)
                if not tool_c_path:
                    continue
                norm_tool = tool_c_path.replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
                if norm_src == norm_tool or norm_tool.startswith(norm_src + "\\"):
                    ok, msg = dev_unapply_tool(tool, target_drive)
                    if ok:
                        dev_env_cfg.pop(tool_id, None)
                        unconfigured.append(tool["name"])
                        log.info(f"自动撤销开发环境配置: {tool['name']}")
            except Exception as e:
                log.debug("忽略异常: %s", e)
        if unconfigured:
            try:
                save_all(self.cfg)
            except Exception as e:
                log.debug("忽略异常: %s", e)
            try:
                self._refresh_dev_env_table()
            except Exception as e:
                log.debug("忽略异常: %s", e)

    def _rebuild_all_links_wizard(self):
        """重装系统后一键重建所有符号链接的向导

        流程：
        1. 扫描所有 migrated 记录，统计需要重建的链接数
        2. 如果 C 盘路径含用户名，询问用户名是否变更
        3. 后台线程批量重建符号链接
        4. 完成后刷新已迁移表
        """
        migrated = self.cfg.get("migrated", [])
        if not migrated:
            QMessageBox.information(self, "无迁移记录",
                "当前没有任何迁移记录，无需重建。")
            return

        # 扫描统计
        need_rebuild = 0
        already_ok = 0
        target_gone = 0
        c_username = os.environ.get("USERNAME", "")
        old_usernames = set()

        for m in migrated:
            src = m.get("src", "")
            dst = m.get("dst", "")
            if not src or not dst:
                continue
            src_clean = src.replace("\\\\?\\", "")
            dst_clean = dst.replace("\\\\?\\", "")

            # 检测 src 路径中的用户名
            if "\\Users\\" in src_clean:
                parts = src_clean.split("\\")
                for j, p in enumerate(parts):
                    if j > 0 and parts[j-1].lower() == "users" and p:
                        if p.lower() != c_username.lower():
                            old_usernames.add(p)
                        break

            if not os.path.exists(dst_clean):
                target_gone += 1
            elif is_symlink(src_clean):
                # 符号链接存在，检查是否指向正确目标
                cur_tgt = get_symlink_target(src_clean)
                if cur_tgt:
                    cur_tgt = cur_tgt.replace("\\\\?\\", "")
                    if os.path.normpath(cur_tgt).lower() == os.path.normpath(dst_clean).lower():
                        already_ok += 1
                    else:
                        need_rebuild += 1  # 指向错误目标，需要重建
                else:
                    need_rebuild += 1  # 无法解析目标（断链），需要重建
            else:
                need_rebuild += 1

        # 构建提示信息
        info_lines = [
            f"迁移记录总数: {len(migrated)}",
            f"符号链接正常: {already_ok}",
            f"需要重建: {need_rebuild}",
        ]
        if target_gone > 0:
            info_lines.append(f"⚠ 目标盘数据丢失: {target_gone}（无法重建）")

        if need_rebuild == 0 and target_gone == 0:
            QMessageBox.information(self, "无需重建",
                "所有符号链接状态正常，无需重建。\n\n" + "\n".join(info_lines))
            return

        # 用户名映射
        username_map = None
        if old_usernames:
            # 有疑似旧用户名，询问映射
            map_lines = []
            for old_name in sorted(old_usernames):
                map_lines.append(f"  {old_name} → ?")
            reply = QMessageBox.question(self, "检测到用户名变更",
                f"迁移记录中的 C 盘路径包含以下用户名：\n\n"
                f"{chr(10).join(map_lines)}\n\n"
                f"当前系统用户名: {c_username}\n\n"
                f"是否将这些旧用户名映射到当前用户名 {c_username}？\n"
                f"（选\「是\」自动替换路径中的用户名，选\「否\」保持原路径）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if reply == QMessageBox.Yes:
                username_map = {old: c_username for old in old_usernames}

        # 最终确认
        reply = QMessageBox.question(self, "确认重建链接",
            f"{'\n'.join(info_lines)}\n\n"
            f"将批量重建 {need_rebuild} 个符号链接。\n"
            f"后台执行，不移动数据，只创建符号链接。\n\n"
            f"确定？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if reply != QMessageBox.Yes:
            return

        # 后台线程执行重建
        self.status_label.setText("正在重建所有符号链接（后台执行）...")
        self._log_monitor("install", "开始重装系统后一键重建所有链接")
        self.progress.setVisible(True)
        self.progress.setRange(0, len(migrated))
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("重建链接中 0/%d (%p%%)" % len(migrated))

        from PySide6.QtCore import QThread, Signal

        class _RebuildAllWorker(QThread):
            progress_signal = Signal(int, int, str)  # (current, total, msg)
            done_signal = Signal(int, int, int, list)  # (rebuilt, skipped, failed, details)
            log_signal = Signal(str, str)  # (event_type, message)
            def __init__(self, migrator, username_map):
                super().__init__()
                self.migrator = migrator
                self.username_map = username_map
            def run(self):
                try:
                    def _progress(current, total, msg):
                        try:
                            self.progress_signal.emit(current, total, msg)
                        except Exception as e:
                            log.debug("忽略异常: %s", e)
                    def _log(et, msg):
                        try:
                            self.log_signal.emit(et, msg)
                        except Exception as e:
                            log.debug("忽略异常: %s", e)
                    self.migrator.log_callback = _log
                    rebuilt, skipped, failed, details = self.migrator.rebuild_all_links(
                        username_map=self.username_map, progress_cb=_progress)
                    self.done_signal.emit(rebuilt, skipped, failed, details)
                except Exception as e:
                    log.error(f"重建所有链接异常: {e}")
                    self.done_signal.emit(0, 0, -1, [])
                finally:
                    self.migrator.log_callback = None

        worker = _RebuildAllWorker(self.migrator, username_map)
        self._rebuild_all_worker = worker

        def _on_progress(current, total, msg):
            self.progress.setValue(current)
            self.progress.setFormat(f"重建链接 {current}/{total} (%p%%)")
            self.status_label.setText(msg)

        def _on_rebuild_log(event_type, message):
            try:
                self.status_label.setText(message[:200])
                self._log_monitor(event_type, message)
            except Exception as e:
                log.debug("忽略异常: %s", e)

        def _on_rebuild_done(rebuilt, skipped, failed, details):
            self.progress.setVisible(False)
            if failed < 0:
                QMessageBox.critical(self, "重建失败", "重建过程中发生异常，请查看日志。")
                self.status_label.setText("重建链接失败")
                return
            # 构建结果详情
            detail_lines = []
            for src, dst, status, msg in details[:20]:
                icon = {"rebuilt": "✅", "rebuilt_ps": "✅", "already_ok": "⏭️",
                        "target_gone": "❌", "merge_failed": "❌",
                        "delete_failed": "❌", "mkdir_failed": "❌",
                        "mklink_failed": "❌"}.get(status, "?")
                detail_lines.append(f"  {icon} {os.path.basename(src)}: {msg}")
            if len(details) > 20:
                detail_lines.append(f"  ... 等共 {len(details)} 条")
            detail_text = "\n".join(detail_lines)

            QMessageBox.information(self, "重建链接完成",
                f"✅ 重建: {rebuilt} 个\n"
                f"⏭️ 跳过（已正常）: {skipped} 个\n"
                f"❌ 失败: {failed} 个\n\n"
                f"详情:\n{detail_text}")
            self.status_label.setText(f"重建链接完成: 重建{rebuilt}，跳过{skipped}，失败{failed}")
            self._log_monitor("install",
                f"重装系统重建链接完成: 重建{rebuilt}，跳过{skipped}，失败{failed}")
            # 刷新已迁移表
            try:
                self._refresh_migrated_only()
            except Exception as e:
                log.error(f"重建后刷新已迁移表失败: {e}")

        worker.progress_signal.connect(_on_progress)
        worker.log_signal.connect(_on_rebuild_log)
        worker.done_signal.connect(_on_rebuild_done)
        worker.start()

    def _move_row_to_scan(self, src_path):
        """还原成功后：从已迁移表删除该行，添加到待迁移表（只计算这一个目录）"""
        # 计算大小和说明
        # 注意：还原刚完成时 MFT 缓存可能未更新，强制用 os.walk 重新计算
        from utils import is_system_path
        from software_detect import get_dir_description
        import os
        size = 0
        try:
            total = 0
            for dirpath, dirnames, filenames in os.walk(src_path):
                for f in filenames:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, f))
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
            size = round(total / 1024 / 1024, 1)
        except Exception as e:
            log.debug("忽略异常: %s", e)
        try:
            desc = get_dir_description(src_path) or ""
        except Exception:
            desc = ""
        entry = os.path.basename(src_path)
        # 推断location
        location = ""
        la = os.environ.get("LOCALAPPDATA", "").lower()
        ap = os.environ.get("APPDATA", "").lower()
        up = os.environ.get("USERPROFILE", "").lower()
        pl = la + "\\programs"
        sp = src_path.lower()
        if sp.startswith(la + "\\") and not sp.startswith(pl):
            location = "Local"
        elif sp.startswith(pl):
            location = "Programs"
        elif sp.startswith(ap + "\\"):
            location = "Roaming"
        elif up and sp.startswith(up + "\\"):
            # 用户目录一级子目录（注意：Local/Programs/Roaming 是它的子目录，须先判）
            location = "User"
        elif sp.startswith("c:\\program files (x86)"):
            location = "Program Files (x86)"
        elif sp.startswith("c:\\program files"):
            location = "Program Files"
        elif sp.startswith("c:\\programdata"):
            location = "ProgramData"
        # 添加到待迁移表（单行插入，排序已启用时Qt自动定位）
        row = self.table_scan.rowCount()
        self.table_scan.insertRow(row)
        item0 = QTableWidgetItem(src_path)
        item0.setToolTip(src_path)
        item0.setFlags(item0.flags() & ~Qt.ItemIsEditable)
        self.table_scan.setItem(row, 0, item0)
        item1 = QTableWidgetItem(location)
        item1.setToolTip(location)
        item1.setFlags(item1.flags() & ~Qt.ItemIsEditable)
        self.table_scan.setItem(row, 1, item1)
        si = NumericTableWidgetItem(_format_size(size))
        si.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        si.setData(Qt.UserRole, float(size))
        _apply_size_item_color(si, size)
        si.setFlags(si.flags() & ~Qt.ItemIsEditable)
        self.table_scan.setItem(row, 2, si)
        item3 = QTableWidgetItem(entry)
        item3.setToolTip(entry)
        item3.setFlags(item3.flags() & ~Qt.ItemIsEditable)
        self.table_scan.setItem(row, 3, item3)
        item4 = QTableWidgetItem(desc)
        item4.setToolTip(desc if desc else entry)
        # 说明列保持可编辑
        self.table_scan.setItem(row, 4, item4)
        # 系统文件涂色
        if is_system_path(src_path):
            sys_brush = QColor("#FFF3E0")
            for col in range(self.table_scan.columnCount()):
                cell = self.table_scan.item(row, col)
                if cell:
                    cell.setBackground(sys_brush)
            item4.setText("[系统] " + desc)
        self._update_stats(migrated_count=self.table_migrated.rowCount(),
                           scan_count=self.table_scan.rowCount())

    def _move_rows_to_scan(self, src_paths):
        """批量还原成功后：从已迁移表删除，添加到待迁移表"""
        for src_path in src_paths:
            self._move_row_to_scan(src_path)

    def _move_row_to_migrated(self, scan_row):
        """迁移成功后：从待迁移表删除该行，添加到已迁移表（不重新扫描）"""
        # 从待迁移表读取该行数据
        src_path = self.table_scan.item(scan_row, 0).text()
        # 从config.json读取最新迁移记录（migrate方法已添加）
        last_migrated = None
        for m in reversed(self.cfg.get("migrated", [])):
            if m["src"] == src_path:
                last_migrated = m
                break
        if not last_migrated:
            return
        # 从待迁移表删除该行
        self.table_scan.removeRow(scan_row)
        # 添加到已迁移表（单行插入，排序已启用时Qt自动定位）
        row = self.table_migrated.rowCount()
        self.table_migrated.insertRow(row)
        item0 = QTableWidgetItem(last_migrated["src"])
        item0.setToolTip(last_migrated["src"])
        self.table_migrated.setItem(row, 0, item0)
        item1 = QTableWidgetItem(last_migrated["dst"])
        item1.setToolTip(last_migrated["dst"])
        self.table_migrated.setItem(row, 1, item1)
        size_val = last_migrated.get("size_mb", 0)
        si = NumericTableWidgetItem(_format_size(size_val))
        si.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        si.setData(Qt.UserRole, float(size_val))
        _apply_size_item_color(si, size_val)
        self.table_migrated.setItem(row, 2, si)
        st = QTableWidgetItem("正常")
        st.setForeground(QColor("#2E7D32"))
        st.setToolTip("符号链接有效，数据在目标盘")
        self.table_migrated.setItem(row, 3, st)
        # 链接目标列 - 新迁移成功，显示目标盘路径
        tgt_item = QTableWidgetItem(last_migrated["dst"])
        tgt_item.setToolTip(last_migrated["dst"])
        tgt_item.setForeground(QColor("#2E7D32"))
        self.table_migrated.setItem(row, 4, tgt_item)
        # 说明列
        desc_m = self._get_dir_description_safe(last_migrated["src"])
        item5 = QTableWidgetItem(desc_m)
        item5.setToolTip(desc_m if desc_m else os.path.basename(last_migrated["src"]))
        self.table_migrated.setItem(row, 5, item5)
        # 迁移时间列
        item6 = QTableWidgetItem(last_migrated.get("time", ""))
        item6.setToolTip(last_migrated.get("time", ""))
        self.table_migrated.setItem(row, 6, item6)
        # 更新状态栏和统计标签
        self.status_label.setText(f"迁移成功: {os.path.basename(src_path)}")
        self._update_stats(migrated_count=self.table_migrated.rowCount(),
                           scan_count=self.table_scan.rowCount())
        # 切换到已迁移标签页
        self.tabs.setCurrentIndex(0)
        # 清理 scan_cache 中已迁移项的残留记录，避免 smart_refresh 误判 mtime 未变化
        try:
            scan_cache = self.cfg.get("scan_cache", [])
            new_cache = [c for c in scan_cache if c.get("path") != src_path]
            if len(new_cache) != len(scan_cache):
                self.cfg["scan_cache"] = new_cache
                save_all(self.cfg)
        except Exception as e:
            log.error(f"清理 scan_cache 残留失败: {e}")

    def _relocate_selected(self, rows):
        """改迁选中项到其他盘（移动目标数据，更新C盘链接指向）

        只支持单行改迁（多行目标选择复杂，暂不支持）。
        调用 migrator.migrate_symlink 执行：复制真实数据到新位置 →
        更新C盘链接指向 → 删除旧真实数据目录。
        """
        if len(rows) != 1:
            QMessageBox.warning(self, "提示", "改迁一次只支持选择一个目录。\n请只选一行后重试。")
            return
        row = rows[0]
        try:
            src_path = self.table_migrated.item(row, 0).text()
        except Exception:
            return

        # 防重入：如果上一个改迁线程还在运行，拒绝启动新的
        if hasattr(self, '_relocate_worker') and self._relocate_worker and self._relocate_worker.isRunning():
            QMessageBox.warning(self, "请稍候", "上一个改迁任务还在执行中，请等待完成后再试。")
            return

        # 检查 src 是否是符号链接（改迁的前提）
        if not is_symlink(src_path):
            QMessageBox.warning(self, "无法改迁",
                f"C 盘路径不是符号链接，无法改迁：\n{src_path}\n\n"
                f"改迁仅适用于已迁移（符号链接）的目录。")
            return

        # 解析当前符号链接的真实目标
        real_target = get_symlink_target(src_path)
        if real_target:
            real_target = real_target.replace("\\\\?\\", "")
        if not real_target or not os.path.exists(real_target):
            QMessageBox.critical(self, "无法改迁",
                f"符号链接的真实数据目录不存在：\n{real_target or '(空)'}\n\n"
                f"可能数据已丢失，请先还原或修复链接。")
            return

        # 弹出目录选择对话框让用户选新目标盘/目录
        new_base = QFileDialog.getExistingDirectory(self,
            "选择改迁目标目录（数据将移动到此目录下）",
            real_target)  # 默认展开当前真实数据所在目录
        if not new_base:
            return
        # 规范化路径：QFileDialog 在部分 Windows 版本返回正斜杠，转为反斜杠
        new_base = os.path.normpath(new_base)

        # 构建新目标路径：目标目录/src目录名
        src_name = os.path.basename(src_path.rstrip("\\/"))
        new_dst = os.path.join(new_base, src_name)

        # 如果新目标和旧目标相同，无需改迁
        if os.path.normpath(new_dst).lower() == os.path.normpath(real_target).lower():
            QMessageBox.information(self, "无需改迁",
                f"新目标路径与当前真实数据路径相同：\n{new_dst}")
            return

        # 如果新目标已存在且不是符号链接，需要确认覆盖
        if os.path.exists(new_dst):
            if is_symlink(new_dst):
                QMessageBox.critical(self, "目标路径冲突",
                    f"目标路径已存在符号链接：\n{new_dst}\n\n"
                    f"请先手动删除该符号链接或选择其他目录。")
                return
            ret = QMessageBox.question(self, "目标路径已存在",
                f"目标路径已存在：\n{new_dst}\n\n"
                f"镜像同步会合并覆盖同名文件。\n确定继续？",
                QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                return

        # 最终确认
        ret = QMessageBox.question(self, "确认改迁",
            f"将改迁以下目录到新位置：\n\n"
            f"C 盘链接: {src_path}\n"
            f"当前数据: {real_target}\n"
            f"新 目 标: {new_dst}\n\n"
            f"将执行：复制数据 → 更新C盘链接 → 删除旧数据目录\n"
            f"后台执行，请稍候...\n\n确定？",
            QMessageBox.Yes | QMessageBox.No)
        if ret != QMessageBox.Yes:
            return

        self.status_label.setText(f"正在改迁: {src_path} → {new_dst}（后台执行）...")

        class _RelocateWorker(QThread):
            done_signal = Signal(bool, str, str)  # (ok, msg, src_path)
            log_signal = Signal(str, str)  # (event_type, message) 复制进度
            def __init__(self, migrator, src, dst, real_target):
                super().__init__()
                self.migrator = migrator
                self.src = src
                self.dst = dst
                self.real_target = real_target
            def run(self):
                def _relocate_progress(event_type, message):
                    try:
                        self.log_signal.emit(event_type, message)
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                self.migrator.log_callback = _relocate_progress
                try:
                    ok, msg = self.migrator.migrate_symlink(
                        self.src, self.dst, self.real_target)
                    self.done_signal.emit(ok, msg, self.src)
                except Exception as e:
                    self.done_signal.emit(False, str(e), self.src)
                finally:
                    self.migrator.log_callback = None

        self._relocate_worker = _RelocateWorker(
            self.migrator, src_path, new_dst, real_target)
        def _on_relocate_log(event_type, message):
            try:
                self.status_label.setText(message[:200])
                self._log_monitor(event_type, message)
            except Exception as e:
                log.debug("忽略异常: %s", e)
        self._relocate_worker.log_signal.connect(_on_relocate_log)

        def _on_relocate_done(ok, msg, path):
            if getattr(self, '_force_quit', False):
                self._log_monitor("install", f"程序退出中，改迁结果未弹窗: {path} ok={ok}")
                return
            # 处理目标非空警告：弹确认框，确认后用 force_overwrite=True 重试
            if not ok and msg.startswith("NEED_CONFIRM_OVERWRITE\n"):
                warning = msg[len("NEED_CONFIRM_OVERWRITE\n"):]
                ret = QMessageBox.warning(self, "⚠ 目标目录非空", warning,
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if ret == QMessageBox.Yes:
                    self._log_monitor("install", f"用户确认覆盖目标目录，重试改迁: {path}")
                    class _RelocateWorkerForce(QThread):
                        done_signal = Signal(bool, str, str)
                        log_signal = Signal(str, str)
                        def __init__(self, migrator, src, dst, real_target):
                            super().__init__()
                            self.migrator = migrator
                            self.src = src
                            self.dst = dst
                            self.real_target = real_target
                        def run(self):
                            def _progress(event_type, message):
                                try:
                                    self.log_signal.emit(event_type, message)
                                except Exception as e:
                                    log.debug("忽略异常: %s", e)
                            self.migrator.log_callback = _progress
                            try:
                                ok2, msg2 = self.migrator.migrate_symlink(
                                    self.src, self.dst, self.real_target, force_overwrite=True)
                                self.done_signal.emit(ok2, msg2, self.src)
                            except Exception as e:
                                self.done_signal.emit(False, str(e), self.src)
                            finally:
                                self.migrator.log_callback = None
                    self._relocate_worker = _RelocateWorkerForce(
                        self.migrator, path, new_dst, real_target)
                    self._relocate_worker.done_signal.connect(_on_relocate_done)
                    self._relocate_worker.log_signal.connect(
                        lambda et, m: (self.status_label.setText(m[:200]),
                                       self._log_monitor(et, m)))
                    self._relocate_worker.start()
                else:
                    self._log_monitor("install", f"用户取消覆盖目标目录，改迁中止: {path}")
                    self.status_label.setText(f"改迁已取消: {path}")
                return
            if ok:
                self._log_monitor("install", f"改迁成功: {msg}")
                # 更新开发环境配置中的 target_path（数据搬到了新位置）
                self._update_dev_env_target_path(path, new_dst)
                # 清理目标盘符号链接残留
                try:
                    cleaned, _, _ = self.migrator.cleanup_symlink_residues()
                    if cleaned > 0:
                        self._log_monitor("install",
                            f"🔗 已清理目标盘 {cleaned} 个符号链接残留")
                except Exception as e:
                    log.error(f"改迁后清理符号链接残留失败: {e}")
                QMessageBox.information(self, "改迁成功", msg)
                # 更新已迁移表中该行的 dst
                for r in range(self.table_migrated.rowCount()):
                    if (self.table_migrated.item(r, 0) and
                            self.table_migrated.item(r, 0).text() == path):
                        # 更新 dst 列
                        from ui_widgets import NumericTableWidgetItem, _format_size, _apply_size_item_color
                        dst_item = self.table_migrated.item(r, 1)
                        if dst_item:
                            dst_item.setText(new_dst)
                            dst_item.setToolTip(new_dst)
                        # 更新 size 列
                        try:
                            new_size = get_dir_size_fast(new_dst) if os.path.exists(new_dst) else 0
                        except Exception:
                            new_size = 0
                        size_item = self.table_migrated.item(r, 2)
                        if size_item:
                            size_item.setText(_format_size(new_size))
                            size_item.setData(Qt.UserRole, float(new_size))
                            _apply_size_item_color(size_item, new_size)
                        # 更新 config 记录
                        for m in self.cfg.get("migrated", []):
                            if m.get("src") == path:
                                m["dst"] = new_dst
                                m["size_mb"] = new_size
                                m["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                break
                        save_all(self.cfg)
                        break
                # 刷新已迁移表状态（更新链接目标列 + 大小 + 状态）
                self._update_migrated_rows_status([r])
                self.status_label.setText(f"改迁成功: {path} → {new_dst}")
            else:
                self._log_monitor("error", f"改迁失败: {path} - 原因: {msg}")
                QMessageBox.critical(self, "改迁失败", msg)
                self.status_label.setText(f"改迁失败: {path}")
            # 无论成功失败都刷新待处理事务按钮
            try:
                self._update_pending_decisions_button()
            except Exception as e:
                log.debug("忽略异常: %s", e)
        self._relocate_worker.done_signal.connect(_on_relocate_done)
        self._relocate_worker.start()

    def _migrated_context_menu(self, pos):
        """已迁移表右键菜单 - 修复/还原/改迁/删除记录/打开目录"""
        rows = sorted(set(idx.row() for idx in self.table_migrated.selectedIndexes()))
        if not rows:
            return
        menu = QMenu(self)
        # 统计选中行的状态
        need_fix = 0
        for row in rows:
            status = self.table_migrated.item(row, 3).text()
            if status in ("断链", "丢失"):
                need_fix += 1
        # 修复链接（只对断链/丢失状态有效）
        if need_fix > 0:
            act_fix = menu.addAction(f"修复链接（{need_fix} 个需要修复）")
        else:
            act_fix = menu.addAction("修复链接（选中项均正常，无需修复）")
            act_fix.setEnabled(False)
        menu.addSeparator()
        act_restore = menu.addAction("还原选中（数据放回C盘）")
        act_relocate = menu.addAction("改迁到其他盘（移动目标数据）")
        act_relink = menu.addAction("重建链接")
        menu.addSeparator()
        act_del_link = menu.addAction("删除链接（保留目标数据）")
        act_del_record = menu.addAction("删除记录（只删记录不动文件）")
        menu.addSeparator()
        act_open_src = menu.addAction("打开C盘路径")
        act_open_dst = menu.addAction("打开目标盘路径")
        act_copy = menu.addAction("复制路径")
        action = menu.exec(self.table_migrated.viewport().mapToGlobal(pos))
        if action is None:
            return
        if action == act_fix:
            self._fix_selected_links(rows)
        elif action == act_restore:
            # 批量还原（后台线程，避免 UI 卡死）
            if QMessageBox.question(self, "确认批量还原",
                f"将还原 {len(rows)} 个目录到C盘\n\n"
                f"后台执行，请稍候...\n\n确定？",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            # 收集要还原的路径
            paths_to_restore = []
            for row in rows:
                try:
                    p = self.table_migrated.item(row, 0).text()
                    paths_to_restore.append(p)
                except Exception as e:
                    log.debug("忽略异常: %s", e)
            self.status_label.setText(f"正在批量还原 {len(paths_to_restore)} 个目录（后台执行）...")

            class _BatchRestoreWorker(QThread):
                done_signal = Signal(list, list)  # (success_paths, fail_msgs)
                progress_signal = Signal(str, int, int)  # (当前路径, 当前序号, 总数)
                log_signal = Signal(str, str)  # (event_type, message) 复制进度
                def __init__(self, migrator, paths):
                    super().__init__()
                    self.migrator = migrator
                    self.paths = paths
                def run(self):
                    success_paths = []
                    fail_msgs = []
                    total = len(self.paths)
                    # 设置 log_callback：复制进度实时输出到状态栏 + 监控日志
                    def _batch_restore_progress(event_type, message):
                        try:
                            self.log_signal.emit(event_type, message)
                        except Exception as e:
                            log.debug("忽略异常: %s", e)
                    self.migrator.log_callback = _batch_restore_progress
                    try:
                        for i, p in enumerate(self.paths, 1):
                            self.progress_signal.emit(p, i, total)
                            try:
                                ok, msg = self.migrator.restore(p)
                                if ok:
                                    success_paths.append(p)
                                else:
                                    fail_msgs.append((p, msg))
                            except Exception as e:
                                fail_msgs.append((p, str(e)))
                    finally:
                        self.migrator.log_callback = None
                    self.done_signal.emit(success_paths, fail_msgs)

            self._batch_restore_worker = _BatchRestoreWorker(self.migrator, paths_to_restore)
            self._batch_restore_worker.progress_signal.connect(
                lambda path, idx, total: self.status_label.setText(
                    f"正在还原 ({idx}/{total}): {path}"))
            # 复制进度信号：更新状态栏 + 监控日志
            def _on_batch_restore_log(event_type, message):
                try:
                    self.status_label.setText(message[:200])
                    self._log_monitor(event_type, message)
                except Exception as e:
                    log.debug("忽略异常: %s", e)
            self._batch_restore_worker.log_signal.connect(_on_batch_restore_log)

            def _on_batch_restore_done(success_paths, fail_msgs):
                # 还原后自动撤销对应开发工具的环境变量配置
                for p in success_paths:
                    self._auto_unconfig_dev_env_after_restore(p)
                # 从已迁移表删除成功的行，添加到待迁移表
                if success_paths:
                    # 按 src_path 查找并删除（倒序避免行号变化）
                    rows_to_del = []
                    for r in range(self.table_migrated.rowCount()):
                        item = self.table_migrated.item(r, 0)
                        if item and item.text() in success_paths:
                            rows_to_del.append(r)
                    for r in reversed(rows_to_del):
                        self.table_migrated.removeRow(r)
                    self._move_rows_to_scan(success_paths)
                # 清理目标盘符号链接残留（还原为真实空目录）
                try:
                    cleaned, scanned, _ = self.migrator.cleanup_symlink_residues()
                    if cleaned > 0:
                        self._log_monitor("install",
                            f"🔗 已清理目标盘 {cleaned} 个符号链接残留（还原为真实空目录）")
                except Exception as e:
                    log.error(f"还原后清理符号链接残留失败: {e}")
                fail_count = len(fail_msgs)
                success_count = len(success_paths)
                detail = ""
                if fail_msgs:
                    detail = "\n\n失败详情:\n" + "\n".join(f"  ✗ {p}: {m}" for p, m in fail_msgs[:5])
                QMessageBox.information(self, "批量还原完成",
                    f"成功: {success_count} 个\n失败: {fail_count} 个{detail}")
                self.status_label.setText(f"批量还原完成：成功 {success_count}，失败 {fail_count}")
                # 刷新待处理事务按钮
                try:
                    self._update_pending_decisions_button()
                except Exception as e:
                    log.debug("忽略异常: %s", e)

            self._batch_restore_worker.done_signal.connect(_on_batch_restore_done)
            self._batch_restore_worker.start()
        elif action == act_relocate:
            self._relocate_selected(rows)
        elif action == act_relink:
            # 批量重建链接
            # 安全检查：检测是否有 C 盘真实目录（非符号链接），有则弹警告确认
            # 断链场景下 C 盘可能是软件更新写入的真实数据，直接删会丢失
            real_dir_rows = []
            for row in rows:
                _src = self.table_migrated.item(row, 0).text()
                if os.path.exists(_src) and not is_symlink(_src):
                    real_dir_rows.append((_src, row))
            if real_dir_rows:
                _names = "\n".join(f"  • {s}" for s, _ in real_dir_rows[:5])
                if len(real_dir_rows) > 5:
                    _names += f"\n  ... 等共 {len(real_dir_rows)} 个"
                _warn = QMessageBox(self)
                _warn.setIcon(QMessageBox.Critical)
                _warn.setWindowTitle("⚠️ 重建链接将删除 C 盘真实目录")
                _warn.setText(
                    f"以下 C 盘路径是真实目录（非符号链接），重建链接将【直接删除】其中所有数据：\n\n"
                    f"{_names}\n\n"
                    f"目标盘已有数据，删除 C 盘数据不会从目标盘恢复。\n"
                    f"如需保留 C 盘新数据，请改用「修复链接」（会先合并到目标盘）。\n\n"
                    f"确定要直接删除这些 C 盘真实目录并重建链接吗？")
                _warn.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
                _warn.setDefaultButton(QMessageBox.No)
                if _warn.exec() != QMessageBox.Yes:
                    self._log_monitor("install", "批量重建链接：用户取消（存在真实目录未确认）")
                    return
            success, fail = 0, 0
            relinked_rows = []
            # 中危-6：批量重建主线程循环含 shutil.rmtree 大目录删除 → UI 卡顿
            # 收集 (row, src, dst) 在主线程读表格，worker 只做 IO
            relink_items = []
            for row in rows:
                src_path = self.table_migrated.item(row, 0).text()
                dst_path = self.table_migrated.item(row, 1).text()
                relink_items.append((row, src_path, dst_path))

            relink_progress = QProgressDialog(f"正在批量重建 {len(relink_items)} 个链接...", None, 0, 0, self)
            relink_progress.setWindowTitle("批量重建链接")
            relink_progress.setWindowModality(Qt.WindowModal)
            relink_progress.setCancelButton(None)
            relink_progress.setMinimumDuration(0)
            relink_progress.show()

            class _BatchRelinkWorker(QThread):
                done_signal = Signal(list)  # [(row, src, dst, ok, msg), ...]
                progress_signal = Signal(str, str)  # (event_type, message)
                def __init__(self, items):
                    super().__init__()
                    self.items = items
                def run(self):
                    results = []
                    for row, src_path, dst_path in self.items:
                        self.progress_signal.emit("install", f"开始重建链接: {src_path}")
                        try:
                            if is_symlink(src_path):
                                try:
                                    os.rmdir(src_path)
                                except OSError:
                                    os.unlink(src_path)
                            elif os.path.exists(src_path):
                                shutil.rmtree(src_path)
                            subprocess.run(["cmd", "/c", "mklink", "/D", src_path, dst_path],
                                capture_output=True, check=True,
                                creationflags=0x08000000)  # CREATE_NO_WINDOW
                            results.append((row, src_path, dst_path, True, "重建成功"))
                            self.progress_signal.emit("install", f"重建链接成功: {src_path}")
                        except Exception as e:
                            results.append((row, src_path, dst_path, False, str(e)))
                            self.progress_signal.emit("error", f"重建链接失败: {src_path} - 原因: {e}")
                            log_error_with_reason("重建链接失败", str(e), f"重建: {src_path} -> {dst_path}")
                    self.done_signal.emit(results)

            self._batch_relink_worker = _BatchRelinkWorker(relink_items)

            def _on_relink_progress(event_type, message):
                try:
                    self.status_label.setText(message[:200])
                    self._log_monitor(event_type, message)
                except Exception as e:
                    log.debug("忽略异常: %s", e)
            self._batch_relink_worker.progress_signal.connect(_on_relink_progress)

            def _on_relink_done(results):
                relink_progress.close()
                if getattr(self, '_force_quit', False):
                    return
                success = sum(1 for r in results if r[3])
                fail = len(results) - success
                QMessageBox.information(self, "批量重建完成",
                    f"成功: {success} 个\n失败: {fail} 个")
                # 主线程：补记链接日志 + 更新 dev_env + 更新行状态
                relinked_rows = []
                for row, src_p, dst_p, ok, msg in results:
                    if ok:
                        relinked_rows.append(row)
                        try:
                            self._log_link("重建链接", src_p, dst_p)
                            self._update_dev_env_target_path(src_p, dst_p)
                        except Exception as e:
                            log.error(f"批量重建链接后更新dev_env失败: {e}")
                if relinked_rows:
                    self._update_migrated_rows_status(relinked_rows)

            self._batch_relink_worker.done_signal.connect(_on_relink_done, Qt.QueuedConnection)
            self._batch_relink_worker.start()
        elif action == act_del_link:
            # 批量删除链接（保留目标数据；迁移记录保留在已迁移表，刷新后状态显示丢失）
            if QMessageBox.question(self, "确认删除链接",
                f"将删除 {len(rows)} 个符号链接\n（目标盘数据保留）\n\n确定？",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            success, fail = 0, 0
            deleted_srcs = []
            for row in rows:
                src_path = self.table_migrated.item(row, 0).text()
                dst_path = self.table_migrated.item(row, 1).text()
                self._log_monitor("install", f"开始删除链接: {src_path}")
                ok, msg = self._delete_link_single(src_path, dst_path)
                if ok:
                    success += 1
                    deleted_srcs.append(src_path)
                    self._log_monitor("install", f"删除链接成功: {src_path}")
                else:
                    fail += 1
                    self._log_monitor("error", f"删除链接失败: {src_path} - 原因: {msg}")
            QMessageBox.information(self, "批量删除完成",
                f"成功: {success} 个\n失败: {fail} 个")
            # 撤销对应开发工具的环境变量配置（链接已删，环境变量指向失效）
            for src_p in deleted_srcs:
                try:
                    self._unconfig_dev_env_for_path(src_p)
                except Exception as e:
                    log.error(f"批量删除链接后撤销环境变量失败: {e}")
            # 记录保留在已迁移表（状态自动变「丢失」），仅刷新表格
            if deleted_srcs:
                self._refresh_migrated_only()
        elif action == act_del_record:
            # 只删除config中的记录，不碰文件（删除前记录恢复线索，供「删除记录恢复」找回）
            if QMessageBox.question(self, "确认删除记录",
                f"将删除 {len(rows)} 条迁移记录\n（不删除C盘链接和目标盘数据）\n\n确定？",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            srcs_to_del = set()
            rows_info = []
            for row in rows:
                src_path = self.table_migrated.item(row, 0).text()
                dst_path = self.table_migrated.item(row, 1).text()
                srcs_to_del.add(src_path)
                rows_info.append((src_path, dst_path))
            # 记录恢复线索（「删除记录」会从已迁移表移除条目，供恢复按钮找回；
            # 失败不阻断删除流程）
            for src_path, dst_path in rows_info:
                try:
                    rec_ok, rec_err = self.migrator.record_deleted_link(
                        src_path, dst_path)
                    if not rec_ok:
                        log.warning(f"记录删除记录恢复线索失败: {rec_err}")
                except Exception as e:
                    log.warning(f"记录删除记录恢复线索异常: {e}")
            self.cfg["migrated"] = [m for m in self.cfg["migrated"]
                if m["src"] not in srcs_to_del]
            save_all(self.cfg)
            # 迁移记录移除 → 删除对应目标目录轻量索引（记录移除即删索引）
            for src_path, dst_path in rows_info:
                try:
                    self.migrator.remove_dst_index(dst_path)
                except Exception as e:
                    log.debug("忽略异常: %s", e)
            # 局部刷新：只删除表格中对应的行，不重新扫描文件系统（避免 MFT 索引重建）
            for row in sorted(rows, reverse=True):
                self.table_migrated.removeRow(row)
            self.status_label.setText(f"已删除 {len(srcs_to_del)} 条记录")
        elif action == act_open_src:
            for row in rows:
                path = self.table_migrated.item(row, 0).text()
                if os.path.exists(path):
                    os.startfile(path)
        elif action == act_open_dst:
            for row in rows:
                path = self.table_migrated.item(row, 1).text()
                if os.path.exists(path):
                    os.startfile(path)
        elif action == act_copy:
            paths = []
            for row in rows:
                paths.append(self.table_migrated.item(row, 0).text())
                paths.append("  -> " + self.table_migrated.item(row, 1).text())
            QApplication.clipboard().setText("\n".join(paths))
            self.status_label.setText(f"已复制{len(rows)}个路径")

    def _fix_selected_links(self, rows):
        """批量修复断链/丢失的符号链接"""
        fix_list = []
        for row in rows:
            status = self.table_migrated.item(row, 3).text()
            if status not in ("断链", "丢失"):
                continue
            src_path = self.table_migrated.item(row, 0).text()
            dst_path = self.table_migrated.item(row, 1).text()
            fix_list.append((row, src_path, dst_path))
        if not fix_list:
            QMessageBox.information(self, "提示", "选中项均正常，无需修复")
            return
        if QMessageBox.question(self, "确认修复链接",
            f"将修复 {len(fix_list)} 个断链/丢失的符号链接\n\n"
            f"修复方式：\n"
            f"- 断链：合并C盘新数据到目标盘 → 删除C盘目录 → 创建链接\n"
            f"- 丢失：直接在C盘创建链接指向目标盘\n\n确定？",
            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        success, fail = 0, 0
        fixed_rows = []  # 记录修复成功的行号
        # 中危-6：批量修复主线程循环含 复制引擎合并大目录 → UI 卡顿
        # migrator.fix_broken_link 内部跑复制引擎，移到后台线程
        fix_progress = QProgressDialog(f"正在批量修复 {len(fix_list)} 个链接...", None, 0, 0, self)
        fix_progress.setWindowTitle("批量修复链接")
        fix_progress.setWindowModality(Qt.WindowModal)
        fix_progress.setCancelButton(None)
        fix_progress.setMinimumDuration(0)
        fix_progress.show()

        class _BatchFixLinkWorker(QThread):
            done_signal = Signal(list)  # [(row, src, dst, ok, msg), ...]
            progress_signal = Signal(str, str)  # (event_type, message)
            def __init__(self, migrator, items):
                super().__init__()
                self.migrator = migrator
                self.items = items
            def run(self):
                results = []
                # 设置 复制进度回调（转发到主线程）
                def _fix_progress(event_type, message):
                    try:
                        self.progress_signal.emit(event_type, message)
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                self.migrator.log_callback = _fix_progress
                try:
                    for row, src_path, dst_path in self.items:
                        self.progress_signal.emit("install", f"开始修复链接: {src_path}")
                        try:
                            ok, msg = self.migrator.fix_broken_link(src_path, dst_path)
                            results.append((row, src_path, dst_path, ok, msg))
                            if ok:
                                self.progress_signal.emit("install", f"修复链接成功: {src_path} - {msg}")
                                log.info(f"修复链接成功: {src_path} - {msg}")
                            else:
                                self.progress_signal.emit("error", f"修复链接失败: {src_path} - 原因: {msg}")
                                log_error_with_reason(msg, "", f"批量修复链接: {src_path}")
                                log.error(f"修复链接失败: {src_path} - {msg}")
                        except Exception as e:
                            results.append((row, src_path, dst_path, False, str(e)))
                            self.progress_signal.emit("error", f"修复链接异常: {src_path} - {e}")
                            log_error_with_reason(str(e), "", f"批量修复链接异常: {src_path}")
                finally:
                    self.migrator.log_callback = None
                self.done_signal.emit(results)

        self._batch_fix_link_worker = _BatchFixLinkWorker(self.migrator, fix_list)

        def _on_fix_progress(event_type, message):
            try:
                self.status_label.setText(message[:200])
                self._log_monitor(event_type, message)
            except Exception as e:
                log.debug("忽略异常: %s", e)
        self._batch_fix_link_worker.progress_signal.connect(_on_fix_progress)

        def _on_fix_done(results):
            fix_progress.close()
            if getattr(self, '_force_quit', False):
                return
            success = sum(1 for r in results if r[3])
            fail = len(results) - success
            QMessageBox.information(self, "批量修复完成",
                f"成功: {success} 个\n失败: {fail} 个")
            # 主线程：更新 dev_env target_path + 更新行状态
            fixed_rows = []
            for row, src_p, dst_p, ok, msg in results:
                if ok:
                    fixed_rows.append(row)
                    try:
                        self._update_dev_env_target_path(src_p, dst_p)
                    except Exception as e:
                        log.error(f"批量修复链接后更新dev_env失败: {e}")
            if fixed_rows:
                self._update_migrated_rows_status(fixed_rows)

        self._batch_fix_link_worker.done_signal.connect(_on_fix_done, Qt.QueuedConnection)
        self._batch_fix_link_worker.start()

    def _update_migrated_rows_status(self, rows):
        """只更新指定行的状态（不全盘扫描）
        rows: 行号列表
        """
        status_map = {
            "OK":         ("正常",     "#2E7D32", "符号链接有效，数据在目标盘"),
            "BROKEN":     ("断链",     "#C62828", "C盘路径被软件覆盖为真实目录，点击右键修复"),
            "MISSING":    ("丢失",     "#EF6C00", "C盘路径不存在，点击右键修复（直接创建链接）"),
            "TARGET_GONE":("目标丢失", "#B71C1C", "目标盘数据不存在，需还原或重新迁移"),
        }
        for row in rows:
            try:
                src_path = self.table_migrated.item(row, 0).text()
                dst_path = self.table_migrated.item(row, 1).text()
                # 重新检测该路径的状态
                is_link = is_symlink(src_path)
                target = get_symlink_target(src_path) if is_link else ""
                def norm(p):
                    return p.lower().rstrip("\\").replace("\\\\?\\", "").replace("\\\\?\\UNC\\", "\\\\").replace("/", "\\") if p else ""
                target_norm = norm(target)
                dst_norm = norm(dst_path)
                if is_link and target_norm == dst_norm and os.path.exists(dst_path):
                    status = "OK"
                elif is_link and (not os.path.exists(dst_path)):
                    status = "TARGET_GONE"
                elif is_link and target_norm != dst_norm:
                    status = "BROKEN"
                elif not os.path.exists(src_path):
                    status = "MISSING"
                else:
                    status = "BROKEN"
                # 更新状态列
                status_text, status_color, status_tip = status_map.get(
                    status, ("未知", "#424242", ""))
                st_item = self.table_migrated.item(row, 3)
                if st_item:
                    st_item.setText(status_text)
                    st_item.setForeground(QColor(status_color))
                    st_item.setToolTip(status_tip)
                # 更新链接目标列（第4列）- 让用户看到链接实际指向
                tgt_item = self.table_migrated.item(row, 4)
                if tgt_item:
                    if target:
                        target_display = target.replace("\\\\?\\", "").replace("\\\\?\\UNC\\", "\\\\")
                        tgt_item.setText(target_display)
                        tgt_item.setToolTip(target_display)
                        tgt_item.setForeground(QColor("#2E7D32") if is_link else QColor("#C62828"))
                    else:
                        tgt_item.setText("（非符号链接）")
                        tgt_item.setToolTip("C盘路径不是符号链接，可能是真实目录（被软件覆盖）")
                        tgt_item.setForeground(QColor("#9E9E9E"))
                # 更新大小列（同时更新UserRole确保排序正确）
                size = get_dir_size_fast(dst_path) if os.path.exists(dst_path) else 0
                size_item = self.table_migrated.item(row, 2)
                if size_item:
                    from ui_widgets import NumericTableWidgetItem, _format_size, _apply_size_item_color
                    size_item.setText(_format_size(size))
                    size_item.setData(Qt.UserRole, float(size))
                    _apply_size_item_color(size_item, size)
            except Exception as e:
                log_error_with_reason("未知错误", str(e), f"更新行状态: row={row}")
                log.error(f"更新行状态失败 row={row}: {e}")

    def _relink_single(self, src_path, dst_path):
        """重建单个链接"""
        try:
            if is_symlink(src_path):
                try:
                    os.rmdir(src_path)
                except OSError:
                    os.unlink(src_path)
            elif os.path.exists(src_path):
                shutil.rmtree(src_path)
            subprocess.run(["cmd", "/c", "mklink", "/D", src_path, dst_path],
                capture_output=True, check=True,
                creationflags=0x08000000)  # CREATE_NO_WINDOW 避免弹黑窗
            self._log_link("重建链接", src_path, dst_path)
            return True, "重建成功"
        except Exception as e:
            log_error_with_reason("重建链接失败", str(e), f"重建: {src_path} -> {dst_path}")
            return False, str(e)

    def _delete_link_single(self, src_path, dst_path):
        """删除单个链接（保留目标数据）"""
        try:
            if is_symlink(src_path):
                try:
                    os.rmdir(src_path)
                except OSError:
                    os.unlink(src_path)
            self._log_link("删除链接", src_path, dst_path, "保留目标数据")
            return True, "删除成功"
        except Exception as e:
            log_error_with_reason("删除链接失败", str(e), f"删除链接: {src_path}")
            return False, str(e)

    def _migrate_rows(self, rows):
        """批量迁移到默认盘"""
        # 检查是否包含系统文件
        from utils import is_system_path
        sys_paths = []
        for row in rows:
            p = self.table_scan.item(row, 0).text()
            if is_system_path(p):
                sys_paths.append(p)
        if sys_paths:
            sys_list = "\n".join(sys_paths[:5])
            if len(sys_paths) > 5:
                sys_list += f"\n...等{len(sys_paths)}个系统目录"
            QMessageBox.critical(self, "⚠ 系统文件警告",
                f"选中的目录包含{len(sys_paths)}个系统重要文件：\n\n{sys_list}\n\n"
                f"迁移系统目录可能导致Windows异常！已取消迁移。")
            return
        if QMessageBox.question(self, "确认批量迁移",
            f"将迁移 {len(rows)} 个目录到 {self.cfg['g_root']}\n\n确定？",
            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.status_label.setText(f"正在批量迁移 {len(rows)} 个目录（后台执行）...")

        # 收集所有要迁移的路径（在主线程读表格，避免线程安全问题）
        migrate_items = []  # [(row, src_path), ...]
        for row in rows:
            src_path = self.table_scan.item(row, 0).text()
            migrate_items.append((row, src_path))

        class _BatchMigrateWorker(QThread):
            done_signal = Signal(list, list)  # (success_paths, fail_details)
            progress_signal = Signal(str, int, int)  # (当前路径, 当前序号, 总数)
            log_signal = Signal(str, str)  # (event_type, message) 复制进度
            def __init__(self, migrator, items):
                super().__init__()
                self.migrator = migrator
                self.items = items
            def run(self):
                success_paths = []
                fail_details = []
                total = len(self.items)
                # 设置 log_callback：复制进度实时输出到状态栏 + 监控日志
                def _batch_progress(event_type, message):
                    try:
                        self.log_signal.emit(event_type, message)
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                self.migrator.log_callback = _batch_progress
                try:
                    for i, (_, src_path) in enumerate(self.items, 1):
                        self.progress_signal.emit(src_path, i, total)
                        try:
                            ok, msg = self.migrator.migrate(src_path)
                            if ok:
                                success_paths.append(src_path)
                            else:
                                src_name = os.path.basename(src_path)
                                fail_details.append(f"• {src_name}:\n  {msg}")
                        except Exception as e:
                            src_name = os.path.basename(src_path)
                            fail_details.append(f"• {src_name}:\n  {e}")
                finally:
                    self.migrator.log_callback = None
                self.done_signal.emit(success_paths, fail_details)

        self._batch_migrate_worker = _BatchMigrateWorker(self.migrator, migrate_items)
        self._batch_migrate_worker.progress_signal.connect(
            lambda path, idx, total: self.status_label.setText(
                f"正在迁移 ({idx}/{total}): {path}"))
        def _on_batch_log(event_type, message):
            try:
                self.status_label.setText(message[:200])
                self._log_monitor(event_type, message)
            except Exception as e:
                log.debug("忽略异常: %s", e)
        self._batch_migrate_worker.log_signal.connect(_on_batch_log)

        def _on_batch_done(success_paths, fail_details):
            # 程序退出中：跳过弹窗和 UI 更新
            if getattr(self, '_force_quit', False):
                self._log_monitor("install",
                    f"程序退出中，批量迁移结果未弹窗: 成功 {len(success_paths)} 个")
                return
            success = len(success_paths)
            fail = len(fail_details)
            # 记录日志 + 自动配置开发工具环境变量
            for p in success_paths:
                self._log_monitor("install", f"迁移成功: {p}")
                self._auto_config_dev_env_after_migrate(p)
            for detail in fail_details:
                self._log_monitor("error", f"批量迁移失败: {detail}")
            # 清理目标盘符号链接残留（还原为真实空目录）
            try:
                cleaned, scanned, _ = self.migrator.cleanup_symlink_residues()
                if cleaned > 0:
                    self._log_monitor("install",
                        f"🔗 已清理目标盘 {cleaned} 个符号链接残留（还原为真实空目录）")
            except Exception as e:
                log.error(f"清理符号链接残留失败: {e}")
            # 弹结果
            if fail > 0:
                fail_text = "\n\n".join(fail_details[:10])
                if len(fail_details) > 10:
                    fail_text += f"\n\n... 还有 {len(fail_details) - 10} 个失败项未显示"
                QMessageBox.critical(self, "批量迁移完成（含失败）",
                    f"成功: {success} 个，失败: {fail} 个\n\n"
                    f"失败详情：\n{fail_text}")
            else:
                QMessageBox.information(self, "批量迁移完成",
                    f"成功: {success} 个\n失败: {fail} 个")
            # 不重新扫描，直接从待迁移表删除已迁移的行，添加到已迁移表
            if success_paths:
                self._move_rows_to_migrated(success_paths)
            self.status_label.setText(f"批量迁移完成: 成功 {success} 个，失败 {fail} 个")
            # 刷新待处理事务按钮
            try:
                self._update_pending_decisions_button()
            except Exception as e:
                log.debug("忽略异常: %s", e)
        self._batch_migrate_worker.done_signal.connect(_on_batch_done)
        self._batch_migrate_worker.start()

    def _move_rows_to_migrated(self, migrated_srcs):
        """批量迁移成功后：从待迁移表删除，添加到已迁移表（不重新扫描）"""
        # 从config.json读取这些迁移记录
        new_records = []
        for m in self.cfg.get("migrated", []):
            if m["src"] in migrated_srcs:
                new_records.append(m)
        # 从待迁移表删除匹配的行（倒序删除避免行号变化）
        rows_to_del = []
        for row in range(self.table_scan.rowCount()):
            path = self.table_scan.item(row, 0).text()
            if path in migrated_srcs:
                rows_to_del.append(row)
        # 批量操作前关闭已迁移表排序，避免插入时行序错乱
        sort_was_enabled = self.table_migrated.isSortingEnabled()
        self.table_migrated.setSortingEnabled(False)
        for row in reversed(rows_to_del):
            self.table_scan.removeRow(row)
        # 添加到已迁移表
        for m in new_records:
            row = self.table_migrated.rowCount()
            self.table_migrated.insertRow(row)
            item0 = QTableWidgetItem(m["src"])
            item0.setToolTip(m["src"])
            self.table_migrated.setItem(row, 0, item0)
            item1 = QTableWidgetItem(m["dst"])
            item1.setToolTip(m["dst"])
            self.table_migrated.setItem(row, 1, item1)
            size_val = m.get("size_mb", 0)
            si = NumericTableWidgetItem(_format_size(size_val))
            si.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            si.setData(Qt.UserRole, float(size_val))
            _apply_size_item_color(si, size_val)
            self.table_migrated.setItem(row, 2, si)
            st = QTableWidgetItem("正常")
            st.setForeground(QColor("#2E7D32"))
            st.setToolTip("符号链接有效，数据在目标盘")
            self.table_migrated.setItem(row, 3, st)
            # 链接目标列
            tgt_item = QTableWidgetItem(m["dst"])
            tgt_item.setToolTip(m["dst"])
            tgt_item.setForeground(QColor("#2E7D32"))
            self.table_migrated.setItem(row, 4, tgt_item)
            # 说明列
            desc_m = self._get_dir_description_safe(m["src"])
            item5 = QTableWidgetItem(desc_m)
            item5.setToolTip(desc_m if desc_m else os.path.basename(m["src"]))
            self.table_migrated.setItem(row, 5, item5)
            # 迁移时间列
            item6 = QTableWidgetItem(m.get("time", ""))
            item6.setToolTip(m.get("time", ""))
            self.table_migrated.setItem(row, 6, item6)
        if new_records:
            self.tabs.setCurrentIndex(0)
            self.status_label.setText(f"批量迁移成功 {len(new_records)} 个")
        # 批量插入完成，恢复排序状态
        if sort_was_enabled:
            self.table_migrated.setSortingEnabled(True)
        self._update_stats(migrated_count=self.table_migrated.rowCount(),
                           scan_count=self.table_scan.rowCount())
        # 清理 scan_cache 中已迁移项的残留记录
        try:
            scan_cache = self.cfg.get("scan_cache", [])
            migrated_set = set(migrated_srcs)
            new_cache = [c for c in scan_cache if c.get("path") not in migrated_set]
            if len(new_cache) != len(scan_cache):
                self.cfg["scan_cache"] = new_cache
                save_all(self.cfg)
        except Exception as e:
            log.error(f"清理 scan_cache 残留失败(批量): {e}")

    def _migrate_rows_to_custom(self, rows):
        """批量迁移到用户指定的盘和目录"""
        # 检查是否包含系统文件
        from utils import is_system_path
        sys_paths = []
        for row in rows:
            p = self.table_scan.item(row, 0).text()
            if is_system_path(p):
                sys_paths.append(p)
        if sys_paths:
            sys_list = "\n".join(sys_paths[:5])
            if len(sys_paths) > 5:
                sys_list += f"\n...等{len(sys_paths)}个系统目录"
            QMessageBox.critical(self, "系统文件警告",
                f"选中的目录包含{len(sys_paths)}个系统重要文件：\n\n{sys_list}\n\n"
                f"迁移系统目录可能导致Windows异常！已取消迁移。")
            return
        # 选择目标盘
        target_dir = QFileDialog.getExistingDirectory(
            self, f"选择迁移目标目录（{len(rows)}个目录将迁移到此目录下）",
            self.cfg["g_root"],
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks
        )
        if not target_dir:
            return
        # QFileDialog 在 Windows 上默认返回正斜杠，统一转为反斜杠
        target_dir = os.path.normpath(target_dir)
        if target_dir[0].upper() == "C":
            QMessageBox.warning(self, "提示", "不能迁移到C盘！")
            return
        if QMessageBox.question(self, "确认迁移",
            f"将迁移 {len(rows)} 个目录到:\n{target_dir}\n\n确定？",
            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.status_label.setText(f"正在批量迁移 {len(rows)} 个目录到 {target_dir}（后台执行）...")

        # 收集所有要迁移的路径（在主线程读表格，避免线程安全问题）
        migrate_items = []  # [(src_path, dst_path), ...]
        for row in rows:
            src_path = self.table_scan.item(row, 0).text()
            src_name = os.path.basename(src_path)
            dst_path = os.path.join(target_dir, src_name)
            migrate_items.append((src_path, dst_path))

        class _BatchMigrateCustomWorker(QThread):
            done_signal = Signal(list, list)  # (success_paths, fail_details)
            progress_signal = Signal(str, int, int)  # (当前路径, 当前序号, 总数)
            log_signal = Signal(str, str)  # (event_type, message) 复制进度
            def __init__(self, migrator, items):
                super().__init__()
                self.migrator = migrator
                self.items = items
            def run(self):
                success_paths = []
                fail_details = []
                total = len(self.items)
                def _custom_progress(event_type, message):
                    try:
                        self.log_signal.emit(event_type, message)
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                self.migrator.log_callback = _custom_progress
                try:
                    for i, (src_path, dst_path) in enumerate(self.items, 1):
                        self.progress_signal.emit(src_path, i, total)
                        try:
                            ok, msg = self.migrator.migrate(src_path, dst_path)
                            if ok:
                                success_paths.append(src_path)
                            else:
                                src_name = os.path.basename(src_path)
                                fail_details.append(f"• {src_name}:\n  {msg}")
                        except Exception as e:
                            src_name = os.path.basename(src_path)
                            fail_details.append(f"• {src_name}:\n  {e}")
                finally:
                    self.migrator.log_callback = None
                self.done_signal.emit(success_paths, fail_details)

        self._batch_migrate_custom_worker = _BatchMigrateCustomWorker(self.migrator, migrate_items)
        self._batch_migrate_custom_worker.progress_signal.connect(
            lambda path, idx, total: self.status_label.setText(
                f"正在迁移 ({idx}/{total}): {path}"))
        def _on_custom_log(event_type, message):
            try:
                self.status_label.setText(message[:200])
                self._log_monitor(event_type, message)
            except Exception as e:
                log.debug("忽略异常: %s", e)
        self._batch_migrate_custom_worker.log_signal.connect(_on_custom_log)

        def _on_custom_done(success_paths, fail_details):
            success = len(success_paths)
            fail = len(fail_details)
            for p in success_paths:
                self._log_monitor("install", f"迁移成功: {p}")
                self._auto_config_dev_env_after_migrate(p)
            for detail in fail_details:
                self._log_monitor("error", f"批量迁移失败: {detail}")
            if fail > 0:
                fail_text = "\n\n".join(fail_details[:10])
                if len(fail_details) > 10:
                    fail_text += f"\n\n... 还有 {len(fail_details) - 10} 个失败项未显示"
                QMessageBox.critical(self, "迁移完成（含失败）",
                    f"成功: {success} 个，失败: {fail} 个\n目标: {target_dir}\n\n"
                    f"失败详情：\n{fail_text}")
            else:
                QMessageBox.information(self, "迁移完成",
                    f"成功: {success} 个\n失败: {fail} 个\n目标: {target_dir}")
            if success_paths:
                self._move_rows_to_migrated(success_paths)
            self.status_label.setText(f"迁移完成: 成功 {success} 个，失败 {fail} 个")
        self._batch_migrate_custom_worker.done_signal.connect(_on_custom_done)
        self._batch_migrate_custom_worker.start()

    def restore_selected(self):
        row = self.table_migrated.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先在'已迁移'标签页选择要还原的目录")
            return
        # 防重入：如果上一个还原线程还在运行，拒绝启动新的
        # 避免两个 复制进程同时操作同一目录导致数据损坏
        if hasattr(self, '_restore_worker') and self._restore_worker and self._restore_worker.isRunning():
            QMessageBox.warning(self, "请稍候", "上一个还原任务还在执行中，请等待完成后再试。")
            return
        src_path = self.table_migrated.item(row, 0).text()
        if QMessageBox.question(self, "确认还原",
            f"确定要还原以下符号链接吗？数据将放回C盘。\n\n{src_path}\n\n"
            f"将执行：删除符号链接 -> 复制数据回C盘\n"
            f"后台执行，请稍候...",
            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        # 后台线程执行还原，避免 UI 卡死
        self.status_label.setText(f"正在还原 {src_path} ...")
        self._log_monitor("install", f"开始还原: {src_path}")

        class _RestoreWorker(QThread):
            done_signal = Signal(bool, str, str)  # (ok, msg, src_path)
            progress_signal = Signal(str, str)  # (event_type, message) 进度提示
            def __init__(self, migrator, path):
                super().__init__()
                self.migrator = migrator
                self.path = path
            def run(self):
                # 设置 log_callback：复制进度实时输出到状态栏 + 监控日志
                # （与待迁移区迁移同一套机制）
                def _restore_progress(event_type, message):
                    try:
                        self.progress_signal.emit(event_type, message)
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                self.migrator.log_callback = _restore_progress
                try:
                    ok, msg = self.migrator.restore(self.path)
                    self.done_signal.emit(ok, msg, self.path)
                except Exception as e:
                    self.done_signal.emit(False, str(e), self.path)
                finally:
                    self.migrator.log_callback = None

        self._restore_worker = _RestoreWorker(self.migrator, src_path)
        # 进度信号：更新状态栏（左下角）+ 监控日志
        def _on_restore_progress(event_type, message):
            try:
                self.status_label.setText(message[:200])
                self._log_monitor(event_type, message)
            except Exception as e:
                log.debug("忽略异常: %s", e)
        self._restore_worker.progress_signal.connect(_on_restore_progress)

        def _on_restore_done(ok, msg, path):
            if getattr(self, '_force_quit', False):
                self._log_monitor("install", f"程序退出中，还原结果未弹窗: {path} ok={ok}")
                return
            if ok:
                self._log_monitor("install", f"还原成功: {path}")
                # 还原后自动撤销对应开发工具的环境变量配置
                self._auto_unconfig_dev_env_after_restore(path)
                # 清理目标盘符号链接残留（还原为真实空目录）
                try:
                    cleaned, scanned, _ = self.migrator.cleanup_symlink_residues()
                    if cleaned > 0:
                        self._log_monitor("install",
                            f"🔗 已清理目标盘 {cleaned} 个符号链接残留（还原为真实空目录）")
                except Exception as e:
                    log.error(f"还原后清理符号链接残留失败: {e}")
                QMessageBox.information(self, "成功", msg)
                # 从已迁移表删除该行，添加到待迁移表（只刷新这一个目录）
                # 注意：row 可能已变化，按 src_path 查找当前行
                for r in range(self.table_migrated.rowCount()):
                    if self.table_migrated.item(r, 0) and self.table_migrated.item(r, 0).text() == path:
                        self.table_migrated.removeRow(r)
                        break
                self._move_row_to_scan(path)
                self.status_label.setText(f"还原完成: {path}")
            else:
                self._log_monitor("error", f"还原失败: {path} - 原因: {msg}")
                QMessageBox.critical(self, "失败", msg)
                self.status_label.setText(f"还原失败: {path}")
            # 无论成功失败都刷新待处理事务按钮
            try:
                self._update_pending_decisions_button()
            except Exception as e:
                log.debug("忽略异常: %s", e)

        self._restore_worker.done_signal.connect(_on_restore_done)
        self._restore_worker.start()

    def recover_deleted_links(self):
        """已迁移区「♻️ 删除记录恢复」按钮：按线索恢复被删除的链接+迁移记录。

        线索由「删除链接（保留目标数据）」操作记录（src/dst/时间/目标盘指纹），
        恢复时重算指纹：一致→自动恢复；不一致→二次确认；目标丢失→不可恢复。
        """
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
            QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
            QMessageBox,
        )
        # 删除记录恢复对话框：路径列 Interactive 可拖拽 + ElideNone 无省略号
        from i18n import tr as _tr
        try:
            records = self.migrator.list_deleted_links()
        except Exception as e:
            QMessageBox.warning(self, _tr("删除记录恢复"), f"{_tr('读取恢复线索失败')}: {e}")
            return
        if not records:
            QMessageBox.information(self, _tr("删除记录恢复"),
                _tr("没有可恢复的删除记录。\n"
                    "使用「删除记录（只删记录不动文件）」删除迁移记录时会自动记录线索，"
                    "之后可在此一键恢复。"))
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(_tr("删除记录恢复"))
        # 两列 400px 对称 + 其余列，需 1060 宽才无横向滚动条
        dlg.resize(1060, 420)
        layout = QVBoxLayout(dlg)
        tip = QLabel(_tr(
            "以下为删除记录时记录的恢复线索（目标盘数据保留）。"
            "选择后重新创建迁移记录（若 C 盘链接也已删则一并重建链接）；"
            "与删除时内容不一致的项需要确认后才会恢复。"))
        tip.setWordWrap(True)
        layout.addWidget(tip)
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels([
            _tr("恢复"), _tr("C盘路径"), _tr("目标盘路径"), _tr("删除时间"), _tr("校对状态")])
        # 路径列：两列完全对称（Interactive 可拖拽 + 400px 宽 + 无省略号）
        # 无省略号用 NoElideDelegate 绘制层强制（view 级 setTextElideMode 实测不生效）
        from ui_widgets import NoElideDelegate
        table.setTextElideMode(Qt.ElideNone)
        table.setItemDelegate(NoElideDelegate(table))
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Interactive)
        table.setColumnWidth(1, 400)
        table.setColumnWidth(2, 400)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        status_map = {
            "ok": "✅ " + _tr("一致，可恢复"),
            "diff": "⚠️ " + _tr("内容有差异"),
            "gone": "❌ " + _tr("目标丢失"),
        }
        for rec in records:
            row = table.rowCount()
            table.insertRow(row)
            cb = QCheckBox()
            cb.setEnabled(rec["status"] != "gone")
            table.setCellWidget(row, 0, cb)
            _src_item = QTableWidgetItem(rec.get("src", ""))
            _src_item.setToolTip(rec.get("src", ""))
            table.setItem(row, 1, _src_item)
            _dst_item = QTableWidgetItem(rec.get("dst", ""))
            _dst_item.setToolTip(rec.get("dst", ""))
            table.setItem(row, 2, _dst_item)
            table.setItem(row, 3, QTableWidgetItem(rec.get("time", "")))
            table.setItem(row, 4, QTableWidgetItem(status_map.get(rec["status"], "?")))
        layout.addWidget(table)

        btn_row = QHBoxLayout()
        btn_recover = QPushButton(_tr("恢复选中"))
        btn_close = QPushButton(_tr("关闭"))
        # 固定最小宽度：窗口拉窄时按钮文字不挤在一起
        btn_recover.setMinimumWidth(100)
        btn_close.setMinimumWidth(80)
        btn_row.addWidget(btn_recover)
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        def _do_recover():
            rows = [r for r in range(table.rowCount())
                    if isinstance(table.cellWidget(r, 0), QCheckBox)
                    and table.cellWidget(r, 0).isChecked()]
            if not rows:
                QMessageBox.warning(dlg, _tr("删除记录恢复"), _tr("请先勾选要恢复的项"))
                return
            diff_rows = [r for r in rows if records[r]["status"] == "diff"]
            if diff_rows:
                ret = QMessageBox.question(
                    dlg, _tr("确认恢复"),
                    _tr(f"有 {len(diff_rows)} 项目标盘内容与删除时不一致"
                        "（可能已被软件更新修改），仍要恢复这些链接吗？"))
                if ret != QMessageBox.StandardButton.Yes:
                    return
            ok_count, fail_list = 0, []
            for r in rows:
                rec = records[r]
                ok, msg = self.migrator.restore_deleted_link(
                    rec, force=(rec["status"] == "diff"))
                if ok:
                    ok_count += 1
                else:
                    fail_list.append(f"{rec.get('src', '')}: {msg}")
            if fail_list:
                QMessageBox.warning(dlg, _tr("删除记录恢复"),
                    _tr(f"成功 {ok_count} 个，失败 {len(fail_list)} 个") + ":\n"
                    + "\n".join(fail_list[:5]))
            else:
                QMessageBox.information(dlg, _tr("删除记录恢复"),
                    _tr(f"已恢复 {ok_count} 个链接与迁移记录"))
            dlg.accept()
            self._refresh_migrated_only()

        btn_recover.clicked.connect(_do_recover)
        btn_close.clicked.connect(dlg.reject)
        dlg.exec()

    def scan_orphan_data(self):
        """扫描软件卸载后数据残留：C 盘符号链接已消失但目标盘数据仍残留的迁移记录

        场景：软件自带的卸载程序只删了 C 盘符号链接（软件已卸载），
        目标盘的真实数据残留占用空间。
        本功能扫描所有迁移记录，找出这些残留数据并支持批量清理。

        扫描范围：所有盘符（C-Z）的目标路径。

        防误伤设计：
        - C 盘符号链接仍存在 → 正常，跳过
        - C 盘是真实目录（链接被覆盖）→ 走修复流程，跳过
        - C 盘路径不存在 + 目标盘有数据 → 列为残留
        - 重装系统检测：若 >50% 记录的 C 盘路径都不存在，弹严重警告
          （可能是重装系统后链接全丢，应先用"重建全部链接"而非删数据）

        中危-6 修复：扫描（os.path.exists + get_dir_size_fast）和清理
        （shutil.rmtree/unlink）原本在主线程执行，导致 UI 卡顿。
        现已移至后台线程，主线程仅负责 UI 交互。
        """
        migrated = self.cfg.get("migrated", [])
        if not migrated:
            QMessageBox.information(self, "提示", "暂无迁移记录，无需扫描卸载残留。")
            return

        # 第一遍扫描：统计 C 盘路径不存在的记录数（用于重装系统检测）
        # 仅 os.path.exists 调用，数量级小（migrated 记录数），保留主线程
        c_missing_count = 0
        for m in migrated:
            src = m.get("src", "")
            if src and not os.path.exists(src):
                c_missing_count += 1

        # 重装系统检测：超过 50% 记录的 C 盘路径都不存在
        # 这种场景应该用"重建全部链接"恢复，而不是删数据
        missing_ratio = c_missing_count / len(migrated) if migrated else 0
        if missing_ratio > 0.5:
            ret = QMessageBox.warning(self, "⚠️ 检测到可能重装了系统",
                f"共 {len(migrated)} 条迁移记录，其中 {c_missing_count} 条的 C 盘路径不存在"
                f"（占比 {missing_ratio*100:.0f}%）。\n\n"
                f"这通常表示重装了系统，C 盘符号链接全部丢失，但目标盘数据还在。\n"
                f"此时应该用「重建全部链接」恢复符号链接，而不是删除数据！\n\n"
                f"是否仍然继续扫描？（建议先点「重建全部链接」）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret != QMessageBox.Yes:
                return

        # 中危-6：第二遍扫描（含 get_dir_size_fast 大目录遍历）移至后台线程
        # 收集 migrated 副本传给 worker（避免线程间共享可变对象）
        migrated_snapshot = [
            {"src": m.get("src", ""), "dst": m.get("dst", ""),
             "desc": m.get("desc", ""), "time": m.get("time", "")}
            for m in migrated
        ]

        # 进度对话框（模态但非阻塞，因为 worker 在后台跑）
        progress = QProgressDialog("正在扫描卸载残留数据...", None, 0, 0, self)
        progress.setWindowTitle("扫描卸载残留")
        progress.setWindowModality(Qt.WindowModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.show()
        QApplication.processEvents()

        class _OrphanScanWorker(QThread):
            done_signal = Signal(list)
            def __init__(self, migrated_list):
                super().__init__()
                self.migrated_list = migrated_list
            def run(self):
                orphans = []
                for m in self.migrated_list:
                    src = m.get("src", "")
                    dst = m.get("dst", "")
                    if not src or not dst:
                        continue
                    # C 盘符号链接仍存在且有效 → 正常，跳过
                    if is_symlink(src):
                        continue
                    # C 盘路径存在（真实目录，链接被覆盖）→ 走修复流程，跳过
                    if os.path.exists(src):
                        continue
                    # C 盘路径不存在 → 检查目标盘数据是否仍存在
                    if not os.path.exists(dst):
                        continue  # 目标盘也没数据了，跳过
                    # 计算目标盘数据大小
                    try:
                        size_bytes = get_dir_size_fast(dst)
                    except Exception:
                        size_bytes = 0
                    orphans.append({
                        "src": src,
                        "dst": dst,
                        "size_bytes": size_bytes,
                        "desc": m.get("desc", ""),
                        "time": m.get("time", ""),
                    })
                self.done_signal.emit(orphans)

        self._orphan_scan_worker = _OrphanScanWorker(migrated_snapshot)

        def _on_scan_done(orphans):
            progress.close()
            if getattr(self, '_force_quit', False):
                return
            if not orphans:
                QMessageBox.information(self, "扫描完成",
                    "未发现卸载残留数据。\n\n所有迁移记录的 C 盘符号链接均正常，\n或目标盘数据已随链接一起清理。")
                return
            self._show_orphan_dialog(orphans)

        self._orphan_scan_worker.done_signal.connect(_on_scan_done, Qt.QueuedConnection)
        self._orphan_scan_worker.start()

    def _show_orphan_dialog(self, orphans):
        """显示卸载残留扫描结果对话框（由 scan_orphan_data 后台扫描完成后调用）"""

        # 弹出对话框显示扫描结果
        total_size = sum(o["size_bytes"] for o in orphans)
        total_size_str = _format_size(total_size)

        dlg = QDialog(self)
        dlg.setWindowTitle(f"卸载残留扫描结果（{len(orphans)} 项，共 {total_size_str}）")
        dlg.setMinimumSize(900, 500)
        dlg_layout = QVBoxLayout(dlg)

        # 说明文字
        info_label = QLabel(
            f"发现 {len(orphans)} 项卸载残留数据，共占用 {total_size_str} 空间。\n"
            f"这些数据是之前迁移到目标盘的，但 C 盘符号链接已消失（软件可能已卸载）。\n"
            f"勾选要清理的条目，点击[清理选中]删除目标盘残留数据。\n"
            f"⚠️ 如果只是临时删除了链接或重装了系统，请先点[重建全部链接]恢复，不要清理！")
        info_label.setStyleSheet("color: #424242; font-size: 13px; padding: 8px;")
        info_label.setWordWrap(True)
        dlg_layout.addWidget(info_label)

        # 表格
        table = QTableWidget(len(orphans), 6)
        table.setHorizontalHeaderLabels(["", "C盘原路径", "目标盘路径", "大小", "说明", "迁移时间"])
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)

        for i, orphan in enumerate(orphans):
            # 复选框列
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk_item.setCheckState(Qt.Checked)  # 默认勾选
            table.setItem(i, 0, chk_item)
            table.setItem(i, 1, QTableWidgetItem(orphan["src"]))
            table.setItem(i, 2, QTableWidgetItem(orphan["dst"]))
            size_item = NumericTableWidgetItem(orphan["size_bytes"])
            size_item.setText(_format_size(orphan["size_bytes"]))
            _apply_size_item_color(size_item, orphan["size_bytes"])
            table.setItem(i, 3, size_item)
            table.setItem(i, 4, QTableWidgetItem(orphan["desc"]))
            table.setItem(i, 5, QTableWidgetItem(orphan["time"]))

        dlg_layout.addWidget(table)

        # 全选/全不选按钮
        select_row = QHBoxLayout()
        btn_select_all = QPushButton("全选")
        btn_select_none = QPushButton("全不选")
        btn_select_all.clicked.connect(lambda: [
            table.item(r, 0).setCheckState(Qt.Checked) for r in range(table.rowCount())])
        btn_select_none.clicked.connect(lambda: [
            table.item(r, 0).setCheckState(Qt.Unchecked) for r in range(table.rowCount())])
        select_row.addWidget(btn_select_all)
        select_row.addWidget(btn_select_none)
        select_row.addStretch()
        dlg_layout.addLayout(select_row)

        # 按钮
        btn_box = QDialogButtonBox(QDialogButtonBox.Close)
        btn_clean = btn_box.addButton("清理选中", QDialogButtonBox.AcceptRole)
        dlg_layout.addWidget(btn_box)

        def _on_clean():
            # 收集勾选的条目
            selected = []
            for r in range(table.rowCount()):
                if table.item(r, 0).checkState() == Qt.Checked:
                    selected.append(orphans[r])
            if not selected:
                QMessageBox.information(dlg, "提示", "请勾选要清理的条目")
                return
            sel_size = _format_size(sum(o["size_bytes"] for o in selected))
            if QMessageBox.warning(dlg, "⚠️ 确认清理（不可恢复）",
                f"将删除以下 {len(selected)} 项目标盘残留数据（共 {sel_size}）？\n\n"
                f"⚠️ 此操作不可恢复，删除后数据无法找回！\n"
                f"同时会从迁移记录中移除这些条目。\n\n"
                f"如果只是临时删除了链接或重装了系统，\n"
                f"请点[否]取消，改用[重建全部链接]恢复符号链接。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            # 中危-6：清理（shutil.rmtree/unlink）移至后台线程，避免大目录删除时 UI 卡顿
            clean_progress = QProgressDialog(f"正在清理 {len(selected)} 项残留数据...", None, 0, 0, dlg)
            clean_progress.setWindowTitle("清理卸载残留")
            clean_progress.setWindowModality(Qt.WindowModal)
            clean_progress.setCancelButton(None)
            clean_progress.setMinimumDuration(0)
            clean_progress.show()
            QApplication.processEvents()

            class _OrphanCleanWorker(QThread):
                done_signal = Signal(int, list, list)  # (cleaned, failed, success_srcs)
                def __init__(self, items):
                    super().__init__()
                    self.items = items
                def run(self):
                    cleaned = 0
                    failed = []
                    success_srcs = []
                    from pathlib import Path
                    for orphan in self.items:
                        try:
                            dst_path = Path(orphan["dst"])
                            if dst_path.exists():
                                for item in dst_path.iterdir():
                                    if item.is_dir() and not item.is_symlink():
                                        shutil.rmtree(item, ignore_errors=True)
                                    else:
                                        try:
                                            item.unlink()
                                        except Exception as e:
                                            log.debug("忽略异常: %s", e)
                            # 删除空目录本身
                            try:
                                dst_path.rmdir()
                            except Exception:
                                pass  # 目录非空（删除失败）或不存在，忽略
                            cleaned += 1
                            success_srcs.append(orphan["src"])
                            log.info(f"清理卸载残留: {orphan['dst']} (原C盘: {orphan['src']})")
                        except Exception as e:
                            failed.append((orphan["dst"], str(e)))
                            log_error_with_reason("清理卸载残留失败", str(e), orphan["dst"])
                    self.done_signal.emit(cleaned, failed, success_srcs)

            self._orphan_clean_worker = _OrphanCleanWorker(selected)

            def _on_clean_done(cleaned, failed, success_srcs):
                clean_progress.close()
                if getattr(self, '_force_quit', False):
                    return
                # 主线程：根据成功清理的 src 列表更新 cfg
                if success_srcs:
                    success_set = set(success_srcs)
                    self.cfg["migrated"] = [
                        m for m in self.cfg.get("migrated", [])
                        if m.get("src") not in success_set
                    ]
                    save_all(self.cfg)
                    self.refresh()

                msg = f"已清理 {cleaned}/{len(selected)} 项卸载残留"
                if failed:
                    msg += f"\n\n失败 {len(failed)} 项:\n"
                    for dst, err in failed[:5]:
                        msg += f"  {dst}: {err[:60]}\n"
                    if len(failed) > 5:
                        msg += f"  ...等 {len(failed)} 项"
                QMessageBox.information(dlg, "清理完成", msg)
                dlg.accept()

            self._orphan_clean_worker.done_signal.connect(_on_clean_done, Qt.QueuedConnection)
            self._orphan_clean_worker.start()

        btn_clean.clicked.connect(_on_clean)
        btn_box.rejected.connect(dlg.reject)

        dlg.exec()

    def delete_link(self):
        """删除符号链接（保留目标数据，只删除C盘链接）"""
        row = self.table_migrated.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先在'已迁移'标签页选择要操作的目录")
            return
        src_path = self.table_migrated.item(row, 0).text()
        dst_path = self.table_migrated.item(row, 1).text()
        if not is_symlink(src_path):
            QMessageBox.warning(self, "警告", f"该目录不是符号链接:\n{src_path}")
            return
        if QMessageBox.question(self, "确认删除链接",
            f"删除以下符号链接？\n\nC盘链接: {src_path}\n指向: {dst_path}\n\n"
            f"G盘数据保留不删除，C盘链接删除后该位置为空。\n"
            f"（软件重新运行时会在C盘重新创建真实目录）",
            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            os.rmdir(src_path)
            # 从config中移除记录
            self.cfg["migrated"] = [m for m in self.cfg["migrated"] if m["src"] != src_path]
            save_all(self.cfg)
            log.info(f"删除符号链接: {src_path} -> {dst_path}")
            self._log_link("删除链接", src_path, dst_path, "保留目标数据")
            # 撤销对应开发工具的环境变量配置（链接已删除）
            self._unconfig_dev_env_for_path(src_path)
            QMessageBox.information(self, "成功", f"符号链接已删除\nG盘数据保留在: {dst_path}")
        except Exception as e:
            self._log_link("删除链接失败", src_path, dst_path, str(e))
            log_error_with_reason("删除链接失败", str(e), f"删除链接: {src_path}")
            QMessageBox.critical(self, "失败", f"删除失败: {e}")
        self.refresh()

    def rebuild_link(self):
        """重建符号链接（数据已在G盘，在C盘重新创建链接）"""
        # 优先用已迁移表中选中的行（链接已断的情况）
        row = self.table_migrated.currentRow()
        if row >= 0:
            src_path = self.table_migrated.item(row, 0).text()
            dst_path = self.table_migrated.item(row, 1).text()
        else:
            # 从待迁移表选（手动指定G盘目标）
            row = self.table_scan.currentRow()
            if row < 0:
                QMessageBox.information(self, "提示",
                    "请在'已迁移'标签页选择断链的目录，\n或在'待迁移'标签页选择C盘目录后手动输入G盘目标。")
                return
            src_path = self.table_scan.item(row, 0).text()
            dst_path, _ = QFileDialog.getOpenFileName(self, "这个功能需要从已迁移表操作", "", "")
            return
        # 检查G盘目标是否存在
        if not os.path.exists(dst_path):
            QMessageBox.critical(self, "错误", f"G盘目标目录不存在:\n{dst_path}")
            return
        # 如果C盘已存在（非符号链接），先删除
        if os.path.exists(src_path) and not is_symlink(src_path):
            if QMessageBox.question(self, "C盘已有真实目录",
                f"C盘已有真实目录:\n{src_path}\n\n"
                f"需要先删除C盘真实目录才能创建符号链接。\n"
                f"（G盘已有数据，C盘目录将被删除）\n\n确定？",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            try:
                shutil.rmtree(src_path)
            except Exception as e:
                QMessageBox.critical(self, "失败", f"删除C盘真实目录失败: {e}")
                return
        # 如果已是符号链接，先删除旧的
        if is_symlink(src_path):
            try:
                os.rmdir(src_path)
            except Exception:
                os.unlink(src_path)
        # 创建符号链接
        try:
            subprocess.run(["cmd", "/c", "mklink", "/D", src_path, dst_path],
                capture_output=True, check=True,
                creationflags=0x08000000)  # CREATE_NO_WINDOW 避免弹黑窗
            # 更新config记录
            record = {
                "src": src_path, "dst": dst_path,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "size_mb": get_dir_size_fast(dst_path)
            }
            # 移除旧记录，添加新记录
            self.cfg["migrated"] = [m for m in self.cfg["migrated"] if m["src"] != src_path]
            self.cfg["migrated"].append(record)
            save_all(self.cfg)
            log.info(f"重建符号链接: {src_path} -> {dst_path}")
            self._log_link("重建链接", src_path, dst_path, f"{get_dir_size_fast(dst_path)}MB")
            # 更新开发环境配置中的 target_path（如果换到了新位置）
            self._update_dev_env_target_path(src_path, dst_path)
            QMessageBox.information(self, "成功", f"符号链接已重建\n{src_path} -> {dst_path}")
        except subprocess.CalledProcessError as e:
            self._log_link("重建链接失败", src_path, dst_path, str(e))
            log_error_with_reason("重建链接失败", str(e), f"重建: {src_path} -> {dst_path}")
            QMessageBox.critical(self, "失败", f"创建符号链接失败: {e}\n请确保以管理员权限运行")
        except Exception as e:
            self._log_link("重建链接失败", src_path, dst_path, str(e))
            log_error_with_reason("重建链接失败", str(e), f"重建: {src_path} -> {dst_path}")
            QMessageBox.critical(self, "失败", f"重建失败: {e}")
        self.refresh()

    def open_dir(self):
        row = self.table_migrated.currentRow()
        if row >= 0:
            path = self.table_migrated.item(row, 1).text()
        else:
            row = self.table_scan.currentRow()
            if row >= 0:
                path = self.table_scan.item(row, 0).text()
            else:
                QMessageBox.information(self, "提示", "请先选择一个目录")
                return
        self._open_path(path)

    def _open_path(self, path):
        """打开指定路径"""
        path = str(path)
        if os.path.exists(path):
            os.startfile(path)
        else:
            QMessageBox.warning(self, "警告", f"路径不存在: {path}")

    def browse_dir(self):
        """浏览选择C盘目录并迁移"""
        path = QFileDialog.getExistingDirectory(
            self, "选择要迁移的C盘目录",
            os.environ.get("LOCALAPPDATA", "C:\\"),
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks
        )
        if not path:
            return
        # QFileDialog 在 Windows 上默认返回正斜杠，统一转为反斜杠
        path = path.replace("/", "\\")
        # 检查是否在C盘
        if not path[0].upper() in ("C",):
            QMessageBox.warning(self, "提示", f"请选择C盘目录，当前选择: {path}")
            return
        # 检查是否已是符号链接
        if is_symlink(path):
            QMessageBox.information(self, "提示", f"该目录已是符号链接:\n{path}")
            return
        # 确认
        size = get_dir_size_fast(path)
        if QMessageBox.question(self, "确认迁移",
            f"目录: {path}\n大小: {size} MB\n\n"
            f"将执行：数据同步 → 删除C盘原目录 → 创建符号链接到G盘\n\n确定迁移？",
            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.status_label.setText(f"正在迁移 {path}...")
        self._log_monitor("install", f"开始迁移: {path}")

        # 后台线程执行迁移，避免 UI 卡死
        class _BrowseMigrateWorker(QThread):
            done_signal = Signal(bool, str, str)  # (ok, msg, src_path)
            log_signal = Signal(str, str)  # (event_type, message)
            def __init__(self, migrator, path):
                super().__init__()
                self.migrator = migrator
                self.path = path
            def run(self):
                def _browse_progress(event_type, message):
                    try:
                        self.log_signal.emit(event_type, message)
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                self.migrator.log_callback = _browse_progress
                try:
                    ok, msg = self.migrator.migrate(self.path)
                    self.done_signal.emit(ok, msg, self.path)
                except Exception as e:
                    self.done_signal.emit(False, str(e), self.path)
                finally:
                    self.migrator.log_callback = None

        self._browse_migrate_worker = _BrowseMigrateWorker(self.migrator, path)
        def _on_browse_log(event_type, message):
            try:
                self.status_label.setText(message[:200])
                self._log_monitor(event_type, message)
            except Exception as e:
                log.debug("忽略异常: %s", e)
        self._browse_migrate_worker.log_signal.connect(_on_browse_log)
        def _on_browse_done(ok, msg, src_path):
            # 程序退出中：跳过弹窗和 UI 更新
            if getattr(self, '_force_quit', False):
                self._log_monitor("install",
                    f"程序退出中，浏览迁移结果未弹窗: {src_path} ok={ok}")
                return
            if ok:
                self._log_monitor("install", f"迁移成功: {src_path}")
                # 数据迁移成功后，自动检测并配置对应开发工具的环境变量（全自动）
                self._auto_config_dev_env_after_migrate(src_path)
                QMessageBox.information(self, "成功", msg)
            else:
                self._log_monitor("error", f"迁移失败: {src_path} - 原因: {msg}")
                QMessageBox.critical(self, "失败", msg)
            self.refresh()
        self._browse_migrate_worker.done_signal.connect(_on_browse_done)
        self._browse_migrate_worker.start()

    def _browse_migrate_dir(self):
        """选择迁移目标目录"""
        cur = self._migrate_dir_label.text() or "D:\\"
        d = QFileDialog.getExistingDirectory(
            self, "选择迁移目标目录", cur,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks
        )
        if d:
            # QFileDialog 在 Windows 上默认返回正斜杠，统一转为反斜杠
            d = d.replace("/", "\\").rstrip("\\") + "\\"
            self._migrate_dir_label.setText(d)
