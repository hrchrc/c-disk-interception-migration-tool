#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窗口生命周期/自启/资源刷新/缓存清理 Handler（从 main.py 抽出）

包含 21 个方法：
- _load_cache: 加载缓存
- _open_log_dir: 打开日志目录
- show_and_raise: 显示并激活窗口
- force_quit: 强制退出
- closeEvent: 关闭事件处理
- _first_run_auto_scan: 首次运行自动扫描
- toggle_auto: 切换自动迁移开关
- _get_autostart_path: 获取自启路径
- _is_autostart_enabled: 检查自启是否启用
- toggle_autostart: 切换开机自启
- _update_resource: 更新资源显示
- _fmt_speed: 格式化速度（静态方法）
- _auto_refresh_migrated: 自动刷新已迁移表
- _update_stats: 更新统计信息
- _clear_cache: 清除缓存
- _preload_mft_after_clear: 清除缓存后预加载 MFT
- _restart_app: 重启应用
- _get_dir_description_safe: 安全获取目录描述
- _filter_table: 过滤表格
- _is_vague_desc: 判断是否为敷衍描述
- _assess_desc_quality: 评估描述质量

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
import time
import ctypes
import shutil
import subprocess
import logging
import string
import psutil
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QThread, QTimer, QUrl
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit,
    QMessageBox, QMenu, QSystemTrayIcon,
)
from PySide6.QtGui import QColor, QAction, QDesktopServices

from config import (
    log_link_operation, log_error_with_reason, save_all, save_state,
    load_state, CONFIG_DIR, LOG_DIR, G_ROOT, APP_NAME, APP_VERSION,
)
from utils import is_symlink, get_symlink_target, get_dir_size_fast
from software_detect import get_dir_description

log = logging.getLogger('CDriveRelocator')


