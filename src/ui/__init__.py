# -*- coding: utf-8 -*-
"""UI 层模块包（src/ui/）

存放依赖 PySide6 的界面层模块：
- ui_widgets.py        独立控件类（NumericTableWidgetItem / WideEditorDelegate）
- ui_workers.py        QThread Worker 类（开发环境状态检测/工具下载/批量配置）
- ui_devenv.py         开发环境迁移功能 Handler（表格填充/配置应用/数据迁移/右键菜单）
- ui_snapshot.py       配置快照功能 Handler（首次运行自动快照/查看恢复快照）
- ui_ai.py             AI 识别功能 Handler（联网搜索/AI 智能识别/AI 设置对话框）
- ui_whitelist.py      白名单管理 Handler（白名单对话框/增删行）
- ui_scan.py           待迁移/已迁移扫描与表格刷新 Handler
- ui_migrate.py        迁移/还原/修复符号链接/右键菜单 Handler
- ui_monitor_log.py    后台监控信号处理与日志渲染 Handler
- ui_lifecycle.py      窗口生命周期/自启/资源刷新/缓存清理 Handler

各 Handler 通过 self 访问 MainWindow 的属性和其他方法，运行时由 MainWindow 提供。
"""
