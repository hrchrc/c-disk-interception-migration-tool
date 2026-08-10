# 更新日志

## [未发布] - 删除链接恢复（记录线索 + 校对值，2026-08-09）

### 新增
- **删除链接时记录恢复线索**：「删除链接（保留目标数据）」成功后自动计算目标盘**校对值**（文件数 + 总大小 MB，MFT 索引毫秒级）→ 记入 state.json 新增 `deleted_links` 列表（src/dst/time/校对值）
- **「♻️ 删除链接恢复」按钮**（已迁移区）：读线索列表 → 表格（勾选/源/目标/删除时间/**校对状态** ✅一致 ⚠差异 ❌目标丢失）→ 勾选恢复：
  - 校对值一致 → 自动建链（`_create_dir_link` /D→/J 兜底）+ 恢复迁移记录（`_add_migrated_record`）→ 移除线索
  - 不一致（目标盘被软件更新修改）→ 二次确认后强制恢复
  - 目标盘丢失/为空 → 不可恢复；src 已存在 → 拒绝（绝不覆盖）；dst 是符号链接 → 拒绝
- 新增 `record_deleted_link` / `list_deleted_links` / `restore_deleted_link`（migrator.py）；en_us 翻译 14 条
- 单测覆盖 记录→一致恢复 / 增删→差异→拒绝→force / 覆盖保护 / 目标丢失 / dst 链接拒绝（patch 模块级 save_all 防止污染 state.json）

### 审查修正（同轮，严格自审）
- **校对值从"SHA256 清单哈希"改为"文件数+总大小（MFT）"**：SHA256 需 os.walk 遍历全目录，删除链接（尤其批量）时在 UI 主线程执行会卡顿数秒；MFT 统计毫秒级零卡顿，清空/替换/增删文件均可检出（仅"同数量同大小替换"漏检，影响可忽略）

## [未发布] - 线程数自动分级 + 环境诊断重诊（2026-08-09）

### 审查加固（同轮）
- **修复：自动模式下 QSpinBox 初始显示旧手动值**（如旧 config 12 但实际用分级值 16，显示不一致）——自动勾选时初始直接显示分级值
- **防御：`_resolve_copy_threads` 兜底 AttributeError**（cfg=None 等异常场景回退分级）
- 验证：分级 1~32 单调性 + 任何情况不超过 CPU 线程数；手动 0/负数 clamp 到 1；旧 config 无 auto 字段默认自动

## [未发布] - 线程数自动分级 + 环境诊断重诊（2026-08-09）

### 新增/变更
- **复制线程数自动分级（P10）**：新增 `auto_thread_count()` 按 CPU 逻辑线程数分级（AMD/Intel 通用，`os.cpu_count()`）：
  - 低端(≤4)用满 / 中低(5~8)留 1 / 中端(9~16)取 75%（16→12）/ 高端(>16)封顶 16
  - `_resolve_copy_threads()`：读取时统一处理（自动标记或手动值），**硬上限 = CPU 逻辑线程数**（手动输入超限自动 clamp）
  - 配置新增 `copy_threads_auto`（默认 True）；顶部设置区线程下拉框改为「自动」复选框 + QSpinBox（范围 1 ~ os.cpu_count()，勾选自动时禁用并显示分级值）
- **环境诊断弹窗加「🔄 重新诊断」按钮**：重跑完整诊断刷新表格
- 清理回滚残留语言包条目（"将删除 ~ 个备份到回收站"）
- en_us 翻译 2 条（自动 / 重新诊断）

## [未发布] - 环境自检（2026-08-09）

### 新增
- **环境自检模块 `src/core/env_check.py`**：启动时/手动诊断系统环境能力，提前暴露环境隐患（此前 IFileOperation 类未注册、无缓冲 I/O 非对齐等环境问题只能等用户实际迁移时踩坑）：
  - 管理员权限 / 复制引擎 exe 存在性 / **回收站软删除可用性**（IFileOperation CLSID 注册表探测 → SHFileOperationW 兜底判定）/ 符号链接创建权限（临时目录低副作用探测，立即自清理）/ 目标盘存在性+可写性+扇区大小
  - 全部只读或 tempfile 临时目录自清理，不触碰用户数据
- **顶部按钮「环境诊断」**：完整诊断弹窗（含 VSS 还原点占用慢查询），按 ✅/⚠️/❌ 展示
- **启动自动快速自检**（延迟 800ms 不阻塞窗口）：结果写 app.log（不弹窗、不塞监控日志）
- 弹窗文案 en_us 翻译 6 条

## [未发布] - 撤销宽限期备份机制（2026-08-09 回滚）

### 回滚
- **移除 .migrated_backup 宽限期备份机制**（用户决策：目标盘数据已 BLAKE3 校验通过，保留备份占用 C 盘空间属多此一举）：
  - 迁移删源恢复为**直接永久删除**（校验通过后清空+删除，占用时 `._cdrive_bak` 重命名兜底）
  - 删除 `_cleanup_expired_migrated_backups` / `list_migrated_backups` / `delete_migrated_backup` / `recycle_path_to_recycle` 及启动清理调用
  - 删除「📦 迁移备份」按钮与查看弹窗、迁移成功消息的备份提示
  - config.py/config.json 清理 `migrated_backups` / `migrated_backup_retain_days` 字段
  - en_us.json 删除相关翻译条目 47 条（JSON 完整性已验证）
- **保留**：同卷快速移动（fast move）及其删源守卫（源不存在时跳过删源，属独立功能）；VSS 默认非破坏化；错误码/校验/回收站兼容等修复

## [未发布] - 迁移备份管理界面（2026-08-09）

### 变更
- **撤销启动时监控日志的备份提示**（用户反馈日志区已足够多），改为**已迁移区「📦 迁移备份」按钮**：
  - 弹窗查看备份列表：路径 / 创建时间 / 剩余保留天数 / 大小(MB) / 状态（存在或已不存在）
  - 支持「删除选中到回收站」「全部删除到回收站」——**手动删除进回收站可还原**（与到期自动清理的永久删除不同）
  - 弹窗文案全部走 tr() 翻译（en_us 已补 20 条）
- `migrator.py` 新增：`list_migrated_backups()`（含剩余天数/大小计算）、`delete_migrated_backup()`（.migrated_backup 后缀护栏 + 进回收站 + 清理记录）、`recycle_path_to_recycle()`（PowerShell + Microsoft.VisualBasic.FileIO，无 SHFileOperationW 的通配符展开问题；路径经环境变量传递避免转义）
- 修复：备份大小计算单位错误（`get_dir_size_fast` 已返回 MB，不再二次换算）

## [未发布] - VSS 提示与新增文案英文翻译（2026-08-09）

### 变更
- 新增提示全部补 en_us 英文条目（监控日志显示时也过 tr()，ui_monitor_log.py:245）：
  - VSS 占用提示（模板 ~MB）、启动备份提示（模板 ~ 个/~ 天）、fast move 完成/回退/删源跳过、迁移成功弹框的宽限期 backup_hint（片段 3 条）
  - 修复"源目录已重命名为备份（保留期内可找回）"原本值=中文未翻译的条目
- 验证：7 条提示 tr() 输出 0 中文残留；新增条目无重复 key

## [未发布] - VSS 默认非破坏化（2026-08-09）

### 变更
- **不再默认自动删除系统还原点**（`auto_clean_vss` 默认 True → **False**）：软迁移本身不依赖 VSS 清理（数据已搬走、链接已建好），删除还原点是独立的空间回收决策，交给用户
- 迁移/还原后默认仅**检测还原点占用并提示**（新增 `query_vss_usage` 只读查询）：「检测到系统还原点占用 X MB（可能含已迁移数据的旧版本），如需释放可在顶部设置勾选「迁移后清理还原点」（会删除系统所有还原点）」
- 用户主动开启「迁移后清理还原点」时保留原删除逻辑（首次警示一次）；开关 ToolTip 更新为默认关闭说明
- 已保存配置（config.json 中 auto_clean_vss=true）不受默认值影响，需在设置中取消勾选

## [未发布] - P9 同卷快速移动 + 宽限期可见性（2026-08-09）

### 新增
- **同卷快速移动（零复制）**：源/目标同一卷且目标不存在时，整目录原子重命名（`std::fs::rename`），跳过复制与校验（rename 原子性保证数据完整）；失败（跨卷/目标已存在/权限）自动回退复制引擎。参考 c_cleaner_plus 的 os.rename 思路但更保守（仅目标不存在时触发，绝不覆盖）。引擎发 `fast_move=done` 事件，崩溃恢复由 recover 的"src 不存在→补建链接"分支兜底（数据完整在目标，安全）
  - Job 字段 `fast_move_same_volume`（默认 true）；测试模板统一显式关闭（测试要测复制逻辑本身），新增 `integration_fast_move.rs` 覆盖开启/关闭两路径
  - Python：`_run_engine_with_progress` 捕获 fast_move 事件输出日志；migrate() 删源步骤加"源不存在→跳过"守卫（同卷移动后源已不在）
- **宽限期备份可见性**（`.migrated_backup` 机制此前零 UI 提示）：
  - 迁移成功弹框提示"源数据已保留为备份（保留 N 天，可在资源管理器打开找回，到期自动清理释放 C 盘空间）"
  - 启动时检测到未到期备份 → 监控日志提示数量/保留期/找回方式

### 审查加固（同轮，严格自审发现并修复）
- **fast move 排除 Verify 模式**：纯校验任务同卷+目标不存在时会把数据 rename 走（校验变移动）——`job.mode != Mode::Verify` 守卫
- **fast move 排除符号链接源**：rename 会移动链接本身而非数据——`!job.source.is_symlink()` 守卫（符号链接源由 migrate_symlink 走真实目标）
- **SHFileOperationW 通配符防护**：pFrom 会把 `*`/`?` 当通配符展开（Windows 文件名可合法含这些字符 → 误删）——含通配符路径拒绝走 shfileop，回退硬删除（精确路径无通配语义）
- **删源守卫 else 化**：源不存在时不再执行重命名块（避免 FileNotFoundError 误导日志）
- 新增防回归测试：Verify 模式不触发 / 符号链接源不触发（integration_fast_move.rs）

### 说明（原子提交评估）
- 参考 c_cleaner_plus 的 `.partial` 半成品+原子改名提交：评估后**不采用**——现有 ckpt 断点续传 + truncate 精确大小 + BLAKE3 校验已等价保证数据安全（中断半成品会被大小/校验判定重拷），`.partial` 需改动 checkpoint/unbuffered/pipeline/verify/purge 全链路，收益仅为"目标目录短暂干净"，风险反而增加

## [未发布] - 回收站软删除兼容修复（2026-08-09）

### 修复
- **purge 软删除长期降级为硬删除**：本机 IFileOperation 的 CLSID `{3AD05575-8857-4850-8278-1054B1BFCD31}` 未注册（`REGDB_E_CLASSNOTREG`，精简系统/注册表被清理），`RecycleBin::new()` 必然失败 → 每次 mirror purge 都回退硬删除（数据不可恢复），日志报 `purge_fallback_hard`（此前误报 87，现报内部码 0xE0000003）
  - 修复：新增 `recycle_via_shfileop`（shell32 `SHFileOperationW` + `FO_DELETE` + `FOF_ALLOWUNDO`，不依赖 COM 类注册，WinXP+ 全兼容）作为软删除兼容兜底：RecycleBin 初始化失败 → SHFileOperationW 批量软删 → 存在性检查精确统计；SHFileOperationW 也失败才回退硬删除
  - purge 统计逻辑抽为 `count_purged_soft`（RecycleBin / SHFileOperationW 两条路径共用）
  - 日志区分：`purge_recycle_compat`（IFileOperation 失败转兼容路径）/ `purge_fallback_hard`（最终硬删除兜底，带两个错误码）
  - 新增测试 `probe_recycle.rs`：SHFileOperationW 实际删除临时文件/目录进回收站验证
  - 实测：真实 mirror purge 场景 `soft_deleted: true` 生效，不再回退硬删除

## [未发布] - 非对齐尾部复制失败修复（2026-08-09 BUG-13）

### 修复
- **glm-pc-updater 迁移失败的真正根因**：非扇区对齐大小的文件（如 installer.exe 160622764 字节，mod 4096 = 2220）走无缓冲 I/O 复制时，最后一块按扇区对齐 pad 写入后，收尾 `SetEndOfFile(非对齐大小)` 在**无缓冲句柄**上返回 ERROR_INVALID_PARAMETER(87) → 复制失败。sync（`copy_unbuffered_sync`）与 IOCP（`copy_unbuffered_iocp`）两条路径均复现；此前 C:→C: 测试未暴露是因为自适应缓存热启动走了 CopyFileW 掩盖了 bug。
  - 修复：收尾 truncate 改用**缓冲句柄**（`truncate_buffered`，缓冲句柄非对齐截断实测可靠）；先设置时间戳并关闭无缓冲句柄再截断（避免 FILE_SHARE_READ 共享冲突）
  - 新增回归测试 `integration_unaligned_tail.rs`：`adaptive_cache=false` 强制无缓冲路径，覆盖 sync（same）+ IOCP（diff）双路径，非对齐尾部文件必须成功且内容一致（修复前 sync 用例失败复现）
- `recycle.rs` win32_err 非 Win32 facility 兜底 87 → `ERR_NO_OS_ERROR`（回收站初始化失败日志不再误报"参数错误"）
- 实际验证：真实 glm-pc-updater → E: mirror 复制 rc=1 成功，目标文件 160622764 字节精确一致

## [未发布] - 错误码诊断修复（2026-08-09）

### 修复
- **"参数错误（ERROR 87）"误报**（glm-pc-updater 事故）：引擎 BUG-12 防护用 87 当内部哨兵（源文件复制期间被截断/变化），Python 侧按真实 Win32 错误翻译成"路径包含非法字符"误导诊断。现引入引擎内部错误码段（`0xE0000000`，lib.rs 定义）：
  - `ERR_SOURCE_CHANGED (0xE0000001)`：源文件在复制期间被截断/变化 → Python 提示"文件可能正被其他程序写入（如安装器/更新器正在运行），请关闭相关软件后重新迁移"
  - `ERR_PIPELINE_DISCONNECTED (0xE0000002)`：复制流水线异常退出
  - `ERR_NO_OS_ERROR (0xE0000003)`：无原始 OS 错误码兜底
- 6 处 `unwrap_or(87)` 兜底（engine/purge/reparse/verify）改为 `ERR_NO_OS_ERROR`，避免无码错误被误判为"参数错误"
- `retry::classify` 三个内部码显式归 Fatal（防止落入默认 Retry 分支被无谓重试）
- 复制阶段已有错误时跳过 verify（目标树残缺时校验必报"不一致"属噪音；失败路径不删源，完整性由续传重跑保证）
- 新增集成测试 `integration_src_changed.rs`：确定性复现 BUG-12（walk 元数据过期 + 源文件被替换为更短版本），断言返回 `ERR_SOURCE_CHANGED` 且目标残留为部分文件
- 错误码显示优化：引擎内部码（0xE0000000 段）在失败弹框与引擎日志中以 hex 显示（`ERROR 0xE0000001`），与真实 Win32 码区分
- i18n：en_us 语言包补充内部码原因/建议英文译文与"迁移失败（返回码"、"已记录未完成事务"片段条目，修复"下次启动程序会自动续传（引擎幂等重跑）。"未翻译条目；新增 `src/tests/rust_test/verify_i18n_errmsg.py` 验证事故消息英文模式无中文残留

## [未发布] - P6/P7 重构（2026-08-07）

### 新增
- Rust 复制引擎后台低优先级：`background_mode`（句柄级 VeryLow，性能无损）+ `process_background`（进程级极致让路，可选）
- 迁移前 Restart Manager 占用检测（`_check_file_in_use`），提前提示占用进程
- 建链自动选择：符号链接优先（管理员场景与旧版一致），非管理员场景 Junction 兜底
- 源文件宽限期删除：`.migrated_backup` 保留 7 天（可配置）后可找回，到期自动清理
- 开发环境反向搬数据收敛到 `restore_dev_env_data`（pending 事务断电可恢复）

### 变更
- 全部复制调用切换至 Rust 复制引擎（`rust-migrate-engine.exe`），移除对系统 robocopy 的运行时依赖
- pending 事务 stage 名 `robocopy_done/robocopy_failed` → `rustcopy_done/rustcopy_failed`（兼容历史数据）
- monitor 进程白名单加入 `rust-migrate-engine.exe`
- 文案与注释去 robocopy 化（"复制引擎"/"数据同步"）

### 修复
- 后台模式性能：进程级模式实测降 19-23 倍，拆分两级后默认档性能无损
- `_check_file_in_use` 枚举穿透 Junction 子目录导致误报占用
- `os.path.islink` 对 Junction 判定失效（scan_appdata"全链接跳过"逻辑）

### 最终审查修复（2026-08-07 最终审查追加）
- **Restart Manager 三连修**（复现：RM 检测 + 引擎复制组合后进程退出 0xC0000005 崩溃）：
  - `RM_PROCESS_INFO` 结构体 FILETIME 用 `c_ulonglong` 按 8 对齐 → 布局 672 ≠ C 的 668，strAppName 错位 4 字节 → 拆 2×DWORD 对齐 4，加 sizeof 断言
  - `RmStartSession` 向 strSessionKey 缓冲区写 GUID（36+1 wchar）→ 32 wchar 缓冲越界写破坏堆 → 放大到 64 wchar 且每次生成唯一 key
  - 每次调用 `ctypes.WinDLL("rstrtmgr.dll")` 反复 LoadLibrary/FreeLibrary → 退出 GC 时 AV → 模块级缓存句柄
- **恢复方向数据安全两修**（recover_pending_restores）：
  - `rustcopy_done`（复制完成、完整性未验证窗口期）恢复时被当"未完成"→ 删 C 盘完整数据后重拷（D 盘损坏时丢数据）→ 新增专用分支：C 盘完整则只清 D 盘冗余直接提交，不完整才续传
  - "其他阶段"删 C 盘真实目录前不验证 D 盘数据（migrate 方向有 N12 保护、restore 方向缺失）→ 补 D 盘空拒绝（`dst_empty_refused`）
- 新增 `bin/test_p7_normalize.py`：旧 stage（robocopy_*）归一化 3 点 + rustcopy_done 分支 + D 盘空保护共 21 断言
