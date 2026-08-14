#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
C盘拦迁器 - 监控并拦截软件在C盘AppData创建的数据目录
功能：GUI管理 + 系统托盘 + 后台监控 + 自动迁移/还原 + 日志记录
GUI框架：PySide6

本文件定位：MainWindow 骨架（__init__/_build_ui）、通用辅助（eventFilter/apply_threshold/
语言切换）、main() 入口。当前约 1735 行 / 11 个方法，功能已全部分流到 core/ ui/ mft/。
"""

# ============================================================
# ★ AI 开发指引（接手/修改本文件前必读）★
# 配合项目根目录《AI编码防坑指南.md》+《开发规范.md》一起使用，两者优先于本注释。
# ------------------------------------------------------------
# 【本文件只做三件事】
#   1. MainWindow 骨架：__init__、_build_ui（Tab 创建、按钮/布局、通用信号连接）
#   2. 通用辅助：eventFilter、apply_threshold、语言切换、环境诊断等通用入口
#   3. main() 入口：QApplication、单实例锁（CreateMutexW）、崩溃钩子、启动恢复
# 【新功能往哪放——禁止塞进本文件】
#   - 业务逻辑/工具函数    → core/（如 migrator.py、env_check.py、utils.py）
#   - 控件/QThread/Handler → ui/（如 ui_migrate.py、ui_lifecycle.py、ui_workers.py）
#   - MFT 相关            → mft/
#   - 新 Handler 三件套：建 ui/xxx.py（docstring 声明依赖的 MainWindow 属性）
#     → 加入 MainWindow 继承链（本文件 __init__ 前）→ 更新 README
# 【红线（违反即返工）】
#   - 禁止在本文件新增：QThread 子类、requests、winreg、复制引擎调用、
#     psutil 顶层 import、超 50 行的方法（新增方法一律 ≤50 行、单职责）
#   - 禁止 core → ui 依赖（core 不得 import PySide6）
#   - Handler 之间通过 self 调用，禁止互相 import
#   - 方法体 import 必须注释说明原因（本文件已有先例，照抄格式）
# 【改动后必过自检（AI编码防坑指南.md 第五章逐条）】
#   - 不静默失败：每个 except 至少一条日志；兜底降级要打日志说明原因
#   - 不碰用户数据：删除/覆盖/迁移/注册表前校验 + 确认 + 可回滚
#   - 变量使用前已定义、返回值类型一致、无魔法数字/字符串散落
#   - 资源释放：open/CreateFile 配 with/finally，异常路径也要释放
#   - 大目录扫描/网络请求一律放 QThread Worker，主线程禁止
# 【已知遗留（改造需谨慎，勿顺手重排）】
#   - _build_ui 约 1050 行 > 50 行红线（指南 1.2 点名反例，UI 骨架固有体积；
#     如需拆分须新建 _build_tabX 系列方法，保持 UI 行为完全不变）
#   - main() 用 ctypes 做单实例锁/崩溃弹窗（1662/1676 行，入口级职责，保留）
#   - 本文件方法体 import 的 env_check/psutil 均为延迟加载（避免启动开销），
#     新增同款时保持"注释说明 + 局部 import"模式，勿改回顶层 import
# ============================================================


import os
import sys
import json
import shutil
import subprocess
import time
import ctypes
import threading
import string
from pathlib import Path
from datetime import datetime

# ========== 子目录注入 sys.path ==========
# 模块按功能分类到 core/ui/mft 子目录后，通过 sys.path 注入保持原 import 写法不变
# （from config import ... / from ui_widgets import ... / import mft_reader 等）
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
for _sub in ("core", "ui", "mft"):
    _p = os.path.join(_SRC_DIR, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from PySide6.QtCore import Qt, Signal, QObject, QThread, QTimer, QUrl, QEvent
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QCheckBox, QLabel, QLineEdit, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QTextEdit,
    QMessageBox, QSystemTrayIcon, QMenu, QStyle, QProgressBar,
    QFileDialog, QComboBox, QFrame, QSizePolicy, QDialog, QDialogButtonBox,
    QGroupBox, QScrollArea, QSpinBox, QAbstractSpinBox
)
from PySide6.QtGui import QColor, QAction, QDesktopServices, QFont, QCursor, QIcon, QBrush

from config import (
    APP_NAME, APP_VERSION, CONFIG_DIR, CONFIG_FILE, LOG_FILE, LINK_LOG_FILE,
    MONITOR_LOG_FILE, SOFTWARE_DICT_FILE, RECOGNITION_LOG_FILE,
    ERROR_LOG_FILE, LOG_DIR, G_ROOT, AI_KEYS_FILE, APP_ICON_FILE,
    setup_logging, load_config, save_config, save_state, load_state, save_all, is_admin,
    log_link_operation, log_error_with_reason, KNOWN_SOFTWARE_DIRS, COMBO_MAP,
    load_ai_keys, save_ai_keys
)
from i18n import load_language, apply_language, available_languages, tr, current_language, patch_message_boxes, patch_input_dialogs
from utils import (
    is_symlink, get_symlink_target, get_dir_size_fast,
    get_exe_version_info, _read_lnk_target, _match_registry_uninstall
)
from software_detect import get_dir_description
from migrator import Migrator, auto_thread_count
from monitor import ScanWorker, MonitorWorker, SmartScanWorker
from dev_env_migrate import (
    TOOLS as DEV_TOOLS, get_tool_status as dev_get_tool_status,
    apply_tool as dev_apply_tool, get_suggest_path as dev_get_suggest_path,
    unconfigure_tool as dev_unconfigure_tool,
    unapply_tool as dev_unapply_tool,
    collect_original_dir_structure as dev_collect_original_dir_structure,
    get_tool_data_info as dev_get_tool_data_info,
    migrate_tool_data as dev_migrate_tool_data,
    get_tool_default_c_path as dev_get_tool_default_c_path,
    cleanup_bad_env_vars as dev_cleanup_bad_env_vars,
    GITHUB_URLS as DEV_GITHUB_URLS,
)
import dev_env_snapshot as dev_snapshot
from ui_devenv import DevEnvHandler
from ui_snapshot import SnapshotHandler
from ui_ai import AIHandler
from ui_whitelist import WhitelistHandler
from ui_scan import ScanHandler
from ui_migrate import MigrateHandler
from ui_monitor_log import MonitorLogHandler
from ui_lifecycle import LifecycleHandler
from ui_widgets import (
    NumericTableWidgetItem, WideEditorDelegate, NoElideDelegate,
    _format_size, _apply_size_item_color, OneLineLabel,
)
from ui_workers import (
    DevEnvRefreshWorker, DevEnvSizeWorker,
    DevToolDownloadWorker, DevEnvApplyWorker,
    _DEV_TOOL_DOWNLOAD_APIS, _get_arch_suffix,
)
import logging
log = logging.getLogger('CDriveRelocator')

# ========== 全局样式表 ==========

MODERN_QSS = """
/* ===== 全局背景与字体 ===== */
QMainWindow, QWidget {
    background-color: #F5F6FA;
    color: #263238;
    font-family: "Microsoft YaHei", "微软雅黑";
    font-size: 13px;
}

/* ===== 卡片容器 ===== */
QFrame#card, QFrame#cardHeader, QFrame#cardToolbar {
    background-color: #FFFFFF;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
}

/* ===== 按钮通用样式 - 统一淡绿色 ===== */
QPushButton {
    color: #FFFFFF;
    border: none;
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 13px;
    font-family: "Microsoft YaHei", "微软雅黑";
    text-align: center;
    min-width: 70px;
    background-color: #66BB6A;
}
QPushButton:hover {
    background-color: #4CAF50;
}
QPushButton:pressed {
    background-color: #388E3C;
}
QPushButton:disabled {
    background-color: #BDBDBD;
    color: #EEEEEE;
}

/* ===== 复选框与标签 ===== */
QCheckBox, QLabel {
    font-family: "Microsoft YaHei", "微软雅黑";
    font-size: 12px;
    color: #263238;
    background: transparent;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #BDBDBD;
    border-radius: 3px;
    background-color: #FFFFFF;
}
QCheckBox::indicator:hover {
    border: 1px solid #1976D2;
}
QCheckBox::indicator:checked {
    background-color: #1976D2;
    border: 1px solid #1976D2;
}

/* ===== 表格 ===== */
QTableWidget {
    background-color: #FFFFFF;
    alternate-background-color: #FAFAFA;
    border: 1px solid #E0E0E0;
    border-radius: 4px;
    gridline-color: #E0E0E0;
    selection-background-color: #BBDEFB;
    selection-color: #263238;
    font-size: 13px;
    outline: none;
}
QTableWidget::item {
    padding: 5px 4px;
}
QTableWidget::item:selected {
    background-color: #BBDEFB;
    color: #263238;
}
QHeaderView::section {
    background-color: #1976D2;
    color: #FFFFFF;
    padding: 8px 6px;
    border: none;
    border-right: 1px solid #1565C0;
    font-weight: bold;
    font-size: 13px;
}
QHeaderView::section:hover {
    background-color: #1565C0;
}
QHeaderView::section:last {
    border-right: none;
}
QTableCornerButton::section {
    background-color: #1976D2;
    border: none;
}

/* ===== 标签页 ===== */
QTabWidget::pane {
    border: 1px solid #E0E0E0;
    border-top: none;
    border-radius: 0 0 8px 8px;
    background-color: #FFFFFF;
    top: -1px;
}
QTabBar::tab {
    background-color: #F5F6FA;
    color: #757575;
    padding: 8px 20px;
    border: 1px solid #E0E0E0;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
    font-size: 13px;
    font-family: "Microsoft YaHei", "微软雅黑";
}
QTabBar::tab:selected {
    background-color: #FFFFFF;
    color: #1976D2;
    font-weight: bold;
}
QTabBar::tab:hover:!selected {
    background-color: #E3F2FD;
    color: #1976D2;
}

/* ===== 进度条 ===== */
QProgressBar {
    background-color: #E0E0E0;
    border: none;
    border-radius: 8px;
    text-align: center;
    color: #263238;
    font-size: 12px;
    min-height: 16px;
    max-height: 16px;
}
QProgressBar::chunk {
    background-color: #1976D2;
    border-radius: 8px;
}

/* ===== 输入框与下拉框 ===== */
QLineEdit, QComboBox {
    background-color: #FFFFFF;
    border: 1px solid #E0E0E0;
    border-radius: 4px;
    padding: 4px 6px;
    font-size: 13px;
    color: #263238;
    font-family: "Microsoft YaHei", "微软雅黑";
}
QLineEdit:focus, QComboBox:focus {
    border: 1px solid #1976D2;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background-color: #FFFFFF;
    border: 1px solid #E0E0E0;
    border-radius: 4px;
    selection-background-color: #BBDEFB;
    selection-color: #263238;
    padding: 4px;
    outline: none;
}

/* ===== 状态栏 ===== */
QStatusBar {
    background-color: #FFFFFF;
    border-top: 1px solid #E0E0E0;
    color: #616161;
    font-size: 12px;
}
QStatusBar::item {
    border: none;
}
QStatusBar QLabel {
    color: #616161;
}

/* ===== 菜单 ===== */
QMenu {
    background-color: #FFFFFF;
    border: 1px solid #E0E0E0;
    border-radius: 6px;
    padding: 4px;
    font-size: 13px;
}
QMenu::item {
    padding: 6px 24px;
    border-radius: 4px;
}
QMenu::item:selected {
    background-color: #BBDEFB;
    color: #1976D2;
}
QMenu::separator {
    height: 1px;
    background-color: #E0E0E0;
    margin: 4px 8px;
}

/* ===== 滚动条 ===== */
QScrollBar:vertical {
    background-color: #F5F6FA;
    width: 10px;
    border: none;
    margin: 0;
}
QScrollBar::handle:vertical {
    background-color: #BDBDBD;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background-color: #9E9E9E;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
    border: none;
    background: none;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none;
}
QScrollBar:horizontal {
    background-color: #F5F6FA;
    height: 10px;
    border: none;
    margin: 0;
}
QScrollBar::handle:horizontal {
    background-color: #BDBDBD;
    border-radius: 5px;
    min-width: 30px;
}
QScrollBar::handle:horizontal:hover {
    background-color: #9E9E9E;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
    border: none;
    background: none;
}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: none;
}

/* ===== 文本编辑框 ===== */
QTextEdit {
    background-color: #FFFFFF;
    border: 1px solid #E0E0E0;
    border-radius: 4px;
    font-family: "Microsoft YaHei", "微软雅黑";
    color: #263238;
    padding: 4px;
}
QTextEdit:focus {
    border: 1px solid #1976D2;
}

