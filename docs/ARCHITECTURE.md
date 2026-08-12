# 架构说明

面向开发者的模块结构与跨语言接口说明。详细设计见 `docs/decisions.md`（ADR）与 `开发规范.md`。

## 模块结构

```
src/
├── main.py              # 主窗口入口（PySide6，Mixin 组合各 Handler）
├── core/
│   ├── config.py        # 配置/状态读写（CONFIG_FIELDS/STATE_FIELDS 白名单 + 原子写）
│   ├── scan_dirs.py     # 扫描目录单一数据源（6 目录 + 用户目录，动态排除）
│   ├── migrator.py      # 迁移核心：事务状态机/校验/断点续传/链接操作（~4500 行，上帝文件）
│   ├── migrate_engine.py# 引擎适配层：job.json → Rust 子进程 → JSONL 事件 → 回调
│   ├── monitor.py       # 后台监控：ReadDirectoryChangesW 拦截 + 用户目录提醒
│   ├── fast_scan.py     # MFT 索引加载与查询（Rust 优先 → Cython → os.walk 降级链）
│   ├── utils.py         # 工具：is_symlink/is_junction/占位符检测/系统路径判定
│   ├── software_detect.py / ai_recognizer.py  # 13 层软件识别 + AI 兜底
│   ├── dev_env_migrate.py / dev_env_snapshot.py  # 开发环境迁移与快照
│   └── env_check.py     # 环境自检（管理员/引擎/回收站/符号链接/目标盘文件系统）
├── ui/                  # 各 Handler mixin（ui_migrate/ui_scan/ui_devenv/ui_widgets 等）
├── mft/                 # mft_reader.py + mft_fast.pyx（Cython 兜底，与 Rust 索引 A/B 一致）
└── tests/               # test_safety_regressions.py（纯核心逻辑，不依赖 PySide6）

rust-engine/crates/rust-migrate-engine/   # Rust 引擎：MFT 索引 + 复制引擎
bin/rust-migrate-engine.exe               # 引擎构建产物（源码模式运行也用此 exe）
```

## Python ↔ Rust 引擎接口

引擎为独立进程，通过 **job.json + JSONL 事件流**通信（`migrate_engine.py`）：

```
Python: run_job(source, target, mode, verify, ...)
  → 写临时 job.json（source/target 统一加 \\?\ 长路径前缀）
  → 启动 rust-migrate-engine.exe --job <path> --log-format jsonl
  → 逐行解析 stdout JSONL 事件（progress/file_error/job_done/...）
  → 事件显示层剥离 \\?\ 前缀后回调
```

- **job 字段**：source/target/mode（copy|mirror|verify）/verify（none|hash）/retry/flush_checkpoint_mb/purge/background_mode 等
- **退出码**：0/1/2 成功（无文件/有文件/部分成功），8/16 失败，255 取消；内部错误码 0xE0000000 段（源变化/流水线断开/无 OS 错误）
- **数据安全**：BLAKE3 校验（verify=hash）、checkpoint 断点续传、非对齐尾部缓冲截断

## 事务状态机（迁移）

```
校验（路径包含/目标盘/空间/占位符）
  → 写 pending_migrations（断电恢复依据，stage 记录进度）
  → 引擎镜像复制（幂等重跑）
  → 文件数完整性验证
  → 删源（rd /s /q 优先，cmd 元字符走 rmtree）
  → 建链接（/D 符号链接优先 → /J → PowerShell）
  → 移除 pending，加入 migrated
```

断电恢复：启动时 `recover_pending_migrations()` 按 stage 续传；还原方向同理
（`pending_restores` + `recover_pending_restores()`）。

## MFT 索引（大小统计）

- Rust 引擎读卷构建索引（`--mft-index`），文件大小用 **AllocatedSize（实际占用）**
  而非 DataLength（逻辑大小）——OneDrive 占位符/稀疏文件目录不虚高
- Python `fast_scan` 直载紧凑数组（mmap 名字池），目录大小 O(1)
- 降级链：Rust 失败 → Cython（mft_fast.pyd）→ os.walk；Cython 与 Rust 保持
  A/B 一致（`bin/test_mft_rust_load.py --ab` 验证，改大小字段必须同步两处）

## 关键红线

- 删除铁律：见 CONTRIBUTING.md（盘符根禁删、禁 shell 拼串、删除前验证）
- 系统保护：`is_system_path`（Program Files/ProgramData 系统位置 + Defender/WindowsApps）
- 用户数据目录绝不自动删除
