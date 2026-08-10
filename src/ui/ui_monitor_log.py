#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后台监控信号处理与日志渲染 Handler（从 main.py 抽出）

包含 13 个方法：
- _start_monitor: 启动后台监控
- _setup_tray: 设置系统托盘
- _on_tray_activated: 托盘激活回调
- on_monitor_log: 监控日志回调
- _flush_pending_log_render: 刷新待渲染日志
- _tail_app_log: 跟踪应用日志文件
- _render_monitor_log: 渲染监控日志到文本框
- _log_link: 记录链接操作日志
- on_alert: 警报回调
- on_installer_detected: 检测到安装器回调
- on_installer_confirm: 安装器确认对话框
- _log_monitor: 记录监控事件
- _refresh_log_only: 仅刷新日志区

这些方法原属 MainWindow，抽取为 Handler 以降低 main.py 体量。
方法内通过 self 访问 MainWindow 的属性和其他方法，运行时由 MainWindow 提供。

依赖的 MainWindow 属性：
- self.cfg                  配置字典
- self.monitor_worker       MonitorWorker 实例
- self.log_text             日志文本框控件
- self.tray                 系统托盘控件
"""
import os
import sys
import time
import logging
import subprocess
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QThread, QTimer, QUrl
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit,
    QMessageBox, QMenu, QSystemTrayIcon, QStyle,
)
from PySide6.QtGui import QColor, QAction, QIcon, QCursor

from config import (
    log_link_operation, log_error_with_reason, MONITOR_LOG_FILE,
    LOG_FILE, LOG_DIR, APP_NAME, APP_ICON_FILE, save_all,
)
from utils import is_symlink, get_symlink_target
from monitor import MonitorWorker
import html as _html

log = logging.getLogger('CDriveRelocator')


class MonitorLogHandler:
    """后台监控信号处理与日志渲染 Handler"""

    def _start_monitor(self):
        self.monitor_thread = QThread()
        self.monitor_worker = MonitorWorker(
            self.migrator,
            interval=self.cfg.get("scan_interval", 60),
            threshold=self.cfg.get("size_threshold", 50),
            auto_migrate=self.cfg.get("auto_migrate", False)
        )
        self.monitor_worker.moveToThread(self.monitor_thread)
        self.monitor_thread.started.connect(self.monitor_worker.run)
        self.monitor_worker.log_signal.connect(self.on_monitor_log, Qt.QueuedConnection)
        self.monitor_worker.alert_signal.connect(self.on_alert, Qt.QueuedConnection)
        self.monitor_worker.installer_signal.connect(self.on_installer_detected, Qt.QueuedConnection)
        self.monitor_worker.installer_confirm_signal.connect(self.on_installer_confirm, Qt.QueuedConnection)
        self.monitor_worker.finished_signal.connect(self.monitor_thread.quit, Qt.QueuedConnection)
        self.monitor_thread.start()

    def _setup_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        # 托盘图标：程序图标（config.APP_ICON_FILE 统一路径，缺失时回退系统图标）
        _icon = QIcon(str(APP_ICON_FILE)) if APP_ICON_FILE.exists() else self.style().standardIcon(QStyle.SP_ComputerIcon)
        self.tray_icon.setIcon(_icon)
        self.tray_icon.setToolTip(APP_NAME)
        # 菜单挂到主窗口（无 parent 的 QMenu 不在控件树里，i18n 切换
        # 语言时 findChildren 找不到、无法翻译）
        menu = QMenu(self)
        act_show = QAction("显示窗口", self)
        act_show.triggered.connect(self.show_and_raise)
        act_open_logs = QAction("打开日志目录", self)
        act_open_logs.triggered.connect(self._open_log_dir)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.force_quit)
        menu.addAction(act_show)
        menu.addAction(act_open_logs)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_activated(self, reason):
        """托盘图标被激活（双击/单击）时唤出主窗口"""
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_and_raise()

    def on_monitor_log(self, event_type, message):
        """监控日志 - 不同事件类型用不同颜色，中英文双语标签
        同时写入独立的 监控日志.log 文件（不混入Python logging的[INFO]等）

        防抖优化：高频日志（如 复制引擎每 500 文件一次）时，UI 渲染限频到
        每 500ms 一次，避免 561 次重绘导致 UI 假死。
        """
        full_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # cache 存原文（渲染时翻译：切换语言后重新渲染即新语言）；
        # 状态栏即时翻译；日志文件保留中文原文（便于排查）
        from i18n import tr
        # 追加到cache，超过1000条时移除最旧的
        self._monitor_log_cache.append((full_ts, event_type, message))
        if len(self._monitor_log_cache) > 1000:
            self._monitor_log_cache = self._monitor_log_cache[-1000:]
        # 状态栏立即更新（轻量操作）
        self.status_label.setText(tr(message))
        # 写入日志文件（IO 操作，不阻塞 UI）
        # 注：message 可能含 \n（如 alert 的多行消息），写入文件会破坏
        # "[时间] [类型] 消息" 的单行格式，导致刷新时正则不匹配变 unknown。
        # 解决：把 \n 替换为 ⏎ 符号保留视觉换行提示，文件保持单行。
        try:
            safe_msg = message.replace("\r\n", " ⏎ ").replace("\n", " ⏎ ").replace("\r", " ⏎ ")
            with open(MONITOR_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{full_ts}] [{event_type}] {safe_msg}\n")
            # 日志轮转：超过10MB自动轮转，避免无限增长（每60秒检查一次）
            try:
                from config import rotate_log_if_needed
                rotate_log_if_needed(MONITOR_LOG_FILE)
            except Exception:
                pass
        except Exception:
            pass
        # UI 渲染限频：距上次渲染 > 500ms 才重绘，否则延迟到下次
        import time as _time
        now = _time.time()
        last = getattr(self, "_last_log_render_ts", 0)
        if now - last >= 0.5:
            self._render_monitor_log()
            self._last_log_render_ts = now
            # 如果有待渲染的日志，用 QTimer 安排下一次渲染
            self._log_render_pending = False
        else:
            # 标记有待渲染，用单次 QTimer 在 500ms 后渲染
            if not getattr(self, "_log_render_pending", False):
                self._log_render_pending = True
                from PySide6.QtCore import QTimer
                QTimer.singleShot(500, self._flush_pending_log_render)

    def _flush_pending_log_render(self):
        """渲染延迟的监控日志（防抖机制的回调）"""
        try:
            import time as _time
            self._render_monitor_log()
            self._last_log_render_ts = _time.time()
            self._log_render_pending = False
        except Exception as e:
            log.error(f"_flush_pending_log_render 异常: {e}")

    def _tail_app_log(self):
        """每 2 秒读取 app.log 新增行，追加到监控日志 cache（类型 applog）"""
        try:
            if not LOG_FILE.exists():
                return
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                f.seek(self._applog_pos)
                new_lines = f.readlines()
                self._applog_pos = f.tell()
            if not new_lines:
                return
            # 过滤空行，解析时间戳和消息
            for line in new_lines:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                # app.log 格式：2026-07-16 08:11:08,142 [INFO] 消息
                # 提取时间戳和消息内容
                import re as _re
                m = _re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[(\w+)\] (.*)$", line)
                if m:
                    ts = m.group(1)
                    level = m.group(2)
                    msg = m.group(3)
                    # 只显示 INFO/WARNING/ERROR 级别（DEBUG 不显示）
                    if level in ("DEBUG",):
                        continue
                    self._monitor_log_cache.append((ts, "applog", f"[{level}] {msg}"))
                else:
                    # 不匹配格式的行也显示
                    self._monitor_log_cache.append(
                        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "applog", line))
            # 限制 cache 大小
            if len(self._monitor_log_cache) > 1000:
                self._monitor_log_cache = self._monitor_log_cache[-1000:]
            # 只有当前在监控日志 Tab 才渲染（避免不必要的 UI 刷新）
            if self.tabs.currentIndex() == 2:
                self._render_monitor_log()
        except Exception:
            pass

    # 监控日志事件类型→(中文标签, 颜色) 映射
    _LOG_TYPE_MAP = {
        "init":      ("初始化 init",      "#1565C0"),
        "new":       ("新目录 new",       "#2E7D32"),
        "kill":      ("拦截 kill",        "#C62828"),
        "warn":      ("警告 warn",        "#EF6C00"),
        "error":     ("错误 error",       "#B71C1C"),
        "install":   ("安装 install",     "#6A1B9A"),
        "fix":       ("修复 fix",         "#00838F"),
        "migrate":   ("迁移 migrate",     "#1565C0"),
        "alert":     ("警报 alert",       "#C62828"),
        "installer": ("安装器 installer", "#6A1B9A"),
        "link":      ("链接操作 link",    "#00838F"),
        "applog":    ("应用日志 applog",  "#5D4037"),
        "dev_env":   ("开发环境 dev_env", "#00695C"),
    }

    # 筛选下拉框中文标签→event_type 映射
    _FILTER_MAP = {
        "全部": None, "初始化": "init", "新目录": "new", "拦截": "kill",
        "警告": "warn", "错误": "error", "安装": "install", "修复": "fix",
        "迁移": "migrate", "警报": "alert", "安装器": "installer", "链接操作": "link",
        "应用日志": "applog", "开发环境": "dev_env",
    }

    def _render_monitor_log(self):
        """根据当前筛选条件从cache渲染监控日志到UI"""
        try:
            filter_text = self.combo_log_filter.currentText() if hasattr(self, 'combo_log_filter') else "全部"
            filter_type = self._FILTER_MAP.get(filter_text)
            # 按筛选条件过滤
            if filter_type:
                entries = [e for e in self._monitor_log_cache if e[1] == filter_type]
            else:
                entries = self._monitor_log_cache
            # 渲染
            from i18n import tr
            self.log_text.clear()
            for full_ts, etype, msg in entries:
                # 显示完整年月日时分秒（不再截断为时分秒）
                label, color = self._LOG_TYPE_MAP.get(etype, (etype, "#424242"))
                # 渲染时翻译（cache 存原文：切换语言后重新渲染即新语言；
                # tr 有缓存，限频渲染下开销可忽略）
                display = tr(str(msg))
                # 转义 msg 中的 HTML 特殊字符，防止路径/进程名含 <>& 破坏渲染
                safe_msg = _html.escape(display)
                self.log_text.append(
                    f'<span style="color:#757575">[{full_ts}]</span> '
                    f'<span style="color:{color};font-weight:bold">[{label}]</span> '
                    f'<span style="color:#263238">{safe_msg}</span>')
        except Exception as e:
            log.error(f"_render_monitor_log异常: {e}")

    def _log_link(self, action, src, dst="", extra=""):
        """记录链接操作 - 同时写入链接日志文件和监控日志cache"""
        log_link_operation(action, src, dst, extra)
        msg = f"{action}: {src}"
        if dst:
            msg += f" -> {dst}"
        if extra:
            msg += f" | {extra}"
        self.on_monitor_log("link", msg)

    def on_alert(self, title, message):
        """右下角弹窗通知 - 防重复"""
        # 防重复：相同标题+消息5秒内不重复弹
        now = time.time()
        key = title + "|" + message[:50]
        if hasattr(self, '_last_alerts') and key in self._last_alerts:
            if now - self._last_alerts[key] < 5:
                return  # 5秒内重复，跳过
        if not hasattr(self, '_last_alerts'):
            self._last_alerts = {}
        self._last_alerts[key] = now
        # 清理超过60秒的记录
        self._last_alerts = {k: v for k, v in self._last_alerts.items() if now - v < 60}

        if self.tray_icon:
            self.tray_icon.showMessage(title, message, QSystemTrayIcon.Warning, 5000)
        # 通过cache机制渲染（支持筛选和1000条限制）
        self.on_monitor_log("alert", f"{title}: {message}")
        log.warning(f"[alert] {title}: {message}")

    def on_installer_detected(self, proc_name):
        """检测到安装器进程 - 日志记录，不弹窗不抢焦点"""
        msg = f"检测到安装器进程: {proc_name}"
        # 只写监控日志（用户可见事件），不写 app.log（避免重复）
        self.on_monitor_log("installer", msg)

    def on_installer_confirm(self, name, pid, exe, cmdline_str, hit_keyword=""):
        """系统级安装器拦截 - 暂停进程后弹窗询问用户决策
        3 个选项：放行并加入信任 / 拒绝并终止 / 稍后手动迁移
        决策通过 monitor_worker.set_decision() 回传给后台等待线程

        特性：
        - 60 秒倒计时实时显示（每秒更新剩余秒数）
        - 超时自动关闭并放行
        - 窗口置顶 + 居中屏幕中央
        - 命中关键字用红色加粗染色显示（RichText）
        """
        import html as _html
        # 截断过长 cmdline 便于显示
        cmd_display = cmdline_str if len(cmdline_str) <= 200 else cmdline_str[:200] + "..."
        # HTML 转义（防止路径/命令中的特殊字符破坏富文本）
        name_esc = _html.escape(name)
        exe_esc = _html.escape(exe)
        cmd_esc = _html.escape(cmd_display)
        # 命中关键字染色：红色加粗（核心信息，让用户一眼看到为何被拦）
        if hit_keyword:
            hit_kw_html = (
                f'<span style="color:#C62828;font-weight:bold">'
                f'{_html.escape(hit_keyword)}</span>'
            )
        else:
            hit_kw_html = '<span style="color:#757575">未提供</span>'

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("⚠ 检测到系统级安装器")
        msg_box.setIcon(QMessageBox.Warning)
        # 强制富文本模式（支持 HTML 染色）
        msg_box.setTextFormat(Qt.RichText)
        # 窗口置顶
        msg_box.setWindowFlags(msg_box.windowFlags() | Qt.WindowStaysOnTopHint)

        btn_allow = msg_box.addButton("放行并加入信任", QMessageBox.AcceptRole)
        btn_kill = msg_box.addButton("拒绝并终止", QMessageBox.RejectRole)
        btn_migrate = msg_box.addButton("稍后手动迁移", QMessageBox.ActionRole)
        msg_box.setDefaultButton(btn_migrate)

        # 倒计时显示（每秒更新）- 富文本格式，<br> 换行
        remaining = [60]  # 用 list 包装以便闭包修改

        def update_text():
            if remaining[0] > 0:
                msg_box.setText(
                    f"⚠ 已暂停以下进程，等待你的决策：<br><br>"
                    f"进程：<b>{name_esc}</b><br>"
                    f"PID：{pid}<br>"
                    f"路径：{exe_esc}<br>"
                    f"命令：{cmd_esc}<br>"
                    f"命中规则：{hit_kw_html}<br><br>"
                    f"⏱ 剩余 <b style='color:#1565C0'>{remaining[0]}</b> 秒（到时自动放行）"
                )
                remaining[0] -= 1
            else:
                # 倒计时归零，关闭对话框（超时放行）
                timer.stop()
                try:
                    msg_box.done(QMessageBox.Rejected)
                except Exception:
                    pass

        update_text()  # 初始显示（60 秒）

        timer = QTimer(self)
        timer.timeout.connect(update_text)
        timer.start(1000)

        # 窗口置顶 + 居中（在 exec() 进入事件循环后立即执行）
        def raise_and_center():
            msg_box.raise_()
            msg_box.activateWindow()
            try:
                from PySide6.QtGui import QGuiApplication
                screen = QGuiApplication.primaryScreen()
                if screen:
                    geo = screen.availableGeometry()
                    msg_box.move(
                        geo.center().x() - msg_box.width() // 2,
                        geo.center().y() - msg_box.height() // 2
                    )
            except Exception:
                pass

        QTimer.singleShot(0, raise_and_center)

        msg_box.exec()
        timer.stop()

        # 处理用户决策
        clicked = msg_box.clickedButton()
        try:
            if clicked == btn_allow:
                # 放行 + 加入白名单（与白名单管理界面共用 whitelist 字段）
                # 系统级安装器统一用进程名作为关键词（winget/choco/scoop/msiexec）
                kw = name.lower()
                try:
                    wl = self.cfg.setdefault("whitelist", [])
                    # 避免重复添加相同关键词
                    if not any(
                        (w.get("keyword") or "").lower() == kw
                        for w in wl if isinstance(w, dict)
                    ):
                        wl.append({"keyword": kw, "desc": f"自动添加(安装器拦截放行)"})
                        save_all(self.cfg)
                        # 同步更新 monitor 内存中的 whitelist
                        if hasattr(self, 'monitor_worker') and self.monitor_worker:
                            self.monitor_worker.whitelist = wl
                except Exception:
                    pass
                self.monitor_worker.set_decision(pid, "allow")
                # 日志由 monitor.py 后台线程统一打印（避免重复）
            elif clicked == btn_kill:
                self.monitor_worker.set_decision(pid, "kill")
                # 日志由 monitor.py 后台线程统一打印（避免重复）
            elif clicked == btn_migrate:
                # 稍后手动迁移
                self.monitor_worker.set_decision(pid, "migrate_later")
                # 日志由 monitor.py 后台线程统一打印（避免重复）
            else:
                # 超时（倒计时归零自动关闭），不回传决策，让后端自己超时处理
                self.on_monitor_log("warn",
                    f"弹窗超时未决策: {name} (PID:{pid})，后端将自动放行")
        except Exception as e:
            # 决策回传失败，默认放行避免进程卡死
            try:
                self.monitor_worker.set_decision(pid, "allow")
            except Exception:
                pass
            log_error_with_reason("安装拦截决策回传失败", str(e),
                f"on_installer_confirm: {name} PID:{pid}")

    def _log_monitor(self, event_type, message):
        """向监控日志发消息（主线程安全）"""
        try:
            self.on_monitor_log(event_type, message)
        except Exception:
            pass

    def _refresh_log_only(self):
        """刷新监控日志 - 从 监控日志.log 文件读取最近1000条到cache，再渲染"""
        try:
            if not MONITOR_LOG_FILE.exists():
                self._monitor_log_cache = []
                self._render_monitor_log()
                self.log_text.append('<span style="color:#757575">暂无监控日志</span>')
                self.status_label.setText("暂无监控日志")
                return
            with open(MONITOR_LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()[-1000:]  # 最多读取1000条
            import re
            # 匹配 [时间] [事件类型] 消息
            # event_type 用通用 [a-zA-Z_]+ 匹配，避免新增类型时漏改正则导致 unknown
            line_re = re.compile(r'^\[([^\]]+)\]\s+\[([a-zA-Z_]+)\]\s*(.*)$')
            cache = []
            for line in lines:
                line = line.rstrip()
                if not line:
                    continue
                m = line_re.match(line)
                if m:
                    ts_str = m.group(1)
                    etype = m.group(2)
                    msg = m.group(3)
                    cache.append((ts_str, etype, msg))
                else:
                    cache.append(( "", "unknown", line))
            self._monitor_log_cache = cache[-1000:]  # 确保不超过1000条
            self._render_monitor_log()
            self.status_label.setText(f"监控日志已刷新（{len(self._monitor_log_cache)}条）")
        except Exception as e:
            self.status_label.setText(f"刷新日志失败: {e}")
            log_error_with_reason("未知错误", str(e), "_refresh_log_only")