/* ===== 工具提示 ===== */
QToolTip {
    background-color: #263238;
    color: #FFFFFF;
    border: none;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
}
"""
# ========== 独立控件与 Worker 已移至 ui_widgets.py / ui_workers.py ==========

class MainWindow(QMainWindow, DevEnvHandler, SnapshotHandler, AIHandler, WhitelistHandler, ScanHandler, MigrateHandler, MonitorLogHandler, LifecycleHandler):
    def __init__(self, config):
        super().__init__()
        self.cfg = config
        self.migrator = Migrator(self.cfg)
        # migrator.log_callback 默认为 None，普通迁移区（待迁移区/已迁移区）不输出
        # 详细阶段日志，避免刷屏。开发工具迁移区的 worker 会临时设置 log_callback
        # 通过自己的 verbose_log_sig 信号转发到监控日志。
        self.monitor_thread = None
        self.monitor_worker = None
        self.scan_thread = None
        self.scan_worker = None
        self.smart_scan_thread = None
        self.smart_scan_worker = None
        self._online_thread = None
        self.tray_icon = None
        self._force_quit = False
        self._monitor_log_cache = []  # 监控日志缓存：[(full_ts, event_type, message), ...]，最多1000条
        # 已取消但可能仍在 os.walk 的后台 worker 引用列表（防止 GC 导致 QThread C++ 对象
        # 在线程仍运行时被销毁，引发 segfault——表现是程序突然闪退且无 Python 异常日志）
        # worker 真正 finished 后会自动从该列表移除并 deleteLater
        self._old_dev_env_workers = []

        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(1400, 900)
        self.setMinimumSize(1100, 720)
        self.setStyleSheet(MODERN_QSS)

        self._build_ui()
        # 界面语言：加载语言包并刷新全部控件文案（语言包缺失时回退中文原文）
        load_language(self.cfg.get("language", "zh_cn"))
        patch_message_boxes()
        patch_input_dialogs()
        apply_language(self)
        log.info("UI构建完成")
        # 启动时异步恢复未完成的迁移/还原事务（断电/崩溃保护）
        # 必须异步：recover_pending_* 内部会执行复制引擎，同步调用会阻塞 UI 导致白屏假死
        # 监控线程不依赖恢复结果，可以并行启动
        self._recover_worker = None

        def _on_recover_done(results):
            # 1. 显示事务恢复结果
            if results:
                self._show_recovery_results(results)
            # 2. 事务恢复完成后，扫描并修复历史链式符号链接 + 多对一冲突
            #    三步修复：链式套娃→多对一冲突→中间节点清理，保证一对一连接
            try:
                fixed, scanned, details = self.migrator.fix_chain_symlinks()
                if fixed > 0:
                    log.info(f"启动修复 {fixed}/{scanned} 个链式符号链接")
                    self.on_monitor_log("init",
                        f"🔗 检测到 {fixed} 个链式符号链接，已修复为直指真实数据"
                        f"（多对一冲突已清理为一对一）")
                    for src, old_dst, new_dst in details[:5]:
                        log.info(f"  链式修复: {src} | {old_dst} → {new_dst}")
            except Exception as e:
                log.error(f"启动修复链式链接异常: {e}")

        QTimer.singleShot(500, lambda: self._start_async_recover(
            recover_type="both",
            on_done_callback=_on_recover_done))
        self._start_monitor()
        log.info("监控启动完成")
        # 后台构建已迁移目标目录轻量索引（跨盘校对值，防删除记录/恢复卡顿）
        # 只对 MFT 未覆盖的跨盘目标 os.walk 构建，后台线程执行不卡 UI
        try:
            threading.Thread(
                target=self.migrator.build_all_dst_indexes, daemon=True).start()
        except Exception as e:
            log.debug("忽略异常: %s", e)
        self._setup_tray()
        log.info("托盘设置完成")
        # 启动时加载缓存，不重新扫描
        self._load_cache()
        # 首次在本机运行时自动创建开发环境快照（作为"还原到初始状态"的底线）
        # 延迟 2.5 秒执行，避免阻塞窗口显示
        QTimer.singleShot(2500, self._first_run_auto_snapshot)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        # ===== a. 标题栏卡片 =====
        header_card = QFrame()
        header_card.setObjectName("cardHeader")
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(18, 14, 18, 14)
        header_layout.setSpacing(8)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_main = QLabel(APP_NAME)
        title_main.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #263238; background: transparent;")
        title_sub = QLabel("监控·拦截·迁移·修复 全方位守护C盘")
        title_sub.setStyleSheet(
            "font-size: 12px; color: #757575; background: transparent;")
        title_box.addWidget(title_main)
        # 副标题行：介绍词 + 免责声明（右侧，同排）
        # 免责声明：部分操作涉及系统底层（文件索引/链接迁移），可能触发安全软件提示；
        # 文案双语（i18n 翻译表），hover 显示完整说明
        sub_row = QHBoxLayout()
        sub_row.setSpacing(10)
        sub_row.addWidget(title_sub)
        _disc_note = tr("注意：")
        _disc_body = tr("部分操作可能触发安全软件提示；本软件无毒、无后台程序，可查源码")
        disclaimer = QLabel(
            f'<span style="color:#E53935; font-weight:bold; font-size:12px;">{_disc_note}</span>'
            f'<span style="color:#757575; font-size:12px;">{_disc_body}</span>')
        disclaimer.setStyleSheet("background: transparent;")
        disclaimer.setToolTip(tr(
            "本软件部分操作（如深层文件索引、目录链接迁移）涉及系统底层，"
            "可能触发安全软件提醒。本软件无毒、无任何后台程序、不联网上传数据，"
            "源码可供查验。"))
        sub_row.addWidget(disclaimer)
        title_box.addLayout(sub_row)
        header_layout.addLayout(title_box)
        header_layout.addStretch()

        # 管理员状态指示器
        admin_label = QLabel()
        if is_admin():
            admin_label.setText("● 管理员")
            admin_label.setStyleSheet(
                "color: #43A047; font-size: 13px; font-weight: bold; background: transparent;")
        else:
            admin_label.setText("● 未提权")
            admin_label.setStyleSheet(
                "color: #E53935; font-size: 13px; font-weight: bold; background: transparent;")
        header_layout.addWidget(admin_label)

        # 语言切换下拉框（i18n）——暂时隐藏：切换功能仍有边界问题待完善，
        # 先保留框架（语言包/tr 机制），后续完善后取消 hide() 即恢复入口
        self.language_combo = QComboBox()
        self.language_combo.setFixedWidth(90)
        # 标记跳过翻译（语言名本身不被控件树翻译刷新）
        self.language_combo.setProperty("i18n_skip", True)
        for code, label in available_languages():
            self.language_combo.addItem(label, code)
        idx = self.language_combo.findData(current_language())
        if idx >= 0:
            self.language_combo.setCurrentIndex(idx)
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        self.language_combo.hide()
        header_layout.addWidget(self.language_combo)
        layout.addWidget(header_card)

        # ===== b. 工具栏卡片 =====
        toolbar_card = QFrame()
        toolbar_card.setObjectName("cardToolbar")
        toolbar_outer = QVBoxLayout(toolbar_card)
        toolbar_outer.setContentsMargins(14, 8, 14, 8)
        toolbar_outer.setSpacing(6)

        # ===== 顶部工具栏按钮区（单行4个按钮） =====
        btn_grid = QHBoxLayout()
        btn_grid.setSpacing(8)

        # 按钮组（带图标）
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setObjectName("btn_refresh")
        self.btn_refresh.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))

        self.btn_whitelist = QPushButton("白名单")
        self.btn_whitelist.setObjectName("btn_whitelist")
        self.btn_whitelist.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))

        # 清空缓存按钮：清空scan_cache+识别记录.json
        self.btn_clear_cache = QPushButton("清空缓存")
        self.btn_clear_cache.setObjectName("btn_clear_cache")
        self.btn_clear_cache.setIcon(self.style().standardIcon(QStyle.SP_DialogDiscardButton))
        self.btn_clear_cache.setToolTip("清空待迁移扫描缓存和识别记录（不影响已迁移记录和白名单）")
        self.btn_clear_cache.clicked.connect(self._clear_cache)
        # 重启程序按钮
        self.btn_restart = QPushButton("重启程序")
        self.btn_restart.setObjectName("btn_restart")
        self.btn_restart.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.btn_restart.setToolTip("重启C盘拦迁器（保存当前配置后重启）")
        self.btn_restart.clicked.connect(self._restart_app)
        # 环境诊断按钮：一键检查管理员权限/Rust 引擎/回收站/符号链接/目标盘/还原点
        self.btn_env_diag = QPushButton("环境诊断")
        self.btn_env_diag.setObjectName("btn_env_diag")
        self.btn_env_diag.setIcon(self.style().standardIcon(QStyle.SP_MessageBoxInformation))
        self.btn_env_diag.setToolTip(
            "一键检查运行环境：管理员权限、Rust 引擎、回收站可用性、\n"
            "符号链接权限、目标盘状态、系统还原点占用")
        self.btn_env_diag.clicked.connect(self.show_env_diagnosis)

        # 单行4个按钮
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        for btn in (self.btn_refresh, self.btn_whitelist, self.btn_clear_cache, self.btn_restart, self.btn_env_diag):
            btn.setMinimumWidth(80)
            btn.setMaximumWidth(120)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            row1.addWidget(btn)

        toolbar_outer.addLayout(row1)

        # ===== 第二排：选项区 =====
        # 顺序：[迁移后清理还原点] [自动拦截] 阈值(MB) — 阈值与自动拦截关联（监控大目录弹窗也用它），放一起更直观
        opt_row = QHBoxLayout()
        opt_row.setSpacing(8)
        self.chk_clean_vss = QCheckBox("迁移后清理还原点")
        self.chk_clean_vss.setChecked(self.cfg.get("auto_clean_vss", True))
        self.chk_clean_vss.setToolTip(
            "迁移/还原删除大量文件后，Windows 系统还原机制仍保留文件旧版本快照\n"
            "导致 C 盘空间看起来没释放。开启后自动清理系统还原点（卷影副本），立即释放空间。\n"
            "（需管理员权限，会删除系统还原点）")
        opt_row.addWidget(self.chk_clean_vss)
        self.chk_auto = QCheckBox("自动拦截")
        self.chk_auto.setChecked(self.cfg.get("auto_migrate", False))
        self.chk_auto.setToolTip(
            "开启后，监控到安装程序向 C 盘写入大量文件时自动拦截（暂停进程并询问）。\n"
            "关闭时仅记录日志，不主动拦截。\n"
            "建议：日常开启；若误杀正常软件安装可临时关闭。")
        opt_row.addWidget(self.chk_auto)
        opt_row.addWidget(QLabel("阈值(MB):"))
        self.edit_threshold = QLineEdit(str(self.cfg.get("size_threshold", 50)))
        self.edit_threshold.setFixedWidth(50)
        self.edit_threshold.setToolTip(
            "监控告警阈值（单位 MB）。\n"
            "后台监控发现 C 盘新建目录大小达到此值时，弹『发现大目录』告警提醒迁移。\n"
            "默认 50MB。值越小告警越频繁，值越大只关注大目录。\n")
        opt_row.addWidget(self.edit_threshold)
        opt_row.addSpacing(10)
        # 用户目录写入提醒（右下角气泡开关；气泡上点"不再提醒"也会关闭它）
        self.chk_user_dir_notify = QCheckBox("用户目录写入提醒")
        self.chk_user_dir_notify.setChecked(self.cfg.get("user_dir_notify_enabled", True))
        self.chk_user_dir_notify.setToolTip(
            "监控当前用户目录下新建目录，右下角气泡提醒（只提醒不拦截）。\n"
            "AI 工具/开发工具常往用户目录写缓存数据，提醒可及时发现膨胀。\n"
            "气泡上点'不再提醒'也会关闭此开关。")
        opt_row.addWidget(self.chk_user_dir_notify)
        opt_row.addSpacing(10)
        # 复制选项:P5 用户可选(哈希校验开关 + 线程数),避免冷盘校验太慢
        self.chk_verify = QCheckBox("复制校验")
        self.chk_verify.setChecked(self.cfg.get("verify_hash", True))
        self.chk_verify.setToolTip(
            "复制后执行 BLAKE3 哈希校验(逐文件内容比对,防数据残缺)。\n"
            "开启:数据最安全,但冷盘校验耗时约等于复制时间的一半。\n"
            "关闭:只保证文件数/大小一致,速度快。")
        opt_row.addWidget(self.chk_verify)
        opt_row.addWidget(QLabel("线程:"))
        # 复制线程数：初始值按 CPU 低/中/高端自动分配，用户直接输入数字（上限=CPU 逻辑线程数）
        self.spin_threads = QSpinBox()
        self.spin_threads.setButtonSymbols(QAbstractSpinBox.NoButtons)  # 纯输入，去掉上下调节按钮
        _cpu_max = os.cpu_count() or 4
        self.spin_threads.setRange(1, _cpu_max)
        if self.cfg.get("copy_threads_auto", True):
            _init_threads = auto_thread_count(_cpu_max)
        else:
            _init_threads = int(self.cfg.get("copy_threads", auto_thread_count(_cpu_max)))
        self.spin_threads.setValue(max(1, min(_init_threads, _cpu_max)))
        self.spin_threads.setSuffix("")
        self.spin_threads.setToolTip(
            f"复制/校验线程数，可直接输入（1~{_cpu_max}）。\n"
            "初始值已按 CPU 低/中/高端自动分配；\n"
            "机械硬盘推荐 10-12；固态硬盘推荐 8-16。")
        opt_row.addWidget(self.spin_threads)
        self.spin_threads.valueChanged.connect(self._on_threads_changed)
        opt_row.addSpacing(10)
        # 迁移到 - 按钮形式，点击选目录
        opt_row.addWidget(QLabel("迁移到:"))
        self._migrate_dir_label = QLabel(self.cfg.get("g_root", G_ROOT))
        self._migrate_dir_label.setStyleSheet(
            "color: #1565C0; font-weight: bold; padding: 0 4px; background: transparent;")
        opt_row.addWidget(self._migrate_dir_label)
        self.btn_browse_dir = QPushButton("请输入目录")
        self.btn_browse_dir.setObjectName("btn_browse_dir")
        self.btn_browse_dir.setToolTip("选择迁移目标目录")
        self.btn_browse_dir.clicked.connect(self._browse_migrate_dir)
        opt_row.addWidget(self.btn_browse_dir)
        self.btn_apply = QPushButton("应用")
        self.btn_apply.setObjectName("btn_apply")
        opt_row.addWidget(self.btn_apply)
        opt_row.addStretch()
        # 开机启动
        self.chk_autostart = QCheckBox("开机启动")
        self.chk_autostart.setChecked(self._is_autostart_enabled())
        opt_row.addWidget(self.chk_autostart)
        # 文件搜索框（基于 MFT 索引，需先刷新加载 MFT）
        opt_row.addSpacing(10)
        opt_row.addWidget(QLabel("搜索:"))
        self.edit_search = QLineEdit()
        self.edit_search.setPlaceholderText("文件名（如 *.exe 或 test）")
        self.edit_search.setFixedWidth(200)
        self.edit_search.returnPressed.connect(self._on_search_files)
        opt_row.addWidget(self.edit_search)
        self.btn_search = QPushButton("搜索")
        self.btn_search.setObjectName("btn_search")
        self.btn_search.setToolTip("在 C 盘搜索文件名（基于 MFT 索引，需先刷新加载 MFT）")
        self.btn_search.clicked.connect(self._on_search_files)
        opt_row.addWidget(self.btn_search)
        toolbar_outer.addLayout(opt_row)

        layout.addWidget(toolbar_card)

        # 进度条（加粗 + 渐变动态样式）
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setFixedHeight(24)
        self.progress.setTextVisible(True)
        self.progress.setAlignment(Qt.AlignCenter)
        self.progress.setStyleSheet("""
            QProgressBar {
                border: 2px solid #1565C0;
                border-radius: 6px;
                background-color: #E3F2FD;
                text-align: center;
                font-weight: bold;
                font-size: 13px;
                color: #0D47A1;
            }
            QProgressBar::chunk {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #42A5F5, stop:0.5 #1E88E5, stop:1 #1565C0);
                border-radius: 4px;
                margin: 1px;
            }
        """)
        layout.addWidget(self.progress)

        # ===== d. 标签页区域（主卡片） =====
        tabs_card = QFrame()
        tabs_card.setObjectName("card")
        tabs_card_layout = QVBoxLayout(tabs_card)
        tabs_card_layout.setContentsMargins(8, 8, 8, 8)
        self.tabs = QTabWidget()
        tabs_card_layout.addWidget(self.tabs)
        layout.addWidget(tabs_card, stretch=1)

        # Tab1: 已迁移
        tab1 = QWidget()
        t1 = QVBoxLayout(tab1)
        t1.setContentsMargins(0, 0, 0, 0)
        self.table_migrated = QTableWidget(0, 7)
        # 已迁移区支持任意盘间迁移（含 D→D / D→E），首列显示源路径而非"C盘路径"
        self.table_migrated.setHorizontalHeaderLabels(["源路径", "目标盘", "大小(MB)", "状态", "目标路径", "说明", "迁移时间"])
        header_m = self.table_migrated.horizontalHeader()
        header_m.setSectionResizeMode(QHeaderView.Interactive)
        # 每列最小列宽100px（防止拖太窄直接变三个点）
        header_m.setMinimumSectionSize(100)
        header_m.setDefaultSectionSize(120)
        header_m.resizeSection(0, 420)  # 源路径（加宽：路径完整显示更久才到边界）
        header_m.resizeSection(1, 420)  # 目标盘（原200太窄，显示28字符就出三点）
        header_m.resizeSection(2, 100)
        header_m.resizeSection(3, 120)
        header_m.resizeSection(4, 400)  # 目标路径（路径列，同步加宽）
        header_m.resizeSection(5, 200)
        header_m.resizeSection(6, 150)
        # 去省略号：view.setTextElideMode 在本环境渲染不生效，
        # 用 NoElideDelegate 从绘制层强制无"..."（实测有效）
        self.table_migrated.setTextElideMode(Qt.ElideNone)
        self.table_migrated.setItemDelegate(NoElideDelegate(self.table_migrated))
        self.table_migrated.setWordWrap(False)
        self.table_migrated.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_migrated.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table_migrated.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table_migrated.setAlternatingRowColors(True)
        # 启用点击列标题排序（填充时需先setSortingEnabled(False)再恢复）
        self.table_migrated.setSortingEnabled(True)
        header_m.setSectionsClickable(True)
        header_m.setSortIndicatorShown(True)
        header_m.setToolTip("点击列标题排序，再次点击切换升序/降序")
        # 双击打开目标目录
        self.table_migrated.cellDoubleClicked.connect(lambda row, col: self._open_path(self.table_migrated.item(row, 1).text()))
        # 右键菜单
        self.table_migrated.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table_migrated.customContextMenuRequested.connect(self._migrated_context_menu)
        # 搜索框 + 提示标签 + 统计 + 刷新按钮
        search_row1 = QHBoxLayout()
        search_lbl1 = QLabel("搜索:")
        search_lbl1.setStyleSheet("color: #424242; font-size: 12px; padding: 2px;")
        search_row1.addWidget(search_lbl1)
        self.search_migrated = QLineEdit()
        self.search_migrated.setPlaceholderText("输入关键词过滤已迁移软件（路径/说明/状态）...")
        self.search_migrated.setClearButtonEnabled(True)
        self.search_migrated.textChanged.connect(lambda: self._filter_table(self.table_migrated, self.search_migrated.text()))
        search_row1.addWidget(self.search_migrated, stretch=2)
        tip_row1 = QHBoxLayout()
        tip1 = QLabel("提示：双击行可直接打开目标盘目录 | 右键更多操作（修复/还原/删除等）| 链接目标列显示符号链接实际指向（灰色=非符号链接）")
        tip1.setStyleSheet("color: #1565C0; font-size: 12px; padding: 2px;")
        tip_row1.addWidget(tip1, stretch=1)
        self.stat_migrated = QLabel("共0项")
        self.stat_migrated.setStyleSheet("color: #424242; font-size: 12px; font-weight: bold; padding: 2px 8px; background-color: #F5F5F5; border-radius: 8px; border: 1px solid #BDBDBD;")
        tip_row1.addWidget(self.stat_migrated)
        self.btn_refresh_migrated = QPushButton("刷新已迁移")
        self.btn_refresh_migrated.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.btn_refresh_migrated.clicked.connect(self._refresh_migrated_only)
        tip_row1.addWidget(self.btn_refresh_migrated)
        # 删除记录恢复：列出「删除记录（只删记录不动文件）」留下的线索，校验后恢复
        self.btn_recover_deleted = QPushButton("♻️ 删除记录恢复")
        self.btn_recover_deleted.setObjectName("btn_recover_deleted")
        self.btn_recover_deleted.setToolTip(
            "恢复被「删除记录」操作移除的迁移记录：\n"
            "按记录校验目标盘数据（文件数+大小），一致则恢复迁移记录\n"
            "（若 C 盘链接也已被删则一并重建链接）；\n"
            "数据有差异或目标丢失时给出提示。")
        self.btn_recover_deleted.clicked.connect(self.recover_deleted_links)
        tip_row1.addWidget(self.btn_recover_deleted)
        # 扫描卸载残留：C 盘链接已消失但目标盘数据仍残留
        self.btn_scan_orphan = QPushButton("扫描卸载残留")
        self.btn_scan_orphan.setObjectName("btn_scan_orphan")
        self.btn_scan_orphan.setIcon(self.style().standardIcon(QStyle.SP_DialogDiscardButton))
        self.btn_scan_orphan.setToolTip(
            "扫描所有迁移记录，找出 C 盘符号链接已消失（软件可能已卸载）\n"
            "但目标盘数据仍残留的卸载残留数据，支持批量清理释放空间。\n\n"
            "扫描范围：所有盘符（C-Z）的目标路径。\n"
            "⚠️ 重装系统后请先点「重建全部链接」恢复，不要用此功能删数据！")
        self.btn_scan_orphan.clicked.connect(self.scan_orphan_data)
        tip_row1.addWidget(self.btn_scan_orphan)
        # 重装系统后一键重建所有链接（显眼按钮，橙色背景）
        self.btn_rebuild_all = QPushButton("🔄 重建全部链接")
        self.btn_rebuild_all.setObjectName("btn_rebuild_all")
        self.btn_rebuild_all.setStyleSheet(
            "QPushButton { background-color: #FF6F00; color: white; font-weight: bold; "
            "padding: 5px 14px; border-radius: 6px; font-size: 13px; }"
            "QPushButton:hover { background-color: #FF8F00; }"
            "QPushButton:pressed { background-color: #E65100; }")
        self.btn_rebuild_all.setToolTip(
            "适用于重装系统后的恢复场景\n\n"
            "重装系统后 C 盘符号链接全部丢失，但其他盘数据还在。\n"
            "点击此按钮自动扫描所有迁移记录，\n"
            "重新在 C 盘创建符号链接指向目标盘数据。\n\n"
            "⚠ 注意：不会移动或删除任何数据，只创建符号链接。")
        self.btn_rebuild_all.clicked.connect(self._rebuild_all_links_wizard)
        tip_row1.addWidget(self.btn_rebuild_all)
        t1.addLayout(search_row1)
        t1.addLayout(tip_row1)
        t1.addWidget(self.table_migrated)
        self.tabs.addTab(tab1, "已迁移 / 符号链接")

        # Tab2: 待迁移
        tab2 = QWidget()
        t2 = QVBoxLayout(tab2)
        t2.setContentsMargins(0, 0, 0, 0)
        self.table_scan = QTableWidget(0, 5)
        self.table_scan.setHorizontalHeaderLabels(["C盘路径", "位置", "大小", "目录名", "说明"])
        # 所有列设为Interactive，用户可自由拖动列宽
        header_s = self.table_scan.horizontalHeader()
        header_s.setSectionResizeMode(QHeaderView.Interactive)
        # 每列最小列宽100px（防止拖太窄直接变三个点）
        header_s.setMinimumSectionSize(100)
        header_s.setDefaultSectionSize(120)
        # 设置默认列宽
        header_s.resizeSection(0, 420)  # C盘路径（加宽：路径显示更久才到边界）
        header_s.resizeSection(1, 100)  # 位置
        header_s.resizeSection(2, 140)  # 大小（加宽显示KB/MB单位）
        header_s.resizeSection(3, 150)  # 目录名
        header_s.resizeSection(4, 420)  # 说明（加宽，长说明也能显示完整）
        # 去省略号：NoElideDelegate 绘制层强制无"..."（实测有效）
        self.table_scan.setTextElideMode(Qt.ElideNone)
        self.table_scan.setItemDelegate(NoElideDelegate(self.table_scan))
        self.table_scan.setWordWrap(False)
        self.table_scan.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_scan.setSelectionMode(QTableWidget.ExtendedSelection)  # 支持多选
        # 允许双击编辑"说明"列（第4列），其他列不可编辑
        self.table_scan.setEditTriggers(QTableWidget.DoubleClicked)
        self.table_scan.itemChanged.connect(self._on_scan_item_changed)
        # 说明列使用宽编辑器委托，编辑时输入框不会太小
        self.table_scan.setItemDelegateForColumn(4, WideEditorDelegate(self.table_scan))
        self.table_scan.setAlternatingRowColors(True)
        # 启用点击列标题排序（填充时需先setSortingEnabled(False)再恢复）
        self.table_scan.setSortingEnabled(True)
        header_s.setSectionsClickable(True)
        header_s.setSortIndicatorShown(True)
        header_s.setToolTip("点击列标题排序，再次点击切换升序/降序")
        # 右键菜单
        self.table_scan.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table_scan.customContextMenuRequested.connect(self._scan_context_menu)
        # 搜索框 + 提示标签 + 统计 + 刷新按钮
        search_row2 = QHBoxLayout()
        search_lbl2 = QLabel("搜索:")
        search_lbl2.setStyleSheet("color: #424242; font-size: 12px; padding: 2px;")
        search_row2.addWidget(search_lbl2)
        self.search_scan = QLineEdit()
        self.search_scan.setPlaceholderText("输入关键词过滤待迁移软件（路径/目录名/说明）...")
        self.search_scan.setClearButtonEnabled(True)
        self.search_scan.textChanged.connect(lambda: self._filter_table(self.table_scan, self.search_scan.text()))
        search_row2.addWidget(self.search_scan, stretch=2)
        # 联网补全说明按钮（2026-08-10 暂隐藏：免费数据源质量差+慢，与 AI 识别功能重叠，
        # 代码保留，待有更好的数据源/方案再启用——恢复时取消下面注释即可）
        # self.btn_online_search = QPushButton("联网补全说明")
        # self.btn_online_search.setIcon(self.style().standardIcon(QStyle.SP_CommandLink))
        # self.btn_online_search.setToolTip("对当前表格中说明为空的条目联网搜索软件来源，自动补全说明（Wikipedia 百科）")
        # self.btn_online_search.clicked.connect(self._online_search_descriptions)
        # search_row2.addWidget(self.btn_online_search)
        self.btn_ai_recognize = QPushButton("AI智能识别")
        self.btn_ai_recognize.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.btn_ai_recognize.setToolTip("用大模型AI批量识别目录（需在设置页填API Key，支持智谱/硅基流动/DeepSeek等）")
        self.btn_ai_recognize.clicked.connect(self._ai_recognize_descriptions)
        search_row2.addWidget(self.btn_ai_recognize)
        self.btn_ai_settings = QPushButton("AI设置")
        self.btn_ai_settings.setIcon(self.style().standardIcon(QStyle.SP_FileDialogListView))
        self.btn_ai_settings.setToolTip("配置 AI 大模型识别（API Key、平台选择等）")
        self.btn_ai_settings.clicked.connect(self._open_ai_settings)
        search_row2.addWidget(self.btn_ai_settings)
        tip_row2 = QHBoxLayout()
        tip2 = QLabel("提示：双击行可直接打开C盘目录 | 右键可批量迁移/打开/复制路径")
        tip2.setStyleSheet("color: #1565C0; font-size: 12px; padding: 2px;")
        tip_row2.addWidget(tip2, stretch=1)
        self.stat_scan = QLabel("共0项")
        self.stat_scan.setStyleSheet("color: #424242; font-size: 12px; font-weight: bold; padding: 2px 8px; background-color: #F5F5F5; border-radius: 8px; border: 1px solid #BDBDBD;")
        tip_row2.addWidget(self.stat_scan)
        self.btn_refresh_scan = QPushButton("刷新待迁移")
        self.btn_refresh_scan.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.btn_refresh_scan.clicked.connect(self.smart_refresh_scan)
        tip_row2.addWidget(self.btn_refresh_scan)
        t2.addLayout(search_row2)
        t2.addLayout(tip_row2)
        t2.addWidget(self.table_scan)
        self.tabs.addTab(tab2, "待迁移（C盘大目录）")

        # Tab3: 日志
        tab3 = QWidget()
        t3 = QVBoxLayout(tab3)
        t3.setContentsMargins(0, 0, 0, 0)
        # 日志刷新按钮行
        log_row = QHBoxLayout()
        log_tip = QLabel("提示：监控日志实时显示拦截/迁移事件，最多保留1000条")
        log_tip.setStyleSheet("color: #1565C0; font-size: 12px; padding: 2px;")
        log_row.addWidget(log_tip, stretch=1)
        # 筛选下拉框
        log_row.addWidget(QLabel("筛选:"))
        self.combo_log_filter = QComboBox()
        self.combo_log_filter.addItems([
            "全部", "初始化", "新目录", "拦截", "警告", "错误",
            "安装", "修复", "迁移", "警报", "安装器", "链接操作", "应用日志",
            "开发环境"
        ])
        self.combo_log_filter.currentIndexChanged.connect(self._render_monitor_log)
        log_row.addWidget(self.combo_log_filter)
        self.btn_open_log_dir = QPushButton("打开日志目录")
        self.btn_open_log_dir.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        self.btn_open_log_dir.clicked.connect(self._open_log_dir)
        log_row.addWidget(self.btn_open_log_dir)
        self.btn_refresh_log = QPushButton("刷新日志")
        self.btn_refresh_log.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.btn_refresh_log.clicked.connect(self._refresh_log_only)
        log_row.addWidget(self.btn_refresh_log)
        t3.addLayout(log_row)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("font-family: Microsoft YaHei; font-size: 14px;")
        t3.addWidget(self.log_text)
        self.tabs.addTab(tab3, "监控日志")

        # Tab4: 使用说明（带滚动条）
        tab4 = QWidget()
        t4 = QVBoxLayout(tab4)
        t4.setContentsMargins(0, 0, 0, 0)
        help_text = QTextEdit()
        help_text.setReadOnly(True)
        # #27:使用说明里的盘符从 cfg["g_root"] 动态读取(原硬编码 D/G 与实际配置不符)
        _g_root = str(self.cfg.get("g_root", "D:\\"))
        _g_drive = _g_root[0].upper() if len(_g_root) >= 2 and _g_root[1] == ":" else "D"
        help_text.setHtml(f"""
