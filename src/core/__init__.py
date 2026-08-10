# -*- coding: utf-8 -*-
"""核心业务逻辑模块包（src/core/）

存放与 UI 无关的纯业务逻辑模块：
- config.py             配置管理（常量、config.json/state.json 加载保存、日志设置）
- utils.py              工具函数（目录大小、符号链接、PE版本信息、lnk快捷方式、注册表匹配）
- software_detect.py    软件识别（13层识别管线 + 位置感知兜底）
- migrator.py           迁移核心逻辑（迁移/还原/扫描/修复符号链接）
- monitor.py            后台监控线程（watchdog + 安装器进程检测 + 自动修复）
- dev_env_migrate.py    开发环境路径迁移（30+ 工具的环境变量配置）
- dev_env_snapshot.py   开发环境快照（保存/恢复环境变量配置状态）
- ai_recognizer.py      多平台 AI 识别（智谱/硅基/DeepSeek/讯飞/通义/文心/Groq）
- fast_scan.py          基于 MFT 的高性能扫描封装
"""