class LifecycleHandler:
    """窗口生命周期/自启/资源刷新/缓存清理 Handler"""

    def _load_cache(self):
        """加载缓存的扫描结果，立即显示 - 补充desc字段
        首次运行（无缓存且无迁移记录）自动扫描C盘并弹提示
        启动时不同步计算desc（七重检测慢），改为启动后延迟异步补全
        """
        cache = self.cfg.get("scan_cache", [])
        cache_time = self.cfg.get("scan_cache_time", "")
        migrated_records = self.cfg.get("migrated", [])
        if cache:
            # 启动时不同步计算desc，直接用cache中的desc（可能为空）
            # 延迟到启动后异步补全空desc
            self.on_scan_finished(self.migrator.scan_migrated(), cache)
            self.status_label.setText(
                f"已加载缓存 ({cache_time}) | 点击'刷新'重新扫描")
            # 启动后延迟2秒异步补全空desc（不阻塞窗口显示）
            QTimer.singleShot(2000, self._async_fill_empty_desc)
        elif not migrated_records:
            # 首次运行：无缓存且无迁移记录，延迟弹提示后自动扫描
            self.status_label.setText("检测到首次运行，准备自动扫描C盘...")
            QTimer.singleShot(1000, self._first_run_auto_scan)
        else:
            # Auto-scan on empty cache
            self.status_label.setText("未找到扫描缓存，自动扫描中...")
            self.status_label.setStyleSheet(
                "color: #1565C0; font-size: 13px; font-weight: bold; padding: 4px 10px; background-color: #E3F2FD; border-radius: 6px;")
            QTimer.singleShot(500, self._first_run_auto_scan)

    def _open_log_dir(self):
        """打开日志目录（包含app.log、错误日志.log、监控日志.log等）"""
        try:
            log_dir = str(LOG_DIR)
            if os.path.exists(log_dir):
                subprocess.Popen(["explorer", log_dir], creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                QMessageBox.information(self, "提示", f"日志目录不存在:\n{log_dir}")
        except Exception as e:
            log_error_with_reason("未知错误", str(e), "_open_log_dir")
            QMessageBox.critical(self, "失败", f"打开日志目录失败: {e}")

    def show_and_raise(self):
        """从托盘恢复并激活主窗口"""
        self.showNormal()  # 从最小化/隐藏状态恢复
        self.show()
        self.raise_()
        self.activateWindow()
        # Windows 下强制切到前台
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.SetForegroundWindow(int(self.winId()))

    def force_quit(self):
        """托盘「退出」菜单触发 - 强制退出程序

        关键修复：直接启动兜底强制退出定时器，不依赖 closeEvent 是否被触发
        （如果主窗口被模态对话框阻塞，close() 可能无法触发 closeEvent）
        """
        self._force_quit = True
        # 立即启动兜底：1.5 秒后强制 os._exit(0)
        # os._exit 是 C 级 ExitProcess，不可能被阻塞，确保一定能退出
        import threading, os as _os
        def _hard_exit():
            _os._exit(0)
        _exit_timer = threading.Timer(1.5, _hard_exit)
        _exit_timer.daemon = True
        _exit_timer.start()
        try:
            self.close()
        except Exception:
            # close 失败也没关系，兜底定时器会强制退出
            pass

    def closeEvent(self, event):
        if not self._force_quit:
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(APP_NAME, "已最小化到托盘，双击图标恢复",
                QSystemTrayIcon.Information, 2000)
        else:
            # 标记正在退出，让所有 done_signal 回调跳过弹窗和 UI 更新
            self._force_quit = True
            # 兜底强制退出定时器已在 force_quit 中启动（如果从 force_quit 进入）
            # 这里再启动一个兜底（覆盖用户点窗口 X 按钮但 _force_quit 已为 True 的场景）
            import threading, os as _os
            if not getattr(self, '_exit_timer_started', False):
                self._exit_timer_started = True
                def _hard_exit():
                    _os._exit(0)
                _exit_timer = threading.Timer(1.5, _hard_exit)
                _exit_timer.daemon = True
                _exit_timer.start()

            # ⚠️ 关键修复：最先强制终止复制引擎子进程 + 设置恢复取消标志
            # 必须在所有 worker.wait() 之前调用，这样引擎被终止后 worker 线程能快速退出
            try:
                if hasattr(self, 'migrator') and self.migrator:
                    self.migrator.force_cancel_copy()
            except Exception as e:
                log.error(f"退出时强制终止复制引擎失败: {e}")

            # 停止监控线程（watcher 是 daemon 线程，进程退出时自动终止，无需单独 join）
            if self.monitor_worker:
                self.monitor_worker.stop()
            if self.monitor_thread:
                self.monitor_thread.quit()
                self.monitor_thread.wait(500)
            # 各 worker 超时统一缩短为 500ms（反正有 1.5 秒兜底兜着）
            # 累计最大 ~6 秒，但实际绝大多数线程会立即响应 quit
            for attr_name, has_stop in (
                ('_recover_worker', False),
                ('_migrate_worker', False),
                ('_batch_migrate_worker', False),
                ('_relocate_worker', False),
                ('_batch_restore_worker', False),
                ('_browse_migrate_worker', False),
                ('_online_thread', False),
                ('_size_calc_thread', True),
                ('_migrated_thread', False),
                ('_desc_fill_thread', False),
                ('_search_thread', False),
                # N11: 补齐此前漏网的线程——退出时未停会导致 复制引擎/写文件被强杀中断
                ('scan_thread', False),
                ('smart_scan_thread', False),
                ('_ai_thread', False),
                ('_mft_preload_thread', False),
                ('_dev_env_rollback_worker', False),
                # 中危-6：补齐卸载残留扫描/清理、批量重建/修复链接 worker
                ('_orphan_scan_worker', False),
                ('_orphan_clean_worker', False),
                ('_batch_relink_worker', False),
                ('_batch_fix_link_worker', False),
            ):
                worker = getattr(self, attr_name, None)
                if worker and worker.isRunning():
                    if has_stop and hasattr(worker, 'stop'):
                        worker.stop()
                    worker.quit()
                    worker.wait(500)
            # _batch_migrate_custom_worker 内部跑复制引擎，quit 无法中断子进程
            # 必须先 force_cancel_copy 终止复制引擎，否则 wait 必超时、退出被强杀
            _batch_custom = getattr(self, '_batch_migrate_custom_worker', None)
            if _batch_custom and _batch_custom.isRunning():
                try:
                    self.migrator.force_cancel_copy()
                except Exception:
                    pass
                _batch_custom.quit()
                _batch_custom.wait(1000)
            # 停止 app.log tail 定时器
            if hasattr(self, '_applog_timer'):
                self._applog_timer.stop()
            # 中危修复：停止资源监控和自动刷新定时器，避免退出后仍触发回调
            if hasattr(self, '_resource_timer'):
                self._resource_timer.stop()
            if hasattr(self, '_auto_refresh_timer'):
                self._auto_refresh_timer.stop()
            # 安全取消开发环境后台 worker（超时缩短为 500ms）
            self._safe_cancel_dev_env_worker("_dev_env_refresh_worker", wait_ms=500)
            self._safe_cancel_dev_env_worker("_dev_env_apply_worker", wait_ms=500)
            self._safe_cancel_dev_env_worker("_dev_env_restore_worker", wait_ms=500)
            # 关闭 MFT 扫描器（释放资源）
            try:
                from utils import get_mft_scanner
                scanner = get_mft_scanner()
                if scanner is not None:
                    scanner.close()
            except Exception:
                pass
            if self.tray_icon:
                self.tray_icon.hide()
            # 记录退出时间到监控日志（方便排查中断问题）
            try:
                from config import log_link_operation
                log_link_operation("程序退出", "C盘拦迁器关闭")
            except Exception:
                pass
            event.accept()
            # 兜底强制退出已在 closeEvent 开头用 threading.Timer 启动（独立线程，不受 wait 阻塞）
            # 这里无需再用 QTimer.singleShot（主线程可能被 wait 阻塞导致 QTimer 不触发）

    def _first_run_auto_scan(self):
        """首次运行自动扫描 - 直接开始扫描（不弹模态对话框，避免阻塞UI）"""
        self.status_label.setText("首次运行，自动扫描C盘中...")
        self.refresh()

    def toggle_auto(self, checked):
        self.cfg["auto_migrate"] = checked
        save_all(self.cfg)
        if self.monitor_worker:
            self.monitor_worker.auto_migrate = checked
        self.status_label.setText(f"自动拦截: {'开启' if checked else '关闭'}")

    def toggle_clean_vss(self, checked):
        """迁移后自动清理系统还原点（卷影副本）开关"""
        self.cfg["auto_clean_vss"] = checked
        save_all(self.cfg)
        self.status_label.setText(f"迁移后清理还原点: {'开启' if checked else '关闭'}")

    def _get_autostart_path(self):
        """注册表开机启动路径"""
        import winreg
        return winreg

    def _is_autostart_enabled(self):
        """检查是否已设置开机启动"""
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
            val, _ = winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def toggle_autostart(self, checked):
        """开关开机启动"""
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
            if checked:
                # 用启动.vbs启动（静默+提权）
                vbs_path = str(CONFIG_DIR / "启动.vbs")
                if os.path.exists(vbs_path):
                    cmd = f'wscript.exe "{vbs_path}"'
                else:
                    # M14：__file__ 是 ui_lifecycle.py（无 main 入口），开机自启必失败
                    # 改用 sys.argv[0]（实际启动脚本，即 main.py）；打包模式用 sys.executable
                    if getattr(sys, 'frozen', False):
                        cmd = f'"{sys.executable}"'
                    else:
                        main_script = os.path.abspath(sys.argv[0]) if sys.argv[0] else __file__
                        cmd = f'"{sys.executable}" "{main_script}"'
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                self.status_label.setText("开机启动: 已开启")
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
                self.status_label.setText("开机启动: 已关闭")
            winreg.CloseKey(key)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"设置开机启动失败: {e}")
            self.chk_autostart.setChecked(not checked)

    def _update_resource(self):
        """刷新资源占用显示 + 网速显示 - 只显示本进程内存/线程 + 系统CPU/内存
        不显示本进程CPU（Windows调度导致显示不准）"""
        # H27：_proc/_net_last 延迟初始化（main.py 不再顶层 import psutil）
        if self._proc is None:
            try:
                self._proc = psutil.Process(os.getpid())
                self._net_last = psutil.net_io_counters()
                self._net_last_time = time.time()
            except Exception:
                return
        try:
            mem_info = self._proc.memory_info()
            proc_mem_mb = mem_info.rss / 1024 / 1024
            # 系统总内存
            sys_mem = psutil.virtual_memory()
            sys_mem_pct = sys_mem.percent
            sys_mem_used_gb = sys_mem.used / 1024 / 1024 / 1024
            sys_mem_total_gb = sys_mem.total / 1024 / 1024 / 1024
            # 系统CPU
            sys_cpu = psutil.cpu_percent(interval=None)
            # 线程数
            num_threads = self._proc.num_threads()
            self.resource_label.setText(
                f"内存:{proc_mem_mb:.0f}MB 线程:{num_threads} | "
                f"系统CPU:{sys_cpu:.0f}% 内存:{sys_mem_used_gb:.1f}/{sys_mem_total_gb:.1f}GB({sys_mem_pct:.0f}%)")
        except Exception:
            pass
        # 网速计算（下载/上传速率）
        try:
            now = time.time()
            cur = psutil.net_io_counters()
            dt = now - self._net_last_time
            if dt > 0:
                down_bytes = cur.bytes_recv - self._net_last.bytes_recv
                up_bytes = cur.bytes_sent - self._net_last.bytes_sent
                down_speed = down_bytes / dt
                up_speed = up_bytes / dt
                self.net_label.setText(
                    f"↓{self._fmt_speed(down_speed)} ↑{self._fmt_speed(up_speed)}")
            self._net_last = cur
            self._net_last_time = now
        except Exception:
            pass

    @staticmethod
    def _fmt_speed(speed_bps):
        """把字节/秒格式化为 KB/s 或 MB/s"""
        if speed_bps < 1024:
            return f"{speed_bps:.0f}B/s"
        elif speed_bps < 1024 * 1024:
            return f"{speed_bps / 1024:.1f}KB/s"
        else:
            return f"{speed_bps / 1024 / 1024:.2f}MB/s"

    def _auto_refresh_migrated(self):
        """自动刷新已迁移表状态（轻量，不全盘扫描）
        只检测符号链接状态，不重新计算目录大小"""
        try:
            if self.table_migrated.rowCount() == 0:
                return
            status_map = {
                "OK":         ("正常",     "#2E7D32", "符号链接有效，数据在目标盘"),
                "BROKEN":     ("断链",     "#C62828", "C盘路径被软件覆盖为真实目录，点击右键修复"),
                "MISSING":    ("丢失",     "#EF6C00", "C盘路径不存在，点击右键修复（直接创建链接）"),
                "TARGET_GONE":("目标丢失", "#B71C1C", "目标盘数据不存在，需还原或重新迁移"),
            }
            changed = 0
            for row in range(self.table_migrated.rowCount()):
                try:
                    src_item = self.table_migrated.item(row, 0)
                    dst_item = self.table_migrated.item(row, 1)
                    if not src_item or not dst_item:
                        continue
                    src = src_item.text()
                    dst = dst_item.text()
                    is_link = is_symlink(src)
                    target = get_symlink_target(src) if is_link else ""
                    def norm(p):
                        return p.lower().rstrip("\\").replace("\\\\?\\", "").replace("\\\\?\\UNC\\", "\\\\")
                    target_norm = norm(target) if target else ""
                    dst_norm = norm(dst)
                    if is_link and target_norm == dst_norm and os.path.exists(dst):
                        status = "OK"
                    elif is_link and not os.path.exists(dst):
                        status = "TARGET_GONE"
                    elif is_link and target_norm != dst_norm:
                        status = "BROKEN"
                    elif not os.path.exists(src):
                        status = "MISSING"
                    else:
                        status = "BROKEN"
                    # 检查状态是否变化
                    st_item = self.table_migrated.item(row, 3)
                    if st_item:
                        status_text, status_color, status_tip = status_map.get(
                            status, ("未知", "#424242", ""))
                        # i18n：状态词渲染时翻译
                        from i18n import tr
                        status_text = tr(status_text)
                        if st_item.text() != status_text:
                            st_item.setText(status_text)
                            st_item.setForeground(QColor(status_color))
                            st_item.setToolTip(status_tip)
                            changed += 1
                    # 更新链接目标列
                    tgt_item = self.table_migrated.item(row, 4)
                    if tgt_item:
                        if target:
                            target_display = target.replace("\\\\?\\", "").replace("\\\\?\\UNC\\", "\\\\")
                            if tgt_item.text() != target_display:
                                tgt_item.setText(target_display)
                                tgt_item.setToolTip(target_display)
                                tgt_item.setForeground(QColor("#2E7D32") if is_link else QColor("#C62828"))
                        elif tgt_item.text() != "（非符号链接）":
                            tgt_item.setText("（非符号链接）")
                            tgt_item.setForeground(QColor("#9E9E9E"))
                            tgt_item.setToolTip("C盘路径不是符号链接，可能是真实目录（被软件覆盖）")
                except Exception:
                    pass
            if changed > 0:
                self._update_stats(migrated_count=self.table_migrated.rowCount())
                # 在监控日志记录状态变化
                try:
                    self.on_monitor_log("fix", f"自动检测到{changed}个链接状态变化（已自动更新显示）")
                except Exception:
                    pass
        except Exception:
            pass

    def _update_stats(self, migrated_count=None, scan_count=None):
        """更新两个标签页的统计标签"""
        if migrated_count is not None:
            # 统计已迁移表各状态数量
            ok = broken = missing = target_gone = 0
            for row in range(self.table_migrated.rowCount()):
                st_item = self.table_migrated.item(row, 3)
                if st_item:
                    txt = st_item.text()
                    if txt == "正常": ok += 1
                    elif txt == "断链": broken += 1
                    elif txt == "丢失": missing += 1
                    elif txt == "目标丢失": target_gone += 1
            self.stat_migrated.setText(
                f"共{migrated_count}项 | 正常{ok} 断链{broken} 丢失{missing} 目标丢失{target_gone}")
        if scan_count is not None:
            total_mb = 0
            for row in range(self.table_scan.rowCount()):
                try:
                    # 从UserRole读取原始数值（表格text是"xxx.xMB"/"xxx.xKB"格式无法直接float）
                    size_item = self.table_scan.item(row, 2)
                    if size_item:
                        val = size_item.data(Qt.UserRole)
                        if val is None:
                            val = size_item.text()
                        total_mb += float(val)
                except Exception:
                    pass
            self.stat_scan.setText(f"共{scan_count}项 | {total_mb:.0f}MB")

    def _clear_cache(self):
        """一键清空缓存：清空scan_cache、scan_cache_time、识别记录.json、AI识别缓存
        不影响已迁移记录(migrated)、白名单、拦截日志等配置
        """
        from PySide6.QtWidgets import QMessageBox
        from config import log
        logger = log or __import__('logging').getLogger("CDriveRelocator")
        logger.info("[清空缓存] 用户点击清空缓存按钮，开始流程")
        reply = QMessageBox.question(
            self, "清空缓存",
            "将清空以下缓存（不影响已迁移记录和白名单）：\n\n"
            "1. 待迁移扫描缓存（scan_cache）\n"
            "2. 识别记录.json\n"
            "3. AI智能识别结果（ai_recognize_cache.json）\n\n"
            "清空后请点击『刷新待迁移』重新扫描。是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            logger.info("[清空缓存] 用户取消操作")
            return
        # 先停止可能正在运行的 desc 补全线程，避免清空后回调访问已清空数据
        # 闪退根因：quit()对 QThread.run() 无效（只有事件循环才响应），wait(2000) 不够（单条超时 5 秒）
        # 修复：设 _cancel 标志让 run() 主动退出，wait 增加到 6 秒（比单条超时 5 秒略长）
        try:
            if hasattr(self, '_desc_fill_thread') and self._desc_fill_thread:
                _t = self._desc_fill_thread
                if _t.isRunning():
                    logger.info("[清空缓存] 请求 desc 补全线程取消（设 _cancel=True）")
                    try:
                        _t.cancel()
                    except Exception as e:
                        logger.warning(f"[清空缓存] cancel 调用异常: {e}")
                    # 等 6 秒（比单条超时 5 秒略长，确保当前正在跑的子线程结束）
                    if _t.wait(6000):
                        # 线程已在 wait 内结束，finished 信号已发出，直接 deleteLater
                        _t.deleteLater()
                        logger.info("[清空缓存] desc 线程已结束并释放")
                    else:
                        logger.warning("[清空缓存] desc 线程 6 秒内未退出，移入退役列表保活防 segfault")
                        # wait 超时说明线程仍在运行，连接 finished 信号等其结束后清理
                        def _cleanup_desc(_w=_t):
                            try:
                                if hasattr(self, '_old_dev_env_workers') and _w in self._old_dev_env_workers:
                                    self._old_dev_env_workers.remove(_w)
                                _w.deleteLater()
                            except Exception:
                                pass
                        _t.finished.connect(_cleanup_desc)
                        if hasattr(self, '_old_dev_env_workers'):
                            self._old_dev_env_workers.append(_t)
                else:
                    # 线程未运行，直接释放
                    _t.deleteLater()
                self._desc_fill_thread = None
                logger.info("[清空缓存] desc 线程引用已清除")
        except BaseException as e:
            logger.exception(f"[清空缓存] 停止 desc 线程异常: {e}")
        # 停止可能正在运行的 AI 识别线程（这是闪退的关键原因）
        try:
            if hasattr(self, '_ai_thread') and self._ai_thread:
                _t = self._ai_thread
                if _t.isRunning():
                    logger.info("[清空缓存] 请求 AI 识别线程取消")
                    # 用 cancel 标志让线程在下一批开始前安全退出，不调用 terminate（会 segfault）
                    try:
                        _t.cancel()
                    except Exception as e:
                        logger.warning(f"[清空缓存] cancel 调用异常: {e}")
                    # 阻塞等待最多 5 秒（让它跑完当前批次）
                    if _t.wait(5000):
                        # 线程已在 wait 内结束，finished 信号已发出，直接 deleteLater
                        _t.deleteLater()
                        logger.info("[清空缓存] AI 线程已结束并释放")
                    else:
                        logger.warning("[清空缓存] AI 线程 5 秒内未退出，移入退役列表保活防 segfault")
                        # wait 超时说明线程仍在运行，连接 finished 信号等其结束后清理
                        def _cleanup_ai(_w=_t):
                            try:
                                if hasattr(self, '_old_dev_env_workers') and _w in self._old_dev_env_workers:
                                    self._old_dev_env_workers.remove(_w)
                                _w.deleteLater()
                            except Exception:
                                pass
                        _t.finished.connect(_cleanup_ai)
                        if hasattr(self, '_old_dev_env_workers'):
                            self._old_dev_env_workers.append(_t)
                else:
                    # 线程未运行，直接释放
                    _t.deleteLater()
                self._ai_thread = None
                logger.info("[清空缓存] AI 线程引用已清除")
        except BaseException as e:
            logger.exception(f"[清空缓存] 停止 AI 线程异常: {e}")
        # 分步执行，每步写日志，便于定位闪退位置
        try:
            logger.info("[清空缓存] 步骤1: 清空 cfg 字段")
            self.cfg["scan_cache"] = []
            self.cfg["scan_cache_time"] = ""
            self.cfg["desc_cache"] = {}
            # 同步清空 AI 识别缓存配置
            ai_cfg = self.cfg.get("ai_recognize", {})
            ai_cfg["cache"] = {}
            self.cfg["ai_recognize"] = ai_cfg
            logger.info("[清空缓存] 步骤2: 保存 config.json")
            save_all(self.cfg)
            logger.info("[清空缓存] 步骤3: 删除识别记录.json")
            try:
                from config import RECOGNITION_LOG_FILE
                if RECOGNITION_LOG_FILE.exists():
                    RECOGNITION_LOG_FILE.unlink()
            except Exception as e:
                logger.warning(f"[清空缓存] 删除识别记录异常(忽略): {e}")
            logger.info("[清空缓存] 步骤3.5: 删除 AI 识别缓存文件 ai_recognize_cache.json")
            try:
                # 实际位置在程序根目录（与config.json同目录）
                from config import BASE_DIR
                ai_cache_file = BASE_DIR / "ai_recognize_cache.json"
                if ai_cache_file.exists():
                    ai_cache_file.unlink()
                    logger.info("[清空缓存] 已删除 ai_recognize_cache.json")
            except Exception as e:
                logger.warning(f"[清空缓存] 删除 AI 缓存文件异常(忽略): {e}")
            logger.info("[清空缓存] 步骤4: 清空待迁移表格")
            self.table_scan.setSortingEnabled(False)
            self.table_scan.blockSignals(True)
            self.table_scan.setRowCount(0)
            self.table_scan.blockSignals(False)
            self.table_scan.setSortingEnabled(True)
            self.table_scan.viewport().update()
            logger.info("[清空缓存] 步骤5: 更新统计标签")
            self._update_stats(scan_count=0)
            logger.info("[清空缓存] 步骤6: 更新状态栏")
            self.status_label.setText("缓存已清空，正在后台预加载 MFT 索引...")
            self.on_monitor_log("init", "已清空扫描缓存和识别记录")
            logger.info("[清空缓存] 全部完成，启动 MFT 预加载")
            # 方案A：清空缓存后自动后台加载 MFT 索引，避免下次刷新待迁移时退回 os.walk 慢扫
            QTimer.singleShot(300, self._preload_mft_after_clear)
        except BaseException as e:
            # 捕获 BaseException 以便记录所有类型的异常（含 SystemExit 等）
            import traceback
            tb = traceback.format_exc()
            logger.error(f"[清空缓存] 闪退异常: {e}\n{tb}")
            try:
                log_error_with_reason("清空缓存闪退", f"{e}\n{tb}", "MainWindow._clear_cache")
            except Exception:
                pass
            self.status_label.setText(f"清空缓存失败: {e}")
            # 重新抛出，让程序顶层崩溃处理器（如有）也能感知
            raise

    def _preload_mft_after_clear(self):
        """清空缓存后自动后台加载 MFT 索引（不阻塞 UI）
        加载完成后用户点"刷新待迁移"就秒开，不会退回 os.walk 慢扫
        """
        try:
            from utils import get_mft_scanner, set_mft_scanner
            scanner = get_mft_scanner()
            if scanner is not None and getattr(scanner, '_loaded', False):
                self.status_label.setText("缓存已清空，MFT 索引已就绪，请点击『刷新待迁移』重新扫描")
                return
            from PySide6.QtCore import QThread, Signal
            class MftPreloadThread(QThread):
                progress = Signal(str)
                done_signal = Signal(bool, str)
                def run(self):
                    try:
                        import time
                        import pythoncom
                        try:
                            pythoncom.CoInitialize()
                        except Exception:
                            pass
                        self.progress.emit("正在后台加载 MFT 索引...")
                        from fast_scan import MftScanner
                        t0 = time.time()
                        new_scanner = MftScanner("C")
                        new_scanner.load()
                        set_mft_scanner(new_scanner)
                        elapsed = time.time() - t0
                        msg = f"MFT 加载完成（{elapsed:.1f}秒，{new_scanner.file_count} 文件 / {new_scanner.dir_count} 目录）"
                        from config import log
                        log.info(f"[清空缓存] 预加载 MFT: {msg}")
                        self.done_signal.emit(True, msg)
                    except BaseException as e:
                        from config import log
                        log.warning(f"[清空缓存] 预加载 MFT 失败（不影响功能，下次刷新会用 os.walk 兜底）: {e}")
                        self.done_signal.emit(False, str(e))
            self._mft_preload_thread = MftPreloadThread()
            self._mft_preload_thread.progress.connect(
                lambda msg: self.status_label.setText(f"缓存已清空，{msg}"), Qt.QueuedConnection)
            self._mft_preload_thread.done_signal.connect(
                lambda ok, msg: self.status_label.setText(
                    f"缓存已清空，MFT {'加载完成' if ok else '加载失败'}，请点击『刷新待迁移』重新扫描"
                ), Qt.QueuedConnection)
            self._mft_preload_thread.start()
        except BaseException as e:
            from config import log
            log.warning(f"[清空缓存] 启动 MFT 预加载失败: {e}")
            self.status_label.setText("缓存已清空，请点击『刷新待迁移』重新扫描")

    def _restart_app(self):
        """重启本程序：保存配置后用新进程启动，当前进程强制退出
        注意：单例锁靠命名 Mutex 实现（N7 修复后），
        重启时必须先 CloseHandle 释放 Mutex，否则新进程 CreateMutexW 会命中
        ERROR_ALREADY_EXISTS 而认为"已有实例运行"并退出，导致新旧进程都没了
        """
        from PySide6.QtWidgets import QMessageBox
        import subprocess
        import sys

        def _flush_log():
            """强制刷新日志缓冲到磁盘，防止 _os._exit 导致诊断日志丢失"""
            for _h in log.handlers:
                try:
                    _h.flush()
                except Exception:
                    pass

        reply = QMessageBox.question(
            self, "重启程序",
            "将保存当前配置并重启C盘拦迁器。是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        try:
            # 保存配置
            save_all(self.cfg)
            self.on_monitor_log("init", "程序重启中...")
            log.info("=== 用户触发重启 ===")
            _flush_log()

            # 关键：先释放单实例 Mutex，让新进程 CreateMutexW 不会命中 ERROR_ALREADY_EXISTS
            # N7 修复后单例锁改用 Mutex，旧的"改窗口标题" trick 对 Mutex 无效
            # Mutex handle 存在 config 模块（避免 import main 在打包模式下的不确定性）
            try:
                import config as _cfg_mod
                _handle = _cfg_mod.SINGLE_INSTANCE_MUTEX_HANDLE
                log.info(f"重启：当前 Mutex handle=0x{_handle:X}" if _handle
                         else "重启：Mutex handle 为空（可能已被释放过）")
                if _handle:
                    # 确保 CloseHandle 接收 64 位指针（与 main.py 中 restype=c_void_p 对齐）
                    ctypes.windll.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
                    _ret = ctypes.windll.kernel32.CloseHandle(_handle)
                    log.info(f"已释放单实例 Mutex，CloseHandle 返回 {_ret}（1=成功）")
                    _cfg_mod.SINGLE_INSTANCE_MUTEX_HANDLE = None
                else:
                    log.warning("单实例 Mutex handle 为空，无法释放（可能已被释放过）")
                _flush_log()
            except Exception as e:
                log.error(f"释放单实例 Mutex 失败: {e}")
                _flush_log()

            # 保留窗口标题修改（旧逻辑，作为 FindWindowW 兜底的额外保险）
            self.setWindowTitle(f"{APP_NAME} v{APP_VERSION} [正在退出...]")

            # 启动新进程（DETACHED_PROCESS 让新进程独立运行，不作为子进程）
            # CREATE_NEW_PROCESS_GROUP 让新进程不受 Ctrl+C 等信号影响
            creationflags = 0
            if sys.platform == 'win32':
                # DETACHED_PROCESS = 0x00000008, CREATE_NEW_PROCESS_GROUP = 0x00000200
                creationflags = 0x00000008 | 0x00000200

            if getattr(sys, 'frozen', False):
                # 打包模式：直接启动 exe（不传 script 参数，否则 exe 会把它当作脚本路径）
                _exe = sys.executable
                log.info(f"重启(打包模式)：启动 {_exe}")
                _flush_log()
                proc = subprocess.Popen([_exe], creationflags=creationflags)
            else:
                # 源码模式：python main.py
                # sys.argv[0] 可能是相对路径，转绝对路径；若拿不到则回退到本文件同级的 main.py
                script = os.path.abspath(sys.argv[0]) if sys.argv else "main.py"
                if not os.path.exists(script):
                    _fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
                    log.warning(f"重启：sys.argv[0]='{script}' 不存在，回退到 {_fallback}")
                    script = _fallback
                python = sys.executable
                log.info(f"重启(源码模式)：{python} {script}  cwd={os.path.dirname(script) or None}")
                _flush_log()
                proc = subprocess.Popen([python, script],
                    cwd=os.path.dirname(script) or None,
                    creationflags=creationflags)

            log.info(f"新进程已启动，PID={proc.pid}")
            _flush_log()

            # 给新进程一点启动时间，再真正退出当前进程
            # 必须设置 _force_quit=True，否则 closeEvent 会把窗口藏到托盘而不退出
            from PySide6.QtCore import QTimer
            def _do_quit():
                self._force_quit = True
                self.close()
                # 兜底：1 秒后强制退出，防止 closeEvent 中线程 wait 超时后
                # 非守护线程（如 monitor_thread）阻止进程退出，导致旧进程残留
                def _force_exit():
                    log.info("重启：旧进程强制退出")
                    _flush_log()
                    import os as _os
                    _os._exit(0)
                QTimer.singleShot(1500, _force_exit)
            QTimer.singleShot(300, _do_quit)
        except Exception as e:
            log_error_with_reason("重启失败", str(e), "MainWindow._restart_app")
            _flush_log()
            self.status_label.setText(f"重启失败: {e}")

    def _get_dir_description_safe(self, path):
        """安全获取目录说明（异常时返回空字符串）"""
        try:
            from software_detect import get_dir_description
            return get_dir_description(path) or ""
        except Exception:
            return ""

    def _filter_table(self, table, keyword):
        """根据关键词过滤表格行（实时搜索）"""
        keyword = keyword.strip().lower()
        for row in range(table.rowCount()):
            if not keyword:
                table.setRowHidden(row, False)
                continue
            matched = False
            for col in range(table.columnCount()):
                item = table.item(row, col)
                if item and keyword in item.text().lower():
                    matched = True
                    break
            table.setRowHidden(row, not matched)

    def _is_vague_desc(self, desc):
        """判断desc是否敷衍/缺斤少两，需要联网补全
        空、太短、通用词、含"相关"、以"软件"/"应用"结尾等都算敷衍
        通用去掉所有 [xxx] 前缀后判断
        """
        if not desc:
            return True
        desc = desc.strip()
        if not desc:
            return True
        # 通用去掉所有 [xxx] 前缀（如[系统]、[扫描发现]等）
        import re
        desc = re.sub(r'^\[[^\]]*\]\s*', '', desc).strip()
        # 太短（<4字）算敷衍
        if len(desc) < 4:
            return True
        # 含"相关"算敷衍（如"百度相关软件"、"微软相关组件"）
        if "相关" in desc:
            return True
        # 通用词列表（七重检测的兜底说明，不够精确）
        vague_words = [
            "应用数据", "缓存数据", "临时文件", "日志文件", "备份文件",
            "配置/设置数据", "更新程序", "更新数据", "自动更新程序",
            "崩溃报告数据", "崩溃报告", "应用包数据", "更新程序/更新数据",
            "Node.js项目", "公共文件", "Microsoft公共组件", "发布者数据",
            "通信数据", "已连接设备数据", "历史记录", "部署数据", "调试数据",
            "对等分发缓存", "应用数据(系统)", "用户安装的程序", "卸载信息",
            ".NET程序集缓存",
        ]
        if desc in vague_words:
            return True
        # 以"应用"结尾且太短（如"xxx应用"但xxx很短）
        if desc.endswith("应用") and len(desc) <= 8:
            return True
        return False

    def _assess_desc_quality(self, desc, dir_name):
        """评估描述质量，返回 'good', 'low', 'wrong' 或 'vague'

        用于给扫描表格的行上色：
        - 'wrong': 描述大概率错误（联网搜索返回无关百科/歌曲/游戏名）
        - 'low': 描述质量低（可能不准确、过短、或属于笼统描述）
        - 'good': 描述可信
        """
        if not desc or not desc.strip():
            return "low"

        desc_lower = desc.lower()
        dl = (dir_name or "").lower()

        # === 错误特征：明确表示返回了无关结果 ===
        wrong_signatures = [
            # 百科返回了公司简介而不是软件名
            ("拥有强大互联网基础的领先AI公司", "百度"),  # 百度百科公司简介
            ("韩国互动娱乐软件公司", "NCSOFT"),  # 公司简介
            ("网易（NASDAQ", "网易"),  # 公司简介
            # 返回了歌曲/专辑/影视
            ("Ghen演唱的歌曲", "涂鸦/tuya"),
            ("收录于专辑", ""),
            ("日本歌手下川", ""),
            ("2023年上映的", "novel-box"),
            ("1978年12月30日出生于", "devin"),
            ("NEXTON公司的旗下品牌", ""),
            ("发售的恋爱冒险", ""),
            # 返回了语言/词汇定义
            ("班图语支的一种语言", "FLiNGTrainer"),
            ("英语中具有名词、形容词、动词", "Purple"),
            ("源于英文流入中国后的简写", "pc"),
            ("以 C语言撰写", "TScreen"),
            # 返回了游戏/小说不相关内容
            ("即时战略游戏《星际争霸", "Sentry"),
            ("神之天平（ASTLIBRA", "Steam"),
            # 返回了食物/物品
            ("把柑橘属植物", "Marmalade"),
            ("一种果酱", "Marmalade"),
            # 返回了汽车/品牌
            ("于1998年至2006年生产", ""),
            ("本田汽车零部件", ""),
            # 返回了人物简介
            ("职业篮球运动员", "devin"),
            ("自媒体创作者", "Ultralytics"),
            # 返回了VR相机公司
            ("深圳看到科技有限公司", "obsidian"),
            # 返回了其他语言定义
            ("Microsoft? Windows? Operating System", ""),
            # 部署被解释成汉语词语
            ("是一个汉语词语", "deployment"),
            # Minecraft相关内容
            ("轻型坦克R型", ""),
        ]
        for sig, restricted_dir in wrong_signatures:
            if sig.lower() in desc_lower:
                if not restricted_dir or restricted_dir.lower() in dl:
                    return "wrong"

        # === 低质量特征 ===
        low_signatures = [
            # 过短（<=4字符，还没什么信息量的）
            len(desc.strip()) <= 4,
            # 纯公司名认成软件名（过于笼统）
            desc.strip() in ("Intel", "NVIDIA", "Adobe", "Tencent", "Google", "Microsoft",
                             "Bytedance", "Baidu", "网易", "Netease", "VMware"),
            # 包管理器被笼统描述
            ("npm" in desc_lower and "node" in desc_lower and "package" not in desc_lower),
            # 返回了百科第一句长描述（>60字的百科文章误判为软件描述）
            len(desc) > 60 and ("成立于" in desc or "是一家" in desc or "开发" in desc),
            # 描述中只有公司信息没有产品信息
            ("公司" in desc and "软件" not in desc and "版本" not in desc),
            # 听起来像文件格式/协议/概念，不像软件（如DBG=音频格式、SSH=协议）
            ("文件格式" in desc and "具有高清晰度" in desc_lower),
            ("安全外壳协议" in desc and "ssh" in dl),
            ("汉语词语" in desc and "部署" in dl),
            # 描述为空但目录名不像是系统/通用目录
            not desc.strip() and dl not in ("temp", "cache", "logs", "log", "crashdumps", ""),
        ]
        for sig in low_signatures:
            if isinstance(sig, bool):
                if sig:
                    return "low"
            elif sig.lower() in desc_lower:
                return "low"

        # === 笼统特征 ===
        if ("相关" in desc) or (desc.endswith("软件")):
            return "low"

        return "good"