<h2>C盘拦迁器 v{APP_VERSION} 使用说明</h2>

<p><b>C盘拦迁器</b>是一款 C 盘空间管理工具，帮助用户快速定位 C 盘中占用空间的文件夹，并支持将大目录安全迁移到其他磁盘，C 盘原位置自动创建符号链接，软件无感知继续正常运行。同时提供开发环境迁移、安装器拦截、链接自动修复、AI 智能识别、配置快照等高级功能。</p>

<p><b>核心能力一览</b>：MFT 全盘快速索引 · 目录大小扫描 · 一键迁移与还原（断点续传）· 符号链接自动维护 · BLAKE3 哈希完整性校验 · 开发环境配置迁移（30+ 工具）· 配置快照与回滚 · 安装器进程拦截 · 系统文件识别保护 · 13 层软件自动识别 · AI 大模型兜底识别 · 文件名快速搜索 · 实时监控日志</p>

<h3>一、快速上手（5 步完成首次迁移）</h3>
<ol>
<li><b>启动扫描</b>：程序启动后自动建立 MFT 全盘索引（数秒完成）并扫描 C 盘 6 个关键目录 + 当前用户目录，扫描进度在底部状态栏显示</li>
<li><b>查看占用</b>：切换到"待迁移"标签页，点击"大小"列标题按从大到小排序，快速定位占空间的目录</li>
<li><b>选择迁移</b>：勾选要迁移的目录（可按 Ctrl/Shift 多选），右键选"迁移到默认盘"或"迁移到指定位置"</li>
<li><b>自动完成</b>：程序自动把数据复制到目标盘 → 删除 C 盘原目录 → 在原位置创建符号链接，全程无需关闭软件</li>
<li><b>随时还原</b>：如需还原，切换到"已迁移"标签页，选中后右键选"还原"，数据自动搬回 C 盘</li>
</ol>
<p><b>提示</b>：迁移不影响软件运行，符号链接对软件完全透明，照常读写数据。建议以管理员身份运行以获得完整权限。</p>

