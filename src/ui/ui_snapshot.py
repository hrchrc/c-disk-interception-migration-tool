#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置快照功能 Handler(从 main.py 抽出)

包含 4 个方法:
- _first_run_auto_snapshot: 首次运行自动快照
- _collect_all_env_var_names: 收集所有环境变量名
- _create_dev_env_snapshot: 创建快照
- _view_dev_env_snapshots: 查看快照列表并恢复

这些方法原属 MainWindow,抽取为 Handler 以降低 main.py 体量。
方法内通过 self 访问 MainWindow 的属性和其他方法,运行时由 MainWindow 提供。
"""
import os
import logging

from PySide6.QtWidgets import (QMessageBox, QDialog, QVBoxLayout, QHBoxLayout,
                                QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                                QHeaderView, QDialogButtonBox, QInputDialog)
from PySide6.QtCore import Qt

import dev_env_snapshot as dev_snapshot
from dev_env_migrate import (
    TOOLS as DEV_TOOLS,
    collect_original_dir_structure as dev_collect_original_dir_structure,
)
from config import log_error_with_reason, save_all

log = logging.getLogger('CDriveRelocator')


class SnapshotHandler:
    """配置快照功能 Handler"""

    def _first_run_auto_snapshot(self):
        """首次在本机运行时自动创建/恢复首个原始快照

        判定逻辑（基于 _initial 快照是否存在 + 隐藏备份是否存在）：
        - 有 _initial 快照 → 已有首个，跳过
        - 无 _initial + 有隐藏备份 → 首个被删，从备份完整恢复（内容/UUID/时间戳不变）
        - 无 _initial + 无隐藏备份 → 真正首次运行，创建首个并写备份
        """
        try:
            snap_dir = dev_snapshot._snapshots_dir()

            # 检查是否存在 _initial 快照（首个原始快照）
            has_initial = any(
                dev_snapshot._INITIAL_SUFFIX in p.stem
                for p in snap_dir.glob("*.json")
            )
            if has_initial:
                # 已有首个原始快照，无需操作
                return

            # 检查是否有隐藏备份
            has_backup = dev_snapshot.has_original_backup()

            if has_backup:
                # 场景2：首个被删，从备份完整恢复（不是重新生成）
                success, result = dev_snapshot._restore_from_backup()
                if success:
                    log.info(f"首个原始快照已从备份恢复: {result}")
                    self.on_monitor_log("dev_env",
                        f"📸 检测到首个原始快照缺失，已从隐藏备份完整恢复: {result}（内容/UUID/时间戳与原始一致）")
                    self.status_label.setText(
                        f"首个原始快照已从备份恢复: {result}")
                else:
                    log.warning(f"从备份恢复首个快照失败: {result}")
            else:
                # 场景3：真正首次运行，创建首个并写备份
                configured = self.cfg.get("dev_env_configured", {})
                env_var_names = self._collect_all_env_var_names()
                try:
                    original_dirs = dev_collect_original_dir_structure()
                except Exception as e:
                    log.error(f"收集原始目录结构失败: {e}")
                    original_dirs = []
                note = "首次运行自动快照（系统初始状态）"
                success, result = dev_snapshot.create_initial_snapshot(
                    configured, env_var_names, note, original_dirs)
                if success:
                    log.info(f"首个原始快照已创建: {result}")
                    self.on_monitor_log("dev_env",
                        f"📸 首次运行自动快照已创建: {result}（作为还原底线）")
                    self.status_label.setText(
                        f"首次运行已自动创建初始快照: {result}")
                else:
                    log.warning(f"首个原始快照创建失败: {result}")
        except Exception as e:
            log.error(f"首个原始快照创建/恢复异常: {e}")

    def _collect_all_env_var_names(self):
        """从 DEV_TOOLS 收集所有相关环境变量名"""
        names = set()
        for tool in DEV_TOOLS:
            for ev in tool["env_vars"]:
                names.add(ev["name"])
        return list(names)

    def _create_dev_env_snapshot(self):
        """创建快照（开发环境迁移区：环境变量 + 配置记录 + 原始目录结构）"""
        # 弹输入框让用户填备注
        from PySide6.QtWidgets import QInputDialog
        note, ok = QInputDialog.getText(self, "📸 保存快照",
            "请输入快照备注（类似 commit message）：\n"
            "（例如：配置了 npm/cargo/go 到 D 盘）\n\n"
            "快照保存（仅开发环境迁移区）：\n"
            "  • 开发环境变量\n"
            "  • 开发环境配置记录\n"
            "  • 原始目录结构（未迁移前的 C 盘路径状态）\n"
            "不保存已迁移区/待迁移区的数据。",
            text="")
        if not ok:
            return
        # 收集当前配置（开发环境迁移区）
        configured = self.cfg.get("dev_env_configured", {})
        env_var_names = self._collect_all_env_var_names()
        # 收集原始目录结构（主线程同步执行，目录少时无明显卡顿）
        try:
            original_dirs = dev_collect_original_dir_structure()
        except Exception as e:
            log.error(f"收集原始目录结构失败: {e}")
            original_dirs = []
        # 创建快照（只传 original_dirs，不传 migrated/scan_cache）
        success, result = dev_snapshot.create_snapshot(
            configured, env_var_names, note, original_dirs=original_dirs)
        if success:
            count = dev_snapshot.get_snapshot_count()
            self.status_label.setText(f"快照已保存：{result}（共 {count} 个快照）")
            self.on_monitor_log("dev_env",
                f"📸 保存快照 {result}（备注: {note or '无'}，"
                f"环境变量{len(env_var_names)}个，配置记录{len(configured)}条，"
                f"原始目录{len(original_dirs)}个，共 {count} 个快照）")
            QMessageBox.information(self, "保存成功",
                f"✓ 快照已保存：{result}\n"
                f"备注：{note or '（无）'}\n"
                f"保存内容（仅开发环境迁移区）：\n"
                f"  • 开发环境变量：{len(env_var_names)} 个\n"
                f"  • 开发环境配置记录：{len(configured)} 条\n"
                f"  • 原始目录结构：{len(original_dirs)} 个\n\n"
                f"当前共 {count} 个快照（上限 500 个，超出自动删除最旧的）")
        else:
            self.status_label.setText(f"快照保存失败: {result}")
            self.on_monitor_log("dev_env", f"✗ 快照保存失败: {result}")
            QMessageBox.warning(self, "保存失败", f"快照保存失败: {result}")

    def _view_dev_env_snapshots(self):
        """查看历史快照对话框（统一还原入口）
        - 恢复此快照：还原环境变量 + 配置记录
        - 还原到默认状态：批量回滚所有已配置工具（调用 _rollback_all_dev_env）
        - 查看详情 / 删除快照
        """
        snapshots = dev_snapshot.list_snapshots()
        if not snapshots:
            QMessageBox.information(self, "无快照",
                "暂无历史快照。\n\n点击『📸 保存快照』创建第一个快照。")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"📋 快照管理（共 {len(snapshots)} 个，上限 500 个）")
        dlg.resize(850, 500)
        layout = QVBoxLayout(dlg)

        # 说明文字（用 QTextEdit 保持换行格式 + 提供滚动条）
        from PySide6.QtWidgets import QTextEdit
        hint = QTextEdit()
        hint.setReadOnly(True)
        hint.setFixedHeight(95)
        hint.setPlainText(
            "💡 仿 GitHub commit 的还原配置入口（仅开发环境迁移区）：\n"
            "  • 双击行可查看快照详情（环境变量值、配置记录、原始目录结构）\n"
            "  • 『⏪ 恢复此快照』还原环境变量+配置记录\n"
            "  • 『🔄 还原到默认状态』批量回滚所有已配置工具（清空开发环境配置）\n"
            "  • 🛡️ 标记的是首个原始快照（软件首次运行时的状态，作为还原底线）\n"
            "  • 『⭐ 加星』『🏷 标签』可标记常用快照，『只看星标』快速筛选\n"
            "  • 恢复前会自动创建当前状态的快照（便于再次撤销）")
        hint.setStyleSheet(
            "QTextEdit { color: #555; background-color: #fafafa; "
            "border: 1px solid #e0e0e0; padding: 4px; }")
        layout.addWidget(hint)

        # 快照列表表格（含"原始目录数"列）
        tbl = QTableWidget(len(snapshots), 7)
        tbl.setHorizontalHeaderLabels(
            ["时间", "备注", "环境变量", "配置记录", "原始目录", "文件名", "标记"])
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        tbl.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        tbl.setSelectionBehavior(QTableWidget.SelectRows)
        tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        for i, snap in enumerate(snapshots):
            tbl.setItem(i, 0, QTableWidgetItem(snap["created_time"]))
            tbl.setItem(i, 1, QTableWidgetItem(snap["note"] or "(无备注)"))
            tbl.setItem(i, 2, QTableWidgetItem(str(snap["env_count"])))
            tbl.setItem(i, 3, QTableWidgetItem(str(snap["configured_count"])))
            tbl.setItem(i, 4, QTableWidgetItem(str(snap.get("original_dirs_count", 0))))
            tbl.setItem(i, 5, QTableWidgetItem(snap["filename"]))
            # 标记列：⭐星标 + 🛡️原始 + 🏷标签 组合显示
            parts = []
            if snap.get("starred"):
                parts.append("⭐")
            if snap["is_first"]:
                parts.append("🛡️ 原始" if snap.get("is_protected", False) else "原始")
            if snap.get("tag"):
                parts.append(f"🏷 {snap['tag']}")
            tbl.setItem(i, 6, QTableWidgetItem(" ".join(parts)))
        layout.addWidget(tbl, 1)

        # 按钮区
        btn_row = QHBoxLayout()
        btn_view = QPushButton("🔍 查看详情")
        btn_restore = QPushButton("⏪ 恢复此快照")
        btn_restore.setStyleSheet("QPushButton { background-color: #FF9800; color: white; font-weight: bold; }")
        btn_rollback = QPushButton("🔄 还原到默认状态")
        btn_rollback.setStyleSheet("QPushButton { background-color: #C62828; color: white; font-weight: bold; }")
        btn_rollback.setToolTip("批量回滚所有已配置工具：删除环境变量 + 撤销配置命令 + 还原 Maven/Bazel 配置文件\n"
            "目标盘数据目录保留不动（需手动删除）")
        btn_star = QPushButton("⭐ 加星")
        btn_star.setToolTip("为选中快照加星标/取消星标（方便快速查找）")
        btn_tag = QPushButton("🏷 标签")
        btn_tag.setToolTip("为选中快照编辑自定义标签")
        btn_filter_star = QPushButton("只看星标")
        btn_filter_star.setCheckable(True)
        btn_filter_star.setToolTip("只显示已加星标的快照")
        btn_delete = QPushButton("🗑 删除")
        btn_close = QPushButton("关闭")
        btn_row.addWidget(btn_view)
        btn_row.addWidget(btn_restore)
        btn_row.addWidget(btn_rollback)
        btn_row.addWidget(btn_star)
        btn_row.addWidget(btn_tag)
        btn_row.addWidget(btn_filter_star)
        btn_row.addWidget(btn_delete)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        def _view_detail():
            rows = {idx.row() for idx in tbl.selectedIndexes()}
            if not rows:
                QMessageBox.information(dlg, "提示", "请先选择一行")
                return
            row = sorted(rows)[0]
            snap = snapshots[row]
            data = dev_snapshot.load_snapshot(snap["filename"])
            if not data:
                QMessageBox.warning(dlg, "错误", "无法加载快照数据")
                return
            # 弹详情对话框
            detail_lines = [
                f"📅 创建时间: {data.get('created_time', '')}",
                f"📝 备注: {data.get('note', '(无)')}",
                f"📁 文件名: {snap['filename']}",
            ]
            if snap.get("starred"):
                detail_lines.append("⭐ 已加星标")
            if snap.get("tag"):
                detail_lines.append(f"🏷 标签: {snap['tag']}")
            detail_lines.extend([
                "",
                "=== 环境变量值 ===",
            ])
            env_values = data.get("env_values", {})
            if not env_values:
                detail_lines.append("(空)")
            else:
                for name, val in env_values.items():
                    if val is None:
                        detail_lines.append(f"  {name} = (未设置)")
                    else:
                        detail_lines.append(f"  {name} = {val}")
            detail_lines.append("")
            detail_lines.append("=== 开发环境配置记录 ===")
            records = data.get("configured_records", {})
            if not records:
                detail_lines.append("(无配置记录)")
            else:
                for tid, rec in records.items():
                    detail_lines.append(f"  • {rec.get('name', tid)} → {rec.get('target_path', '?')}")
            detail_lines.append("")
            detail_lines.append("=== 原始目录结构（未迁移前的 C 盘路径状态） ===")
            original_dirs = data.get("original_dirs", [])
            if not original_dirs:
                detail_lines.append("(无原始目录结构记录)")
            else:
                for d in original_dirs:
                    path = d.get("original_path", "")
                    if not path:
                        continue  # 跳过空路径
                    status_parts = []
                    if d.get("exists"):
                        if d.get("is_symlink"):
                            status_parts.append(f"符号链接→{d.get('symlink_target', '?')}")
                        else:
                            status_parts.append("真实目录")
                    else:
                        status_parts.append("不存在")
                    if d.get("on_c"):
                        status_parts.append("C盘")
                    status = " | ".join(status_parts)
                    detail_lines.append(
                        f"  • [{d.get('category', '')}] {d.get('name', '')}\n"
                        f"    路径: {path}\n"
                        f"    状态: {status}")
            # 兼容旧快照字段（新快照不再保存 migrated_records/scan_cache）
            legacy_migrated = data.get("migrated_records")
            legacy_scan_cache = data.get("scan_cache")
            if legacy_migrated or legacy_scan_cache:
                detail_lines.append("")
                detail_lines.append("=== 旧版字段（仅供兼容，新快照不再保存） ===")
                if legacy_migrated:
                    detail_lines.append(f"  • migrated_records: {len(legacy_migrated)} 条（属于已迁移区）")
                if legacy_scan_cache:
                    detail_lines.append(f"  • scan_cache: {len(legacy_scan_cache)} 条（属于待迁移区）")
            # 用 QTextEdit 显示，避免 QMessageBox 内容过长被截断
            from PySide6.QtWidgets import QTextEdit, QDialog as _QD, QVBoxLayout as _VL, QPushButton as _PB
            detail_dlg = _QD(dlg)
            detail_dlg.setWindowTitle(f"快照详情 - {snap['created_time']}")
            detail_dlg.resize(720, 560)
            dl_layout = _VL(detail_dlg)
            te = QTextEdit()
            te.setReadOnly(True)
            te.setPlainText("\n".join(detail_lines))
            dl_layout.addWidget(te)
            btn_ok = _PB("关闭")
            btn_ok.clicked.connect(detail_dlg.accept)
            dl_layout.addWidget(btn_ok)
            detail_dlg.exec()

        def _restore():
            rows = {idx.row() for idx in tbl.selectedIndexes()}
            if not rows:
                QMessageBox.information(dlg, "提示", "请先选择一行")
                return
            row = sorted(rows)[0]
            snap = snapshots[row]

            # 恢复前确认
            reply = QMessageBox.question(dlg, "⚠️ 确认恢复快照",
                f"即将恢复到快照：\n"
                f"  时间: {snap['created_time']}\n"
                f"  备注: {snap['note'] or '(无)'}\n\n"
                f"恢复操作（仅开发环境迁移区）：\n"
                f"  • 把所有相关环境变量还原到快照时的值\n"
                f"  • 快照后新增的环境变量会被删除\n"
                f"  • 配置记录还原为快照时的状态\n\n"
                f"⚠️ 恢复前会自动创建当前状态的快照（便于再次撤销）。\n"
                f"确认恢复吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
            # 先保存当前状态快照（只传 original_dirs，不传 migrated/scan_cache）
            configured = self.cfg.get("dev_env_configured", {})
            env_var_names = self._collect_all_env_var_names()
            try:
                cur_original_dirs = dev_collect_original_dir_structure()
            except Exception as e:
                log.error(f"恢复前收集原始目录结构失败: {e}")
                cur_original_dirs = []
            dev_snapshot.create_snapshot(configured, env_var_names,
                note=f"恢复前的自动备份（恢复到 {snap['timestamp']}）",
                original_dirs=cur_original_dirs)
            # 执行恢复（不还原迁移记录）
            # H7：restore_snapshot 现在返回完整快照数据，无需重复 load_snapshot
            ok, msg, data = dev_snapshot.restore_snapshot(
                snap["filename"], env_var_names, restore_migrated=False)
            # 恢复配置记录（configured_records 与环境变量同步，避免状态分裂）
            if data and "configured_records" in data:
                self.cfg["dev_env_configured"] = data["configured_records"]
            # 同步 Maven/Bazel 配置文件（快照不保存配置文件内容，需根据 configured_records 重新应用）
            # 避免 dev_env_configured 记录与 settings.xml/.bazelrc 真实状态脱节
            # 仅在快照数据有效时同步，data 为 None（快照损坏）时跳过，避免错误 unconfigure
            if data:
                try:
                    from dev_env_migrate import (
                        _configure_maven_settings, _unconfigure_maven_settings,
                        _configure_bazelrc, _unconfigure_bazelrc,
                    )
                    snap_configured = data.get("configured_records", {})
                    # Maven：快照有记录则重新配置 localRepository，无记录则还原
                    maven_cfg = snap_configured.get("maven_repo")
                    if maven_cfg:
                        maven_drive = (maven_cfg.get("target_drive") or "D").upper()
                        if len(maven_drive) == 1 and maven_drive.isalpha():
                            _configure_maven_settings(
                                maven_drive + ":",
                                repo_path_override=maven_cfg.get("target_path") or None)
                    else:
                        _unconfigure_maven_settings()
                    # Bazel：同上
                    bazel_cfg = snap_configured.get("bazel_output")
                    if bazel_cfg:
                        bazel_drive = (bazel_cfg.get("target_drive") or "D").upper()
                        if len(bazel_drive) == 1 and bazel_drive.isalpha():
                            _configure_bazelrc(
                                bazel_drive + ":",
                                root_path_override=bazel_cfg.get("target_path") or None)
                    else:
                        _unconfigure_bazelrc()
                except Exception as e:
                    log.error(f"恢复快照后同步 Maven/Bazel 配置文件失败: {e}")
            try:
                save_all(self.cfg)
            except Exception as e:
                log.error(f"保存配置失败: {e}")
            if ok:
                self.on_monitor_log("dev_env",
                    f"⏪ 恢复快照 {snap['filename']} 成功")
                QMessageBox.information(dlg, "恢复成功",
                    f"✓ 已恢复到快照 {snap['created_time']}\n\n{msg}\n\n"
                    f"⚠️ 请重新打开终端/编辑器让环境变量生效。\n"
                    f"当前状态的快照已自动保存（便于撤销恢复）。")
                dlg.accept()
                # 刷新开发环境迁移区表格
                try:
                    self._refresh_dev_env_table()
                except Exception as e:
                    log.error(f"恢复快照后刷新开发环境迁移区失败: {e}")
                # 联动刷新已迁移区和待迁移区
                # 恢复快照会改变 dev_env_configured，影响待迁移区的「[已配置]」标记
                # 已迁移区不受 dev_env_configured 影响，但为保险起见也刷新
                try:
                    self._refresh_migrated_only()
                except Exception as e:
                    log.error(f"恢复快照后刷新已迁移区失败: {e}")
                try:
                    self._light_refresh_scan_table()
                except Exception as e:
                    log.error(f"恢复快照后刷新待迁移区失败: {e}")
            else:
                self.on_monitor_log("dev_env",
                    f"⏩ 恢复快照 {snap['filename']} 部分失败")
                QMessageBox.warning(dlg, "恢复完成（有失败项）", msg)

        def _rollback_to_default():
            """还原到默认状态：调用 _rollback_all_dev_env 批量回滚所有配置"""
            dlg.accept()  # 先关闭快照对话框，让回滚对话框可以正常显示
            # 延迟调用，确保对话框已关闭
            from PySide6.QtCore import QTimer
            QTimer.singleShot(100, self._rollback_all_dev_env)

        def _delete():
            rows = {idx.row() for idx in tbl.selectedIndexes()}
            if not rows:
                QMessageBox.information(dlg, "提示", "请先选择一行")
                return
            row = sorted(rows)[0]
            snap = snapshots[row]
            # 首个原始快照：额外警告
            extra_warn = ""
            if snap["is_first"]:
                extra_warn = "\n⚠️ 这是首个原始快照，删除后下次启动会自动重新生成一个新的！"
            reply = QMessageBox.question(dlg, "确认删除",
                f"即将删除快照：\n  {snap['created_time']}\n  {snap['note'] or '(无)'}{extra_warn}\n\n确认删除吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
            ok, msg = dev_snapshot.delete_snapshot(snap["filename"])
            if ok:
                self.on_monitor_log("dev_env", f"🗑 删除快照 {snap['filename']}")
                dlg.accept()
                self._view_dev_env_snapshots()  # 重新打开刷新
            else:
                QMessageBox.warning(dlg, "删除失败", msg)

        def _toggle_star():
            """切换选中快照的星标"""
            rows = {idx.row() for idx in tbl.selectedIndexes()}
            if not rows:
                QMessageBox.information(dlg, "提示", "请先选择一行")
                return
            row = sorted(rows)[0]
            snap = snapshots[row]
            new_starred = not snap.get("starred", False)
            ok, _ = dev_snapshot.set_snapshot_mark(snap["filename"], starred=new_starred)
            if ok:
                self.on_monitor_log("dev_env",
                    f"{'⭐ 已加星' if new_starred else '☆ 已取消星标'}: {snap['filename']}")
                dlg.accept()
                self._view_dev_env_snapshots()  # 刷新

        def _edit_tag():
            """编辑选中快照的自定义标签"""
            rows = {idx.row() for idx in tbl.selectedIndexes()}
            if not rows:
                QMessageBox.information(dlg, "提示", "请先选择一行")
                return
            row = sorted(rows)[0]
            snap = snapshots[row]
            old_tag = snap.get("tag", "")
            new_tag, ok = QInputDialog.getText(dlg, "编辑标签",
                f"为快照设置标签（留空清除）：\n  {snap['filename']}\n  {snap['created_time']}",
                text=old_tag)
            if ok:
                dev_snapshot.set_snapshot_mark(snap["filename"], tag=new_tag.strip())
                self.on_monitor_log("dev_env",
                    f"🏷 标签已更新: {snap['filename']} → {new_tag.strip() or '(无)'}")
                dlg.accept()
                self._view_dev_env_snapshots()  # 刷新

        def _toggle_filter_star(checked):
            """只看星标：隐藏/显示非星标行"""
            for i in range(tbl.rowCount()):
                snap = snapshots[i]
                tbl.setRowHidden(i, checked and not snap.get("starred", False))

        btn_view.clicked.connect(_view_detail)
        btn_restore.clicked.connect(_restore)
        btn_rollback.clicked.connect(_rollback_to_default)
        btn_star.clicked.connect(_toggle_star)
        btn_tag.clicked.connect(_edit_tag)
        btn_filter_star.toggled.connect(_toggle_filter_star)
        btn_delete.clicked.connect(_delete)
        btn_close.clicked.connect(dlg.accept)
        tbl.doubleClicked.connect(lambda: _view_detail())

        dlg.exec()