<p><b>⚠️ 迁移方式（重要）</b>：本软件<b>只支持迁移文件夹</b>（不支持单独迁移单个文件）。迁移时把<b>整个源文件夹</b>（保留文件夹名）放入目标目录——例如迁移 <code>C:/Users/…/Android/Sdk</code> 到 <code>D:/</code> 后，数据位于 <code>D:/Sdk/</code> 下，C 盘原路径自动变为符号链接指向 <code>D:/Sdk</code>。目标目录原有的文件与迁移数据<b>天然隔离</b>，不会被覆盖或删除。</p>

<h3>二、待迁移标签页</h3>
<p>列出 C 盘 6 个关键目录 + 当前用户目录下的一级子目录及大小，自动识别每个目录对应的软件名称。</p>

<p><b>扫描范围（6 个关键目录 + 当前用户目录）</b>：</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><td>%LocalAppData%</td><td>本地应用数据（用户级，不随登录漫游）</td></tr>
<tr><td>%LocalAppData%\Programs</td><td>本地安装的程序（用户级，免管理员）</td></tr>
<tr><td>%AppData%</td><td>漫游应用数据（用户级，随账户漫游）</td></tr>
<tr><td>C:\Program Files</td><td>64 位系统级程序</td></tr>
<tr><td>C:\Program Files (x86)</td><td>32 位系统级程序</td></tr>
<tr><td>C:\ProgramData</td><td>系统级程序共享数据（所有用户共用）</td></tr>
<tr><td>%UserProfile%</td><td>当前用户目录（AI 工具/开发工具缓存等），自动排除已监控的 AppData 子目录与桌面/文档/下载等系统文件夹</td></tr>
</table>
<p><b>说明</b>：只扫描这些目录下的一级子目录，不递归扫描孙目录，保证扫描速度。</p>

<p><b>用户目录写入提醒</b>：当前用户目录下新建目录时，右下角弹出气泡提醒（只提醒不拦截，软件内小窗，约 10 秒自动淡出）。可在气泡上点"不再提醒"，或通过工具栏「用户目录写入提醒」开关关闭/重新开启。</p>

<p><b>大小显示规则</b>：</p>
<ul>
<li>≥ 1 MB — 显示 MB（如 123.45 MB）</li>
<li>&lt; 1 MB — 显示 KB 或字节，并用<b style="color:#FB8C00">橙色</b>标注提示是小目录</li>
<li>0 字节 — 显示 0B</li>
<li>正在计算中 — 显示"计算中..."</li>
</ul>

<p><b>说明列前缀（4 种状态）</b>：</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><td><b>[~] 前缀</b></td><td>后台异步补全中，稍后会刷新为完整说明</td></tr>
<tr><td><b>[?] 前缀</b></td><td>识别置信度较低，建议双击手动编辑或用"AI智能识别"补全</td></tr>
<tr><td><b>[系统] 前缀</b></td><td>系统文件，整行涂橙色背景，迁移需二次确认（见"系统文件保护"章节）</td></tr>
<tr><td><b>[已配置] 前缀</b></td><td>该目录对应的开发工具已在"开发环境迁移"区配置，数据仍在 C 盘</td></tr>
<tr><td><b>无前缀</b></td><td>已识别完成，可直接迁移</td></tr>
</table>

<p><b>表格操作</b>：</p>
<ul>
<li><b>排序</b>：点击任意列标题可升序/降序，再次点击切换方向。大小列按数值排序而非字符串</li>
<li><b>多选</b>：Ctrl+点击多选不相邻行；Shift+点击选择连续范围行</li>
<li><b>双击说明列</b>：进入编辑模式，手动修改软件描述，回车保存，Esc 取消</li>
<li><b>双击行号/其他列</b>：在资源管理器中打开该目录</li>
<li><b>右键菜单</b>：见下方"右键菜单"章节</li>
<li><b>列宽调整</b>：拖动列标题边界可调整列宽，过窄时显示省略号，鼠标悬停显示完整内容</li>
</ul>

<p><b>文件名快速搜索</b>：页面顶部搜索框支持按文件名搜索整个 C 盘（基于 MFT 索引，毫秒级返回）。支持 * 和 ? 通配符（如 *.exe、test?1.txt），不输入通配符时自动按"包含"匹配。搜索结果弹窗显示文件名、路径、大小。</p>

<h3>三、已迁移标签页</h3>
<p>显示所有已迁移的目录，表格 6 列：C 盘路径、大小、状态、目标路径、说明、迁移时间。</p>

<p><b>状态列 4 种</b>：</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><td><b>正常</b>（绿色）</td><td>链接有效，目标盘数据存在，一切正常</td></tr>
<tr><td><b>断链</b>（红色）</td><td>软件把符号链接覆盖成了真实目录（软件更新时常发生），可右键"修复链接"或等自动修复</td></tr>
<tr><td><b>丢失</b>（橙色）</td><td>C 盘路径不存在了（可能被手动删除），右键"修复链接"可重建</td></tr>
<tr><td><b>目标丢失</b>（深红）</td><td>目标盘数据没了（U盘/移动硬盘已拔出或数据被误删），需重新迁移</td></tr>
</table>

<p><b>目标路径列颜色</b>：绿色=目标存在正常；红色=目标异常；灰色=该路径不是符号链接（可能是真实目录）</p>

<p><b>刷新机制</b>：</p>
<ul>
<li><b>刷新已迁移按钮</b>：重新检测所有链接状态和目标盘目录大小</li>
<li><b>后台自动检查</b>：每隔 60 秒自动检测一次链接状态，发现异常会在监控日志记录</li>
</ul>

<p><b>链接类型</b>：只显示本工具创建的符号链接。手动/系统创建的目录联接（junction）不会出现在此表，避免误操作（对 junction 点"还原"会把目标盘数据复制回 C 盘，破坏手动布局）。</p>

<h3>四、开发环境迁移标签页</h3>
<p>专为开发者设计的配置迁移工具，支持 30+ 种开发工具（npm/pip/cargo/gradle/Android SDK 等），一键把工具的全局包目录、缓存目录、环境变量迁移到目标盘，并自动配置好所有相关环境变量和配置文件。</p>

<p><b>支持的工具类别</b>：</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><td>Node.js</td><td>npm 全局包、yarn、pnpm</td></tr>
<tr><td>Python</td><td>pip、conda</td></tr>
<tr><td>Rust</td><td>cargo、rustup</td></tr>
<tr><td>Go</td><td>GOPATH、GOCACHE、GOMODCACHE</td></tr>
<tr><td>.NET</td><td>dotnet、nuget</td></tr>
<tr><td>Java</td><td>gradle、maven（含 settings.xml 自动配置）</td></tr>
<tr><td>Android</td><td>Android SDK、Android NDK</td></tr>
<tr><td>C++</td><td>conan、vcpkg</td></tr>
<tr><td>其他</td><td>Ruby / Julia / PHP / Dart / R / Haskell / Scala / OCaml / Nim / Elixir / Swift / Bazel / Terraform / VS Code 扩展</td></tr>
<tr><td>仅指引</td><td>Docker / WSL / Visual Studio（提供清理命令但不自动迁移）</td></tr>
</table>

<p><b>表格 9 列说明</b>：</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><td><b>勾选</b></td><td>勾选后可批量"应用选中配置"</td></tr>
<tr><td><b>工具</b></td><td>工具显示名（如"npm 全局包"）</td></tr>
<tr><td><b>类别</b></td><td>工具所属语言（如"Node.js"）</td></tr>
<tr><td><b>当前路径</b></td><td>工具当前数据所在路径（apply 后显示目标盘新路径）</td></tr>
<tr><td><b>C盘默认路径</b></td><td>工具在 C 盘的默认路径（不一定真实存在，仅作参考）</td></tr>
<tr><td><b>占用空间</b></td><td>C 盘数据大小（MFT 快速扫描）</td></tr>
<tr><td><b>建议新路径</b></td><td>目标盘新路径（<b>双击此列可修改</b>，弹出浏览对话框）</td></tr>
<tr><td><b>状态</b></td><td>工具当前状态（见下方状态说明）</td></tr>
<tr><td><b>提示</b></td><td>操作建议或未安装提示</td></tr>
</table>

<p><b>状态列 6 种</b>：</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><td><b>未安装（单击下载）</b></td><td>未检测到工具安装，<b>单击或双击此单元格</b>弹出下载菜单（访问官网/GitHub）</td></tr>
<tr><td><b>✓ 已就绪</b></td><td>工具已安装且环境变量指向 C 盘默认路径，可配置迁移</td></tr>
<tr><td><b>✓ 已配置到X:盘</b></td><td>环境变量已指向目标盘，但数据未迁移（C 盘仍是真实目录）</td></tr>
<tr><td><b>✓ 已迁移到X:盘</b></td><td>数据已迁移到目标盘，C 盘原位置变符号链接</td></tr>
<tr><td><b>⚠ 已配置（数据未迁移）</b></td><td>环境变量已配但数据未迁移，可单独迁移数据</td></tr>
<tr><td><b>⚠ 路径异常</b></td><td>环境变量指向了无效路径，建议还原后重新配置</td></tr>
</table>

<p><b>顶部按钮区（6 个按钮）</b>：</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><td><b>迁移到 [{_g_drive}▼]</b></td><td>选择目标盘符（默认 {_g_drive} 盘），所有迁移操作的目标盘</td></tr>
<tr><td><b>刷新检测</b></td><td>重新检测所有工具的安装状态、当前路径、占用空间（流式刷新，逐行更新）</td></tr>
<tr><td><b>迁移选中工具到新路径</b></td><td>对勾选的工具批量执行：设置环境变量 + 执行配置命令 + 可选迁移数据。会弹出选项询问"是否同时迁移数据"</td></tr>
<tr><td><b>⚡ 一键补配环境变量</b></td><td>扫描所有"数据已迁移（C 盘是符号链接）但环境变量未配置"的工具，一键批量配置环境变量。适用于先在待迁移区迁移了数据，但没在这里配环境变量的情况</td></tr>
<tr><td><b>查看清理/卸载指引</b></td><td>显示所有已配置工具的清理命令（如 npm cache clean --force），方便卸载或重置</td></tr>
<tr><td><b>📸 保存快照</b></td><td>保存当前开发环境配置快照（仿 GitHub commit），最多保留 500 个，超出自动删除最旧的（首个原始快照永久保留）</td></tr>
<tr><td><b>📋 快照管理（恢复/还原）</b></td><td>快照管理统一入口：恢复到历史快照 / 还原到默认状态 / 查看快照详情 / 加星标标签 / 删除快照</td></tr>
</table>

<p><b>右键菜单（支持多选）</b>：</p>
<ul>
<li><b>配置并迁移此工具到新路径</b>：对单个工具执行配置（设置环境变量指向新路径，并可选把 C 盘数据复制到新路径，C 盘原位置变符号链接）</li>
<li><b>在资源管理器打开当前路径</b>：在资源管理器打开工具当前数据目录</li>
<li><b>在资源管理器打开目标路径</b>：在资源管理器打开目标盘新路径（不存在时询问是否创建）</li>
<li><b>查看清理/卸载指引</b>：显示该工具的清理命令和卸载步骤</li>
<li><b>查看此工具的配置记录</b>：显示已配置工具的详细记录（目标盘、目标路径、配置时间、环境变量列表）</li>
<li><b>🌐 访问官网下载</b>：打开工具官网下载页面</li>
<li><b>还原此工具数据到 C 盘</b>：全自动还原（撤销环境变量 + 还原数据 + 清理记录）</li>
<li><b>批量还原 N 个工具数据到 C 盘</b>：多选时显示，批量全自动还原</li>
</ul>

<p><b>还原流程说明</b>（全自动执行）：</p>
<ol>
<li>撤销环境变量（删除注册表项 + 执行 unconfig 命令 + 还原 Maven/Bazel 配置文件）</li>
<li>还原数据（D 盘数据复制回 C 盘 → 删除 C 盘符号链接 → 删除 D 盘冗余数据）</li>
<li>清理配置记录（从 dev_env_configured 移除）</li>
<li>三区联动刷新（开发环境迁移区 + 已迁移区 + 待迁移区）</li>
</ol>
<p><b>父目录符号链接还原</b>：如果工具路径的父目录（如 C:\Android）是符号链接，还原时会识别并还原整个父目录（含所有子目录），还原前会弹确认框提示。</p>

<p><b>双击操作</b>：</p>
<ul>
<li><b>双击"建议新路径"列</b>：弹出浏览对话框修改建议路径（支持自定义目标位置）</li>
<li><b>双击"状态"列的"未安装"</b>：弹出下载菜单（同右键"访问官网下载"）</li>
<li><b>双击其他列</b>：显示该工具的清理/卸载指引</li>
</ul>

<h3>五、配置快照系统</h3>
<p>仿 GitHub commit 的开发环境配置快照，可保存任意时刻的所有环境变量值和配置记录，随时回滚到历史状态。</p>

<p><b>快照内容</b>：</p>
<ul>
<li>所有相关环境变量的当前值</li>
<li>开发环境配置记录</li>
<li>原始目录结构（便于查看配置前的目录状态）</li>
</ul>

<p><b>保存快照</b>：</p>
<ol>
<li>点击"📸 保存快照"按钮</li>
<li>输入快照备注（如"配置 npm 前的初始状态"）</li>
<li>快照保存为 JSON 文件（位于程序目录 dev_env_snapshots/）</li>
</ol>

<p><b>快照管理对话框</b>：</p>
<p>点击"📋 快照管理（恢复/还原）"按钮打开，可执行以下操作：</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><td><b>⏪ 恢复此快照</b></td><td>把环境变量和配置记录恢复到快照时的状态。恢复前会自动创建当前状态的快照（便于撤销恢复）</td></tr>
<tr><td><b>🔄 还原到默认状态</b></td><td>清空所有开发环境配置（撤销所有环境变量 + 清空配置记录）</td></tr>
<tr><td><b>🔍 查看详情</b></td><td>显示快照内的环境变量值、配置记录、原始目录结构</td></tr>
<tr><td><b>⭐ 加星 / 🏷 标签</b></td><td>给快照加星标或自定义标签，便于快速查找。"只看星标"按钮可筛选星标快照</td></tr>
<tr><td><b>🗑 删除</b></td><td>删除该快照（首个原始快照可删，删后下次启动自动恢复）</td></tr>
</table>

<p><b>首个原始快照（🛡️ 标记）</b>：</p>
<p>程序首次在本机运行时自动创建，是"还原到初始状态"的最终底线。</p>
<ul>
<li><b>自动恢复</b>：即使用户删了首个快照，下次启动时会自动恢复（内容完全不变）</li>
<li><b>身份唯一性</b>：手动创建的快照永远不会带原始标记，无法冒充首个</li>
<li><b>永不被自动清理</b>：超出 500 上限时只删非首个快照，首个原始快照永久保留</li>
</ul>

<p><b>快照标记</b>（独立存储，不修改快照本身）：</p>
<ul>
<li>⭐ 星标：标记常用快照，"只看星标"按钮快速筛选</li>
<li>🏷 标签：自定义文字标签，显示在"标记"列</li>
</ul>

<p><b>快照上限</b>：最多保留 500 个快照，超出自动删除最旧的（首个原始快照除外）。</p>

<p><b>恢复快照后</b>：</p>
<ol>
<li>环境变量已恢复（<b>需重新打开终端/编辑器让环境变量生效</b>）</li>
<li>开发环境迁移区表格自动刷新</li>
<li>已迁移区和待迁移区联动刷新</li>
</ol>

<h3>六、顶部按钮区</h3>
<table border="1" cellpadding="4" cellspacing="0">
<tr><td><b>刷新</b></td><td>智能扫描 C 盘一级子目录，复用已有大小数据，只对新增目录计算，后台执行不卡 UI。扫描完成后自动后台补全未识别的软件说明；同时刷新已迁移目录的链接状态</td></tr>
<tr><td><b>白名单</b></td><td>管理不被自动拦截的安装器名单（见"拦截器与白名单"章节）</td></tr>
<tr><td><b>AI 智能识别</b></td><td>对未识别的目录调用 AI 大模型生成软件说明（需先在 AI 设置配置 API Key）</td></tr>
<tr><td><b>AI 设置</b></td><td>配置 AI 大模型 API（支持智谱/SiliconFlow/DeepSeek/讯飞/通义/Ernie/Groq 七平台），填写 API Key 后启用 AI 兜底识别</td></tr>
<tr><td><b>清空缓存</b></td><td>清空扫描结果和软件描述缓存，下次刷新重新识别（会先取消异步补全线程）</td></tr>
<tr><td><b>重启程序</b></td><td>保存当前配置后重启 C 盘守护者</td></tr>
</table>
<p><b>迁移/还原/链接操作</b>：请通过表格右键菜单完成（见"右键菜单"章节）。</p>

<h3>七、右键菜单</h3>
<p><b>待迁移页右键菜单</b>（支持多选操作）：</p>
<ul>
<li>迁移到默认盘 — 直接迁移到设置区选择的盘符</li>
<li>迁移到指定位置 — 弹窗选择目标盘符和路径</li>
<li>打开目录 — 在资源管理器打开</li>
<li>复制路径 — 复制 C 盘完整路径到剪贴板</li>
<li>AI智能识别 — 调用 AI 大模型生成说明（需配置 API Key）</li>
</ul>
<p><b>已迁移页右键菜单</b>（支持多选操作）：</p>
<ul>
<li>修复链接 — 把 C 盘新数据合并到目标盘后重建符号链接（断链时用）</li>
<li>还原 — 数据搬回 C 盘，删除迁移记录</li>
<li>重建链接 — 不合并数据，直接在 C 盘重建符号链接</li>
<li>删除链接 — 只删 C 盘链接，保留目标盘数据</li>
<li>删除记录 — 从已迁移列表移除该条目（不删数据）</li>
<li>打开目录 — 在资源管理器打开</li>
<li>复制路径 — 复制路径到剪贴板</li>
</ul>

<h3>八、AI 智能识别</h3>
<p>当内置 19421 条软件数据库无法识别某个目录时，可调用 AI 大模型生成描述。</p>
<ol>
<li>点击"AI 设置"按钮，选择平台（支持智谱、DeepSeek、OpenAI、Kimi、豆包、Gemini 等 13 个国内外平台）</li>
<li>填写对应平台的 API Key（各平台官网注册获取）</li>
<li>点击"测试连接"验证 API 可用</li>
<li>在待迁移页右键未识别目录，选"AI 智能识别"</li>
<li>状态栏会实时显示识别进度、API 调用次数和 token 消耗</li>
</ol>
<p><b>支持平台</b>：智谱 GLM、SiliconFlow、DeepSeek、讯飞星火、通义千问、Ernie 文心、Groq</p>
<p><b>API Key 安全存储</b>：API Key 独立保存在 ai_keys.json 文件（与 config.json 分离），点击"清空缓存"不会删除 API Key，无需重新配置。每个平台的 Key 独立保存，切换平台不丢失。</p>
<p><b>识别结果缓存</b>：AI 识别结果会缓存到 ai_recognize_cache.json，下次扫描直接复用。批量识别时自动并发调用（4 线程），单次超时 30 秒。token 消耗在状态栏实时显示（输入/输出/总量）。</p>

<h3>九、监控日志标签页</h3>
<p>实时显示后台事件，每条日志带<b>完整年月日时分秒</b>时间戳（如 [2026-07-22 15:30:45]）。不同颜色代表不同事件类型：</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><td><b>蓝色</b> [初始化 init]</td><td>程序启动信息、配置加载</td></tr>
<tr><td><b>绿色</b> [新目录 new]</td><td>发现 C 盘新增目录</td></tr>
<tr><td><b>红色</b> [拦截 kill]</td><td>杀掉安装器进程</td></tr>
<tr><td><b>橙色</b> [警告 warn]</td><td>检测到安装器运行或链接被覆盖</td></tr>
<tr><td><b>深红</b> [错误 error]</td><td>程序出错信息</td></tr>
<tr><td><b>紫色</b> [安装 install]</td><td>检测到安装文件写入</td></tr>
<tr><td><b>青色</b> [修复 fix]</td><td>链接自动修复成功</td></tr>
<tr><td><b>蓝色</b> [迁移 migrate]</td><td>迁移操作记录</td></tr>
<tr><td><b>深红</b> [警报 alert]</td><td>重要警告弹窗记录</td></tr>
<tr><td><b>紫色</b> [安装器 installer]</td><td>检测到安装器进程启动</td></tr>
<tr><td><b>青色</b> [链接操作 link]</td><td>符号链接创建/删除/重建记录</td></tr>
<tr><td><b>棕色</b> [应用日志 applog]</td><td>应用运行日志（来自 app.log）</td></tr>
</table>
<p><b>筛选下拉框</b>：可按事件类型筛选，只看特定类型日志</p>
<p><b>刷新日志按钮</b>：重新读取日志文件</p>
<p><b>应用日志</b>：选择"应用日志"类型可查看 app.log 实时内容，方便排查问题</p>

<h3>十、自动功能</h3>
<p><b>自动修复</b>：后台每隔 60 秒自动检查所有已迁移目录，发现链接被软件覆盖就把 C 盘新数据搬回目标盘并重建链接，无需手动处理。修复记录在监控日志显示 [修复 fix]。</p>
<p><b>自动拦截</b>：勾选后，发现安装器进程立即杀掉，阻止软件在 C 盘安装。不勾选则只记录日志不杀进程。拦截记录在监控日志显示 [拦截 kill]。</p>
<p><b>后台扫描</b>：程序启动后自动后台扫描，扫描中底部状态栏显示进度，扫描完成自动刷新待迁移表。</p>
<p><b>失败自动恢复</b>：迁移或还原过程中如果意外中断（断电/强制关机），下次启动自动恢复未完成的事务。首次失败自动重试，再次失败停止自动尝试，需用户手动决策（重试/放弃/暂不处理）。</p>

<h3>十一、拦截器与白名单</h3>
<p><b>安装器拦截</b>：开启自动拦截后，后台监控常见安装器进程（如 setup.exe、installer.exe 等），发现立即杀掉，防止软件往 C 盘写入数据。</p>
<p><b>白名单管理</b>：</p>
<ul>
<li>白名单中的进程不会被拦截</li>
<li>可手动添加进程名（如某软件的更新程序）</li>
<li>可删除白名单条目</li>
<li>"恢复默认"按钮还原为内置白名单（含 Windows 更新、杀毒软件等系统进程）</li>
</ul>
<p><b>说明</b>：白名单匹配进程名（不区分大小写），不含路径。建议把常用软件的更新程序加入白名单避免误杀。</p>

<h3>十二、系统文件保护</h3>
<p>程序内置系统文件识别：20 种系统路径前缀（Program Files / ProgramData 下的系统组件，如 Microsoft、WindowsApps、Common Files 等）+ 安全软件关键词（Windows Defender、Windows Security、WindowsApps）。用户级缓存（如 AppData\Local\Microsoft\Windows 下的 INetCache）与软件缓存/模拟目录（如 .wine 的 system32）不再误标 [系统]，可直接迁移。</p>
<p><b>识别后表现</b>：</p>
<ul>
<li>待迁移表中整行涂橙色背景，说明列加 [系统] 前缀</li>
<li><b>单个系统文件迁移</b>：弹红色警告框，需手动确认才迁移</li>
<li><b>批量迁移含系统文件</b>：直接拒绝，不迁移任何文件</li>
</ul>
<p><b>建议</b>：系统文件不要迁移，可能导致系统无法启动或软件异常。[系统] 标记的目录请跳过。</p>

<h3>十三、软件识别机制</h3>
<p>程序内置 13 层识别管线 + 19421 条 winget 软件数据库，自动识别目录对应的软件：</p>
<ol>
<li>缓存类目录短路（如 node_modules、npm-cache 等直接返回固定描述）</li>
<li>组合匹配（手动精调的高质量映射）</li>
<li>winget 软件数据库匹配（19421 包，国内国际软件统一处理）</li>
<li>关键字匹配（已知软件目录字典）</li>
<li>updater/update 后缀目录识别</li>
<li>通用词匹配（updater/cache + 具体软件名）</li>
<li>反向域名包名识别</li>
<li>注册表卸载项 + WMI 查询</li>
<li>特征文件 + 关联目录 + 动态索引</li>
<li>App Paths + 开始菜单 lnk</li>
<li>PE 版本信息（读取 exe 文件版本资源）</li>
<li>lnk 快捷方式 + 文件标识（package.json 等）</li>
<li>厂商容器目录判定（如 Autodesk、Adobe 等大厂目录）</li>
</ol>
<p><b>兜底</b>：以上 13 层都未命中时，启用智能兜底（位置感知），根据目录所在位置（Local/Roaming/Program Files 等）和类型生成差异化描述。</p>
<p><b>联网补全</b>：识别失败时可右键"联网补全说明"，并发查询 Wikipedia、必应、百科等多源，自动筛选最适合的软件描述。</p>

<h3>十四、设置区</h3>
<table border="1" cellpadding="4" cellspacing="0">
<tr><td><b>迁移到</b></td><td>选择目标盘符（默认 {_g_drive} 盘），所有迁移操作默认目标</td></tr>
<tr><td><b>复制校验</b></td><td>迁移后执行 BLAKE3 哈希校验（逐文件内容比对，防数据残缺）。开启最安全但冷盘校验耗时约等于复制时间的一半；关闭只保证文件数/大小一致、速度快</td></tr>
<tr><td><b>线程</b></td><td>复制/校验并发线程数（默认 12，可调低以让出 CPU）</td></tr>
<tr><td><b>迁移后清理还原点</b></td><td>迁移/还原删除大量文件后，清理系统还原点（卷影副本）以立即释放 C 盘空间（需管理员权限）</td></tr>
<tr><td><b>自动拦截</b></td><td>开关安装器拦截功能（见"拦截器与白名单"章节）</td></tr>
<tr><td><b>阈值(MB)</b></td><td>监控告警阈值：后台监控发现 C 盘新建目录大小达到此值时弹『发现大目录』告警（默认 50MB）。与待迁移列表无关</td></tr>
<tr><td><b>开机启动</b></td><td>勾选后随 Windows 开机自动启动并后台运行</td></tr>
</table>

<h3>十五、状态栏</h3>
<p>底部状态栏显示三部分信息：</p>
<ul>
<li><b>左侧</b>：当前操作状态（如"扫描中...耗时 5s"、"就绪"、"迁移中..."）</li>
<li><b>右侧绿色</b>：实时网速（↓下载速度 ↑上传速度）</li>
<li><b>右侧蓝色</b>：内存占用 / CPU 占用 / 线程数</li>
</ul>
<p><b>AI 识别时</b>：额外显示 token 消耗（输入/输出/总量）和 API 调用次数。</p>
<p><b>待处理事务按钮</b>：当有迁移/还原失败的事务等待处理时，状态栏会出现橙色"待处理事务"按钮，点击可查看失败事务并选择：重试迁移 / 放弃迁移（保留 D 盘数据）/ 暂不处理。</p>

<h3>十六、日志与数据文件</h3>
<p>程序运行产生的日志和数据文件位于程序目录下：</p>
<table border="1" cellpadding="4" cellspacing="0">
<tr><td><b>logs/app.log</b></td><td>应用运行日志（每 7 天一个文件，自动清理 3 年以上日志）</td></tr>
<tr><td><b>logs/监控日志.log</b></td><td>监控事件日志（带完整年月日时分秒时间戳）</td></tr>
<tr><td><b>logs/链接记录日志.log</b></td><td>符号链接创建/删除/重建记录</td></tr>
<tr><td><b>logs/错误日志.log</b></td><td>错误信息（含人话原因和建议处理方式）</td></tr>
<tr><td><b>logs/识别记录.json</b></td><td>软件识别结果记录（路径→说明+方法+时间）</td></tr>
<tr><td><b>src/data/software_dict.json</b></td><td>软件识别字典（已知软件目录+组合映射，静态只读）</td></tr>
<tr><td><b>ai_keys.json</b></td><td>AI API Key 存储文件（独立于 config.json，清空缓存不丢失）</td></tr>
<tr><td><b>ai_recognize_cache.json</b></td><td>AI 识别结果缓存（清空缓存会删除，重新识别）</td></tr>
<tr><td><b>config.json</b></td><td>配置文件（用户可编辑的设置：迁移记录、扫描缓存、AI 平台/启用状态等，不含 API Key）</td></tr>
<tr><td><b>state.json</b></td><td>状态文件（程序自动维护：已迁移记录、开发环境配置记录、待处理事务等，不建议手动编辑）</td></tr>
<tr><td><b>dev_env_snapshots/</b></td><td>开发环境配置快照目录</td></tr>
</table>
<p><b>说明</b>：遇到问题时查看对应日志文件排查。错误日志含人话原因和建议处理方式，非技术人员也能看懂。<b>API Key 单独存在 ai_keys.json，清空缓存不会丢失。</b></p>

<h3>十七、常见问题</h3>
<p><b>Q：迁移后软件还能正常用吗？</b><br>A：可以。迁移会在 C 盘原位置创建符号链接，对软件完全透明，照常读写数据。软件更新、保存配置都不受影响。</p>
<p><b>Q：迁移时需要关闭软件吗？</b><br>A：建议关闭正在使用该目录的软件，避免文件占用导致复制失败。如遇"文件被占用"错误，请关闭相关软件后重试。</p>
<p><b>Q：系统文件能迁移吗？</b><br>A：系统文件在待迁移表中整行标橙色并加 [系统] 前缀，迁移时需二次确认。单个系统文件迁移弹红色警告框，批量迁移含系统文件时直接拒绝。建议不要迁移系统文件。</p>
<p><b>Q：目标盘拔了怎么办？</b><br>A：已迁移表中该目录状态变"目标丢失"（深红），重新插上目标盘后右键"修复链接"即可恢复。</p>
<p><b>Q：链接断了怎么办？</b><br>A：已迁移表中状态变"断链"（红色），通常是软件更新把链接覆盖成了真实目录。右键"修复链接"会把新数据合并到目标盘并重建链接，也可等自动修复（每 60 秒检查一次）。</p>
<p><b>Q：闪退怎么办？</b><br>A：查看 logs 目录下的 app.log 和错误日志.log，里面有详细错误信息（含人话原因）。也可点"清空缓存"重置后重试。仍不行可删除 config.json 让程序重建配置。</p>
<p><b>Q：软件说明识别不准？</b><br>A：可双击说明列手动编辑。也可右键选"联网补全说明"在线查询，或配置 AI 后用"AI 智能识别"生成更精准的描述。</p>
<p><b>Q：异步补全时能刷新吗？</b><br>A：不能。异步补全运行时点刷新会提示"异步补全中，请稍候再刷新..."，等几秒补全完成后再刷新。</p>
<p><b>Q：清空缓存后 API Key 会丢失吗？</b><br>A：不会。API Key 独立存储在 ai_keys.json 文件（与 config.json 分离），清空缓存只删除扫描结果和识别缓存，不影响 API Key。清空缓存后无需重新配置 AI。</p>
<p><b>Q：为什么有些目录识别为"笼统说明"？</b><br>A：部分目录（如 Microsoft 公共组件）无法精确识别到具体产品，会返回笼统说明。可双击手动编辑或联网补全。</p>
<p><b>Q：管理员权限有什么用？</b><br>A：创建符号链接、删除系统目录、拦截进程都需要管理员权限。建议以管理员身份运行以获得完整功能。</p>
<p><b>Q：支持哪些目标盘？</b><br>A：支持本地硬盘、U盘、移动硬盘。但建议用固定盘（如 D/E/F/G 盘），移动设备拔出后链接会失效。目标盘为 FAT32/exFAT 时，环境诊断会给出警告（FAT32 单文件最大 4GB，大文件迁移会失败；exFAT 无 NTFS 的 ACL/硬链接/稀疏文件支持，部分属性会丢失），建议使用 NTFS 格式的磁盘。</p>
<p><b>Q：目录里有 OneDrive 等云盘占位文件能迁移吗？</b><br>A：可以，但迁移会触发这些文件从云端下载（占用流量和时间），弱网/离线时可能拖慢甚至失败。迁移前会检测并提示，建议先在云盘客户端选择"始终保留在此设备"下载完成后再迁移。</p>
<p><b>Q：迁移记录在哪？</b><br>A：保存在 state.json 的 migrated 字段，可在"已迁移"标签页查看全部记录。</p>
<p><b>Q：开发环境迁移和普通迁移有什么区别？</b><br>A：普通迁移只搬数据 + 创建符号链接；开发环境迁移除了搬数据，还会自动配置环境变量（如 npm prefix、cargo home）和修改配置文件（如 Maven settings.xml、Bazel .bazelrc），让工具完整迁移到目标盘。</p>
<p><b>Q：开发环境迁移后环境变量没生效？</b><br>A：环境变量写入注册表后会广播 WM_SETTINGCHANGE 消息，但<b>已打开的终端/编辑器不会自动刷新</b>。请重新打开终端/编辑器，或重启资源管理器（任务管理器→ explorer.exe→重启）。</p>
<p><b>Q：开发工具还原后数据会丢失吗？</b><br>A：不会。还原流程是：先把 D 盘数据复制回 C 盘 → 验证文件数完整 → 删除 C 盘符号链接 → 删除 D 盘冗余数据。数据完整性验证失败会中止还原，不会丢数据。</p>
<p><b>Q：快照恢复后能撤销吗？</b><br>A：可以。恢复快照前会自动创建当前状态的快照（带"恢复前自动备份"标记），可随时恢复到恢复前的状态。</p>
<p><b>Q：迁移中断了怎么办？</b><br>A：程序支持断电续传。迁移过程分 6 步并记录 stage，下次启动自动从断点续传。如果连续两次失败，会停止自动重试，状态栏出现橙色"待处理事务"按钮，点击后可选择重试或放弃。</p>
<p><b>Q：迁移时如何保证数据完整？</b><br>A：三重保障：① 开启"复制校验"后，复制完逐文件执行 BLAKE3 哈希比对（内容不一致会重传）；② 断电/中断自动断点续传，已复制部分不重传；③ 还原时先验证目标文件数完整才删除链接，验证失败会中止，不会丢数据。</p>
""")
        t4.addWidget(help_text)
        self.tabs.addTab(tab4, "使用说明")

        # ===== Tab5: 开发环境迁移 =====
        tab5 = QWidget()
        t5 = QVBoxLayout(tab5)
        t5.setContentsMargins(8, 8, 8, 8)

        # 顶部说明（用 QTextEdit 保持换行格式 + 提供滚动条）
        from PySide6.QtWidgets import QTextEdit as _QTE
        desc_label = _QTE()
        desc_label.setReadOnly(True)
        desc_label.setFixedHeight(130)
        desc_label.setPlainText(
            "⚠ 此区只负责包/缓存路径的环境变量配置，不负责主程序的迁移！（主程序迁移请在「待迁移」标签页操作）\n\n"
            "【开发环境迁移】把 npm / cargo / go / pip 等开发工具的「包/缓存路径」改到非 C 盘，"
            "让以后新装的包自动落到目标盘；C 盘已有数据时自动搬走 + 留符号链接（对工具透明）。\n\n"
            "本区 vs 待迁移区的分工：\n"
            "  • 本区管「包/缓存路径」—— 改环境变量 + 可选搬数据\n"
            "  • 待迁移区管「主程序本身」（nodejs.exe / python.exe / git.exe 等）—— 数据搬走 + C 盘留符号链接\n\n"
            "操作提示：\n"
            "  • 勾选要配置的工具（默认全部不勾选）\n"
            "  • 双击「建议新路径」列可修改目标位置\n"
            "  • 点击第0列标题（勾选列顶部）可一键全选/全不选（自动排除未安装）\n"
            "  • 配置前会自动检测 C 盘数据量，给迁移建议\n"
            "  • C 盘无数据时只配环境变量，不创建符号链接\n"
            "  • 配置后需重新打开终端/编辑器才生效"
        )
        desc_label.setStyleSheet(
            "QTextEdit { color: #333; background-color: #F5F5F5; "
            "border: 1px solid #e0e0e0; padding: 8px; }")
        t5.addWidget(desc_label)

        # 目标盘选择行
        drive_row = QHBoxLayout()
        drive_row.addWidget(QLabel("目标盘符:"))
        self.dev_target_drive = QComboBox()
        # 列出所有可用盘符（排除 C）
        drives = []
        for letter in string.ascii_uppercase:
            if letter == 'C':
                continue
            if os.path.exists(f"{letter}:\\"):
                drives.append(letter)
        if not drives:
            drives = ['D']  # 兜底
        self.dev_target_drive.addItems(drives)
        self.dev_target_drive.setCurrentText('D' if 'D' in drives else drives[0])
        # 盘符变化时只更新"建议新路径"列（纯字符串替换，毫秒级，不触发完整刷新）
        # 完整刷新（检测工具状态）由"刷新检测"按钮或程序启动触发
        self.dev_target_drive.currentTextChanged.connect(self._on_dev_target_drive_changed)
        # 防重入标志：正在刷新时跳过新请求
        self._dev_env_refreshing = False
        drive_row.addWidget(self.dev_target_drive)
        drive_row.addWidget(QLabel("  (会把 {D}\\dev\\类别\\xxx 作为新路径)"))
        drive_row.addStretch(1)

        self.btn_refresh_dev = QPushButton("刷新检测")
        self.btn_refresh_dev.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.btn_refresh_dev.clicked.connect(self._refresh_dev_env_table)
        drive_row.addWidget(self.btn_refresh_dev)

        self.btn_apply_dev = QPushButton("迁移选中工具到新路径")
        self.btn_apply_dev.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 6px 16px; }"
            "QPushButton:hover { background-color: #43A047; }")
        self.btn_apply_dev.setToolTip(
            "对勾选的工具批量执行：设置环境变量 + 可选复制数据到目标路径\n"
            "弹出选项询问『是否同时迁移数据』")
        self.btn_apply_dev.clicked.connect(self._apply_dev_env_selected)
        drive_row.addWidget(self.btn_apply_dev)

        # 一键配置已迁移工具：数据已迁移（符号链接）但环境变量没配的工具，批量配置
        self.btn_apply_migrated = QPushButton("⚡ 一键补配环境变量")
        self.btn_apply_migrated.setStyleSheet(
            "QPushButton { background-color: #FF6F00; color: white; font-weight: bold; padding: 6px 16px; }"
            "QPushButton:hover { background-color: #E65100; }")
        self.btn_apply_migrated.setToolTip(
            "扫描所有「数据已迁移（C盘是符号链接）但环境变量未配置」的工具，\n"
            "一键批量配置环境变量指向目标盘。\n"
            "适用于：先在待迁移区迁移了数据，但没在开发环境迁移区配环境变量的情况。")
        self.btn_apply_migrated.clicked.connect(self._apply_migrated_tools_env)
        drive_row.addWidget(self.btn_apply_migrated)

        self.btn_clean_guide = QPushButton("查看清理/卸载指引")
        self.btn_clean_guide.clicked.connect(self._show_dev_clean_guide)
        drive_row.addWidget(self.btn_clean_guide)

        # 快照按钮组（仿 GitHub commit，统一还原入口）
        drive_row.addWidget(QLabel("｜ 快照/还原:"))
        self.btn_create_snapshot = QPushButton("📸 保存快照")
        self.btn_create_snapshot.setToolTip("保存当前开发环境配置的快照（仿 GitHub commit）\n"
            "最多保留 500 个，超出自动删除最旧的（首个原始快照永不被删）")
        self.btn_create_snapshot.clicked.connect(self._create_dev_env_snapshot)
        drive_row.addWidget(self.btn_create_snapshot)

        self.btn_view_snapshots = QPushButton("📋 快照管理（恢复/还原）")
        self.btn_view_snapshots.setToolTip("快照管理统一入口：\n"
            "• 恢复到历史快照（还原环境变量+迁移记录+配置记录）\n"
            "• 还原到默认状态（清空所有开发环境配置）\n"
            "• 查看快照详情 / 删除快照")
        self.btn_view_snapshots.clicked.connect(self._view_dev_env_snapshots)
        drive_row.addWidget(self.btn_view_snapshots)
        t5.addLayout(drive_row)

        # 工具表格
        self.table_dev_env = QTableWidget(0, 9)
        self.table_dev_env.setHorizontalHeaderLabels(
            ["", "工具", "类别", "当前路径", "C盘默认路径", "占用空间", "建议新路径（双击可调整）", "状态", "提示"])
        header_d = self.table_dev_env.horizontalHeader()
        header_d.setSectionResizeMode(QHeaderView.Interactive)
        header_d.setMinimumSectionSize(80)
        header_d.setDefaultSectionSize(120)
        # 表头 tooltip 提示（列号：0勾选 1工具 2类别 3当前路径 4C盘原位置 5占用空间 6建议新路径 7状态 8提示）
        self.table_dev_env.horizontalHeaderItem(4).setToolTip("C 盘默认路径（配置前原始位置）")
        self.table_dev_env.horizontalHeaderItem(5).setToolTip("C 盘占用空间（MFT 快速扫描）")
        self.table_dev_env.horizontalHeaderItem(6).setToolTip("💡 双击此列单元格可修改建议路径（弹出浏览对话框）")
        header_d.resizeSection(0, 30)   # 勾选（窄，可调）
        header_d.resizeSection(1, 180)  # 工具
        header_d.resizeSection(2, 90)   # 类别
        header_d.resizeSection(3, 280)  # 当前路径
        header_d.resizeSection(4, 200)  # C盘原位置
        header_d.resizeSection(5, 80)   # 占用空间（缩窄，只显示"1.2 GB"等短文本）
        header_d.resizeSection(6, 260)  # 建议新路径
        header_d.resizeSection(7, 150)  # 状态
        header_d.resizeSection(8, 220)  # 提示
        # 禁用左上角全选按钮（避免"空气按钮"误触）
        self.table_dev_env.setCornerButtonEnabled(False)
        # 第0列（勾选框）可调宽，设最小宽度避免拖到看不见
        header_d.setSectionResizeMode(0, QHeaderView.Interactive)
        header_d.setMinimumSectionSize(20)
        # 启用表头点击排序（所有列都能点表头排序）
        self.table_dev_env.setSortingEnabled(True)
        header_d.setSectionsClickable(True)
        # 第0列表头显示"全选"提示，点击切换全选/全不选（覆盖排序行为）
        self.table_dev_env.horizontalHeaderItem(0).setText("")
        self.table_dev_env.horizontalHeaderItem(0).setToolTip("点击此列标题：全选/全不选")
        header_d.sectionClicked.connect(self._on_dev_env_header_clicked)
        self.table_dev_env.setTextElideMode(Qt.ElideNone)
        self.table_dev_env.setItemDelegate(NoElideDelegate(self.table_dev_env))
        self.table_dev_env.setWordWrap(False)
        self.table_dev_env.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_dev_env.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table_dev_env.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table_dev_env.setAlternatingRowColors(True)
        self.table_dev_env.verticalHeader().setVisible(False)
        # 双击：建议新路径列(5)弹浏览对话框，其他列查看清理指引
        self.table_dev_env.cellDoubleClicked.connect(self._on_dev_env_double_click)
        # 单击：状态列(6)的"未安装"单元格弹下载菜单
        # 注意1：PySide6 中通过实例属性覆盖 mousePressEvent 不可靠（Qt 走 C++ 虚函数表）
        # 注意2：QTableWidget 的鼠标事件发给 viewport 而非 QTableWidget 本身
        #        所以必须把事件过滤器安装在 viewport() 上，否则收不到 MouseButtonPress/MouseMove
        self.table_dev_env.setMouseTracking(True)
        self.table_dev_env.viewport().installEventFilter(self)
        # 勾选状态变化时同步 UserRole + 整行变色
        self.table_dev_env.itemChanged.connect(self._on_dev_env_item_changed)
        # 右键菜单
        self.table_dev_env.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table_dev_env.customContextMenuRequested.connect(self._dev_env_context_menu)
        t5.addWidget(self.table_dev_env, stretch=1)

        # 底部统计
        self.stat_dev_env = QLabel("共0项 | 已装0项 | 待配置0项 | 已配置0项")
        self.stat_dev_env.setStyleSheet(
            "color: #424242; font-size: 12px; font-weight: bold; padding: 4px 8px; "
            "background-color: #F5F5F5; border-radius: 8px; border: 1px solid #BDBDBD;")
        t5.addWidget(self.stat_dev_env)

        # 插到待迁移（位置1）后面，即位置2（监控日志和使用说明后移一位）
        self.tabs.insertTab(2, tab5, "开发环境迁移")

        # 首次进入该 Tab 时自动检测（延迟，避免启动卡顿）
        self.tabs.currentChanged.connect(self._on_dev_env_tab_changed)

        # OneLineLabel：长错误消息自动压成单行并截断（原文入 tooltip），
        # 防止多行消息把状态栏撑高（此前开发环境区配置失败会撑大状态栏）
        self.status_label = OneLineLabel("就绪")
        self.statusBar().addWidget(self.status_label)
        # 待处理事务按钮：点击查看所有 fail_count >= 2 的事务（用户决策入口）
        # 默认可见但禁用（灰色），有待处理事务时启用+橙色，让用户始终能看到入口
        self.btn_pending_decisions = QPushButton("待处理事务")
        self.btn_pending_decisions.setToolTip(
            "查看所有累计失败 2 次以上的迁移/还原事务，可重试或放弃")
        self.btn_pending_decisions.setStyleSheet(
            "QPushButton { color: #757575; background-color: #E0E0E0; "
            "border: none; padding: 2px 10px; border-radius: 8px; font-weight: bold; }"
            "QPushButton:enabled { color: #fff; background-color: #FF6F00; }"
            "QPushButton:enabled:hover { background-color: #FF8F00; }")
        self.btn_pending_decisions.clicked.connect(self._show_pending_decisions_dialog)
        self.btn_pending_decisions.setEnabled(False)  # 默认禁用，有待处理事务时启用
        self.statusBar().addWidget(self.btn_pending_decisions)
        # 启动后延迟环境自检（不阻塞窗口显示）：发现严重问题（fail）才弹窗提示
        QTimer.singleShot(5000, self._startup_env_check)
        # 启动后延迟检查是否有待处理事务（不阻塞窗口显示）
        QTimer.singleShot(3000, self._update_pending_decisions_button)
        # 网速监控（右侧状态栏）
        self.net_label = QLabel("↓0KB/s ↑0KB/s")
        self.net_label.setStyleSheet(
            "color: #2E7D32; font-weight: bold; padding: 2px 10px; "
            "background-color: #E8F5E9; border-radius: 8px; border: 1px solid #A5D6A7;")
        self.net_label.setMinimumWidth(150)
        self.statusBar().addPermanentWidget(self.net_label)
        # H27：网速基线延迟到 _update_resource 首次调用时初始化，
        # 避免 main.py 顶层 import psutil（psutil 仅 UI 层需要，命令行模式无需加载）
        self._net_last = None
        self._net_last_time = 0.0
        # 资源占用监控（右侧状态栏）
        self.resource_label = QLabel("CPU: 0.0% | 内存: 0MB")
        self.resource_label.setStyleSheet(
            "color: #1565C0; font-weight: bold; padding: 2px 10px; "
            "background-color: #E3F2FD; border-radius: 8px; border: 1px solid #90CAF9;")
        self.resource_label.setMinimumWidth(140)
        self.statusBar().addPermanentWidget(self.resource_label)
        # H27：_proc 延迟到 _update_resource 首次调用时初始化，避免 main.py 顶层 import psutil
        self._proc = None
        self._resource_timer = QTimer(self)
        self._resource_timer.timeout.connect(self._update_resource)
        self._resource_timer.start(2000)

        # ===== 自动刷新定时器（30秒刷新已迁移表状态）=====
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.timeout.connect(self._auto_refresh_migrated)
        self._auto_refresh_timer.start(30000)  # 30秒

        # ===== app.log tail 定时器（每 2 秒读取新增行，显示到监控日志页）=====
        self._applog_pos = 0  # app.log 已读取到的文件偏移
        self._applog_timer = QTimer(self)
        self._applog_timer.timeout.connect(self._tail_app_log)
        self._applog_timer.start(2000)  # 2秒


        # 连接
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_whitelist.clicked.connect(self.manage_whitelist)
        self.btn_apply.clicked.connect(self.apply_threshold)
        self.chk_auto.toggled.connect(self.toggle_auto)
        self.chk_clean_vss.toggled.connect(self.toggle_clean_vss)
        self.chk_autostart.toggled.connect(self.toggle_autostart)
        self.chk_user_dir_notify.toggled.connect(self.toggle_user_dir_notify)
        # 双击表格行打开目录（table_migrated 的双击连接已在表格初始化时完成，此处不重复连接）
        def _scan_double_click(row, col):
            # 说明列(第4列)双击进入编辑，不打开目录
            if col == 4:
                return
            self._open_path(self.table_scan.item(row, 0).text())
        self.table_scan.cellDoubleClicked.connect(_scan_double_click)

        # 立即填充骨架表格（用户启动即可看到完整框架，不会白屏）
        self._prefill_dev_env_skeleton()
        # 预加载开发环境迁移表格：尝试缓存秒开 → 后台静默刷新最新数据
        # 骨架已就位，无需 2 秒延迟，立即尝试缓存
        QTimer.singleShot(50, self._preload_dev_env_table)

    # ========== 线程分级 + 环境诊断 + 删除链接恢复 ==========
    def _on_threads_changed(self, value):
        """线程数变化：手动输入即固定（不再自动分级），立即保存配置"""
        self.cfg["copy_threads"] = value
        self.cfg["copy_threads_auto"] = False
        save_config(self.cfg)

    def show_env_diagnosis(self):
        """环境自检弹窗：显示管理员/引擎/回收站/符号链接/目标盘/还原点检查结果"""
        from env_check import run_full_checks
        try:
            results = run_full_checks(self.cfg.get("g_root", G_ROOT))
        except Exception as e:
            QMessageBox.warning(self, "环境诊断", f"环境诊断失败：{e}")
            return
        icon = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
        lines = []
        for item in results:
            if isinstance(item, (tuple, list)) and len(item) >= 3:
                name, level, msg = item[0], item[1], item[2]
                lines.append(f"{icon.get(level, '✅')} {name}：{msg}")
            else:
                lines.append(str(item))
        QMessageBox.information(self, "环境诊断", "\n".join(lines) if lines else "未发现异常")

    def _startup_env_check(self):
        """启动静默自检：仅发现 fail 级问题才弹窗提示，其余不打扰"""
        try:
            from env_check import run_fast_checks
            results = run_fast_checks(self.cfg.get("g_root", G_ROOT))
            fails = [r for r in results
                     if isinstance(r, (tuple, list)) and len(r) >= 3 and r[1] == "fail"]
            if fails:
                names = "、".join(str(r[0]) for r in fails[:3])
                QMessageBox.warning(self, "环境检查",
                                    f"检测到环境问题：{names}\n点击「环境诊断」按钮查看详情")
        except Exception as e:
            log.error(f"启动环境自检失败: {e}")

    # ========== 开发环境迁移 Tab5 相关 ==========
    def eventFilter(self, obj, event):
        """事件过滤器：处理开发环境表格 viewport 的鼠标移动和点击事件
        - 鼠标移动到状态列(7)"未安装"单元格时切换为手指光标
        - 左键单击状态列(7)"未安装"单元格时弹出下载菜单
        必须安装在 viewport() 上才能收到鼠标事件
        列号：0勾选 1工具 2类别 3当前路径 4C盘原位置 5占用空间 6建议新路径 7状态 8提示
        """
        if obj is self.table_dev_env.viewport():
            try:
                etype = event.type()
                if etype == QEvent.MouseMove:
                    pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                    index = self.table_dev_env.indexAt(pos)
                    if index.isValid() and index.column() == 7:
                        item = self.table_dev_env.item(index.row(), 7)
                        if item and "未安装" in item.text():
                            self.table_dev_env.setCursor(QCursor(Qt.PointingHandCursor))
                        else:
                            self.table_dev_env.unsetCursor()
                    else:
                        self.table_dev_env.unsetCursor()
                elif etype == QEvent.MouseButtonPress:
                    if event.button() == Qt.LeftButton:
                        pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                        index = self.table_dev_env.indexAt(pos)
                        if index.isValid() and index.column() == 7:
                            item = self.table_dev_env.item(index.row(), 7)
                            if item and "未安装" in item.text():
                                self._show_dev_env_download_menu(index.row())
                                return True  # 事件已处理，阻止默认行为
            except Exception as e:
                import traceback
                log.error(f"[DEV_ENV] eventFilter 异常: {e}\n{traceback.format_exc()}")
        return super().eventFilter(obj, event)

    def _on_language_changed(self, index):
        """语言切换：加载语言包 → 刷新界面文案 → 持久化到 config.json。"""
        try:
            code = self.language_combo.itemData(index)
            if not code or code == current_language():
                return
            load_language(code)
            apply_language(self)
            self.cfg["language"] = code
            save_all(self.cfg)
            log.info("界面语言切换为: %s", code)
        except Exception as e:
            log.warning("语言切换失败: %s", e)

    def apply_threshold(self):
        try:
            val = int(self.edit_threshold.text())
            new_root = self._migrate_dir_label.text().strip()
            if not new_root:
                QMessageBox.warning(self, "提示", "请先点击\"请输入目录\"选择迁移目标目录")
                return
            # 统一反斜杠（避免 QFileDialog 返回正斜杠写入 config.json）
            new_root = new_root.replace("/", "\\").rstrip("\\") + "\\"
            self._migrate_dir_label.setText(new_root)
            self.cfg["size_threshold"] = val
            self.cfg["g_root"] = new_root
            # P5 复制选项一并保存(哈希校验开关 + 线程数)
            self.cfg["verify_hash"] = self.chk_verify.isChecked()
            self.cfg["copy_threads"] = self.spin_threads.value()
            save_all(self.cfg)
            self.migrator.cfg = self.cfg
            if self.monitor_worker:
                self.monitor_worker.threshold = val
            self.status_label.setText(
                f"阈值:{val}MB | 迁移到:{new_root} | "
                f"校验:{'开' if self.cfg['verify_hash'] else '关'} | "
                f"线程:{self.cfg['copy_threads']} (无需重新扫描)")
        except ValueError:
            QMessageBox.critical(self, "错误", "请输入有效数字")

# ========== 入口 ==========

def main():
    global log
    log = setup_logging()
    logging.getLogger('CDriveRelocator').handlers = log.handlers  # 同步handler

    # H28：启动兜底——faulthandler 捕获段错误等底层崩溃，excepthook 捕获未处理异常
    # console=False（PyInstaller 打包）下默认静默死亡，加这两项才能留下崩溃日志
    try:
        import faulthandler
        # 把崩溃 traceback 写入日志文件，同时输出到 stderr（打包时 stderr 被丢弃也无妨）
        faulthandler.enable()
    except Exception as e:
        log.debug("忽略异常: %s", e)

    def _excepthook(exc_type, exc_value, exc_tb):
        """全局未捕获异常钩子：写日志 + 弹窗提示，避免静默死亡"""
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.error(f"未捕获的异常: {exc_type.__name__}: {exc_value}", exc_info=(exc_type, exc_value, exc_tb))
        try:
            ctypes.windll.user32.MessageBoxW(
                0, f"程序遇到未预期的错误：\n{exc_type.__name__}: {exc_value}\n\n"
                   f"详细信息已记录到日志文件，请反馈给开发者。\n日志: {LOG_FILE}",
                f"{APP_NAME} 错误", 0x10)  # MB_ICONERROR
        except Exception as e:
            log.debug("忽略异常: %s", e)
    sys.excepthook = _excepthook

    # 单实例检测：防止用户重复启动导致多个监控线程同时跑
    # 多实例会导致：拦截弹窗重复弹 N 次、监控日志重复刷屏、CPU 浪费
    # 用命名 Mutex 检测（窗口未建时也能拦截），比 FindWindowW 更可靠
    _mutex_name = f"Global\\CDriveRelocator_{APP_VERSION}_SingleInstance"
    # 64 位系统上 HANDLE 是 8 字节，ctypes 默认 restype=c_int（4字节）会截断 handle
    # 必须设置 restype=c_void_p，否则 CloseHandle 收到截断值无法释放 Mutex
    ctypes.windll.kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, _mutex_name)
    # 存到 config 模块（而非 main 的 global），让 ui_lifecycle._restart_app 能可靠拿到并释放
    import config as _cfg_mod
    _cfg_mod.SINGLE_INSTANCE_MUTEX_HANDLE = _mutex_handle
    log.info(f"单实例 Mutex 已创建，handle=0x{_mutex_handle:X}")
    _already_running = ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    if _already_running:
        # 已有实例在运行，尝试激活其窗口并退出
        _hwnd = ctypes.windll.user32.FindWindowW(None, f"{APP_NAME} v{APP_VERSION}")
        if _hwnd != 0:
            ctypes.windll.user32.ShowWindow(_hwnd, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(_hwnd)
        ctypes.windll.user32.MessageBoxW(
            0, f"{APP_NAME} 已经在运行了，不要重复启动。\n\n已自动切换到已打开的窗口。",
            "提示", 0x40)  # MB_ICONINFORMATION
        return

    log.info(f"=== {APP_NAME} 启动 ===")
    log.info(f"管理员权限: {is_admin()}")
    log.info(f"配置文件: {CONFIG_FILE}")
    log.info(f"日志文件: {LOG_FILE}")

    # 启动时清理坏环境变量（历史 bug 可能写入 "0 MB" 等非路径值到 ANDROID_HOME 等）
    try:
        from dev_env_migrate import cleanup_bad_env_vars
        cleaned = cleanup_bad_env_vars()
        if cleaned:
            log.info(f"启动时清理了 {len(cleaned)} 个坏环境变量: {cleaned}")
    except Exception as e:
        log.error(f"启动时清理坏环境变量失败: {e}")

    # 启动自愈：清除软件管理变量的环境变量残留（注册表无但进程有）
    # 实测事故（2026-08-13）：配置写入进程环境后注册表被外部清理，从旧进程链
    # 启动的软件继承残留导致检测显示旧路径（H:\...）；启动即清恢复默认检测
    try:
        from dev_env_migrate import clean_env_var_residues
        cleaned_res = clean_env_var_residues()
        if cleaned_res:
            log.info(f"启动自愈：清除 {len(cleaned_res)} 个环境变量残留: {cleaned_res}")
    except Exception as e:
        log.error(f"启动自愈清理环境变量残留失败: {e}")

    try:
        log.info("步骤1: 创建QApplication...")
        app = QApplication(sys.argv)
        # 程序图标（窗口/任务栏），打包与源码模式路径由 config 统一解析
        try:
            if APP_ICON_FILE.exists():
                app.setWindowIcon(QIcon(str(APP_ICON_FILE)))
        except Exception as e:
            log.debug("忽略异常: %s", e)
        app.setQuitOnLastWindowClosed(False)
        log.info("步骤2: 加载配置...")
        # 合并配置（config.json）和状态（state.json）为统一字典
        cfg = {**load_config(), **load_state()}
        log.info("步骤3: 创建主窗口...")
        window = MainWindow(cfg)
        log.info("步骤4: 显示窗口...")
        window.show()
        window.raise_()
        window.activateWindow()
        log.info("步骤5: 进入事件循环...")
        sys.exit(app.exec())
    except Exception as e:
        log.error(f"启动失败: {e}", exc_info=True)
        try:
            ctypes.windll.user32.MessageBoxW(
                0, f"程序启动失败：\n{type(e).__name__}: {e}\n\n"
                   f"详细信息已记录到日志文件。\n日志: {LOG_FILE}",
                f"{APP_NAME} 启动错误", 0x10)  # MB_ICONERROR
        except Exception as e:
            log.debug("忽略异常: %s", e)
        raise

if __name__ == "__main__":
    main()
