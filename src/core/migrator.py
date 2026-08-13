#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""迁移核心逻辑 - 迁移/还原/扫描/修复"""

import os
import re
import time
import shutil
import subprocess
import threading
from pathlib import Path
from datetime import datetime

import logging
log = logging.getLogger('CDriveRelocator')
from config import save_config, save_all, log_link_operation, log_error_with_reason
from utils import (is_symlink, is_junction, get_symlink_target, get_dir_size_fast,
                   link_fix_locked, count_cloud_placeholder_files)
from software_detect import get_dir_description
from scan_dirs import (get_scan_dirs, get_monitored_base_norms, get_known_folder_paths,
                       is_user_dir_excluded, norm_path, USER_LABEL)

# Windows 下调用子进程时不弹黑框的标志
_NO_WINDOW_FLAGS = 0x08000000

# 用户取消时的专用返回码（区别于引擎 0-2 成功 / >=8 失败）
# 调用方判断需同时检查 rc >= 8 或 rc == _CANCELLED_RC
_CANCELLED_RC = -1

# Win32 错误码 → (中文原因, 建议)
# 引擎(Rust)返回标准 Win32 错误码,此表统一适用(ADR-005)
# Rust 引擎输出 JSONL file_error 事件,直接用 code 字段查表
_WIN32_ERR_MAP = {
    # === 永久性故障(不可重试) ===
    2:  ("系统找不到指定文件",    "源文件已被删除或路径不存在"),
    3:  ("系统找不到指定路径",    "源路径不存在，请检查目录是否完整"),
    5:  ("拒绝访问",              "权限不足，请以管理员身份运行本程序"),
    19: ("介质受写入保护",        "目标盘可能被写保护，请检查目标盘状态"),
    87: ("参数错误",              "路径包含非法字符，请重命名后重试"),
    108:("磁盘未插入",            "可移动介质未就绪，请插入后重试"),
    112:("磁盘空间不足",          "目标盘空间不足，请清理后重试"),
    161:("路径名非法",            "目标路径含非法字符或格式错误，请重命名"),
    183:("文件已存在",            "目标已存在且不可覆盖，请先删除目标文件"),
    # === 暂时性故障(可重试) ===
    32: ("文件正被另一进程使用",  "文件被占用，建议关闭相关软件后重试"),
    33: ("文件被另一进程锁定",    "文件锁冲突，请稍候重试或关闭占用软件"),
    53: ("网络路径不可达",        "网络路径暂时无法访问，请检查网络连接"),
    67: ("网络名找不到",          "网络名暂时无效，请确认共享已启用"),
    120:("功能未实现",            "系统暂不支持此操作，请重试或更新系统"),
    121:("信号量超时",            "网络连接超时，请检查网络稳定性"),
    145:("目录非空",              "目标目录非空且存在冲突，请清理后重试"),
    232:("管道已关闭",            "网络连接已断开，请重试"),
    233:("管道未连接",            "网络管道未连接，请检查网络后重试"),
    1130:("服务器内存不足",       "远程服务器内存不足，请稍候重试"),
    1722:("RPC 服务器不可用",     "RPC 服务不可达，请检查网络和远程服务"),
    # === 引擎内部码 ===
    1742:("无法访问重解析点",     "符号链接或重解析点未处理，请检查路径"),
    # === 引擎内部码(非 Win32,0xE0000000 保留段,rust lib.rs 定义) ===
    # 严禁复用真实 Win32 码(如 87=ERROR_INVALID_PARAMETER)做内部哨兵,
    # 否则会按真实错误码翻译成误导性诊断(2026-08-09 BUG-12 哨兵事故)
    0xE0000001: ("源文件在复制期间被截断或发生变化",
                 "文件可能正被其他程序写入（如安装器/更新器正在运行），请关闭相关软件后重新迁移该目录"),
    0xE0000002: ("复制流水线异常退出", "引擎内部异常，请查看日志或重试"),
    0xE0000003: ("未知系统错误", "无法确定具体错误原因，请查看日志或重试"),
}
# Windows ERROR_SHARING_VIOLATION：文件被占用（此错误码需附加嫌疑软件名到建议）
_ERR_SHARING_VIOLATION = 32
def _ps_quote(s):
    """PowerShell 单引号字符串转义：单引号双写（' → ''）

    用于 New-Item -Path '...' 等命令拼接，防止路径含单引号时注入任意 PS 代码。
    """
    return str(s).replace("'", "''")


def _get_migrated_desc(src_path, config=None):
    """获取迁移记录的说明文字（优先级：desc_cache > 目录名）

    直接复用待迁移区的 desc_cache，不调用 get_dir_description 现场识别
    （现场识别可能返回不准确的简短结果如 "sdk"）。

    查找策略：
    1. 精确匹配 src_path
    2. 规范化路径匹配（去 \\\\?\\ 前缀、统一小写）
    3. 父目录匹配（src 是子目录时，用父目录的 desc，如 Android\\Sdk 用 Android 的 desc）
    4. 兜底用目录名
    """
    if config:
        try:
            desc_cache = config.get("desc_cache", {})
            # 1. 精确匹配
            desc = desc_cache.get(src_path, "")
            if desc:
                return desc
            # 规范化函数
            def _norm(p):
                return p.replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
            norm_src = _norm(src_path)
            # 2. 规范化路径匹配
            for k, v in desc_cache.items():
                if _norm(k) == norm_src:
                    return v
            # 3. 父目录匹配：src 是子目录时（如 Android\Sdk），用父目录（Android）的 desc
            #    避免 desc_cache 只缓存了父目录时，子目录迁移记录拿到 basename 兜底
            src_path_norm = src_path.replace("\\\\?\\", "").replace("/", "\\").rstrip("\\")
            parent = os.path.dirname(src_path_norm)
            if parent and parent != src_path_norm:
                # 精确查父目录
                desc = desc_cache.get(parent, "")
                if desc:
                    return desc
                # 规范化查父目录
                norm_parent = _norm(parent)
                for k, v in desc_cache.items():
                    if _norm(k) == norm_parent:
                        return v
        except Exception as e:
            log.debug("忽略异常: %s", e)
    # 4. 兜底用目录名
    try:
        return os.path.basename(src_path.rstrip("\\/"))
    except Exception:
        return ""


# ========== VSS 卷影副本查询/清理（2026-08-09 拆分为查询+删除两步）==========
def query_vss_usage():
    """查询 C 盘卷影副本占用（只读，不删除）。

    用于默认模式下"迁移后提示还原点占用"，让用户决定是否清理。

    :return: (数量, 总大小MB)；查询失败返回 (0, 0)
    """
    import subprocess
    try:
        ps_script = (
            "try {"
            "  $shadows = Get-CimInstance -ClassName Win32_ShadowCopy; "
            "  $count = ($shadows | Measure-Object).Count; "
            "  $total = 0; "
            "  if ($count -gt 0) { $total = ($shadows | Measure-Object -Property AllocatedSpace -Sum).Sum }; "
            "  Write-Output \"$count $([math]::Round($total / 1MB, 0))\""
            "} catch {"
            "  Write-Output '0 0'"
            "}"
        )
        ret = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, encoding="utf-8", errors="ignore",
            creationflags=0x08000000, timeout=15)
        if ret.returncode == 0:
            parts = ret.stdout.strip().split()
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                return int(parts[0]), int(parts[1])
    except Exception as e:
        log.debug("忽略异常: %s", e)
    return 0, 0


def clean_vss_shadows():
    """删除所有 VSS 卷影副本，释放被系统还原点占用的磁盘空间

    场景：迁移/还原删除大量文件后，Windows 系统保护机制仍保留这些文件的数据快照，
    导致 C 盘空间不释放。清理 VSS 后空间立即归还。

    实现：优先用 PowerShell Remove-CimInstance（无需交互确认）；
          失败则回退 vssadmin（需管道传入 Y）。

    :return: (成功?, 删除数量, 释放大小MB 或 错误信息)
    """
    import subprocess
    try:
        # 先记录清理前剩余空间（C 盘）
        before_free = 0
        try:
            import shutil
            before_free = shutil.disk_usage("C:\\").free
        except Exception as e:
            log.debug("忽略异常: %s", e)

        # 方案1：PowerShell Remove-CimInstance（推荐，无需确认）
        # 用 try/catch 包裹：Remove-CimInstance 失败时 exit 1，避免 returncode 恒 0 误判
        ps_script = (
            "try {"
            "  $shadows = Get-CimInstance -ClassName Win32_ShadowCopy; "
            "  $count = ($shadows | Measure-Object).Count; "
            "  if ($count -gt 0) { $shadows | Remove-CimInstance }; "
            "  Write-Output $count"
            "} catch {"
            "  Write-Error $_.Exception.Message; "
            "  exit 1"
            "}"
        )
        ret = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, encoding="utf-8", errors="ignore",
            creationflags=0x08000000, timeout=30)
        if ret.returncode == 0:
            # 从 stdout 最后一行解析数量（过滤掉可能的警告/空行）
            count = 0
            for line in reversed(ret.stdout.strip().splitlines()):
                line = line.strip()
                if line.isdigit():
                    count = int(line)
                    break
            # 计算释放空间
            after_free = 0
            try:
                after_free = shutil.disk_usage("C:\\").free
            except Exception as e:
                log.debug("忽略异常: %s", e)
            freed_mb = max(0, (after_free - before_free) // 1024 // 1024)
            return True, count, freed_mb

        # 方案2：vssadmin 兜底（需要 Y 确认，用管道传入）
        # 仅用 returncode 判断，不匹配输出字符串（中文系统输出非英文会导致匹配失败）
        ret2 = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "'y' | vssadmin delete shadows /for=C: /all"],
            capture_output=True, encoding="utf-8", errors="ignore",
            creationflags=0x08000000, timeout=30)
        if ret2.returncode == 0:
            after_free = 0
            try:
                after_free = shutil.disk_usage("C:\\").free
            except Exception as e:
                log.debug("忽略异常: %s", e)
            freed_mb = max(0, (after_free - before_free) // 1024 // 1024)
            return True, -1, freed_mb  # vssadmin 不返回数量，用 -1 表示未知

        err = ret2.stderr.strip() or ret2.stdout.strip() or ret.stderr.strip()
        return False, 0, f"VSS 清理失败: {err}"
    except subprocess.TimeoutExpired:
        return False, 0, "VSS 清理超时（30秒）"
    except Exception as e:
        return False, 0, f"VSS 清理异常: {e}"


def auto_thread_count(cpu_count):
    """按 CPU 逻辑线程数自动分级复制线程数（AMD/Intel 通用，os.cpu_count 返回逻辑处理器数）。

    分级原则（IO 密集复制任务，线程过多收益递减甚至变慢）：
    - 低端(≤4 线程)：用满
    - 中低(5~8)：留 1 个给系统
    - 中端(9~16)：取 75%（16 线程 → 12）
    - 高端(>16)：封顶 16

    :param cpu_count: os.cpu_count() 返回值（可能 None）
    """
    cpu = max(1, int(cpu_count or 1))
    if cpu <= 4:
        return cpu
    if cpu <= 8:
        return cpu - 1
    if cpu <= 16:
        return max(8, cpu * 3 // 4)
    return 16


def _resolve_copy_threads(cfg):
    """解析复制/校验线程数（P10：自动分级 + 手动输入，上限=CPU 逻辑线程数）。

    :param cfg: 配置 dict（copy_threads_auto / copy_threads）
    :return: 最终线程数（1 ~ os.cpu_count()）
    """
    cpu = os.cpu_count() or 4
    try:
        if cfg.get("copy_threads_auto", True):
            threads = auto_thread_count(cpu)
        else:
            threads = int(cfg.get("copy_threads", 12))
    except (TypeError, ValueError, AttributeError):
        threads = auto_thread_count(cpu)
    # 硬上限：不超过 CPU 逻辑线程数（AMD/Intel 通用）
    return max(1, min(threads, cpu))


# ========== 迁移核心逻辑 ==========

# cmd 元字符:cmd /c 会重新解析命令行,无空格路径不会被自动加引号,
# & | ^ % ( ) < > 会被当命令操作符/环境变量展开,* ? 是通配符。
# 含这些字符的路径不走 cmd,改 shutil.rmtree(不经命令解析)。
_CMD_META_CHARS = set("&|^%()<>*?")

# _safe_rd 作为删除入口,拒绝盘符根与系统关键路径。
# 正常调用点都是迁移源(非盘符根);防御的是持久数据被篡改后进入恢复循环的场景。
_FORBIDDEN_RD_PATHS = frozenset([
    r"C:\WINDOWS", r"C:\PROGRAM FILES", r"C:\PROGRAM FILES (X86)",
    r"C:\USERS", r"C:\RECOVERY", r"C:\SYSTEM VOLUME INFORMATION",
])
# C:\WINDOWS 整棵子树(SYSTEM32 等)也拒绝;其余关键路径只精确拒绝本身,
# 不误伤其子目录(C:\Users\aaa\... 是正常迁移范围)。
_FORBIDDEN_RD_PREFIXES = (r"C:\WINDOWS",)

# Restart Manager (rstrtmgr.dll) 句柄模块级缓存(P6 审查修复):
# 实测每次调用 _check_file_in_use 都 ctypes.WinDLL() 加载/卸载该 DLL,
# 进程退出 GC 时卸载会触发 0xC0000005 访问冲突(引擎/RM 检测后退出必崩)。
# 进程内复用同一句柄只加载一次,退出时由解释器统一清理。
_RM_DLL = None
_RM_DLL_LOCK = threading.Lock()
# 已迁移目标目录轻量索引（dst_index）的并发保护锁：
# 异步构建线程（_add_migrated_record 后台）与 remove_dst_index / build_all
# 都会"读-改-写" cfg["dst_index"]，无锁时批量迁移会丢索引条目（竞态）。
# 注意：锁内只做快速 dict 操作与写盘，慢遍历必须在锁外。
_DST_INDEX_LOCK = threading.Lock()


def _get_rm_dll():
    """返回 rstrtmgr.dll 的 WinDLL 句柄(懒加载,模块级单例)。"""
    import ctypes
    global _RM_DLL
    if _RM_DLL is None:
        with _RM_DLL_LOCK:
            if _RM_DLL is None:
                _RM_DLL = ctypes.WinDLL("rstrtmgr.dll")
    return _RM_DLL


def _validate_migration_paths(src_path, dst_path):
    """迁移路径安全校验：src==dst / 包含关系（防止镜像同步自毁）

    镜像同步（/MIR）会复制到自身内部，清空源时连带目标一起删除。
    判定逻辑从 migrate() 提取为独立函数，行为可单测。
    返回 (ok: bool, err_msg: str)；ok=True 通过。
    不同盘符的 commonpath 抛 ValueError，不算包含关系。
    """
    _norm_src = os.path.normcase(os.path.normpath(str(src_path)))
    _norm_dst = os.path.normcase(os.path.normpath(str(dst_path)))
    if _norm_src == _norm_dst:
        return False, (f"源路径和目标路径相同，无法迁移：\n"
                       f"  {src_path}\n"
                       f"请选择不同的目标路径。")
    try:
        _common = os.path.commonpath([_norm_src, _norm_dst])
        if _common == _norm_src or _common == _norm_dst:
            return False, (f"源路径和目标路径存在包含关系，可能导致数据全毁：\n"
                           f"  源: {src_path}\n"
                           f"  目标: {dst_path}\n"
                           f"镜像同步会复制到自身内部，清空源时连带目标一起删除。\n"
                           f"请选择不同的目标路径。")
    except ValueError:
        pass  # 不同盘符，commonpath 抛 ValueError，不算包含
    return True, ""


def build_dev_env_paths(cfg):
    """开发环境已配置的 C 盘源路径索引（用于待迁移区橙色提示）

    dev_env_configured 结构: {tool_id: {source_path, target_drive, target_path, name, ...}}
    同一个 C 盘源路径可能被多个工具配置（如 npm_global 和 npm_cache 都在 %APPDATA%\npm 下），
    这里以源路径为 key 建索引，匹配到任一即标橙。
    scan_appdata 与 SmartScanWorker（智能刷新）共用，保证两条路径行为一致。
    """
    dev_env_paths = {}  # {normalized_source_path: {name, target_drive, target_path}}
    if not cfg:
        return dev_env_paths
    for tid, info in (cfg.get("dev_env_configured") or {}).items():
        sp = (info or {}).get("source_path", "")
        if not sp:
            continue
        # 规范化：去 \\?\ 前缀，小写，去末尾反斜杠
        sp_norm = sp.replace("\\\\?\\", "").lower().rstrip("\\")
        dev_env_paths[sp_norm] = {
            "name": info.get("name", ""),
            "target_drive": info.get("target_drive", ""),
            "target_path": info.get("target_path", ""),
        }
    return dev_env_paths


class Migrator:
    def __init__(self, config):
        self.cfg = config
        self.log_callback = None  # 可选的监控日志回调：fn(event_type, message)
        self._cancel_requested = False  # 是否请求取消（退出时设置）
        self._recover_cancel_requested = False  # 是否请求取消恢复循环（退出时设置）
        self._last_copy_fail_reason = None  # 最近一次复制失败的诊断信息（供 _format_copy_fail 取用）
        # 中危-10：保护 _cancel_requested/_engine_for_cancel 复合操作的实例锁
        # 虽然单个 bool/引用赋值在 Python GIL 下是原子的，但
        # force_cancel_copy 的"读实例 → 取消 → 置 None"是复合操作，
        # 与 worker 线程的"置 proc → 读 _cancel_requested"并发时可能竞态。
        self._cancel_lock = threading.Lock()
        # P4:Rust 引擎为复制后端(ADR-003);P7 起完全取代,无切换后门
        # _engine 延迟创建,避免 migrator↔migrate_engine 循环导入
        self._engine = None
        self._engine_for_cancel = None  # 当前运行的引擎实例(供 force_cancel_copy 取消)
        # H3 修复:链接修复互斥锁——后台 _auto_fix_link(每 30 秒周期)与手动
        # fix_broken_link 并发时,两个引擎作业会写同一目标目录且互相覆盖
        # _engine_for_cancel(取消失效);加锁保证同一时刻只有一个修复作业。
        self._link_fix_lock = threading.Lock()
        # 清理上次崩溃残留的还原标志（restoring_in_progress 不持久化，重启自动清空）
        self.cfg["restoring_in_progress"] = []

    def _mark_restoring(self, src_path):
        """标记路径正在还原中，防止后台监控 _periodic_check 误判为'符号链接被覆盖'"""
        restoring = self.cfg.setdefault("restoring_in_progress", [])
        src_str = str(src_path)
        if src_str not in restoring:
            restoring.append(src_str)

    def _unmark_restoring(self, src_path):
        """清除还原标志"""
        restoring = self.cfg.get("restoring_in_progress", [])
        src_str = str(src_path)
        restoring[:] = [x for x in restoring if x != src_str]

    def _emit_log(self, event_type, message):
        """向监控日志回调发送消息（如果设置了回调）"""
        if self.log_callback:
            try:
                self.log_callback(event_type, message)
            except Exception as e:
                log.debug("忽略异常: %s", e)

    def _maybe_clean_vss(self):
        """迁移/还原后处理 VSS 卷影副本（v2：默认非破坏，2026-08-09）

        场景：迁移/还原删除大量文件后，Windows 系统还原机制仍保留文件旧版本快照，
        导致 C 盘空间看起来没释放。清理 VSS 可立即释放，但会删除系统所有还原点。

        策略：
        - auto_clean_vss=False（默认）：仅检测占用并提示，绝不删除——
          软迁移本身不依赖 VSS 清理（数据已搬走、链接已建好），
          是否删除还原点是完全独立的空间回收决策，交给用户
        - auto_clean_vss=True（用户主动开启）：保留原删除逻辑（首次警示一次）
        """
        if not self.cfg.get("auto_clean_vss", False):
            # 非破坏：只检测占用，提示用户（不删任何还原点）
            try:
                count, used_mb = query_vss_usage()
                if count > 0:
                    self._emit_log("info",
                        f"  ℹ️ 检测到系统还原点占用 {used_mb}MB（可能含已迁移数据的旧版本）。"
                        f"如需释放 C 盘空间，可在顶部设置勾选「迁移后清理还原点」"
                        f"（会删除系统所有还原点）。")
            except Exception as e:
                log.debug("忽略异常: %s", e)
            return
        # 用户主动开启自动清理：#29 首次警示(仅一次,记忆到 cfg)
        if not self.cfg.get("vss_clean_warned", False):
            self.cfg["vss_clean_warned"] = True
            try:
                save_all(self.cfg)
            except Exception as e:
                log.debug("忽略异常: %s", e)
            self._emit_log("warn",
                "  ⚠️ 即将自动清理 VSS 卷影副本——这会删除系统上所有的还原点！\n"
                "     如需保留还原点，请在设置中关闭“自动清理 VSS 卷影副本”选项。")
        try:
            ok, count, freed_or_err = clean_vss_shadows()
            if ok:
                if count > 0:
                    self._emit_log("migrate",
                        f"  🧹 已清理 VSS 卷影副本 {count} 个，释放 {freed_or_err}MB 空间")
                elif count == -1:
                    self._emit_log("migrate",
                        f"  🧹 已清理 VSS 卷影副本，释放 {freed_or_err}MB 空间")
                else:
                    self._emit_log("migrate", "  ℹ️ VSS 无卷影副本可清理")
            else:
                self._emit_log("warn", f"  ⚠️ VSS 清理失败: {freed_or_err}")
        except Exception as e:
            self._emit_log("warn", f"  ⚠️ VSS 清理异常: {e}")

    def _add_migrated_record(self, src, dst, size_mb=None, desc=None):
        """向 migrated 表追加记录，自动去重同 src 旧记录

        避免反复迁移/恢复/链式修复导致 migrated 表出现多条指向同一 dst 的记录。
        所有 migrate()/recover_pending_migrations()/migrate_symlink() 的 append
        都应改用此方法，保证全局唯一。
        """
        try:
            _src_norm = os.path.normpath(str(src)).lower()
            self.cfg["migrated"] = [
                m for m in self.cfg.get("migrated", [])
                if os.path.normpath(m.get("src", "")).lower() != _src_norm
            ]
        except Exception as e:
            log.debug("忽略异常: %s", e)
        record = {
            "src": str(src), "dst": str(dst),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "size_mb": size_mb if size_mb is not None else get_dir_size_fast(str(dst)),
            "desc": desc if desc is not None else _get_migrated_desc(str(src), self.cfg),
        }
        self.cfg["migrated"].append(record)
        # 跨盘目标（MFT 未覆盖）：迁移/恢复完成后异步构建轻量索引
        # （后台线程，不阻塞调用线程；数秒内索引就绪，删除记录/恢复对话框
        # 直接用，无需等下次启动的 build_all 兜底）
        try:
            if not self._mft_covers(str(dst)):
                import threading as _th
                _dst_key = str(dst).replace("\\", "/").lower().rstrip("/")
                _dst_str = str(dst)

                def _bg_build():
                    entry = self._build_dst_index(_dst_str)
                    if entry:
                        try:
                            with _DST_INDEX_LOCK:
                                idx = dict(self.cfg.get("dst_index") or {})
                                idx[_dst_key] = entry
                                # 内存更新即可；落盘由 build_all（下次启动）兜底，
                                # 避免后台线程并发写盘覆盖 state.json 其他字段
                                self.cfg["dst_index"] = idx
                        except Exception as e:
                            log.debug("忽略异常: %s", e)

                _th.Thread(target=_bg_build, daemon=True).start()
        except Exception as e:
            log.debug("忽略异常: %s", e)

    def _mft_covers(self, path):
        """路径是否在当前 MFT 扫描器覆盖的卷（仅该卷可毫秒级计算大小/文件数）

        跨盘目标（如迁移到 G 盘的目录）MFT 索引不覆盖，
        此时 _count_files_fast / get_dir_size_fast 会回退 rglob/os.walk
        全量磁盘遍历（大目录卡数秒），删除记录/恢复对话框等即时操作必须避免。
        """
        try:
            from utils import get_mft_scanner
            scanner = get_mft_scanner()
            if scanner is None or not getattr(scanner, "_loaded", False):
                return False
            p = str(path)
            drive = p[:1].upper() if len(p) >= 2 and p[1] == ":" else ""
            return bool(drive) and drive == getattr(scanner, "volume", "").upper()
        except Exception:
            return False

    # ========== 已迁移目标目录轻量索引（跨盘校对值，防删除记录/恢复卡顿）==========
    # MFT 索引只覆盖 C 盘卷；已迁移目标在 D/G 盘，实时统计会全量遍历磁盘。
    # 对已迁移目标目录建轻量索引（文件数+总大小+目录mtime），存 state.json：
    # 删除记录/恢复对话框直接查索引比对（毫秒级），迁移记录移除时删除索引。
    # 索引在后台线程构建（build_all_dst_indexes），不卡 UI；可随时重建。

    def _get_dst_index(self, dst):
        """查已迁移目标目录的轻量索引（无则 None）"""
        try:
            idx = self.cfg.get("dst_index") or {}
            key = str(dst).replace("\\", "/").lower().rstrip("/")
            return idx.get(key)
        except Exception:
            return None

    def _build_dst_index(self, dst):
        """构建单个目标目录的轻量索引（文件数+总大小+目录 mtime）

        全量磁盘遍历（os.walk），必须在后台线程调用；失败返回 None 不中断。
        """
        try:
            file_count = 0
            total = 0
            for _dirpath, _dirnames, filenames in os.walk(dst):
                for f in filenames:
                    try:
                        total += os.path.getsize(os.path.join(_dirpath, f))
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                    file_count += 1
            try:
                mtime = os.path.getmtime(dst)
            except Exception:
                mtime = 0
            return {
                "file_count": file_count,
                "size_mb": round(total / 1024 / 1024, 6),
                "mtime": mtime,
                "built_at": time.time(),
            }
        except Exception:
            return None

    def build_all_dst_indexes(self, max_age=86400):
        """后台构建所有已迁移目标目录的轻量索引（启动时调用，不卡 UI）

        - 只对 MFT 未覆盖的跨盘目标构建；已存在且新鲜（max_age 秒内）的跳过
        - 清理孤儿：已不在迁移记录里的索引删除（记录移除 → 索引删除）
        - 构建阶段（os.walk 遍历）在锁外执行，提交阶段（dict 合并+写盘）
          在 _DST_INDEX_LOCK 内——锁内只有快速操作，不会阻塞 UI 线程
          （如删除记录时 remove_dst_index 也要拿锁）
        - 写盘用读-改-写只更新 dst_index 字段，读取失败只更新内存不写盘
          （2026-08-11 事故教训：防止覆盖 state.json 其他字段）
        :return: 索引条目数（0=异常/无可建）
        """
        try:
            from config import STATE_FILE
            import json as _json
            migrated_dsts = set()
            for m in self.cfg.get("migrated", []):
                d = m.get("dst") or ""
                if d:
                    migrated_dsts.add(str(d).replace("\\", "/").lower().rstrip("/"))
            # ===== 构建阶段（无锁，耗时遍历）=====
            built = {}
            now = time.time()
            for m in self.cfg.get("migrated", []):
                dst = m.get("dst")
                if not dst:
                    continue
                if self._mft_covers(dst):
                    continue  # MFT 覆盖卷毫秒级实时算，无需索引
                key = str(dst).replace("\\", "/").lower().rstrip("/")
                old = (self.cfg.get("dst_index") or {}).get(key)
                if old and now - old.get("built_at", 0) < max_age:
                    continue  # 新鲜，跳过
                entry = self._build_dst_index(dst)
                if entry:
                    built[key] = entry
            # ===== 提交阶段（加锁，快速操作）=====
            with _DST_INDEX_LOCK:
                idx = dict(self.cfg.get("dst_index") or {})
                changed = False
                if built:
                    idx.update(built)
                    changed = True
                # 清理孤儿（迁移记录已移除的目录）
                for key in [k for k in idx if k not in migrated_dsts]:
                    del idx[key]
                    changed = True
                if changed:
                    self.cfg["dst_index"] = idx
                    # 读-改-写：只更新 dst_index 字段，避免并发覆盖其他字段
                    disk = None
                    try:
                        with open(STATE_FILE, "r", encoding="utf-8") as f:
                            disk = _json.load(f)
                    except Exception:
                        disk = None
                    if disk is not None:
                        disk["dst_index"] = idx
                        try:
                            with open(STATE_FILE, "w", encoding="utf-8") as f:
                                _json.dump(disk, f, ensure_ascii=False, indent=2)
                        except Exception as e:
                            log.debug("忽略异常: %s", e)
            return len(idx)
        except Exception:
            return 0

    def remove_dst_index(self, dst):
        """迁移记录移除后删除对应索引条目（记录移除 → 索引删除）"""
        try:
            with _DST_INDEX_LOCK:
                idx = dict(self.cfg.get("dst_index") or {})
                key = str(dst).replace("\\", "/").lower().rstrip("/")
                if key in idx:
                    del idx[key]
                    self.cfg["dst_index"] = idx
                    try:
                        from config import save_state
                        save_state(self.cfg)
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
        except Exception as e:
            log.debug("忽略异常: %s", e)

    def record_deleted_link(self, src, dst):
        """删除链接后记录恢复线索（校对值=文件数+总大小，MFT 毫秒级），供恢复使用。

        只记 src/dst/时间/校对值，不删除任何数据；失败不阻断删除流程（调用方捕获）。
        校对值用 MFT 索引（_count_files_fast + get_dir_size_fast），
        MFT 覆盖卷内毫秒级；跨盘目标（MFT 不覆盖）跳过校对值计算（记 0），
        避免 rglob/os.walk 全量遍历导致"删除记录"卡顿——删除操作必须零等待。
        """
        try:
            if is_symlink(dst):
                return False, "目标盘路径是符号链接，无法记录"
            if self._mft_covers(dst):
                file_count = self._count_files_fast(dst)
                size_mb = get_dir_size_fast(dst)
            else:
                # 跨盘目标：优先用轻量索引（后台构建，毫秒级），无索引记 0
                entry = self._get_dst_index(dst)
                if entry:
                    file_count = entry.get("file_count", 0)
                    size_mb = entry.get("size_mb", 0)
                else:
                    file_count = 0
                    size_mb = 0
            rec = {
                "src": str(src), "dst": str(dst),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "file_count": file_count,
                "size_mb": size_mb,
            }
            # 去重：同 src 只留最新一条线索
            self.cfg["deleted_links"] = [
                x for x in self.cfg.get("deleted_links", [])
                if x.get("src") != str(src)]
            self.cfg["deleted_links"].append(rec)
            save_all(self.cfg)
            return True, ""
        except Exception as e:
            return False, str(e)

    def list_deleted_links(self):
        """列出删除链接恢复线索，并重算目标盘当前校对值给出状态。

        :return: list of dict（原线索 + status + current）
            status: "ok"=一致可恢复 / "diff"=有差异需确认 / "gone"=目标丢失不可恢复
        """
        result = []
        for rec in self.cfg.get("deleted_links", []):
            item = dict(rec)
            dst = rec.get("dst", "")
            if not dst or not os.path.isdir(dst) or is_symlink(dst):
                item["status"] = "gone"
                item["current"] = None
                result.append(item)
                continue
            cur_fc = self._count_files_fast(dst) if self._mft_covers(dst) else None
            if cur_fc is None:
                # 跨盘目标（MFT 未覆盖）：优先用轻量索引比对（后台构建，不卡），
                # 无索引则快速非空检查
                item["current"] = None
                entry = self._get_dst_index(dst)
                if entry and entry.get("file_count"):
                    cur_fc = entry.get("file_count", 0)
                    cur_mb = entry.get("size_mb", 0)
                    item["current"] = {"file_count": cur_fc, "size_mb": cur_mb}
                    if cur_fc == 0:
                        item["status"] = "gone"
                    elif not rec.get("file_count"):
                        # 删除时无校对值（记 0）：不比对，避免假 diff
                        item["status"] = "ok"
                    elif cur_fc == rec.get("file_count") and round(cur_mb, 1) == round(
                            rec.get("size_mb", -1), 1):
                        item["status"] = "ok"
                    else:
                        item["status"] = "diff"
                else:
                    try:
                        with os.scandir(dst) as _it:
                            _non_empty = any(_it)
                        item["status"] = "ok" if _non_empty else "gone"
                    except Exception:
                        item["status"] = "gone"
                result.append(item)
                continue
            cur_mb = get_dir_size_fast(dst)
            item["current"] = {"file_count": cur_fc, "size_mb": cur_mb}
            if cur_fc == 0:
                item["status"] = "gone"
            elif not rec.get("file_count"):
                # 删除时无校对值（当时 MFT 未加载/无索引，记 0）：不比对，
                # 目标非空即可恢复（避免假 diff：0 vs 真实值永远不一致）
                item["status"] = "ok"
            elif cur_fc == rec.get("file_count") and round(cur_mb, 1) == round(
                    rec.get("size_mb", -1), 1):
                item["status"] = "ok"
            else:
                item["status"] = "diff"
            result.append(item)
        return result

    def restore_deleted_link(self, rec, force=False):
        """按线索恢复被删除的迁移记录（目标盘数据 → 补建链接（若链接已删） + 恢复迁移记录）。

        安全校验：
        - src 不存在（或仍是正确指向目标盘的符号链接——「删除记录」不动文件）
        - 目标盘必须存在且有数据
        - 校对值一致才恢复；不一致需 force=True（UI 二次确认后传入）
        - 建链复用 _create_dir_link（/D→/J→PS 兜底 + is_symlink 验证）

        :return: (ok, msg)
        """
        src = str(rec.get("src", ""))
        dst = str(rec.get("dst", ""))
        if not src or not dst:
            return False, "线索缺少 src/dst"
        # 「删除记录」不动文件：C 盘链接可能还在。链接在且指向目标盘 → 只恢复记录不重建
        link_alive = False
        if os.path.lexists(src):
            try:
                link_alive = (
                    is_symlink(src)
                    and os.path.normcase(os.path.normpath(str(get_symlink_target(src))))
                    == os.path.normcase(os.path.normpath(dst)))
            except Exception:
                link_alive = False
            if not link_alive:
                return False, f"源路径已存在（{src}），拒绝覆盖"
        if not os.path.isdir(dst) or is_symlink(dst):
            return False, f"目标盘路径异常（{dst}）"
        cur_mb = 0  # 校对大小：MFT 毫秒级或索引缓存，供 _add_migrated_record 复用
        if self._mft_covers(dst):
            cur_fc = self._count_files_fast(dst)
            cur_mb = get_dir_size_fast(dst)
            if cur_fc == 0:
                return False, "目标盘目录为空，无法恢复"
            # 删除时无校对值（记 0）则不比对，直接恢复（避免假 diff）
            if not force and rec.get("file_count") and (
                    cur_fc != rec.get("file_count")
                    or round(cur_mb, 1) != round(rec.get("size_mb", -1), 1)):
                return False, ("目标盘内容与删除时不一致（可能已被修改），"
                               "如需强制恢复请在确认时勾选")
        else:
            # 跨盘目标：优先用轻量索引比对（后台构建，不卡），无索引快速非空检查
            entry = self._get_dst_index(dst)
            if entry and entry.get("file_count"):
                cur_fc = entry.get("file_count", 0)
                cur_mb = entry.get("size_mb", 0)
                if cur_fc == 0:
                    return False, "目标盘目录为空，无法恢复"
                if not force and rec.get("file_count") and (
                        cur_fc != rec.get("file_count")
                        or round(cur_mb, 1) != round(rec.get("size_mb", -1), 1)):
                    return False, ("目标盘内容与删除时不一致（可能已被修改），"
                                   "如需强制恢复请在确认时勾选")
            else:
                try:
                    with os.scandir(dst) as _it:
                        _non_empty = any(_it)
                    if not _non_empty:
                        return False, "目标盘目录为空，无法恢复"
                except Exception:
                    return False, "目标盘目录为空，无法恢复"
        if not link_alive:
            parent = os.path.dirname(src)
            if parent and not os.path.isdir(parent):
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError as e:
                    return False, f"创建父目录失败: {e}"
            ok, err = self._create_dir_link(src, dst)
            if not ok:
                return False, f"创建链接失败: {err}"
        # 恢复迁移记录 + 移除线索
        # size 用已算好的校对值（MFT 毫秒级或索引缓存），
        # 避免 _add_migrated_record 内部对跨盘目标再次全量遍历卡顿
        self._add_migrated_record(src, dst, size_mb=cur_mb)
        self.cfg["deleted_links"] = [
            x for x in self.cfg.get("deleted_links", [])
            if x.get("src") != src]
        save_all(self.cfg)
        return True, "已恢复迁移记录" if link_alive else "已恢复链接与迁移记录"

    def _real_size_bytes_fast(self, path):
        """目录总大小（字节）：MFT 索引优先（#25，仅当前卷），否则回退 rglob。

        替代 sum(f.lstat().st_size for f in p.rglob('*') if f.is_file()
        and not f.is_symlink()) 的全目录磁盘遍历（大目录数十秒 → 毫秒级）。
        MFT 的 reparse 语义与原逻辑等价：reparse 目录不展开、链接文件 size=0。
        """
        try:
            from utils import get_mft_scanner
            scanner = get_mft_scanner()
            if scanner is not None and getattr(scanner, "_loaded", False):
                p = str(path)
                drive = p[:1].upper() if len(p) >= 2 and p[1] == ":" else ""
                if drive and drive == getattr(scanner, "volume", "").upper():
                    # get_dir_size_mft 返回 MB(round 6 位小数≈1 字节精度),
                    # 内部对 0 结果且目录存在时有 walk 兜底
                    mb = scanner.get_dir_size_mft(p)
                    return round(mb * 1024 * 1024)  # round 而非 int:避免截断丢 1 字节
        except Exception as e:
            log.debug("忽略异常: %s", e)
        return sum(f.lstat().st_size for f in Path(path).rglob('*')
                   if f.is_file() and not f.is_symlink())

    def _count_files_fast(self, path):
        """文件计数：MFT 内存索引优先（#23，仅当前卷），否则回退 rglob。

        替代 sum(1 for _ in Path(p).rglob('*') if _.is_file()) 的全目录
        磁盘遍历（大目录如 Steam 10 万+ 文件，磁盘遍历数十秒 → 毫秒级）。
        """
        try:
            from utils import get_mft_scanner
            scanner = get_mft_scanner()
            if scanner is not None and getattr(scanner, "_loaded", False):
                p = str(path)
                drive = p[:1].upper() if len(p) >= 2 and p[1] == ":" else ""
                if drive and drive == getattr(scanner, "volume", "").upper():
                    n = scanner.count_files(p)
                    if n >= 0:
                        return n
        except Exception as e:
            log.debug("忽略异常: %s", e)
        return sum(1 for _ in Path(path).rglob('*') if _.is_file())

    def _safe_rd(self, path):
        """删除目录:优先 cmd /c rd /s /q(快、不进回收站),路径含 cmd 元字符时改走 rmtree。

        cmd /c 会重新解析命令行,路径含 & % 等元字符时会被当命令操作符/变量展开,
        故含元字符的路径一律 rmtree(不经命令解析)。盘符根与系统关键路径直接拒绝。
        :param path: 要删除的目录路径
        :return: (deleted: bool, err: str)
        """
        # 盘符根/系统关键路径拒绝(路径可能来自被篡改的持久数据)
        norm = path.rstrip("\\/")
        if len(norm) == 2 and norm[1] == ":" and norm[0].isalpha():
            return False, "拒绝:盘符根"
        if norm.startswith("\\\\"):
            return False, "拒绝:UNC网络路径"
        up = norm.upper()
        if up in _FORBIDDEN_RD_PATHS or any(
                up == p or up.startswith(p + "\\") for p in _FORBIDDEN_RD_PREFIXES):
            return False, "拒绝:系统关键路径"
        if any(ch in path for ch in _CMD_META_CHARS):
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception as e:
                return False, f"rmtree: {e}"
            if os.path.exists(path):
                return False, "rmtree 后仍存在(文件被占用)"
            return True, ""
        try:
            r = subprocess.run(["cmd", "/c", "rd", "/s", "/q", path],
                capture_output=True, creationflags=_NO_WINDOW_FLAGS)
        except Exception as e:
            return False, f"rd: {e}"
        if os.path.exists(path):
            return False, f"rd 后仍存在 (rc={r.returncode})"
        return True, ""

    def _cleanup_dir_contents(self, path):
        """清空目录里的所有内容（文件+子目录），但保留目录本身

        用途：还原成功后清理 D 盘冗余数据时，保留目标文件夹本身，
              避免破坏 D:\\dev\\android\\ 这类父目录结构，也免去下次迁移重建目录。

        关键：如果 path 本身是符号链接，必须先删除链接再重建为真实空目录，
              否则 os.listdir 会跟随链接清空目标目录的内容，path 仍是符号链接。

        :param path: 要清空的目标目录路径
        :return: (success: bool, error_msg: str)
        """
        try:
            if not os.path.exists(path) and not os.path.islink(path):
                return True, ""  # 不存在且不是断链符号链接，无需清理
            # 如果 path 本身是符号链接，删除链接并重建为真实空目录
            if os.path.islink(path) or is_symlink(path):
                try:
                    try:
                        os.rmdir(path)
                    except OSError:
                        os.unlink(path)
                except Exception as e:
                    return False, f"删除符号链接失败: {e}"
                try:
                    os.makedirs(path, exist_ok=True)
                except Exception as e:
                    return False, f"重建真实目录失败: {e}"
                log.info(f"符号链接已还原为真实空目录: {path}")
                return True, ""  # 已是空目录，无需再清空
            if not os.path.isdir(path):
                return True, ""  # 不是目录，不处理
            err_parts = []
            for entry in os.listdir(path):
                entry_path = os.path.join(path, entry)
                try:
                    if os.path.islink(entry_path):
                        # 符号链接：先试 os.remove（文件链接/目录链接通用）
                        # 失败则用 os.rmdir 兜底（目录符号链接在某些 Windows 版本上只能用 rmdir 删）
                        try:
                            os.remove(entry_path)
                        except OSError:
                            os.rmdir(entry_path)
                    elif os.path.isfile(entry_path):
                        os.remove(entry_path)
                    elif os.path.isdir(entry_path):
                        # 删子目录(快、不进回收站);安全封装:含 cmd 元字符自动退 rmtree
                        self._safe_rd(entry_path)
                        if os.path.exists(entry_path):
                            shutil.rmtree(entry_path, ignore_errors=True)
                except Exception as e:
                    err_parts.append(f"{entry}: {e}")
            # 验证：目录本身应保留，里面应为空
            if os.path.exists(path):
                remaining = os.listdir(path)
                if remaining:
                    err_parts.append(f"清空后仍有 {len(remaining)} 个残留项")
                    return False, "; ".join(err_parts)
            return True, ""
        except Exception as e:
            return False, f"清空异常: {e}"

    def _get_engine(self):
        """延迟创建 MigrateEngine 实例(避免 migrator↔migrate_engine 循环导入)。

        引擎 exe 缺失时不在此处抛异常,由调用方 engine_available() 检测后降级。
        """
        if self._engine is None:
            from migrate_engine import MigrateEngine
            self._engine = MigrateEngine()
        return self._engine

    def _run_engine_with_progress(self, src, dst, action_label="迁移",
                                  mode="mirror", purge_enabled=True,
                                  verify="none"):
        """通过 Rust 引擎执行复制,JSONL 事件流转 _emit_log 进度(P4 默认路径)。

        对应引擎复制的统一入口版,退出码语义保持兼容:
          Rust 0/1/2  → 原样返回(成功:无文件/有文件/部分成功)
          Rust 8/16   → 原样返回(失败/严重失败)
          Rust 255    → _CANCELLED_RC(进程级 -1 表示取消)
          MigrateEngineError → 16 + startup_failed 诊断

        失败诊断填充 self._last_copy_fail_reason(供 _format_copy_fail 取用),
        优先用 file_error 事件的诊断(含具体失败文件),其次用异常 message。

        :param src: 源目录
        :param dst: 目标目录
        :param action_label: "迁移"/"改迁"/"续传"/"还原"(仅用于日志文案)
        :param mode: "mirror"(=/MIR,含 purge,迁移主路径)或 "copy"(=/E,不含 purge,合并场景)
        :param purge_enabled: 是否启用 purge(mirror 模式下通常 True,copy 模式下 False)
        :param verify: "none"(默认)/"hash"(BLAKE3 校验)/"full"(后续实现)。
            引擎 copy+verify 一体:校验在复制完成后自动执行;
            修复 verify 接线漏改(P4 审核发现,原参数未透传到 run_job)。
        :return: 兼容复制引擎语义的返回码(0-7 成功,>=8 失败,_CANCELLED_RC 取消)
        :sideeffect: 设置 self._last_copy_fail_reason
        """
        # P5 用户选项(顶部设置区):哈希校验开关 + 复制线程数
        # 用户关闭校验时覆盖为 none;线程数通过 RAYON_NUM_THREADS 传给引擎
        if not self.cfg.get("verify_hash", True):
            verify = "none"
        try:
            # P10:自动分级(默认)或手动输入,上限=CPU 逻辑线程数(AMD/Intel 通用)
            _threads = _resolve_copy_threads(self.cfg)
            if _threads > 0:
                os.environ["RAYON_NUM_THREADS"] = str(_threads)
        except (TypeError, ValueError) as e:
            log.debug("忽略异常: %s", e)
        from migrate_engine import MigrateEngineError

        file_count = 0
        last_report = 0
        start_time = time.time()
        first_err = None  # (code, reason, suggestion, file) 取第一个 file_error
        cancelled_by_engine = False
        file_errors = []  # 所有 file_error 事件（用于完成时摘出失败文件）
        _mismatch_count = 0  # 校验不一致累计数（累加展示，避免逐文件刷屏）

        def on_event(evt):
            nonlocal file_count, last_report, first_err, cancelled_by_engine, _mismatch_count
            event = evt.get("event")
            if event == "progress":
                files_done = evt.get("files_done", 0)
                file_count = files_done
                # 限频:每 500 文件或 2 秒报告一次(与原复制路径一致)
                now = time.time()
                if files_done - last_report >= 500 or (last_report == 0 and now - start_time >= 2):
                    elapsed = now - start_time
                    rate = evt.get("rate_fps",
                                   files_done / elapsed if elapsed > 0 else 0)
                    # 累加模式：相同 accumulate key 原地更新，不新增日志行
                    self._emit_log("accumulate:migrate:copy_progress",
                        f"  📦 已复制 {files_done} 个文件（{elapsed:.1f}s，{rate:.1f} 文件/秒）...")
                    last_report = files_done
            elif event == "file_error":
                # 取第一个错误作为主诊断(与原复制路径 first_err 逻辑一致)
                err_path = evt.get("path", "")
                err_reason = evt.get("reason", "未知错误")
                if first_err is None:
                    first_err = (
                        evt.get("code", 0),
                        err_reason,
                        evt.get("suggestion", "请查看日志或重试"),
                        err_path,
                    )
                # 收集所有失败文件（完成时摘出展示）
                file_errors.append((err_path, err_reason))
            elif event == "info" and evt.get("key") == "fast_move":
                # P9:同卷快速移动(原子重命名,零复制)完成/回退事件
                val = evt.get("value", "")
                if val.startswith("done"):
                    detail = val[5:].strip() if len(val) > 5 else ""
                    self._emit_log("migrate",
                        f"  ⚡ 同卷快速移动完成（原子重命名，零复制）: {detail}")
                else:
                    self._emit_log("migrate",
                        f"  ⚡ 同卷快速移动不可用（{val}），回退复制引擎")
            elif event == "info" and evt.get("key") == "resume":
                # 续传信息(引擎从 ckpt 恢复时发出)
                self._emit_log("migrate", f"  📎 续传: {evt.get('value', '')}")
            elif event == "info" and evt.get("key") == "resume_reset":
                # 续传校验未通过(断电/损坏,ckpt 不可信)→ 整文件重传提示(引擎发出)
                # 用户可见:明确告知上次进度作废,数据完整性由重传保证
                self._emit_log("warn", f"  ♻️ {evt.get('value', '')}")
            elif event == "info" and evt.get("key") == "verify_start":
                # BLAKE3 校验开始（copy+verify 一体进入校验阶段）
                # 与复制进度同 accumulate key 原地替换：📦 已复制 → 🔍 校验中
                val = evt.get("value", "")
                self._emit_log("accumulate:migrate:copy_progress",
                    f"  🔍 BLAKE3 校验{val}")
            elif event == "info" and evt.get("key") == "verify":
                # BLAKE3 校验完成（copy+verify 一体，引擎在校验完成后发此事件）
                # 用与复制进度相同的 accumulate key 原地替换进度行：
                # 日志区一行进度演变（📦 已复制 → 🔍 校验中 → 🔍 校验完成），状态栏同步更新
                val = evt.get("value", "")
                self._emit_log("accumulate:migrate:copy_progress",
                    f"  🔍 BLAKE3 校验{val}")
            elif event == "verify_mismatch":
                # 校验不一致（内容与源不同）：累加汇总（累计数 + 最近文件），不刷屏
                _mismatch_count += 1
                err_path = evt.get("path", "?")
                self._emit_log("accumulate:warn:verify_mismatch",
                    f"  ⚠️ BLAKE3 校验不一致（内容与源不同）：第 {_mismatch_count} 个，"
                    f"最近: {err_path}")
            elif event == "cancelled":
                cancelled_by_engine = True

        # 跟踪引擎实例,供 force_cancel_copy 调 request_cancel
        # 用锁保护,与 force_cancel_copy 的读取互斥(沿用 _cancel_lock 锁纪律)
        with self._cancel_lock:
            if self._cancel_requested:
                # B1 修复:取消早返回必须在此处释放 _engine_for_cancel
                # (虽然此处没赋值,但防御性置 None,避免上一次调用残留的引用被误取消)
                self._engine_for_cancel = None
                return _CANCELLED_RC

        # D1 修复:_get_engine() 移到 try 块内
        # 原代码 _get_engine() 在 try 外面,导入失败(ImportError)或构造异常
        # 不会被 except MigrateEngineError 捕获,直接传播到调用方(上层可能崩溃)
        try:
            engine = self._get_engine()
            with self._cancel_lock:
                self._engine_for_cancel = engine
            rc = engine.run_job(
                source=src, target=dst, mode=mode,
                purge_enabled=purge_enabled, purge_soft_delete=True,
                verify=verify,
                on_event=on_event,
            )
        except MigrateEngineError as e:
            # 引擎启动失败 / 崩溃 / rc>=8 且 stderr 非空
            # 尝试从 message 提取 code=N(运行失败场景),否则视为启动失败/崩溃
            msg = str(e)
            m = re.search(r"code=(\d+)", msg)
            if m:
                rc = int(m.group(1))
                # B2 修复:统一为 bool 类型(_format_copy_fail 用 not diag.get("startup_failed") 判断)
                startup_failed = False
            else:
                rc = 16
                # D3 修复:扩展启动失败判断,覆盖"迁移引擎缺失"等其他启动类错误
                startup_failed = any(kw in msg for kw in
                    ("启动引擎失败", "迁移引擎缺失", "引擎不可用"))
            # 优先用 file_error 事件的诊断(含具体失败文件),否则用异常 message
            if first_err:
                self._last_copy_fail_reason = {
                    "code": first_err[0], "reason": first_err[1],
                    "suggestion": first_err[2], "file": first_err[3],
                    "startup_failed": startup_failed,
                }
            else:
                self._last_copy_fail_reason = {
                    "code": rc,
                    "reason": "无法启动迁移引擎" if startup_failed else "引擎异常",
                    "suggestion": msg, "file": "",
                    "startup_failed": startup_failed,
                }
            log_error_with_reason("引擎执行失败", msg,
                f"{action_label}: {src} -> {dst}")
            return rc
        except Exception as e:
            # D1 修复:捕获 _get_engine 的 ImportError / 构造异常等非 MigrateEngineError
            # 原代码这类异常会直接传播到调用方(上层 QThread 无 except 会崩溃)
            msg = str(e)
            self._last_copy_fail_reason = {
                "code": 16,
                "reason": "迁移引擎初始化失败",
                "suggestion": msg, "file": "",
                "startup_failed": True,
            }
            log_error_with_reason("引擎初始化失败", msg,
                f"{action_label}: {src} -> {dst}")
            return 16
        finally:
            with self._cancel_lock:
                self._engine_for_cancel = None

        # 退出码映射:255(进程级 -1)→ _CANCELLED_RC
        # 同时检查 cancelled 事件和取消标志(三重判定,避免漏判)
        if rc == 255 or cancelled_by_engine or self._cancel_requested:
            self._last_copy_fail_reason = None
            self._emit_log("migrate", f"  ⏹ {action_label}已取消，未完成")
            return _CANCELLED_RC

        # 最后一次进度汇报(确保用户看到最终文件数)
        if file_count > 0:
            elapsed = time.time() - start_time
            if file_errors:
                # 有失败文件：在完成行中摘出失败文件路径和原因
                err_summary = "\n".join(
                    f"    • {os.path.basename(p) if p else '?'} — {r}"
                    for p, r in file_errors[:10])
                if len(file_errors) > 10:
                    err_summary += f"\n    ... 还有 {len(file_errors) - 10} 个失败文件未显示"
                self._emit_log("migrate",
                    f"  📦 {action_label}完成：共复制 {file_count} 个文件（耗时 {elapsed:.1f}s）"
                    f"  ⚠ {len(file_errors)} 个文件失败：\n{err_summary}")
            else:
                self._emit_log("migrate",
                    f"  📦 {action_label}完成：共复制 {file_count} 个文件（耗时 {elapsed:.1f}s）✓")

        # 填充失败诊断(引擎 rc>=8 但未 raise 的场景,如 rc=8 无 stderr)
        if first_err:
            self._last_copy_fail_reason = {
                "code": first_err[0], "reason": first_err[1],
                "suggestion": first_err[2], "file": first_err[3],
            }
        else:
            self._last_copy_fail_reason = None

        return rc

    def _create_dir_link(self, src, dst):
        """创建目录链接（P6 建链自动选择：符号链接优先，Junction 兜底）。

        打包 exe 默认管理员运行(uac_admin=True),符号链接 /D 可正常创建,
        与旧版行为完全一致(os.path.islink/readlink 全部兼容);
        非管理员场景(开发者模式 pythonw 直跑)下 /D 失败 → Junction /J 兜底
        (无需管理员权限,旧版此处直接失败)。
        创建顺序:/D → /J → PowerShell New-Item,全部失败才返回 False。
        创建后用 is_symlink(st_reparse_tag 检测,符号链接/junction 均命中)验证。

        :param src: 链接路径（C 盘原路径，需不存在）
        :param dst: 链接目标（D 盘数据路径）
        :return: (ok: bool, err: str)
        """
        # 1) 目录符号链接(/D)：管理员或开发者模式可创建,与旧版行为一致
        try:
            subprocess.run(["cmd", "/c", "mklink", "/D", str(src), str(dst)],
                capture_output=True, check=True, creationflags=_NO_WINDOW_FLAGS)
            if is_symlink(str(src)):
                return True, ""
        except subprocess.CalledProcessError as e:
            log.debug("忽略异常: %s", e)
        # 2) Junction(/J)：无需管理员权限,非管理员场景兜底
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(src), str(dst)],
                           capture_output=True, creationflags=_NO_WINDOW_FLAGS)
        if r.returncode == 0 and is_symlink(str(src)):
            return True, ""
        # 3) PowerShell New-Item 兜底
        try:
            ps_ret = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"New-Item -ItemType SymbolicLink -Path '{_ps_quote(src)}' -Target '{_ps_quote(dst)}'"],
                capture_output=True, text=True, creationflags=_NO_WINDOW_FLAGS)
            if ps_ret.returncode == 0 and is_symlink(str(src)):
                return True, ""
        except Exception as e:
            log.debug("忽略异常: %s", e)
        return False, "mklink /D、/J 和 PowerShell 均失败（请检查路径/权限后重试）"

    def _check_file_in_use(self, path):
        """Restart Manager 检测目录下被占用的文件(RmStartSession/RmRegisterResources/RmGetList)。

        对应执行文档 §2.4 工作流升级:复制前主动检测占用,避免复制到一半
        因文件被占用失败(比事后 ERROR 32 报错体验好)。

        实测注意(2026-08-07 验证):
        - strSessionKey 必须传可写缓冲区(wchar[32]),传只读 c_wchar_p 常量
          会导致 RmStartSession 写只读内存 → 进程 AV(0xC0000005);
        - RmRegisterResources 注册"目录"在本机返回 ERROR_ACCESS_DENIED(5),
          注册"文件列表"有效(占用返回 ERROR_MORE_DATA=234 + 进程名),
          故此处枚举目录下文件(上限 2000,防超大目录拖慢)后按文件注册。

        :param path: 要检测的目录路径
        :return: 占用进程名列表(去重,已排序);检测失败返回 None(不阻断,降级)
        """
        import ctypes
        try:
            # 用模块级缓存句柄(反复 LoadLibrary/FreeLibrary 会在进程退出
            # GC 时触发 0xC0000005,详见 _get_rm_dll 注释)
            rm = _get_rm_dll()
        except OSError:
            return None  # rstrtmgr.dll 不可用(旧系统/精简版),降级不检测

        # ⚠️ 结构体必须与 SDK 布局一致(restartmanager.h):
        # FILETIME 是 2×DWORD(对齐 4),用 c_ulonglong 会按 8 对齐导致整个
        # RM_PROCESS_INFO 从 668 变大到 672 → strAppName 错位 4 字节(乱码),
        # 且 RM 按 C 布局读写与 Python 布局错位 → 进程崩溃(0xC0000005)。
        class RM_UNIQUE_PROCESS(ctypes.Structure):
            _fields_ = [("dwProcessId", ctypes.c_ulong),
                        ("dwLowDateTime", ctypes.c_ulong),
                        ("dwHighDateTime", ctypes.c_ulong)]  # FILETIME 拆 2×DWORD

        class RM_PROCESS_INFO(ctypes.Structure):
            _fields_ = [("Process", RM_UNIQUE_PROCESS),
                        ("strAppName", ctypes.c_wchar * 256),
                        ("strServiceShortName", ctypes.c_wchar * 64),
                        ("ApplicationType", ctypes.c_ulong),
                        ("AppStatus", ctypes.c_ulong),
                        ("TSSessionId", ctypes.c_ulong),
                        ("bRestartable", ctypes.c_int)]

        assert ctypes.sizeof(RM_PROCESS_INFO) == 668, (
            f"RM_PROCESS_INFO 布局异常: {ctypes.sizeof(RM_PROCESS_INFO)} != 668")

        # 64 位下必须显式声明参数/返回类型,否则指针截断
        rm.RmStartSession.argtypes = [ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong,
                                      ctypes.c_wchar_p]
        rm.RmStartSession.restype = ctypes.c_ulong
        rm.RmRegisterResources.argtypes = [ctypes.c_ulong, ctypes.c_uint,
                                           ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_uint,
                                           ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
        rm.RmRegisterResources.restype = ctypes.c_ulong
        rm.RmGetList.argtypes = [ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint),
                                 ctypes.POINTER(ctypes.c_uint),
                                 ctypes.POINTER(RM_PROCESS_INFO), ctypes.POINTER(ctypes.c_ulong)]
        rm.RmGetList.restype = ctypes.c_ulong
        rm.RmEndSession.argtypes = [ctypes.c_ulong]
        rm.RmEndSession.restype = ctypes.c_ulong

        session = ctypes.c_ulong(0)
        # ⚠️ 审查修复:实测 RmStartSession 会向 strSessionKey 缓冲区写入会话标识
        # (GUID,36+1 wchar=74 字节),32 wchar 缓冲装不下 → 越界写破坏堆,
        # 后续 GC/分配时 0xC0000005(仅当 DLL 句柄缓存后,反复调用仍复现)。
        # 缓冲放大到 64 wchar;且每次生成唯一 key(同 key 复用行为未定义)。
        key = (ctypes.c_wchar * 64)()
        key.value = "CDriveRelocatorP6InUseCheck_%08x" % id(key)
        if rm.RmStartSession(ctypes.byref(session), 0, key) != 0:
            return None
        try:
            # 枚举目录下文件(上限 2000,链接不跟随;RM 对目录注册不可靠,按文件注册)
            files = []
            stack = [str(path)]
            while stack and len(files) < 2000:
                cur = stack.pop()
                try:
                    with os.scandir(cur) as it:
                        for e in it:
                            if len(files) >= 2000:
                                break
                            try:
                                # P6 修复:e.is_symlink() 对 Junction 返回 False,
                                # 会穿透枚举链接目标(可能其他盘)的文件——误注册占用
                                # 导致迁移被错误中止;is_symlink(st_reparse_tag) 覆盖两种链接
                                if is_symlink(e.path):
                                    continue
                                if e.is_dir(follow_symlinks=False):
                                    stack.append(e.path)
                                else:
                                    files.append(e.path)
                            except OSError:
                                continue
                except OSError:
                    continue
            if not files:
                return []  # 目录为空(或全部符号链接):无文件可占用
            fnames = (ctypes.c_wchar_p * len(files))(*files)
            rc = rm.RmRegisterResources(session, len(files), fnames, 0, None, 0, None)
            if rc != 0:
                return None
            # 第一次调用只拿所需数量(占用时返回 ERROR_MORE_DATA=234)
            needed = ctypes.c_uint(0)
            count = ctypes.c_uint(0)
            reasons = ctypes.c_ulong(0)
            rc = rm.RmGetList(session, ctypes.byref(needed),
                              ctypes.byref(count), None, ctypes.byref(reasons))
            if rc != 0 and rc != 234:
                return None
            if needed.value == 0:
                return []
            info = (RM_PROCESS_INFO * needed.value)()
            count = ctypes.c_uint(needed.value)
            rc = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count),
                              info, ctypes.byref(reasons))
            if rc != 0:
                return None
            names = set()
            for i in range(count.value):
                name = info[i].strAppName
                if name:
                    names.add(name)
            return sorted(names)
        finally:
            rm.RmEndSession(session)

    def _run_copy_with_progress(self, src, dst, action_label="迁移",
                                verify="hash"):
        """统一复制入口,P4 起默认走 Rust 引擎(ADR-003)。

        P7 起硬编码走引擎(ADR-003 完全取代,不留切换后门)。

        P5(v5 §11.4):主路径默认 verify="hash" 强制开启——
        本入口所有调用方都是"删源/换路径"主业务(迁移/改迁/续传/还原),
        删除原始文件前必须过 BLAKE3 校验(copy+verify 一体,复制完成即校验,
        校验失败 rc>=8 → 调用方中止且不删源)。
        合并类场景(fix_broken_link/_auto_fix_link/rebuild_all_links)直接调
        _run_engine_with_progress(默认 none),不在本入口范围。

        :param src: 源目录
        :param dst: 目标目录
        :param action_label: "迁移"/"改迁"/"续传"/"还原"(仅用于日志文案)
        :param verify: "hash"(默认,P5 强制)/"none"(显式关闭)
        :return: 兼容复制引擎语义的返回码
        """
        # P6:复制前 Restart Manager 占用检测——避免复制到一半因文件占用失败。
        # 命中时直接返回 16(失败语义),不启动引擎;调用方(迁移/还原/续传)统一
        # 走失败分支(保留 pending、不删源)。检测失败(RM 不可用)降级不阻断。
        busy = self._check_file_in_use(src)
        if busy:
            names = ", ".join(busy[:5])
            self._emit_log("warn",
                f"  ⚠️ 检测到 {len(busy)} 个进程正在占用源目录: {names}")
            self._last_copy_fail_reason = {
                "code": 32,
                "reason": f"以下进程正在占用源目录文件: {names}",
                "suggestion": "请关闭这些程序后重试",
                "file": str(src),
                "startup_failed": False,
            }
            return 16
        return self._run_engine_with_progress(src, dst, action_label,
                                              verify=verify)

    def force_cancel_copy(self):
        """请求取消当前复制任务。

        调 request_cancel(优雅退出,引擎在下个块边界 save ckpt 后停止),
        force_kill 兜底(引擎卡在慢 I/O 时)。
        两种路径都设置 _cancel_requested 标志,让 worker 线程的取消检查生效。

        中危-10:用 _cancel_lock 保护 _engine_for_cancel 的复合操作,
        避免与 worker 线程并发时竞态。
        """
        with self._cancel_lock:
            self._cancel_requested = True
            self._recover_cancel_requested = True  # 让 recover_pending_* 循环也停止
            engine = self._engine_for_cancel

        # 引擎路径:先 request_cancel(优雅,引擎 save ckpt 后退出,保留续传能力)
        # 再用 force_kill 兜底(场景:引擎卡在慢 I/O/网络挂载,cancel_token 轮询不到)
        # N2 修复:request_cancel 后先等引擎退出(轮询 returncode,上限 3 秒),
        # 让引擎有机会在块边界保存 ckpt;超时才 force_kill。
        # 原实现立即 force_kill 使 ckpt 几乎必丢,下次续传整文件重传。
        if engine is not None:
            try:
                engine.request_cancel()
            except Exception as e:
                log.debug("忽略异常: %s", e)
            try:
                if not engine.wait_exit(3.0):
                    engine.force_kill()
            except Exception as e:
                log.debug("忽略异常: %s", e)


    def _format_copy_fail(self, rc, src_path, action_label="迁移"):
        """格式化复制失败消息，附加诊断原因 + 失败文件 + 续传提示

        读取 _run_engine_with_progress 填充的 self._last_copy_fail_reason，
        拼出对用户友好的失败消息（含错误码、原因、建议、失败文件、续传说明）。
        取消场景（rc == _CANCELLED_RC）返回专用的取消消息，不显示错误原因。

        :param rc: 复制引擎返回码（或 _CANCELLED_RC 表示取消）
        :param src_path: 源路径（str 或 Path，用于提取嫌疑软件名加到建议）
        :param action_label: "迁移"/"改迁"/"续传"（用于文案）
        :return: (短日志, 长消息)  短日志给 _emit_log，长消息给返回值
        """
        # 取消场景：返回专用消息，不显示错误原因和续传提示
        # 取消时已复制的数据保留在目标盘，下次启动会自动续传
        if rc == _CANCELLED_RC:
            short_log = f"  ⏹ {action_label}已取消"
            long_msg = f"{action_label}已取消。\n已记录未完成事务，下次启动程序会自动续传。"
            return short_log, long_msg

        diag = getattr(self, "_last_copy_fail_reason", None) or {}

        # 从源路径提取嫌疑软件名（顶层目录名），用于占用错误时给出更具体的建议
        # 防御性处理：src_path 为 None/空/非字符串时，suspect_name 保持空
        suspect_name = ""
        if src_path:
            try:
                name = Path(str(src_path)).name
                if name and name.lower() not in ("none", ".", ".."):
                    suspect_name = name
            except Exception:
                suspect_name = ""

        # 短日志：错误码 + 原因（一行）
        if diag.get("reason"):
            short_log = f"  ✗ {action_label}失败（返回码 {rc}）: {diag['reason']}"
        else:
            short_log = f"  ✗ {action_label}失败（返回码 {rc}）"

        # 长消息：错误码 + 原因 + 建议 + 失败文件 + 续传提示
        parts = [f"{action_label}失败（返回码 {rc}）。"]
        if diag.get("reason"):
            # 引擎内部码(0xE0000000 段)显示 hex,真实 Win32 码显示十进制
            code = diag.get('code', '?')
            if isinstance(code, int) and code >= 0xE0000000:
                code_disp = "0x%08X" % code
            else:
                code_disp = code
            parts.append(f"原因：{diag['reason']}（ERROR {code_disp}）")
        if diag.get("suggestion"):
            # 文件被占用时，若能提取到嫌疑软件名，附加到建议里
            suggestion = diag['suggestion']
            if diag.get('code') == _ERR_SHARING_VIOLATION and suspect_name:
                suggestion = f"文件被占用，建议关闭 {suspect_name} 软件后重试"
            parts.append(f"建议：{suggestion}")
        if diag.get("file"):
            parts.append(f"失败文件：{diag['file']}")
        # 仅在复制引擎真正运行过（非启动失败）时才提示续传
        # 启动失败场景下根本没复制数据，提示"已保留已复制数据"会误导用户
        if not diag.get("startup_failed"):
            # 还原场景：数据从目标盘→C盘，源数据完整，C盘部分复制
            # 迁移场景：数据从C盘→目标盘，目标盘已保留已复制的数据
            if "还原" in action_label:
                parts.append(f"已记录未完成事务，源盘数据完整，C盘已部分复制数据。")
            else:
                parts.append(f"已记录未完成事务，目标盘已保留已复制的数据。")
            parts.append(f"下次启动程序会自动续传（引擎幂等重跑）。")
        long_msg = "\n".join(parts)

        return short_log, long_msg

    def _check_dst_nonempty(self, dst_path, src_path):
        """检测目标路径是否非空（防止误删用户文件）

        镜像同步(mirror)会删除目标中源没有的文件，如果用户在目标目录放了文件会被误删。
        此方法在迁移/改迁前检测目标路径是否有内容，有则返回警告。

        :return: (is_nonempty: bool, warning: str)
            is_nonempty=True 表示目标非空，调用方应弹确认框
        """
        try:
            if not os.path.exists(dst_path):
                return False, ""
            if not os.path.isdir(dst_path):
                return False, ""
            entries = os.listdir(dst_path)
            if not entries:
                return False, ""
            # 统计文件数
            file_count = 0
            for entry in entries:
                ep = os.path.join(dst_path, entry)
                if os.path.isfile(ep):
                    file_count += 1
                elif os.path.isdir(ep):
                    try:
                        for _ in Path(ep).rglob('*'):
                            if _.is_file():
                                file_count += 1
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
            return True, (f"目标目录已存在且非空：{dst_path}\n"
                         f"  包含 {len(entries)} 个条目，约 {file_count} 个文件\n"
                         f"  ⚠️ 镜像同步会删除目标中源没有的文件！\n\n"
                         f"是否覆盖目标目录？")
        except Exception:
            return False, ""

    def migrate(self, src_path, dst_path=None, kill_process=None, force_overwrite=False):
        """迁移源目录到目标盘（事务性，支持断电恢复）

        ⚠️ 此操作会删除源目录的真实数据（清空源目录），并在原位置创建符号链接指向目标。
        源目录清空后由符号链接替代，软件通过原路径访问时自动跳转到目标盘，无感知。

        流程：
        1. 写入 pending 事务记录到 config.json（标记开始）
        2. 复制引擎镜像复制（幂等重跑，断电续传）
        3. 文件数完整性验证
        4. 删除源目录（rd /s /q 优先，最快）—— 源目录会被清空删除
        5. 创建符号链接（源路径 → 目标路径）
        6. 从 pending 移除，加入 completed 记录
        6.5 链式修复：若 src 是某条旧记录的 dst（用户对已迁移目录再次换路径），
            自动更新旧记录的 dst 并重建旧 src 链接直指新目标，消除套娃

        断电恢复：启动时调用 recover_pending_migrations() 自动处理未完成事务

        关于"换路径"：用户中途想把数据从 D 盘挪到 H 盘（或挪回 C 盘）是正常需求，
        本方法不阻止。链式套娃由步骤 6.5 自动消除，保证 src 直指最终真实数据。
        多个 src 直指同一个 dst 是用户的合法选择，不算冲突，不会被清理。
        """
        src = Path(src_path)
        if not src.exists() and not is_symlink(src_path):
            log_error_with_reason("源目录不存在", context=f"迁移: {src_path}")
            return False, f"源目录不存在: {src_path}"
        # 如果 src 是符号链接，自动转 migrate_symlink() 处理（支持换路径）
        # 场景：用户对已迁移的目录再次迁移（换路径），src 此时是符号链接
        # migrate_symlink 会更新链接指向新目标，不产生链式
        if is_symlink(src_path):
            if not dst_path:
                return True, f"已经是符号链接且未指定新目标: {src_path}"
            self._emit_log("migrate", f"  🔗 检测到符号链接，自动转改迁模式: {src.name}")
            return self.migrate_symlink(src_path, dst_path, force_overwrite=force_overwrite)
        # 明确拒绝文件源：本工具只支持迁移文件夹（文件无法用目录链接重定向）。
        # 此前文件源会走到引擎 walk 失败(ERROR_DIRECTORY)，用户只看到通用"复制失败"。
        # 拒绝发生在事务写入前，不产生 pending、不触碰源文件。
        if src.is_file():
            log_error_with_reason("不支持迁移单个文件", context=f"迁移: {src_path}")
            return False, (f"仅支持迁移文件夹，暂不支持迁移单个文件：\n  {src_path}\n"
                           f"如需迁移单个文件，请先将其放入一个文件夹后迁移该文件夹。")
        if not dst_path:
            dst_path = str(Path(self.cfg["g_root"]) / src.name / "appdata")
        dst = Path(dst_path)

        # ===== 步骤0.1：src/dst 路径校验（防止镜像同步数据安全事故）=====
        # 修复 N2：原代码 src==dst 时跳过包含校验，复制引擎虽会报错但用户看到模糊错误
        # 现改为显式拒绝，给出清晰提示（判定逻辑提取为 _validate_migration_paths）
        _ok, _err = _validate_migration_paths(src_path, dst_path)
        if not _ok:
            log_error_with_reason("迁移路径校验失败",
                f"src={src_path}, dst={dst_path}", "路径校验拒绝迁移")
            return False, _err

        if kill_process:
            subprocess.run(["taskkill", "/F", "/IM", kill_process],
                           capture_output=True, creationflags=_NO_WINDOW_FLAGS)

        # 检查目标盘是否存在（防止 U 盘被拔、盘符错误等情况导致 mkdir 崩溃）
        if dst.is_absolute():
            dst_root = dst.anchor  # e.g. "D:\\"
            if not os.path.exists(dst_root):
                log_error_with_reason("目标盘不存在", context=f"迁移: {src_path} -> {dst_path}")
                return False, f"目标盘不存在: {dst_root}（请检查目标盘是否已连接）"

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, FileNotFoundError) as e:
            log_error_with_reason("目标目录创建失败",
                f"无法创建目录: {e}", f"迁移: {src_path} -> {dst_path}")
            return False, f"目标目录创建失败: {dst.parent}\n错误: {e}"

        # ===== 步骤0：清理目标路径的残留符号链接 =====
        # 如果目标路径是符号链接（包括断链），复制引擎会跟着链接走
        # 导致数据写错位置或产生链式链接，必须先删除符号链接（只删链接不删数据）
        # 注意：os.path.exists 对断链符号链接返回 False，所以用 is_symlink 判断
        if is_symlink(dst_path):
            try:
                os.rmdir(dst_path)
                log.info(f"迁移: 删除目标路径残留符号链接: {dst_path}")
                self._emit_log("migrate", f"  🔗 已清理目标路径残留符号链接: {os.path.basename(dst_path)}")
            except Exception as e:
                # 删除失败必须中止，否则 复制引擎会跟着符号链接走，产生链式链接
                log_error_with_reason("目标路径符号链接清理失败",
                    f"无法删除符号链接: {e}", f"迁移: {src_path} -> {dst_path}")
                return False, (f"目标路径已存在符号链接但无法删除: {dst_path}\n"
                              f"错误: {e}\n"
                              f"请手动删除该符号链接后重试。")

        # ===== 步骤0.5：目标非空警告（防止误删用户文件）=====
        # 复制引擎（镜像模式）会删除目标中源没有的文件，如果用户在目标目录放了文件会被误删
        # 调用方传 force_overwrite=True 可跳过此检测
        if not force_overwrite:
            is_nonempty, warning = self._check_dst_nonempty(dst_path, src_path)
            if is_nonempty:
                return False, f"NEED_CONFIRM_OVERWRITE\n{warning}"

        # ===== 步骤0.8：磁盘空间预检查（避免 复制引擎写到一半空间不足卡住）=====
        try:
            # 使用 lstat 而非 stat，避免跟随符号链接导致重复计算或循环
            # 排除符号链接文件，防止嵌套链接造成空间计算虚高
            src_size = self._real_size_bytes_fast(src)  # #25:MFT 优先,跨盘回退
            if dst.is_absolute():
                dst_drive = dst.anchor
                dst_usage = shutil.disk_usage(dst_drive)
                src_mb = src_size // 1024 // 1024
                free_mb = dst_usage.free // 1024 // 1024
                if dst_usage.free < src_size:
                    self._emit_log("error", f"  ✗ 目标盘空间不足: 需要 {src_mb}MB，剩余 {free_mb}MB")
                    return False, (f"目标盘空间不足: {dst_drive}\n"
                                  f"  需要: {src_mb}MB\n"
                                  f"  剩余: {free_mb}MB\n"
                                  f"请清理目标盘空间或更换目标盘。")
                self._emit_log("migrate", f"  ✓ 空间检查: 需要 {src_mb}MB，剩余 {free_mb}MB")
        except Exception as e:
            # 预检查失败必须提示用户，不能静默吞掉（否则可能迁移到一半空间不足卡住）
            log.warning(f"磁盘空间检查失败（可能影响迁移）: {e}")
            self._emit_log("warn", f"⚠️ 磁盘空间检查异常: {e}，可能导致迁移中途失败")

        # ===== 云同步占位符检测：迁移前提示，不中断 =====
        # 占位文件复制会触发强制下载（hydration），弱网/离线时拖慢甚至失败。
        # 放在所有校验之后、写事务之前——校验失败时不做无谓扫描。
        try:
            _ph_count, _ph_example = count_cloud_placeholder_files(str(src))
            if _ph_count > 0:
                log.warning(f"迁移源含 {_ph_count} 个云同步占位文件: {_ph_example}")
                self._emit_log("warn",
                    f"  ⚠️ 源目录含 {_ph_count} 个云同步占位文件（OneDrive 等），"
                    f"迁移会触发下载：{_ph_example}")
        except Exception as e:
            log.debug("忽略异常: %s", e)

        # ===== 步骤1：写入 pending 事务记录（断电恢复依据）=====
        pending = self.cfg.setdefault("pending_migrations", [])
        # 清除同 src 的旧 pending 记录（上次未完成）
        pending[:] = [p for p in pending if p.get("src") != src_path]
        pending.append({
            "src": str(src),
            "dst": str(dst),
            "stage": "started",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        save_all(self.cfg)
        self._emit_log("migrate", f"📤 开始迁移: {src.name} | {src} → {dst}")

        # ===== 步骤2：复制引擎复制数据（/MIR 幂等，可断电续传）=====
        log.info(f"复制引擎: {src} -> {dst}")
        self._emit_log("migrate", f"  ⏳ 正在复制数据（镜像模式）: {src.name}...")
        rc = self._run_copy_with_progress(src, dst, action_label="迁移")
        if rc >= 8 or rc == _CANCELLED_RC:
            short_log, long_msg = self._format_copy_fail(rc, src_path, "迁移")
            log_error_with_reason("复制失败",
                f"返回码: {rc}",
                f"迁移: {src_path} -> {dst_path}")
            self._emit_log("error", f"{short_log}: {src.name}")
            # 更新 pending stage，下次启动会自动续传（/MIR 幂等）
            # 不清理目标盘已复制的数据，避免浪费进度
            for p in pending:
                if p.get("src") == src_path:
                    p["stage"] = "rustcopy_failed"
                    if rc == _CANCELLED_RC:
                        # 取消时记录"用户取消"，不写"返回码 -1"（-1 是内部取消码，对用户无意义）
                        p["error"] = "用户取消，下次启动会自动续传"
                    else:
                        diag = getattr(self, "_last_copy_fail_reason", None) or {}
                        if diag.get("reason"):
                            p["error"] = f"返回码 {rc} - {diag['reason']}"
                        else:
                            p["error"] = f"返回码 {rc}"
            save_all(self.cfg)
            return False, long_msg

        # 更新 stage
        for p in pending:
            if p.get("src") == src_path:
                p["stage"] = "rustcopy_done"
        save_all(self.cfg)
        self._emit_log("migrate", f"  ✓ 数据复制完成: {src.name} (返回码 {rc})")

        # ===== 步骤3：文件数完整性验证 =====
        try:
            src_file_count = self._count_files_fast(src)
            dst_file_count = self._count_files_fast(dst)
            self._emit_log("migrate",
                f"  🔍 文件数验证: C盘 {src_file_count} 个 / 目标盘 {dst_file_count} 个 ({src.name})")
            if src_file_count > 0 and dst_file_count < src_file_count:
                log_error_with_reason("数据完整性验证失败",
                    f"源 {src_file_count} 文件, 目标 {dst_file_count} 文件",
                    f"迁移: {src_path} -> {dst_path}")
                for p in pending:
                    if p.get("src") == src_path:
                        p["stage"] = "integrity_failed"
                        p["error"] = f"src={src_file_count}, dst={dst_file_count}"
                save_all(self.cfg)
                return False, (f"数据完整性验证失败：C盘 {src_file_count} 个文件，"
                              f"目标盘仅 {dst_file_count} 个文件。\n"
                              f"可能有文件被占用，请关闭相关程序后重试。\n"
                              f"已记录未完成事务，目标盘已保留已复制的数据，\n"
                              f"下次启动程序会自动续传（引擎幂等补齐缺失文件）。")
        except Exception as e:
            # 验证异常时最该保守：无法确认 dst 数据完整性就删 src 会导致双端丢失
            # 改为中止迁移并标记 integrity_failed，下次启动重新 复制引擎（镜像模式）补齐
            log_error_with_reason("完整性验证异常",
                f"异常: {e}", f"迁移: {src_path} -> {dst_path}")
            for p in pending:
                if p.get("src") == src_path:
                    p["stage"] = "integrity_failed"
                    p["error"] = f"验证异常: {e}"
            save_all(self.cfg)
            return False, (f"完整性验证异常，已中止迁移以保护数据: {e}\n"
                          f"已记录未完成事务，下次启动会自动续传。")

        # ===== 步骤4：清空源目录内容 + 删除空目录（安全删除，非 rd /s /q 暴力删）=====
        # 用户要求：不暴力删文件夹，先清空内容再删空目录，断电时更安全
        # 清空过程中断电：目录还在（部分内容），重启后 复制引擎（镜像模式）补齐再清空
        # 删空目录后断电：目录不存在，重启后直接 mklink
        # mklink /D 要求路径不存在，所以必须删除空目录本身
        # 中危-10：删源前复查取消标志——复制引擎成功返回后、完整性验证期间用户可能退出，
        # 此时 _cancel_requested 为 True，不应继续删源（数据已在 dst，但退出时强杀会导致部分删除）
        with self._cancel_lock:
            _cancelled_now = self._cancel_requested
        if _cancelled_now:
            self._emit_log("migrate", f"  ⏹ 迁移已取消（删源前复查），目标盘数据已完整: {src.name}")
            for p in pending:
                if p.get("src") == src_path:
                    p["stage"] = "rustcopy_done"
                    p["error"] = "用户取消（删源前），目标盘数据已完整"
            save_all(self.cfg)
            return False, f"迁移已取消（删源前），目标盘数据已完整: {dst}\n下次启动会自动续传（仅剩删源+建链接）。"
        self._emit_log("migrate", f"  🗑 正在处理源目录: {src.name}...")
        src_deleted = False
        delete_errors = []

        # P9:同卷快速移动已由引擎完成(源被原子 rename 到目标),或源已被外部删除:
        # 跳过删源步骤,直接进入建链
        if not os.path.exists(str(src)):
            self._emit_log("migrate",
                f"  ⚡ 源目录已不存在(同卷快速移动已完成),跳过删源")
            src_deleted = True

        # 清空源目录内容 + 删除空目录（安全删除，非 rd /s /q 暴力删）
        # 数据已完整复制到目标盘并通过校验，源目录直接永久删除
        if not src_deleted:
            # 步骤4a：清空目录内容（保留目录本身）
            cleanup_ok, cleanup_err = self._cleanup_dir_contents(str(src))
            if not cleanup_ok:
                delete_errors.append(f"清空内容失败: {cleanup_err}")
                # 清空失败，尝试 rd /s /q 兜底（文件可能被占用）
                try:
                    src_deleted, rd_err = self._safe_rd(str(src))
                    if not src_deleted:
                        delete_errors.append(f"rd兜底后仍存在: {rd_err}")
                except Exception as e:
                    delete_errors.append(f"rd兜底异常: {e}")
            else:
                # 步骤4b：内容已清空，删除空目录本身
                try:
                    os.rmdir(str(src))
                    src_deleted = True
                    log.info(f"迁移: 空目录已删除: {src_path}")
                except Exception as e:
                    delete_errors.append(f"删空目录失败: {e}")
                    # 兜底：rd /s /q（空目录应该能直接删）
                    try:
                        src_deleted, rd_err = self._safe_rd(str(src))
                        if not src_deleted:
                            delete_errors.append(f"rd空目录兜底失败: {rd_err}")
                    except Exception as e2:
                        delete_errors.append(f"rd空目录异常: {e2}")

        # 最后兜底：重命名（绕过文件占用）
        if not src_deleted:
            try:
                bak_path = str(src) + "._cdrive_bak"
                if os.path.exists(bak_path):
                    shutil.rmtree(bak_path, ignore_errors=True)
                os.rename(str(src), bak_path)
                log.info(f"源目录被占用，已重命名为 {bak_path}，将由后台清理")
                src_deleted = True
                self._emit_log("warn",
                    f"  ⚠️ 源目录被占用，已重命名为 {os.path.basename(bak_path)}，将由后台清理")
            except Exception as e2:
                delete_errors.append(f"rename兜底: {e2}")

        if not src_deleted:
            err_detail = "; ".join(delete_errors[:3])
            log_error_with_reason("删除源目录失败",
                f"所有删除策略均失败: {err_detail}",
                f"迁移: {src_path}")
            self._emit_log("error", f"  ✗ 删除 C 盘源目录失败: {src.name} - {err_detail[:80]}")
            for p in pending:
                if p.get("src") == src_path:
                    p["stage"] = "delete_failed"
                    p["error"] = err_detail
            save_all(self.cfg)
            return False, (f"删除C盘源目录失败（可能文件被占用）。\n"
                          f"错误详情: {err_detail}\n"
                          f"目标盘已有完整数据: {dst}\n"
                          f"⚠️ 已记录未完成事务，请关闭占用程序后重启程序，会自动重试删除并创建链接。")

        # 更新 stage
        for p in pending:
            if p.get("src") == src_path:
                p["stage"] = "src_deleted"
        save_all(self.cfg)
        self._emit_log("migrate", f"  ✓ C 盘源目录已删除: {src.name}")

        # ===== 步骤5：创建链接（符号链接 /D 优先，Junction 兜底）=====
        self._emit_log("migrate", f"  🔗 正在创建链接: {src.name} → {dst}")
        mklink_ok, mklink_err = self._create_dir_link(str(src), str(dst))
        if not mklink_ok:
            log_error_with_reason("创建链接失败",
                mklink_err, f"迁移: {src_path} -> {dst_path}")
            self._emit_log("error", f"  ✗ 创建链接失败: {src.name}（{mklink_err}）")
            for p in pending:
                if p.get("src") == src_path:
                    p["stage"] = "mklink_failed"
                    p["error"] = mklink_err
            save_all(self.cfg)
            # C 盘已删除但链接未创建，下次启动会自动重试
            return False, (f"创建链接失败（{mklink_err}）。\n"
                          f"⚠️ C 盘目录已删除，数据完整在 D 盘: {dst}\n"
                          f"已记录未完成事务，下次启动程序会自动重试创建链接。")

        # ===== 步骤6：完成事务，从 pending 移除，加入 completed =====
        self._add_migrated_record(str(src), str(dst))
        # 从 pending 移除
        pending[:] = [p for p in pending if p.get("src") != src_path]
        save_all(self.cfg)

        # ===== 步骤6.5：修复链式符号链接 + 清理过渡符号链接（保证一对一）=====
        # 场景：src_path 之前是某个已迁移记录的真实数据目录（有符号链接指向它），
        # 现在 src_path 变成了符号链接指向 dst，原来的符号链接就形成链式：
        #   旧链接 old_src → src_path(现在是链接) → dst
        #
        # 修复两件事：
        # 1. 把旧链接直接重指向 dst，消除套娃
        # 2. 清理 src_path 这个过渡符号链接 + 从 migrated 表移除 src_path→dst 记录
        #    因为 src_path 曾是旧记录的 dst（真实数据），迁走后变成符号链接，
        #    它只是换路径的过渡产物，不是用户想要的最终入口。保留它会造成多对一
        #    （old_src→dst 和 src_path→dst 两条记录指向同一真实数据）。
        #    清理后只剩 old_src→dst 一条记录，保证一对一。
        #
        # 典型场景：C:\Sdk → D:\旧\sdk(真实) 先迁移，再把 D:\旧\sdk 迁到 H:\新
        #   修复前：C:\Sdk→D:\旧\sdk(链接)→H:\新（链式），且 D:\旧\sdk→H:\新（多对一）
        #   修复后：C:\Sdk→H:\新（直指，唯一记录），D:\旧\sdk 符号链接已删除
        try:
            _norm_src = os.path.normpath(src_path).lower()
            chain_fixed = 0
            chain_fix_failed = 0  # 链式修复失败计数，>0 时不清理过渡链接（避免断链）
            for m in self.cfg.get("migrated", []):
                old_src = m.get("src", "")
                old_dst = m.get("dst", "")
                # 找到 dst 指向当前 src_path 的旧记录（排除自引用）
                if (old_src
                        and os.path.normpath(old_dst).lower() == _norm_src
                        and os.path.normpath(old_src).lower() != _norm_src
                        and is_symlink(old_src)):
                    try:
                        # 删除旧符号链接（只删链接不删数据）
                        if os.path.isdir(old_src):
                            os.rmdir(old_src)
                        else:
                            os.unlink(old_src)
                        # 重建为直接指向新 dst 的链接（符号链接 /D 优先）
                        ok, _ = self._create_dir_link(old_src, str(dst))
                        if not ok:
                            raise Exception("创建链接失败")
                        # 更新 config 记录的 dst 字段为新目标
                        m["dst"] = str(dst)
                        m["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        chain_fixed += 1
                        self._emit_log("migrate",
                            f"  🔗 修复链式链接: {os.path.basename(old_src)} → {dst}（原指向 {src_path}）")
                        log_link_operation("修复链式链接", old_src, str(dst),
                            f"原指向 {src_path}, 现直指新目标")
                    except Exception as e:
                        chain_fix_failed += 1
                        log.error(f"修复链式链接失败 {old_src}: {e}")
                        self._emit_log("warn",
                            f"  ⚠️ 修复链式链接失败: {os.path.basename(old_src)} - {e}")

            # ===== 清理过渡符号链接（保证一对一）=====
            # 只有发生了链式修复（chain_fixed > 0）才需要清理
            # 且所有链式修复都成功（chain_fix_failed == 0）才安全删除过渡链接
            # 否则某个旧 src 还指向 src_path，删了会导致它断链
            #
            # 安全防护：只清理非 C 盘的过渡符号链接
            # C 盘路径是软件的原始入口，删除会导致软件找不到数据，必须保留
            # 实际换路径场景 src_path 通常是 D/H 等非 C 盘（曾是迁移目标），可安全清理
            if chain_fixed > 0 and chain_fix_failed == 0:
                try:
                    _src_lower = src_path.lower().replace("\\\\?\\", "")
                    is_c_drive = _src_lower.startswith("c:")
                    if is_c_drive:
                        # C 盘过渡链接保留（软件入口，不能删），但记录仍需移除避免多对一
                        # 旧记录已更新为 old_src→dst，C 盘这条记录保留会导致多对一
                        # 但 C 盘符号链接还在指向 dst，所以保留记录是合理的（链接确实指向 dst）
                        # 这种场景极罕见（C 盘路径曾是另一个记录的 dst），保留现状最安全
                        log.info(f"C 盘过渡链接 {src_path} 保留（软件入口），记录保留")
                        save_all(self.cfg)
                    else:
                        # 非 C 盘过渡链接：清除软连接，重建空目录保留文件夹本身
                        # 用户要求：只清除软连接，不删文件夹目录
                        # 删除符号链接后重建空真实目录，既避免多对一又保留文件夹
                        if is_symlink(src_path):
                            try:
                                if os.path.isdir(src_path):
                                    os.rmdir(src_path)
                                else:
                                    os.unlink(src_path)
                                # 重建空真实目录，保留文件夹本身
                                os.makedirs(src_path, exist_ok=True)
                                log.info(f"清除过渡软连接并保留空目录: {src_path}")
                            except Exception as e:
                                log.warning(f"清除过渡软连接失败（保留现状）: {src_path} - {e}")
                        # 从 migrated 表移除 src_path→dst 这条记录（步骤6刚加的）
                        # 保留旧记录（已更新为 old_src→dst），实现一对一
                        _src_norm = os.path.normpath(src_path).lower()
                        before = len(self.cfg.get("migrated", []))
                        self.cfg["migrated"] = [
                            m for m in self.cfg.get("migrated", [])
                            if os.path.normpath(m.get("src", "")).lower() != _src_norm
                        ]
                        after = len(self.cfg["migrated"])
                        if before > after:
                            log.info(f"移除过渡迁移记录: {src_path} → {dst}（已由旧记录接管）")
                        save_all(self.cfg)
                        self._emit_log("migrate",
                            f"  ✅ 一对一修复: 过渡软连接已清除（空目录保留）{os.path.basename(src_path)}，"
                            f"旧路径直指 {dst}")
                except Exception as e:
                    log.error(f"清理过渡符号链接失败 {src_path}: {e}")
                    self._emit_log("warn",
                        f"  ⚠️ 清理过渡符号链接失败: {os.path.basename(src_path)} - {e}（不影响数据）")
            elif chain_fixed > 0 and chain_fix_failed > 0:
                # 有链式修复失败，不能删过渡链接，保留多对一状态（比断链安全）
                log.warning(f"链式修复有 {chain_fix_failed} 个失败，保留过渡链接 {src_path} 避免断链")
                save_all(self.cfg)
            else:
                if chain_fixed > 0:
                    save_all(self.cfg)
                log.info(f"修复 {chain_fixed} 个链式符号链接 → {dst}")
        except Exception as e:
            log.error(f"扫描链式符号链接异常: {e}")

        log.info(f"迁移成功: {src} → {dst}")
        log_link_operation("创建链接(迁移)", str(src), str(dst), "迁移完成")
        self._emit_log("migrate",
            f"  ✅ 迁移完成: {src.name} → {dst}")
        self._maybe_clean_vss()
        return True, f"迁移成功: {src.name} -> {dst}"

    def migrate_symlink(self, src_path, dst_path, real_target=None, force_overwrite=False):
        """迁移符号链接到新目标（不产生链式链接，事务性，支持断电恢复）

        场景：src_path 已经是符号链接（指向 real_target），用户想改迁到 dst_path。
        通用方法，src_path/dst_path/real_target 均可为任意盘符路径。

        与 migrate() 的区别：
        - migrate() 要求源是真实目录，看到符号链接直接返回"已迁移"
        - migrate_symlink() 专门处理源是符号链接的情况，更新链接指向

        流程（事务性，每步写 pending 到 config.json）：
        1. 写 pending 事务（type=relocate, real_target=...）
        2. 复制引擎 real_target → dst_path（复制真实数据到新位置）
        3. 文件数验证
        4. 删除 src_path 的符号链接（只删链接，不删数据）
        5. mklink src_path → dst_path（创建新符号链接）
        6. 删除 real_target（删除旧的真实数据目录，失败则阻断等用户处理）
        7. 更新 migrated 记录，从 pending 移除

        断电恢复：启动时 recover_pending_migrations() 自动处理 type=relocate 的事务。

        结果：src_path 始终直接指向最终真实数据，不会产生链式符号链接。
        """
        from utils import is_symlink as _is_symlink, get_symlink_target as _get_target

        src = Path(src_path)
        dst = Path(dst_path)

        # 解析符号链接的真实目标
        if real_target is None:
            try:
                real_target = _get_target(src_path)
            except Exception:
                real_target = None
        if real_target:
            real_target = real_target.replace("\\\\?\\", "")
        if not real_target:
            return False, f"无法解析符号链接的目标: {src_path}"

        real = Path(real_target)

        # 如果 real_target 和 dst_path 相同，无需迁移
        if os.path.normpath(real_target).lower() == os.path.normpath(str(dst)).lower():
            return True, f"符号链接已指向目标: {src_path} → {dst_path}"

        # src/dst 包含关系校验（防止 复制引擎（镜像模式）复制到自身内部后清空源导致数据全毁）
        # 用 commonpath 而非 startswith，正确处理盘符根（如 D:\）边界情况
        _norm_real = os.path.normcase(os.path.normpath(str(real)))
        _norm_dst = os.path.normcase(os.path.normpath(str(dst)))
        if _norm_real != _norm_dst:
            try:
                _common = os.path.commonpath([_norm_real, _norm_dst])
                if _common == _norm_real or _common == _norm_dst:
                    log_error_with_reason("改迁路径包含关系",
                        f"real={real_target}, dst={dst_path}",
                        "路径包含校验失败，拒绝改迁")
                    return False, (f"真实数据路径和目标路径存在包含关系，可能导致数据全毁：\n"
                                  f"  真实数据: {real_target}\n"
                                  f"  目标: {dst_path}\n"
                                  f"请选择不同的目标路径。")
            except ValueError:
                pass  # 不同盘符，commonpath 抛 ValueError，不算包含

        # 检查真实数据是否存在
        if not real.exists():
            return False, f"符号链接的真实数据不存在: {real_target}"

        # 检查目标盘是否存在（防止 U 盘被拔、盘符错误等情况导致 mkdir 崩溃）
        if dst.is_absolute():
            dst_root = dst.anchor
            if not os.path.exists(dst_root):
                log_error_with_reason("目标盘不存在", context=f"改迁: {src_path} -> {dst_path}")
                return False, f"目标盘不存在: {dst_root}（请检查目标盘是否已连接）"

        # 确保目标父目录存在
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, FileNotFoundError) as e:
            log_error_with_reason("目标目录创建失败",
                f"无法创建目录: {e}", f"改迁: {src_path} -> {dst_path}")
            return False, f"目标目录创建失败: {dst.parent}\n错误: {e}"

        # ===== 步骤0：清理目标路径的残留符号链接 =====
        # 如果目标路径是符号链接（包括断链），复制引擎会跟着链接走
        # 导致数据写错位置或产生链式链接，必须先删除符号链接（只删链接不删数据）
        # 注意：os.path.exists 对断链符号链接返回 False，所以用 is_symlink 判断
        if is_symlink(dst_path):
            try:
                os.rmdir(dst_path)
                log.info(f"改迁: 删除目标路径残留符号链接: {dst_path}")
                self._emit_log("migrate", f"  🔗 已清理目标路径残留符号链接: {os.path.basename(dst_path)}")
            except Exception as e:
                # 删除失败必须中止，否则 复制引擎会跟着符号链接走，产生链式链接
                log_error_with_reason("目标路径符号链接清理失败",
                    f"无法删除符号链接: {e}", f"改迁: {src_path} -> {dst_path}")
                return False, (f"目标路径已存在符号链接但无法删除: {dst_path}\n"
                              f"错误: {e}\n"
                              f"请手动删除该符号链接后重试。")

        # ===== 步骤0.5：目标非空警告（防止误删用户文件）=====
        if not force_overwrite:
            is_nonempty, warning = self._check_dst_nonempty(dst_path, src_path)
            if is_nonempty:
                return False, f"NEED_CONFIRM_OVERWRITE\n{warning}"

        # ===== 步骤0.8：磁盘空间预检查 =====
        try:
            # 使用 lstat 而非 stat，避免跟随符号链接导致重复计算或循环
            # 排除符号链接文件，防止嵌套链接造成空间计算虚高
            real_size = self._real_size_bytes_fast(real)  # #25:MFT 优先,跨盘回退
            if dst.is_absolute():
                dst_drive = dst.anchor
                dst_usage = shutil.disk_usage(dst_drive)
                real_mb = real_size // 1024 // 1024
                free_mb = dst_usage.free // 1024 // 1024
                if dst_usage.free < real_size:
                    self._emit_log("error", f"  ✗ 目标盘空间不足: 需要 {real_mb}MB，剩余 {free_mb}MB")
                    return False, (f"目标盘空间不足: {dst_drive}\n"
                                  f"  需要: {real_mb}MB\n"
                                  f"  剩余: {free_mb}MB\n"
                                  f"请清理目标盘空间或更换目标盘。")
                self._emit_log("migrate", f"  ✓ 空间检查: 需要 {real_mb}MB，剩余 {free_mb}MB")
        except Exception as e:
            # 预检查失败必须提示用户，不能静默吞掉（否则可能改迁到一半空间不足卡住）
            log.warning(f"磁盘空间检查失败（可能影响改迁）: {e}")
            self._emit_log("warn", f"⚠️ 磁盘空间检查异常: {e}，可能导致改迁中途失败")

        # ===== 步骤1：写入 pending 事务（断电恢复依据）=====
        # type=relocate 标识改迁事务，real_target 记录旧真实数据路径供恢复用
        pending = self.cfg.setdefault("pending_migrations", [])
        pending[:] = [p for p in pending if p.get("src") != src_path]
        pending.append({
            "src": str(src),
            "dst": str(dst),
            "stage": "started",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "relocate",
            "real_target": real_target,
        })
        save_all(self.cfg)

        log.info(f"改迁符号链接: {src_path} (真实数据在 {real_target}) → {dst_path}")
        self._emit_log("migrate", f"📤 改迁: {src.name} | {real_target} → {dst_path}")

        # ===== 步骤2：复制引擎真实数据到新位置 =====
        self._emit_log("migrate", f"  ⏳ 正在复制数据（镜像模式）: {src.name}...")
        rc = self._run_copy_with_progress(real_target, str(dst), action_label="改迁")
        if rc >= 8 or rc == _CANCELLED_RC:
            short_log, long_msg = self._format_copy_fail(rc, src_path, "改迁")
            log_error_with_reason("改迁复制失败",
                f"返回码: {rc}", f"改迁: {real_target} -> {dst_path}")
            self._emit_log("error", f"{short_log}: {src.name}")
            for p in pending:
                if p.get("src") == src_path:
                    p["stage"] = "rustcopy_failed"
                    if rc == _CANCELLED_RC:
                        p["error"] = "用户取消，下次启动会自动续传"
                    else:
                        diag = getattr(self, "_last_copy_fail_reason", None) or {}
                        if diag.get("reason"):
                            p["error"] = f"返回码 {rc} - {diag['reason']}"
                        else:
                            p["error"] = f"返回码 {rc}"
            save_all(self.cfg)
            return False, long_msg

        # 更新 stage
        for p in pending:
            if p.get("src") == src_path:
                p["stage"] = "rustcopy_done"
        save_all(self.cfg)

        # ===== 步骤3：文件数验证 =====
        try:
            src_fc = self._count_files_fast(real)
            dst_fc = self._count_files_fast(dst)
        except Exception as e:
            # 验证异常时保守中止，不绕过验证继续删源（会导致双端丢失）
            log_error_with_reason("改迁完整性验证异常",
                f"异常: {e}", f"改迁: {real_target} -> {dst_path}")
            for p in pending:
                if p.get("src") == src_path:
                    p["stage"] = "integrity_failed"
                    p["error"] = f"验证异常: {e}"
            save_all(self.cfg)
            return False, (f"改迁完整性验证异常，已中止以保护数据: {e}\n"
                          f"已记录未完成事务，下次启动会自动续传。")
        self._emit_log("migrate", f"  🔍 文件数验证: 原 {src_fc} 个 / 新 {dst_fc} 个 ({src.name})")
        if src_fc > 0 and dst_fc < src_fc:
            log_error_with_reason("改迁完整性验证失败",
                f"原 {src_fc} / 新 {dst_fc}", f"改迁: {real_target} -> {dst_path}")
            self._emit_log("error", f"  ✗ 文件数不匹配: 原 {src_fc} / 新 {dst_fc}")
            for p in pending:
                if p.get("src") == src_path:
                    p["stage"] = "integrity_failed"
                    p["error"] = f"src={src_fc}, dst={dst_fc}"
            save_all(self.cfg)
            return False, (f"改迁完整性验证失败(C={src_fc}, D={dst_fc})。\n"
                          f"已记录未完成事务，下次启动程序会自动续传。")

        # ===== 步骤4：删除 src_path 的旧符号链接 =====
        # 只删除符号链接本身，不删除真实数据
        self._emit_log("migrate", f"  🔗 删除旧符号链接: {src.name}")
        try:
            try:
                os.rmdir(src_path)
            except OSError:
                os.unlink(src_path)
            log.info(f"改迁: 删除旧符号链接 {src_path}")
            log_link_operation("改迁删除旧链接", src_path, real_target,
                             f"旧链接指向 {real_target}，即将重建为新链接 → {dst_path}")
        except Exception as e:
            # 删除失败：链接还在，数据在 real_target 和 dst 都有
            # 更新 stage 等 recover 重试，不继续往下走（否则 mklink 会因路径已存在失败）
            err_msg = f"删除旧符号链接失败: {e}"
            log_error_with_reason("改迁删除旧链接失败",
                err_msg, f"改迁: {src_path}")
            self._emit_log("error", f"  ✗ {err_msg}")
            for p in pending:
                if p.get("src") == src_path:
                    p["stage"] = "rustcopy_done"  # recover 会从 rustcopy_done 重试删链接
                    p["error"] = err_msg[:300]
            save_all(self.cfg)
            return False, (f"删除旧符号链接失败：\n  路径: {src_path}\n  错误: {e}\n\n"
                          f"已记录未完成事务，下次启动程序会自动重试。\n"
                          f"或请手动删除该符号链接后重启程序。")

        # ===== 步骤5：创建新链接 src_path → dst_path（符号链接 /D 优先）=====
        self._emit_log("migrate", f"  🔗 正在创建新链接: {src.name} → {dst}")
        mklink_ok, mklink_err = self._create_dir_link(str(src), str(dst))
        if not mklink_ok:
            # 建链失败：旧链接已删，新链接没建起来，数据在 dst 和 real_target 都有
            # 手动恢复旧链接，让用户还能通过旧路径访问数据
            log_error_with_reason("改迁创建链接失败",
                mklink_err, f"改迁: {src_path} -> {dst_path}")
            self._emit_log("error", f"  ✗ 创建新链接失败: {src.name}（{mklink_err}）")
            try:
                # 恢复旧链接指向 real(与主建链一致:/D 优先,/J 兜底)
                self._create_dir_link(str(src), str(real))
                self._emit_log("warn", f"  ⚠️ 已恢复旧链接（指向旧数据 {real}）")
            except Exception as e:
                log.debug("忽略异常: %s", e)
            for p in pending:
                if p.get("src") == src_path:
                    p["stage"] = "mklink_failed"
                    p["error"] = mklink_err
            save_all(self.cfg)
            return False, (f"创建新链接失败（{mklink_err}）。\n"
                          f"⚠️ 已恢复旧链接，数据仍可通过原路径访问。\n"
                          f"已记录未完成事务，下次启动程序会自动重试。")

        # 更新 stage：链接已更新，待清理旧数据
        for p in pending:
            if p.get("src") == src_path:
                p["stage"] = "link_updated"
        save_all(self.cfg)
        log_link_operation("改迁创建新链接", src_path, dst_path,
                         f"旧数据原在 {real_target}，已改迁到 {dst_path}")

        # ===== 步骤6：清空旧的真实数据 real_target（保留目录本身）=====
        # 数据已在 dst_path 且 C 盘链接已指向 dst，可以安全清空旧数据释放空间
        # ⚠️ 关键：只清空 real_target 里面的内容，保留 real_target 文件夹本身！
        #   与 restore 步骤5 保持一致，保留空文件夹方便下次迁移复用，避免破坏父目录结构。
        #   如果 real_target 本身是符号链接（异常状态），_cleanup_dir_contents 会先删链接再建真实空目录。
        # 删除失败必须阻断：否则旧数据残留占用空间且用户不知情
        self._emit_log("migrate", f"  🗑 正在清空旧数据目录（保留目录本身）: {real.name}...")
        cleanup_ok, cleanup_err = self._cleanup_dir_contents(str(real))

        if not cleanup_ok:
            # 清空失败：阻断事务，保留 pending 等下次启动重试
            log_error_with_reason("改迁清空旧数据失败",
                cleanup_err, f"改迁: {real_target} (src={src_path}, dst={dst_path})")
            self._emit_log("error",
                f"  ✗ 旧数据目录清空失败: {real.name} - {cleanup_err[:80]}")
            for p in pending:
                if p.get("src") == src_path:
                    p["stage"] = "cleanup_failed"
                    p["error"] = cleanup_err[:300]
            save_all(self.cfg)
            return False, (f"改迁数据复制和链接更新已完成，但旧数据目录清空失败：\n"
                          f"  旧数据路径: {real_target}\n"
                          f"  错误: {cleanup_err}\n\n"
                          f"⚠️ C 盘链接已指向新位置 {dst_path}，可正常使用。\n"
                          f"   但旧数据目录仍占用磁盘空间，请关闭占用程序后重启程序自动重试。")

        log.info(f"改迁: 已清空旧数据目录（保留目录本身）{real_target}")

        # ===== 步骤7：清理多对一残留 + 更新 migrated 记录，从 pending 移除 =====
        # 1. 扫描 migrated 表，找出所有 dst 指向旧 real_target 的其他记录
        #    （改迁后旧 real_target 内容已被清空，这些记录已无意义）
        # 2. 如果这些记录的 src 在非 C 盘且是符号链接，删除链接并重建空目录
        # 3. 如果 src 在 C 盘，更新其 dst 指向新目标（保持一对一）
        # 4. 最后用 _add_migrated_record 写入当前 src→dst（按 src 去重替换）
        _norm_rt = os.path.normpath(real_target).lower()
        _norm_dst = os.path.normpath(str(dst)).lower()
        _norm_src = os.path.normpath(src_path).lower()
        cleaned_stale = []
        for m in self.cfg.get("migrated", []):
            m_src = m.get("src", "")
            m_dst = m.get("dst", "")
            if not m_src or not m_dst:
                continue
            # 跳过当前 src 自身（由 _add_migrated_record 处理）
            if os.path.normpath(m_src).lower() == _norm_src:
                continue
            # 找到 dst 指向旧 real_target 的其他记录
            if os.path.normpath(m_dst).lower() == _norm_rt:
                # 如果该记录的 src 也指向新 dst，跳过（不会发生，但防御性判断）
                if os.path.normpath(m_dst).lower() == _norm_dst:
                    continue
                m_src_path = m_src.replace("\\\\?\\", "")
                _is_c = m_src_path.lower().startswith("c:")
                if _is_c:
                    # C 盘 src：更新 dst 指向新目标，保持一对一
                    m["dst"] = str(dst)
                    m["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    log.info(f"改迁多对一清理: 更新 C 盘记录 {m_src} → {dst}")
                else:
                    # 非 C 盘 src：删除符号链接（如果有），重建空目录，移除记录
                    if is_symlink(m_src_path):
                        try:
                            if os.path.isdir(m_src_path):
                                os.rmdir(m_src_path)
                            else:
                                os.unlink(m_src_path)
                            os.makedirs(m_src_path, exist_ok=True)
                            log.info(f"改迁多对一清理: 删除非 C 盘过渡链接并保留空目录 {m_src_path}")
                        except Exception as e:
                            log.warning(f"改迁多对一清理: 删除链接失败 {m_src_path}: {e}")
                    cleaned_stale.append(m_src)
        # 移除被清理的非 C 盘记录
        if cleaned_stale:
            _cleaned_set = set(os.path.normpath(s).lower() for s in cleaned_stale)
            self.cfg["migrated"] = [
                m for m in self.cfg.get("migrated", [])
                if os.path.normpath(m.get("src", "")).lower() not in _cleaned_set
            ]
            self._emit_log("migrate",
                f"  ✅ 清理 {len(cleaned_stale)} 条多对一残留记录（旧数据目录已清空）")
        # _add_migrated_record 自动去重同 src 旧记录
        self._add_migrated_record(str(src), str(dst))
        pending[:] = [p for p in pending if p.get("src") != src_path]
        save_all(self.cfg)
        log.info(f"改迁成功: src={src}, dst={dst}")

        self._emit_log("migrate", f"  ✅ 改迁完成: {src.name} → {dst}（旧数据 {real.name} 已清理）")
        self._maybe_clean_vss()
        return True, f"改迁成功: {src.name} → {dst_path}"

    def fix_chain_symlinks(self):
        r"""启动时扫描并修复历史链式符号链接 + 多对一冲突

        三步修复，保证一对一连接：

        第一步：修复链式（套娃）链接
            场景：C:\Sdk(链接) → D:\旧(链接) → H:\新(真实)
            修复：把外层链接重指向最终真实数据目录

        第二步：清理多对一冲突（多个 src 直指同一真实数据）
            场景：C:\Sdk→H、D:\旧→H、D:\dev→H 三条记录都直指 H
            原因：旧版本未做步骤6.5清理，换路径后过渡符号链接和记录未移除
            修复策略：
            - 保留 C 盘 src（软件入口，不能删）
            - 清理非 C 盘 src 的符号链接（只删链接，真实数据在 dst 不受影响）
            - 从 migrated 表移除被清理的记录
            - 如果都是非 C 盘 src，保留路径最短的（通常是父级目录）
            - 如果都是 C 盘 src，保留所有（C 盘不能删，接受多对一）

        第三步：清理无引用的中间层符号链接
            中间节点 = dst 本身是符号链接且不被任何记录引用为 src
            只删链接不删数据

        安全原则：
        - 只删除符号链接（is_symlink=true），绝不删除真实数据目录
        - 只删除非 C 盘的过渡符号链接，C 盘是软件入口必须保留
        - 真实数据目录（dst 里的数据）永远不动

        :return: (fixed_count, scanned_count, details)
        """
        from utils import is_symlink as _is_symlink, get_symlink_target as _get_target

        migrated = self.cfg.get("migrated", [])
        scanned = len(migrated)
        if scanned == 0:
            return 0, 0, []

        def _norm(p):
            try:
                return os.path.normpath(p.replace("\\\\?\\", "")).lower()
            except Exception:
                return ""

        def _is_c_drive(path):
            return path.lower().replace("\\\\?\\", "").startswith("c:")

        fixed_count = 0
        details = []
        changed = False

        # ===== 第一步：修复链式链接（src 真实目标 ≠ dst）=====
        for m in migrated:
            src_path = m.get("src", "")
            old_dst = m.get("dst", "")
            if not src_path or not _is_symlink(src_path):
                continue

            # 逐层解析符号链接指向，最多 8 层防死循环
            current = src_path
            real_target = None
            for _ in range(8):
                try:
                    target = _get_target(current)
                    if not target:
                        break
                    target = target.replace("\\\\?\\", "")
                    if not _is_symlink(target):
                        real_target = target
                        break
                    current = target
                except Exception:
                    break

            if not real_target:
                continue

            if _norm(real_target) == _norm(old_dst):
                continue  # 直指，无链式

            # 链式：重建 src → 真实目标，更新 dst
            try:
                if os.path.isdir(src_path):
                    os.rmdir(src_path)
                else:
                    os.unlink(src_path)
                ok, _ = self._create_dir_link(src_path, real_target)
                if not ok:
                    raise Exception("创建链接失败")
                m["dst"] = real_target
                m["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                fixed_count += 1
                details.append((src_path, old_dst, real_target))
                changed = True
                log_link_operation("启动修复链式链接", src_path, real_target,
                    f"原指向 {old_dst}（链式），现直指真实数据")
                log.info(f"修复链式链接: {src_path} | {old_dst} → {real_target}")
            except Exception as e:
                log.error(f"启动修复链式链接失败 {src_path}: {e}")

        if changed:
            save_all(self.cfg)

        # ===== 第二步：清理多对一冲突（多个 src 直指同一真实数据）=====
        # 解析每条记录 src 的真实目标，按真实目标分组
        migrated = self.cfg.get("migrated", [])
        real_target_to_records = {}  # {rt_norm: [(m, src_path, real_target), ...]}
        for m in migrated:
            src_path = m.get("src", "")
            if not src_path or not _is_symlink(src_path):
                continue
            try:
                target = _get_target(src_path)
                if target:
                    target = target.replace("\\\\?\\", "")
                    # 如果目标是符号链接，继续解析到最终真实数据
                    current = target
                    for _ in range(8):
                        if not _is_symlink(current):
                            break
                        nt = _get_target(current)
                        if not nt:
                            break
                        current = nt.replace("\\\\?\\", "")
                    rt_norm = _norm(current)
                    real_target_to_records.setdefault(rt_norm, []).append(
                        (m, src_path, current))
            except Exception:
                continue

        conflict_cleaned = 0
        conflict_messages = []
        remove_srcs_norm = set()

        for rt_norm, group in real_target_to_records.items():
            if len(group) <= 1:
                continue  # 一对一，无冲突

            # 多对一冲突：按优先级排序，保留最优记录
            # 优先级：C 盘 src > 非 C 盘 src；同盘按路径长度短者优先
            def _priority(item):
                src_path = item[1]
                is_c = _is_c_drive(src_path)
                return (0 if is_c else 1, len(src_path))

            group_sorted = sorted(group, key=_priority)
            keep_info = group_sorted[0]
            remove_infos = group_sorted[1:]
            real_target = keep_info[2]

            conflict_messages.append(
                f"真实目标 {real_target} 被 {len(group)} 条记录引用: "
                f"{[info[1] for info in group]}")

            # 清理冲突记录：只清理非 C 盘的符号链接
            for rm_m, rm_src, rm_rt in remove_infos:
                if _is_c_drive(rm_src):
                    # C 盘 src 保留（软件入口），记录也保留
                    log.info(f"多对一冲突: C 盘 src 保留 {rm_src}（指向 {rm_rt}）")
                    continue
                # 非 C 盘 src：删除符号链接后重建空目录（真实数据在 real_target 不受影响）
                # 用户要求：只清除软连接，不删文件夹目录，删链接后必须 makedirs 重建
                try:
                    if _is_symlink(rm_src):
                        if os.path.isdir(rm_src):
                            os.rmdir(rm_src)
                        else:
                            os.unlink(rm_src)
                        # 重建空真实目录，保留文件夹本身
                        os.makedirs(rm_src, exist_ok=True)
                        log.info(f"多对一冲突: 清理非 C 盘过渡链接并保留空目录 {rm_src}（数据保留在 {rm_rt}）")
                        remove_srcs_norm.add(_norm(rm_src))
                except Exception as e:
                    log.error(f"多对一冲突清理链接失败 {rm_src}: {e}")

        # 从 migrated 表移除被清理的记录
        if remove_srcs_norm:
            before = len(self.cfg.get("migrated", []))
            self.cfg["migrated"] = [
                m for m in self.cfg.get("migrated", [])
                if _norm(m.get("src", "")) not in remove_srcs_norm
            ]
            after = len(self.cfg["migrated"])
            conflict_cleaned = before - after
            if conflict_cleaned > 0:
                changed = True
                log.warning(
                    f"多对一冲突处理: 清理 {conflict_cleaned} 条冲突记录。"
                    f"详情: {' | '.join(conflict_messages[:5])}")

        if changed:
            save_all(self.cfg)

        # ===== 第三步：清理无引用的中间层符号链接 =====
        migrated = self.cfg.get("migrated", [])
        all_srcs_norm = {_norm(mm.get("src", "")) for mm in migrated}
        cleaned_middle = 0
        middle_dst_to_remove = []
        for m in migrated:
            d = m.get("dst", "")
            d_norm = _norm(d)
            if d and _is_symlink(d) and d_norm not in all_srcs_norm:
                try:
                    real_d = _get_target(d)
                    if real_d:
                        real_d = real_d.replace("\\\\?\\", "")
                        if os.path.exists(real_d):
                            if os.path.isdir(d):
                                os.rmdir(d)
                            else:
                                os.unlink(d)
                            # 重建空真实目录，保留文件夹本身
                            # 用户要求：只清除软连接，不删文件夹目录
                            os.makedirs(d, exist_ok=True)
                            cleaned_middle += 1
                            middle_dst_to_remove.append(d_norm)
                            log.info(f"清理链式中间节点符号链接并保留空目录: {d}（真实数据在 {real_d}）")
                except Exception as e:
                    log.debug(f"跳过清理中间节点 {d}: {e}")

        if middle_dst_to_remove:
            before = len(self.cfg.get("migrated", []))
            self.cfg["migrated"] = [
                m for m in self.cfg.get("migrated", [])
                if _norm(m.get("dst", "")) not in middle_dst_to_remove
                or _norm(m.get("src", "")) in all_srcs_norm
            ]
            after = len(self.cfg["migrated"])
            if before != after:
                changed = True
                log.info(f"清理 migrated 表无效中间节点记录: {before - after} 条")

        if cleaned_middle > 0:
            log.info(f"清理 {cleaned_middle} 个链式中间节点符号链接")

        if changed:
            save_all(self.cfg)

        return fixed_count, scanned, details

    def cleanup_symlink_residues(self, base_paths=None):
        """扫描指定路径下的符号链接残留，还原成真实空目录

        用于一键迁移/一键还原完成后，清理目标盘上残留的符号链接。
        只处理"孤立的"符号链接（target 不存在或为空），避免误删仍在使用的链接。

        判断逻辑（关键）：
        - 正常迁移后 D 盘是真实数据目录，不是符号链接
        - 如果 D 盘有符号链接，说明是残留（测试遗留、历史链式中间节点）
        - 但链接的 target 可能仍有数据（链式链接的最终节点），直接删会断开数据链路
        - 所以只清理 target 不存在或 target 为空目录的"真正孤立"链接
        - target 有数据的链接保留，等 fix_chain_symlinks 修复后再清理

        :param base_paths: 要扫描的路径列表，None 时默认扫描 g_root
        :return: (cleaned_count, scanned_count, details)
        """
        cleaned = 0
        scanned = 0
        skipped_has_data = 0
        details = []

        if base_paths is None:
            g_root = self.cfg.get("g_root", "")
            if not g_root or not os.path.exists(g_root):
                return 0, 0, []
            base_paths = [g_root]

        # 收集所有 migrated 记录的 dst（真实数据路径，D 盘）
        # 用于判断符号链接是否是某条迁移记录的正常 dst（异常状态下 dst 可能是符号链接）
        migrated_dsts = set()
        for m in self.cfg.get("migrated", []):
            d = m.get("dst", "").replace("\\\\?\\", "").lower().rstrip("\\")
            if d:
                migrated_dsts.add(d)

        for base in base_paths:
            if not os.path.exists(base):
                continue
            try:
                # 遍历 base 下的所有条目
                for entry in os.listdir(base):
                    entry_path = os.path.join(base, entry)
                    scanned += 1
                    # 先检查是否符号链接（os.path.isdir 对断链符号链接返回 False，会漏掉）
                    # os.path.islink 只判断自身是否链接，不跟随目标
                    if not os.path.islink(entry_path) and not is_symlink(entry_path):
                        continue

                    # 获取符号链接的 target
                    link_target = get_symlink_target(entry_path)
                    link_target_clean = link_target.replace("\\\\?\\", "") if link_target else ""

                    # 判断是否是"真正孤立"的符号链接（可以安全清理）：
                    # 1. target 不存在（断链）→ 可以清理
                    # 2. target 存在但是空目录 → 可以清理
                    # 3. target 存在且有数据 → 不能清理！可能是链式链接的数据链路
                    can_cleanup = False
                    if not link_target_clean or not os.path.exists(link_target_clean):
                        # 断链符号链接，可以清理
                        can_cleanup = True
                    else:
                        # target 存在，检查是否有数据
                        try:
                            target_entries = os.listdir(link_target_clean)
                            if len(target_entries) == 0:
                                # target 是空目录，可以清理
                                can_cleanup = True
                            else:
                                # target 有数据，不能直接清理！
                                # 可能是链式链接的数据链路，删了会断开访问
                                # 也可能是迁移残留（target 是真实数据，链接是多余的）
                                # 保守策略：保留链接，记录日志让用户手动处理
                                can_cleanup = False
                        except Exception:
                            # 无法读取 target，保守起见不清理
                            can_cleanup = False

                    if not can_cleanup:
                        skipped_has_data += 1
                        log.warning(f"保留符号链接（target 有数据，可能是数据链路）: "
                                    f"{entry_path} -> {link_target_clean}")
                        details.append(f"[保留] {entry_path} -> {link_target_clean} (target 有数据)")
                        continue

                    # 孤立的符号链接残留，删除链接并重建为真实空目录
                    try:
                        # 删除符号链接本身（不删除目标数据）
                        # 先试 os.rmdir（目录符号链接），失败试 os.unlink（文件符号链接/断链）
                        try:
                            os.rmdir(entry_path)
                        except OSError:
                            os.unlink(entry_path)
                        # 重建为真实空目录（保留路径，文件夹属性恢复正常）
                        try:
                            os.makedirs(entry_path, exist_ok=True)
                        except Exception as mk_err:
                            # 重建目录失败：链接已删除，必须恢复链接避免数据丢失
                            log.error(f"重建目录失败，尝试恢复符号链接: {entry_path} - {mk_err}")
                            try:
                                if link_target_clean:
                                    self._create_dir_link(entry_path, link_target_clean)
                                    log.warning(f"已恢复链接（重建目录失败）: {entry_path} -> {link_target_clean}")
                                else:
                                    log.error(f"无法恢复链接（无目标）: {entry_path}")
                            except Exception:
                                log.error(f"恢复链接也失败，数据可能丢失: {entry_path}")
                            continue  # 不计入 cleaned
                        cleaned += 1
                        if link_target_clean:
                            detail = f"{entry_path} (原指向 {link_target_clean})"
                        else:
                            detail = f"{entry_path} (断链)"
                        details.append(detail)
                        log.info(f"清理符号链接残留: {entry_path} -> {link_target_clean}，已重建为真实空目录")
                    except Exception as e:
                        log.warning(f"清理符号链接残留失败 {entry_path}: {e}")
            except Exception as e:
                log.warning(f"扫描符号链接残留失败 {base}: {e}")

        if cleaned > 0:
            self._emit_log("migrate",
                f"🔗 清理 {cleaned} 个符号链接残留（已还原为真实空目录）")
            if skipped_has_data > 0:
                self._emit_log("migrate",
                    f"⚠️ 保留 {skipped_has_data} 个 target 有数据的符号链接（可能是数据链路，未自动清理）")
            log.info(f"清理符号链接残留完成: {cleaned}/{scanned} 个，保留 {skipped_has_data} 个（target 有数据）")
        return cleaned, scanned, details

    def recover_pending_migrations(self):
        """启动时扫描未完成的迁移事务，自动恢复或回滚

        事务阶段处理：
        - started/rustcopy_failed/integrity_failed: 清理 D 盘不完整数据
        - rustcopy_done/delete_failed: 重试删除 C 盘 + 创建链接
        - src_deleted/mklink_failed: 直接创建链接（C 盘已删除，D 盘有数据）
        - 其他: 记录警告，不自动处理

        失败次数控制：
        - 每个事务记录 fail_count 字段，每次恢复失败 +1
        - 累计失败 >= 2 次不再自动尝试，保留事务等用户决策
        - 成功后事务记录被移除，fail_count 自然清零
        - 把选择权交给用户，根据失败原因给出建议

        所有工具通用，不针对特定工具。
        :return: list of (src, action, result_msg)
        """
        results = []
        pending = self.cfg.get("pending_migrations", [])
        if not pending:
            return results

        log.info(f"发现 {len(pending)} 个未完成迁移事务，开始恢复...")
        # 复制一份遍历，原列表会被修改
        for p in list(pending):
            # 程序退出时取消恢复循环，避免杀一个复制引擎又启动新的
            if self._recover_cancel_requested:
                log.info("恢复循环被取消（程序退出），剩余事务留到下次启动")
                break
            src = p.get("src", "")
            dst = p.get("dst", "")
            stage = p.get("stage", "")
            # P7:旧版 stage 名兼容——历史 state.json 存量数据可能是 robocopy_done/
            # robocopy_failed(迁移/改迁恢复路径是严格白名单,不归一化会落
            # unknown_stage 永久搁置);写回 p 后下次 save_all 自动迁移为存量新名
            if stage in ("robocopy_done", "robocopy_failed"):
                p["stage"] = "rustcopy_done" if stage == "robocopy_done" else "rustcopy_failed"
                stage = p["stage"]
            if not src or not dst:
                continue

            # 失败次数检查：>= 2 次不再自动恢复，把决策权交给用户
            fail_count = p.get("fail_count", 0)
            if fail_count >= 2:
                last_error = p.get("last_error", "未知原因")
                log.warning(f"跳过自动恢复（已失败 {fail_count} 次）: {src} -> {dst}, "
                            f"stage={stage}, 上次错误: {last_error}")
                results.append((src, "user_decision_required",
                    f"已失败 {fail_count} 次，停止自动恢复。原因: {last_error}。"
                    f"请关闭占用程序后手动迁移，或联系支持。"))
                continue

            log.info(f"恢复事务: {src} -> {dst}, stage={stage}, fail_count={fail_count}")
            try:
                # 改迁事务（type=relocate）走专用恢复逻辑
                if p.get("type") == "relocate":
                    action, msg = self._recover_relocate_pending(p, pending)
                    results.append((src, action, msg))
                    continue

                if stage in ("started", "rustcopy_failed", "integrity_failed"):
                    # 续传模式：复制引擎（镜像模式）是幂等的，重新运行会自动补齐缺失文件
                    # 不清理 D 盘已有数据，避免浪费已复制的进度
                    if is_symlink(src):
                        # src 是符号链接，但这不代表迁移已完成！
                        # 必须验证 dst 数据是否完整，防止误判（详见 migrate_symlink 场景）
                        try:
                            dst_fc = self._count_files_fast(dst)
                        except Exception:
                            dst_fc = 0
                        if dst_fc > 0:
                            # dst 有数据，事务确实已完成（可能是上次成功但未清理 pending）
                            self._add_migrated_record(src, dst)
                            pending[:] = [x for x in pending if x.get("src") != src]
                            results.append((src, "completed", f"已是符号链接且dst有{dst_fc}文件，已补录迁移记录"))
                        else:
                            # dst 无数据，迁移实际未完成，需要重新用复制引擎
                            # src 是符号链接 → 解析真实目标，把真实数据 复制引擎到 dst
                            try:
                                real_target = get_symlink_target(src)
                                if real_target:
                                    real_target = real_target.replace("\\\\?\\", "")
                            except Exception:
                                real_target = None
                            if not real_target or not os.path.exists(real_target):
                                err_msg = f"src是符号链接但无法解析真实目标，dst无数据"
                                self._incr_pending_fail(p, err_msg)
                                results.append((src, "symlink_no_target", err_msg))
                                continue
                            log.info(f"src是符号链接，dst无数据，从真实目标续传: {real_target} -> {dst}")
                            self._emit_log("migrate", f"  ⏳ 从真实目标续传: {os.path.basename(src)}...")
                            rc = self._run_copy_with_progress(real_target, dst, action_label="续传")
                            if rc >= 8 or rc == _CANCELLED_RC:
                                short_log, long_msg = self._format_copy_fail(rc, src, "续传")
                                self._emit_log("error", f"从真实目标续传 {short_log}")
                                # 取消不增加 fail_count，避免 2 次取消后停止自动恢复
                                if rc == _CANCELLED_RC:
                                    self._record_pending_cancel(p, long_msg)
                                else:
                                    self._incr_pending_fail(p, long_msg)
                                results.append((src, "rustcopy_retry_failed", long_msg))
                            else:
                                # 续传成功，验证完整性
                                try:
                                    rt_fc = self._count_files_fast(real_target)
                                    dst_fc2 = self._count_files_fast(dst)
                                except Exception:
                                    rt_fc, dst_fc2 = 1, 1
                                if rt_fc > 0 and dst_fc2 < rt_fc:
                                    err_msg = f"完整性验证失败(真实{rt_fc}/dst{dst_fc2})"
                                    self._incr_pending_fail(p, err_msg)
                                    results.append((src, "integrity_still_failed", err_msg))
                                else:
                                    # 完整性 OK，更新链接指向 dst（符号链接 /D 优先）
                                    try:
                                        os.rmdir(src)
                                    except OSError:
                                        os.unlink(src)
                                    ok, lerr = self._create_dir_link(src, dst)
                                    if not ok:
                                        raise Exception(lerr)
                                    # 清空旧的真实数据目录 real_target（保留目录本身）
                                    # 与 migrate_symlink 步骤6 保持一致，避免磁盘空间冗余
                                    try:
                                        rt_ok, rt_err = self._cleanup_dir_contents(real_target)
                                        if rt_ok:
                                            log.info(f"续传完成: 已清空旧真实数据目录（保留目录本身）{real_target}")
                                        else:
                                            log.warning(f"续传后清空旧数据目录失败（不影响使用）: {real_target} - {rt_err}")
                                    except Exception as e:
                                        log.warning(f"续传后清空旧数据目录异常（不影响使用）: {real_target} - {e}")
                                    self._add_migrated_record(src, dst)
                                    pending[:] = [x for x in pending if x.get("src") != src]
                                    results.append((src, "completed",
                                        f"从真实目标续传完成 ({dst_fc2} 文件)，已更新符号链接"))
                    elif not os.path.exists(src):
                        # C 盘不存在，直接创建链接（符号链接 /D 优先）
                        ok, lerr = self._create_dir_link(src, dst)
                        if ok:
                            self._add_migrated_record(src, dst)
                            pending[:] = [x for x in pending if x.get("src") != src]
                            results.append((src, "completed", "C盘已删，补建链接成功"))
                        else:
                            results.append((src, "mklink_failed",
                                f"创建链接失败: {lerr}"))
                    else:
                        # C 盘是真实目录，继续 复制引擎续传（/MIR 自动补齐）
                        log.info(f"续传复制: {src} -> {dst}")
                        self._emit_log("migrate", f"  ⏳ 续传复制数据: {os.path.basename(src)}...")
                        rc = self._run_copy_with_progress(src, dst, action_label="续传")
                        if rc >= 8 or rc == _CANCELLED_RC:
                            # 仍失败：保留目标盘已有数据，等下次再试
                            # 不清理目标盘，避免浪费已复制的进度
                            short_log, long_msg = self._format_copy_fail(rc, src, "续传")
                            self._emit_log("error", short_log)
                            # 取消不增加 fail_count，避免 2 次取消后停止自动恢复
                            if rc == _CANCELLED_RC:
                                self._record_pending_cancel(p, long_msg)
                            else:
                                self._incr_pending_fail(p, long_msg)
                            results.append((src, "rustcopy_retry_failed", long_msg))
                        else:
                            # 复制引擎成功，验证完整性
                            try:
                                src_fc = self._count_files_fast(src)
                                dst_fc = self._count_files_fast(dst)
                            except Exception:
                                src_fc, dst_fc = 1, 1  # 验证异常时不阻塞
                            if src_fc > 0 and dst_fc < src_fc:
                                err_msg = f"完整性验证仍失败(C={src_fc}, D={dst_fc})"
                                self._incr_pending_fail(p, err_msg)
                                results.append((src, "integrity_still_failed",
                                    err_msg + "，请关闭占用程序后重启程序"))
                            else:
                                # 完整性 OK，删除 C 盘（rd /s /q 直接删除，不进回收站）
                                try:
                                    self._safe_rd(src)
                                    if os.path.exists(src):
                                        shutil.rmtree(src, ignore_errors=True)
                                except Exception as e:
                                    log.debug("忽略异常: %s", e)
                                if not os.path.exists(src):
                                    # 删除成功，创建链接（符号链接 /D 优先）
                                    ok, lerr = self._create_dir_link(src, dst)
                                    if ok:
                                        self._add_migrated_record(src, dst)
                                        pending[:] = [x for x in pending if x.get("src") != src]
                                        results.append((src, "completed",
                                            f"续传完成: 复制+删除+链接 ({dst_fc} 文件)"))
                                    else:
                                        self._incr_pending_fail(p, f"创建链接失败: {lerr}")
                                        results.append((src, "mklink_failed",
                                            f"C盘已删除，但创建链接失败: {lerr}"))
                                else:
                                    err_msg = "C盘目录仍被占用"
                                    self._incr_pending_fail(p, err_msg)
                                    results.append((src, "delete_failed",
                                        err_msg + "，请关闭相关程序后重启程序"))

                elif stage in ("rustcopy_done", "delete_failed"):
                    # D 盘数据完整，C 盘可能还在，重试删除
                    if is_symlink(src):
                        # src 是符号链接，但需验证 dst 数据是否真的完整
                        try:
                            dst_fc = self._count_files_fast(dst)
                        except Exception:
                            dst_fc = 0
                        if dst_fc > 0:
                            # dst 有数据，事务确实已完成
                            self._add_migrated_record(src, dst)
                            pending[:] = [x for x in pending if x.get("src") != src]
                            results.append((src, "completed", f"已是符号链接且dst有{dst_fc}文件，已补录迁移记录"))
                        else:
                            # dst 无数据，这是异常状态（stage=rustcopy_done 但 dst 空）
                            # 可能是 migrate_symlink 中途被杀，需要从 src 的真实目标续传
                            try:
                                real_target = get_symlink_target(src)
                                if real_target:
                                    real_target = real_target.replace("\\\\?\\", "")
                            except Exception:
                                real_target = None
                            if real_target and os.path.exists(real_target):
                                log.info(f"stage={stage}但dst空，从真实目标续传: {real_target} -> {dst}")
                                rc = self._run_copy_with_progress(real_target, dst, action_label="续传")
                                if 0 <= rc < 8:
                                    try:
                                        os.rmdir(src)
                                    except OSError:
                                        os.unlink(src)
                                    ok, lerr = self._create_dir_link(src, dst)
                                    if not ok:
                                        raise Exception(lerr)
                                    self._add_migrated_record(src, dst)
                                    pending[:] = [x for x in pending if x.get("src") != src]
                                    results.append((src, "completed", "从真实目标续传完成，已更新链接"))
                                else:
                                    short_log, long_msg = self._format_copy_fail(rc, src, "续传")
                                    self._emit_log("error", short_log)
                                    # 取消不增加 fail_count，避免 2 次取消后停止自动恢复
                                    if rc == _CANCELLED_RC:
                                        self._record_pending_cancel(p, long_msg)
                                    else:
                                        self._incr_pending_fail(p, long_msg)
                                    results.append((src, "rustcopy_retry_failed", long_msg))
                            else:
                                err_msg = "src是符号链接但真实目标不存在，dst空，无法恢复"
                                self._incr_pending_fail(p, err_msg)
                                results.append((src, "symlink_no_target", err_msg))
                    elif os.path.exists(src):
                        # C 盘还在（真实目录），重试删除
                        # ⚠️ 修复 N12：删源前必须验证 dst 数据完整性
                        # 否则重启期间 D 盘数据丢失/盘符变更时删除 C 盘会导致双端丢失
                        try:
                            dst_fc = self._count_files_fast(dst)
                        except Exception:
                            dst_fc = 0
                        if dst_fc == 0:
                            # dst 无数据，删 C 盘 = 数据全毁，必须拒绝
                            err_msg = (f"目标盘无数据（{dst}），拒绝删除C盘目录以防止数据丢失。"
                                       f"请检查目标盘是否已连接/数据是否完整。")
                            self._incr_pending_fail(p, err_msg)
                            results.append((src, "dst_empty_refused", err_msg))
                            continue
                        try:
                            self._safe_rd(src)
                            if os.path.exists(src):
                                shutil.rmtree(src, ignore_errors=True)
                        except Exception as e:
                            log.debug("忽略异常: %s", e)
                        if not os.path.exists(src):
                            # 删除成功，创建链接（符号链接 /D 优先）
                            ok, lerr = self._create_dir_link(src, dst)
                            if ok:
                                self._add_migrated_record(src, dst)
                                pending[:] = [x for x in pending if x.get("src") != src]
                                results.append((src, "completed", "删除C盘+创建链接成功"))
                            else:
                                self._incr_pending_fail(p, f"创建链接失败: {lerr}")
                                results.append((src, "mklink_failed",
                                    f"C盘已删除，但创建链接失败: {lerr}"))
                        else:
                            err_msg = "C盘目录仍被占用"
                            self._incr_pending_fail(p, err_msg)
                            results.append((src, "delete_failed",
                                err_msg + "，请关闭相关程序后重启程序"))
                    else:
                        # C 盘不存在，直接创建链接（符号链接 /D 优先）
                        ok, lerr = self._create_dir_link(src, dst)
                        if ok:
                            self._add_migrated_record(src, dst)
                            pending[:] = [x for x in pending if x.get("src") != src]
                            results.append((src, "completed", "C盘已删，补建链接成功"))
                        else:
                            self._incr_pending_fail(p, f"创建链接失败: {lerr}")
                            results.append((src, "mklink_failed",
                                f"创建链接失败: {lerr}"))

                elif stage in ("src_deleted", "mklink_failed"):
                    # C 盘已删除，只需创建链接
                    if is_symlink(src):
                        # 已是符号链接，补录记录
                        self._add_migrated_record(src, dst)
                        pending[:] = [x for x in pending if x.get("src") != src]
                        results.append((src, "completed", "已是符号链接，已补录迁移记录"))
                    elif os.path.exists(dst):
                        ok, lerr = self._create_dir_link(src, dst)
                        if ok:
                            self._add_migrated_record(src, dst)
                            pending[:] = [x for x in pending if x.get("src") != src]
                            results.append((src, "completed", "补建链接成功"))
                        else:
                            self._incr_pending_fail(p, f"创建链接失败: {lerr}")
                            results.append((src, "mklink_failed",
                                f"创建链接失败: {lerr}"))
                    else:
                        # D 盘数据也不存在，记录异常
                        results.append((src, "error",
                            "C盘已删除且D盘数据不存在，数据可能丢失！"))
                        pending[:] = [x for x in pending if x.get("src") != src]
                else:
                    results.append((src, "unknown_stage", f"未知事务阶段: {stage}"))
            except Exception as e:
                self._incr_pending_fail(p, f"恢复异常: {e}")
                results.append((src, "error", f"恢复异常: {e}"))

        save_all(self.cfg)
        return results

    def _recover_relocate_pending(self, p, pending):
        """恢复改迁事务（type=relocate）

        改迁事务阶段：
        - started/rustcopy_failed/integrity_failed: 从 real_target 续传 复制引擎到 dst
        - rustcopy_done: 复制引擎+验证已通过，执行删旧链接+建新链接
        - mklink_failed: 旧链接已删新链接没建，重试 mklink
        - link_updated/cleanup_failed: 链接已指向 dst，只需删 real_target

        :param p: pending 事务记录（含 real_target 字段）
        :param pending: pending_migrations 列表引用（用于移除已完成事务）
        :return: (action, msg)
        """
        src = p.get("src", "")
        dst = p.get("dst", "")
        stage = p.get("stage", "")
        # P7:旧版 stage 名兼容(同 recover_pending_migrations,防存量数据落 unknown_stage)
        if stage in ("robocopy_done", "robocopy_failed"):
            p["stage"] = "rustcopy_done" if stage == "robocopy_done" else "rustcopy_failed"
            stage = p["stage"]
        real_target = p.get("real_target", "")

        if not real_target:
            err = "改迁事务缺少 real_target 字段，无法恢复"
            self._incr_pending_fail(p, err)
            pending[:] = [x for x in pending if x is not p]
            return "error", err

        # real_target 不存在说明旧数据已被删（可能上次成功但未清理 pending）
        # 检查 src 是否已指向 dst，是则补录记录完成事务
        if not os.path.exists(real_target):
            if is_symlink(src):
                cur_target = get_symlink_target(src)
                if cur_target:
                    cur_target = cur_target.replace("\\\\?\\", "")
                    if os.path.normpath(cur_target).lower() == os.path.normpath(dst).lower():
                        # 链接已正确指向 dst，补录记录
                        self._add_migrated_record(src, dst)
                        pending[:] = [x for x in pending if x is not p]
                        save_all(self.cfg)
                        return "completed", "旧数据已删且链接已指向dst，补录记录完成"
            # real_target 不存在但链接也没指向 dst，数据可能丢失
            err = f"旧真实数据 {real_target} 不存在，且 src 未指向 dst，数据可能丢失"
            self._incr_pending_fail(p, err)
            return "error", err

        # ===== 阶段1: started/rustcopy_failed/integrity_failed =====
        # 从 real_target 续传 复制引擎到 dst
        if stage in ("started", "rustcopy_failed", "integrity_failed"):
            # 先检查 dst 是否已有完整数据（可能上次复制引擎已成功但 stage 未更新）
            try:
                rt_fc = self._count_files_fast(real_target)
                dst_fc = self._count_files_fast(dst) if os.path.exists(dst) else 0
            except Exception:
                rt_fc, dst_fc = 1, 1

            if dst_fc > 0 and dst_fc >= rt_fc:
                # dst 数据完整，跳过复制，直接进入删旧链接建新链接
                log.info(f"改迁恢复: dst 已有 {dst_fc} 文件，跳过复制")
            else:
                # 需要续传
                log.info(f"改迁恢复: 从 {real_target} 续传到 {dst}")
                self._emit_log("migrate", f"  ⏳ 改迁续传: {os.path.basename(src)}...")
                rc = self._run_copy_with_progress(real_target, dst, action_label="改迁续传")
                if rc >= 8 or rc == _CANCELLED_RC:
                    short_log, long_msg = self._format_copy_fail(rc, src, "改迁续传")
                    self._emit_log("error", short_log)
                    # 取消不增加 fail_count，避免 2 次取消后停止自动恢复
                    if rc == _CANCELLED_RC:
                        self._record_pending_cancel(p, long_msg)
                    else:
                        self._incr_pending_fail(p, long_msg)
                    return "rustcopy_retry_failed", long_msg
                # 验证
                try:
                    rt_fc = self._count_files_fast(real_target)
                    dst_fc = self._count_files_fast(dst)
                except Exception:
                    rt_fc, dst_fc = 1, 1
                if rt_fc > 0 and dst_fc < rt_fc:
                    err = f"改迁续传完整性验证失败(原{rt_fc}/新{dst_fc})"
                    self._incr_pending_fail(p, err)
                    return "integrity_still_failed", err

            # 复制引擎完成，更新 stage 继续往下执行（不 return，落入 rustcopy_done 处理）
            p["stage"] = "rustcopy_done"
            save_all(self.cfg)
            stage = "rustcopy_done"

        # ===== 阶段2: rustcopy_done =====
        # 执行删旧链接 + 建新链接
        if stage == "rustcopy_done":
            # 删除 src 的旧符号链接
            if is_symlink(src):
                try:
                    os.rmdir(src)
                except OSError:
                    os.unlink(src)
                log.info(f"改迁恢复: 删除旧符号链接 {src}")
            elif os.path.exists(src):
                # src 是真实目录（被软件重建），需先删除
                try:
                    self._safe_rd(src)
                    if os.path.exists(src):
                        shutil.rmtree(src, ignore_errors=True)
                except Exception as e:
                    log.debug("忽略异常: %s", e)
                if os.path.exists(src):
                    err = f"改迁恢复: 删除 src 真实目录失败（可能被占用）: {src}"
                    self._incr_pending_fail(p, err)
                    return "delete_src_failed", err

            # 创建新链接 src → dst（符号链接 /D 优先）
            mklink_ok, _ = self._create_dir_link(src, dst)

            if not mklink_ok:
                # 尝试恢复旧链接指向 real_target（让用户能访问数据）
                try:
                    self._create_dir_link(src, real_target)
                except Exception as e:
                    log.debug("忽略异常: %s", e)
                p["stage"] = "mklink_failed"
                p["error"] = "恢复时创建链接失败"
                save_all(self.cfg)
                err = "改迁恢复: 创建新链接失败，已恢复旧链接"
                self._incr_pending_fail(p, err)
                return "mklink_failed", err

            # 链接创建成功，更新 stage 继续往下执行
            p["stage"] = "link_updated"
            save_all(self.cfg)
            stage = "link_updated"

        # ===== 阶段3: mklink_failed =====
        # 旧链接已删（或被恢复），重试建新链接
        if stage == "mklink_failed":
            # 如果 src 已是符号链接且指向 dst，说明上次实际成功了
            if is_symlink(src):
                cur = get_symlink_target(src)
                if cur and os.path.normpath(cur.replace("\\\\?\\", "")).lower() == os.path.normpath(dst).lower():
                    p["stage"] = "link_updated"
                    save_all(self.cfg)
                    stage = "link_updated"
                else:
                    # src 指向其他位置，需删除重建
                    try:
                        os.rmdir(src)
                    except OSError:
                        os.unlink(src)
            elif os.path.exists(src):
                # src 是真实目录（被软件重建），需先删除
                try:
                    self._safe_rd(src)
                    if os.path.exists(src):
                        shutil.rmtree(src, ignore_errors=True)
                except Exception as e:
                    log.debug("忽略异常: %s", e)
                if os.path.exists(src):
                    # 删除失败：不能继续 mklink（路径已存在会失败）
                    err = f"改迁恢复: 删除 src 真实目录失败（被占用）: {src}"
                    self._incr_pending_fail(p, err)
                    return "delete_src_failed", err

            if stage == "mklink_failed":
                mklink_ok, _ = self._create_dir_link(src, dst)
                if not mklink_ok:
                    err = "改迁恢复: 创建链接仍失败"
                    self._incr_pending_fail(p, err)
                    return "mklink_failed", err

                p["stage"] = "link_updated"
                save_all(self.cfg)
                stage = "link_updated"

        # ===== 阶段4: link_updated / cleanup_failed =====
        # 链接已指向 dst，清空旧真实数据 real_target（保留目录本身）
        if stage in ("link_updated", "cleanup_failed"):
            cleanup_ok, cleanup_err = self._cleanup_dir_contents(real_target)
            if not cleanup_ok:
                err = f"改迁恢复: 清空旧数据目录失败: {real_target} - {cleanup_err}"
                self._incr_pending_fail(p, err)
                p["stage"] = "cleanup_failed"
                p["error"] = cleanup_err[:300]
                save_all(self.cfg)
                return "cleanup_failed", err

            # 全部完成，更新 migrated 记录，从 pending 移除
            self._add_migrated_record(src, dst)
            pending[:] = [x for x in pending if x is not p]
            save_all(self.cfg)
            log.info(f"改迁恢复完成: {src} → {dst}")
            return "completed", f"改迁恢复完成: {src} → {dst}"

        # 未知 stage
        err = f"改迁恢复: 未知 stage={stage}"
        self._incr_pending_fail(p, err)
        return "unknown_stage", err

    def _incr_pending_fail(self, pending_entry, error_msg):
        """增加 pending 事务的失败次数计数器，并记录上次失败原因

        达到 2 次后不再自动恢复，把决策权交给用户。
        成功后事务记录会被移除，fail_count 自然清零。
        """
        try:
            pending_entry["fail_count"] = pending_entry.get("fail_count", 0) + 1
            pending_entry["last_error"] = error_msg[:300]  # 截断避免 config.json 过大
            pending_entry["last_fail_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log.warning(f"事务失败计数+1 (fail_count={pending_entry['fail_count']}): "
                        f"{pending_entry.get('src', '')} - {error_msg}")
        except Exception as e:
            log.error(f"更新 fail_count 失败: {e}")

    def _record_pending_cancel(self, pending_entry, msg="用户取消操作"):
        """记录 pending 事务被用户取消（不增加 fail_count）

        与 _incr_pending_fail 的区别：
        - 取消是用户主动行为，不是失败，不应累加 fail_count
        - 若累加 fail_count，2 次取消后事务将停止自动恢复，与取消文案"下次自动续传"矛盾
        - 仅记录 last_error 供用户查看，下次启动仍会自动续传
        """
        try:
            pending_entry["last_error"] = msg[:300]
            pending_entry["last_fail_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log.info(f"事务被用户取消（不计 fail_count）: {pending_entry.get('src', '')}")
        except Exception as e:
            log.error(f"记录取消状态失败: {e}")

    def get_pending_user_decisions(self):
        """获取所有需要用户决策的 pending 事务（fail_count >= 2）

        :return: list of dict，每项含 src/dst/stage/fail_count/last_error/last_fail_time/type
                 type 为 'migration' 或 'restore'
        """
        items = []
        try:
            for p in self.cfg.get("pending_migrations", []):
                if p.get("fail_count", 0) >= 2:
                    items.append({
                        "type": "migration",
                        "src": p.get("src", ""),
                        "dst": p.get("dst", ""),
                        "stage": p.get("stage", ""),
                        "fail_count": p.get("fail_count", 0),
                        "last_error": p.get("last_error", "未知原因"),
                        "last_fail_time": p.get("last_fail_time", ""),
                    })
            for p in self.cfg.get("pending_restores", []):
                if p.get("fail_count", 0) >= 2:
                    items.append({
                        "type": "restore",
                        "src": p.get("src", ""),
                        "dst": p.get("dst", ""),
                        "stage": p.get("stage", ""),
                        "fail_count": p.get("fail_count", 0),
                        "last_error": p.get("last_error", "未知原因"),
                        "last_fail_time": p.get("last_fail_time", ""),
                    })
        except Exception as e:
            log.error(f"获取 user_decision 列表失败: {e}")
        return items

    def manual_retry_pending(self, src, is_restore=False):
        """用户手动重试：重置 fail_count 为 0，下次启动或调用恢复时继续

        :param src: C 盘源路径
        :param is_restore: True 处理 pending_restores，False 处理 pending_migrations
        :return: (bool, str) 成功与否与提示消息
        """
        key = "pending_restores" if is_restore else "pending_migrations"
        label = "还原" if is_restore else "迁移"
        try:
            pending = self.cfg.get(key, [])
            found = False
            for p in pending:
                if p.get("src") == src:
                    p["fail_count"] = 0
                    p.pop("last_error", None)
                    p.pop("last_fail_time", None)
                    p["manual_retry_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    found = True
                    log.info(f"用户手动重试 {label} 事务，fail_count 已清零: {src}")
                    break
            if not found:
                return False, f"未找到 {label} 事务: {src}"
            save_all(self.cfg)
            return True, f"已重置 {label} 事务失败计数，将立即尝试恢复"
        except Exception as e:
            log.error(f"手动重试失败: {e}")
            return False, f"重试失败: {e}"

    def cancel_pending(self, src, is_restore=False):
        """用户放弃：删除 pending 事务记录，保留 D 盘数据由用户自行处理

        :param src: C 盘源路径
        :param is_restore: True 处理 pending_restores，False 处理 pending_migrations
        :return: (bool, str) 成功与否与提示消息
        """
        key = "pending_restores" if is_restore else "pending_migrations"
        label = "还原" if is_restore else "迁移"
        try:
            pending = self.cfg.get(key, [])
            before = len(pending)
            pending[:] = [p for p in pending if p.get("src") != src]
            after = len(pending)
            if before == after:
                return False, f"未找到 {label} 事务: {src}"
            save_all(self.cfg)
            log.warning(f"用户放弃 {label} 事务，已从 pending 列表移除: {src}")
            return True, f"已放弃 {label} 事务，pending 记录已清理"
        except Exception as e:
            log.error(f"放弃事务失败: {e}")
            return False, f"放弃失败: {e}"

    def get_failure_suggestion(self, error_msg, stage, is_restore=False):
        """根据失败错误信息和事务阶段生成建议

        :param error_msg: last_error 文本
        :param stage: 事务阶段
        :param is_restore: 是否还原事务
        :return: str 建议（中文）
        """
        err = (error_msg or "").lower()
        stage = stage or ""
        action = "还原" if is_restore else "迁移"

        # 按错误类型匹配建议
        if "返回码" in err or "引擎" in err:
            return (f"复制数据失败（{action}阶段）。\n"
                    f"可能原因：文件被其他程序占用、目标盘空间不足、权限不足。\n"
                    f"建议：\n"
                    f"  1. 关闭可能使用该目录的程序（如 IDE、浏览器、SDK Manager）\n"
                    f"  2. 检查目标盘剩余空间是否大于源目录\n"
                    f"  3. 以管理员身份重启本程序\n"
                    f"  4. 重试{action}（目标盘已复制的数据会自动续传，不会重复）")
        if "完整性" in err or "integrity" in err:
            return (f"文件数验证失败（{action}阶段）。\n"
                    f"可能原因：复制过程中源目录有新增/删除文件，或部分文件被占用未复制。\n"
                    f"建议：\n"
                    f"  1. 关闭所有可能写入该目录的程序\n"
                    f"  2. 重试{action}（引擎会自动补齐缺失文件）\n"
                    f"  3. 若反复失败，考虑手动复制并创建符号链接")
        if "删除" in err and "c盘" in err:
            return (f"删除 C 盘源目录失败（{action}阶段）。\n"
                    f"可能原因：目录被进程占用、权限不足。\n"
                    f"建议：\n"
                    f"  1. 关闭所有可能使用该目录的程序\n"
                    f"  2. 重启电脑后立即以管理员身份运行本程序\n"
                    f"  3. 必要时手动删除 C 盘源目录后选择重试")
        if "d盘" in err and ("清理" in err or "删除" in err):
            return (f"D 盘冗余数据清理失败（{action}阶段）。\n"
                    f"说明：C 盘数据已完整，仅 D 盘冗余数据未删除。\n"
                    f"建议：\n"
                    f"  1. 可忽略此错误，不影响使用\n"
                    f"  2. 重启程序会自动重试清理\n"
                    f"  3. 必要时手动删除 D 盘冗余目录: 见错误详情中的路径")
        if "链接" in err or "mklink" in err:
            return (f"创建链接失败（{action}阶段）。\n"
                    f"可能原因：未以管理员身份运行、目标路径无效。\n"
                    f"建议：\n"
                    f"  1. 以管理员身份运行本程序后重试\n"
                    f"  2. 检查 C 盘源目录是否已删除（若已删除，重试会重新创建链接）\n"
                    f"  3. 必要时手动执行: mklink /J \"C盘路径\" \"D盘路径\"（Junction 无需管理员权限）")
        if "d盘数据不存在" in err or "数据可能丢失" in err:
            return (f"数据丢失风险（{action}阶段）。\n"
                    f"说明：C 盘已删除但 D 盘数据也丢失。\n"
                    f"建议：\n"
                    f"  1. 立即检查回收站是否有相关数据\n"
                    f"  2. 使用文件恢复工具尝试恢复\n"
                    f"  3. 联系技术支持")
        return (f"{action}事务在阶段 [{stage}] 失败。\n"
                f"错误: {error_msg[:200]}\n"
                f"建议：\n"
                f"  1. 关闭占用程序后重试\n"
                f"  2. 以管理员身份运行本程序\n"
                f"  3. 联系技术支持并提供日志文件")

    def _link_points_to_target(self, src_path, dst_path):
        """链接身份校验(#14)：src 当前是否为指向 dst 的符号链接/junction。

        防止用户手动改过链接后执行还原/撤销，把数据恢复到错误位置。
        """
        if not is_symlink(src_path):
            return False
        target = get_symlink_target(src_path)
        if not target:
            return False
        return (os.path.normpath(target).lower() == os.path.normpath(dst_path).lower())

    def restore(self, src_path):
        """还原：把数据从 D 盘放回 C 盘（事务性，支持断电恢复）

        流程：
        1. 写入 pending 还原事务到 config.json
        2. 删除 C 盘符号链接（如果是符号链接）
        3. 复制引擎数据从 D 盘复制回 C 盘（幂等，可续传）
        4. 文件数完整性验证
        5. 从 migrated 列表移除记录，从 pending 移除事务

        断电恢复：启动时 recover_pending_restores() 自动处理未完成事务
        """
        src = Path(src_path)
        # 从config中查找目标路径
        dst = None
        for m in self.cfg["migrated"]:
            if m["src"] == src_path:
                dst = m["dst"]
                break
        if not dst:
            if is_symlink(src_path):
                dst = get_symlink_target(src_path)
            if not dst:
                log_error_with_reason("找不到迁移记录", context=f"还原: {src_path}")
                return False, f"找不到迁移记录，无法确定目标路径: {src_path}"
        # 规范化目标路径（去掉\\?\前缀）
        if dst.startswith("\\\\?\\"):
            dst = dst[4:]
        if not os.path.exists(dst):
            log_error_with_reason("目标盘数据不存在", f"目标路径: {dst}", f"还原: {src_path}")
            return False, f"目标盘数据不存在: {dst}"

        # ===== 链接身份校验(#14) =====
        # 场景1: src 是符号链接但指向别处(用户手动改过)→ 拒绝,防止还原到错误位置
        # 场景2: src 是真实目录且非空(链接被覆盖/用户手动改)→ 拒绝,防止合并用户数据
        # 场景3: src 不存在或空目录(链接丢失/正常)→ 放行,直接复制回
        if is_symlink(src_path):
            if not self._link_points_to_target(src_path, dst):
                actual = get_symlink_target(src_path) or "未知"
                log_error_with_reason("链接身份校验失败",
                    f"src 指向 {actual}, 预期 {dst}", f"还原: {src_path}")
                return False, (f"链接身份校验失败：{src_path} 当前指向\n  {actual}\n"
                              f"与迁移记录目标不符（预期 {dst}）。\n"
                              f"如果确认是手动改过链接，请先手动修正后再还原。")
        elif os.path.exists(src_path):
            try:
                has_children = any(os.scandir(str(src_path)))
            except OSError:
                has_children = False
            if has_children:
                log_error_with_reason("链接身份校验失败",
                    f"src 是真实目录且非空(链接被覆盖或手动修改)", f"还原: {src_path}")
                return False, (f"链接身份校验失败：{src_path} 现在是真实目录且包含数据，"
                              f"不是指向目标盘的符号链接。\n"
                              f"可能是软件覆盖了链接或手动修改过。\n"
                              f"请先处理该目录（迁移或清理）后再还原，避免数据混淆。")

        # ===== 步骤0前：先写入 pending 还原事务（断电恢复依据）=====
        # 中危修复：原代码在步骤0（删D盘符号链接）之后才写 pending，
        # 如果步骤0和步骤1之间崩溃，D 盘符号链接已删但 pending 无记录，
        # recover 找不到事务，dst 可能已变为 link_target 但无记录。
        # 修复：在删符号链接之前就写入 pending，步骤0后若 dst 变了再更新。
        pending_r = self.cfg.setdefault("pending_restores", [])
        pending_r[:] = [p for p in pending_r if p.get("src") != src_path]
        pending_r.append({
            "src": str(src),
            "dst": str(dst),
            "stage": "started",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        save_all(self.cfg)

        # ===== 步骤0：清理目标盘源路径的残留符号链接 =====
        # 如果 D 盘源路径是符号链接（之前迁移残留），复制引擎会跟着链接走
        # 导致数据从错误的位置复制，必须先删除符号链接
        if is_symlink(dst):
            try:
                # 读取符号链接目标后再删除链接
                link_target = get_symlink_target(dst)
                os.rmdir(dst)
                log.info(f"还原: 删除目标盘残留符号链接: {dst} -> {link_target}")
                self._emit_log("migrate", f"  🔗 已清理目标盘残留符号链接: {os.path.basename(dst)}")
                # 符号链接已删除，需要确定真实数据源
                if link_target and os.path.exists(link_target):
                    # 真实数据在链接目标位置，改从那里复制
                    dst = link_target
                    if dst.startswith("\\\\?\\"):
                        dst = dst[4:]
                    self._emit_log("migrate", f"  ℹ️ 改从真实数据目录复制: {os.path.basename(dst)}")
                    # dst 变了，更新 pending 中的记录
                    for p in pending_r:
                        if p.get("src") == src_path:
                            p["dst"] = str(dst)
                    save_all(self.cfg)
                else:
                    # 符号链接目标不存在（断链），数据丢失
                    log_error_with_reason("目标盘数据丢失",
                        f"符号链接目标不存在: {link_target}", f"还原: {src_path}")
                    return False, (f"目标盘符号链接已删除，但链接目标不存在: {link_target}\n"
                                  f"数据可能已丢失，请检查是否有其他备份。")
            except Exception as e:
                # 删除符号链接失败必须中止，否则 复制引擎会跟着链接走
                log_error_with_reason("目标盘符号链接清理失败",
                    f"无法删除符号链接: {e}", f"还原: {src_path}")
                return False, (f"目标盘路径是符号链接但无法删除: {dst}\n"
                              f"错误: {e}\n"
                              f"请手动删除该符号链接后重试。")

        self._emit_log("migrate", f"📥 开始还原: {src.name} | {dst} → {src}")
        # 标记还原中：防止后台监控 _periodic_check 把 复制引擎回 C 盘的真实数据
        # 误判为"符号链接被覆盖"而触发 _auto_fix_link（会把数据又复制回 D 盘）
        self._mark_restoring(src_path)

        # ===== 步骤2：删除 C 盘符号链接（如果是符号链接）=====
        if is_symlink(src_path):
            try:
                os.rmdir(src_path)
            except OSError:
                os.unlink(src_path)
            log.info(f"还原: 删除符号链接 {src_path}")
            self._emit_log("migrate", f"  🔗 已删除 C 盘符号链接: {src.name}")
        elif not os.path.exists(src_path):
            # C 盘路径不存在（链接已被删），稍后复制引擎会创建
            log.info(f"还原: C盘路径不存在，复制引擎会创建 {src_path}")
            self._emit_log("migrate", f"  ℹ️ C 盘路径不存在，复制引擎会创建: {src.name}")
        elif os.path.isdir(src_path):
            # C 盘是真实目录，用三级兜底删除（rd /s /q → rmtree → rename）
            # 与 migrate() 步骤4 保持一致，避免 ignore_errors=True 静默吞错误
            self._emit_log("migrate", f"  🗑 正在删除 C 盘真实目录: {src.name}...")
            src_deleted = False
            delete_errors = []
            try:
                rd_ok, rd_err = self._safe_rd(str(src))
                if rd_ok:
                    src_deleted = True
                else:
                    delete_errors.append(f"rd /s /q 后仍存在: {rd_err}")
            except Exception as e:
                delete_errors.append(f"rd异常: {e}")

            if not src_deleted:
                # 兜底1：shutil.rmtree（不用 ignore_errors，要拿到错误信息）
                try:
                    shutil.rmtree(str(src))
                    src_deleted = True
                except Exception as e:
                    delete_errors.append(f"rmtree: {e}")
                    # 兜底2：重命名后再 rd（绕过文件占用）
                    try:
                        bak_path = str(src) + "._cdrive_bak"
                        if os.path.exists(bak_path):
                            shutil.rmtree(bak_path, ignore_errors=True)
                        os.rename(str(src), bak_path)
                        log.info(f"还原: C盘目录被占用，已重命名为 {bak_path}，将由后台清理")
                        src_deleted = True
                        self._emit_log("warn",
                            f"  ⚠️ C 盘目录被占用，已重命名为 {os.path.basename(bak_path)}，将由后台清理")
                    except Exception as e2:
                        delete_errors.append(f"rename兜底: {e2}")

            if src_deleted:
                log.info(f"还原: 删除C盘真实目录 {src_path}")
                self._emit_log("migrate", f"  ✓ C 盘真实目录已删除: {src.name}")
            else:
                err_detail = "; ".join(delete_errors[:3])
                log_error_with_reason("还原删除C盘目录失败",
                    f"所有删除策略均失败: {err_detail}",
                    f"还原: {src_path}")
                self._emit_log("error", f"  ✗ 删除 C 盘目录失败: {src.name} - {err_detail[:80]}")
                for p in pending_r:
                    if p.get("src") == src_path:
                        p["stage"] = "delete_c_failed"
                        p["error"] = err_detail
                save_all(self.cfg)
                self._unmark_restoring(src_path)
                return False, (f"删除C盘目录失败（可能文件被占用）。\n"
                              f"错误详情: {err_detail}\n"
                              f"⚠️ 已记录未完成事务，请关闭占用程序后重启程序，会自动重试删除并继续还原。")

        # 更新 stage
        for p in pending_r:
            if p.get("src") == src_path:
                p["stage"] = "c_cleaned"
        save_all(self.cfg)

        # ===== 步骤3：复制引擎数据从 D 盘复制回 C 盘（/MIR 幂等，可续传）=====
        log.info(f"还原复制: {dst} -> {src}")
        self._emit_log("migrate", f"  ⏳ 正在复制数据回 C 盘（镜像模式）: {src.name}...")
        rc = self._run_copy_with_progress(dst, src, action_label="还原")
        if rc >= 8 or rc == _CANCELLED_RC:
            short_log, long_msg = self._format_copy_fail(rc, src_path, "还原")
            log_error_with_reason("还原复制失败",
                f"返回码: {rc}",
                f"还原: {dst} -> {src_path}")
            self._emit_log("error", f"{short_log}: {src.name}")
            for p in pending_r:
                if p.get("src") == src_path:
                    p["stage"] = "rustcopy_failed"
                    if rc == _CANCELLED_RC:
                        p["error"] = "用户取消，下次启动会自动续传"
                    else:
                        diag = getattr(self, "_last_copy_fail_reason", None) or {}
                        if diag.get("reason"):
                            p["error"] = f"返回码 {rc} - {diag['reason']}"
                        else:
                            p["error"] = f"返回码 {rc}"
            save_all(self.cfg)
            self._unmark_restoring(src_path)
            return False, long_msg

        # 更新 stage
        for p in pending_r:
            if p.get("src") == src_path:
                p["stage"] = "rustcopy_done"
        save_all(self.cfg)
        self._emit_log("migrate", f"  ✓ 数据复制回 C 盘完成: {src.name} (返回码 {rc})")

        # ===== 步骤4：文件数完整性验证 =====
        try:
            src_fc = self._count_files_fast(src)
            dst_fc = self._count_files_fast(dst)
            self._emit_log("migrate",
                f"  🔍 文件数验证: C盘 {src_fc} 个 / 目标盘 {dst_fc} 个 ({src.name})")
            if dst_fc > 0 and src_fc < dst_fc:
                log_error_with_reason("还原完整性验证失败",
                    f"C盘 {src_fc} 文件, D盘 {dst_fc} 文件",
                    f"还原: {src_path}")
                self._emit_log("error",
                    f"  ✗ 完整性验证失败: C盘 {src_fc} 个 < 目标盘 {dst_fc} 个 ({src.name})")
                for p in pending_r:
                    if p.get("src") == src_path:
                        p["stage"] = "integrity_failed"
                        p["error"] = f"src={src_fc}, dst={dst_fc}"
                save_all(self.cfg)
                self._unmark_restoring(src_path)
                return False, (f"还原完整性验证失败：D盘 {dst_fc} 个文件，"
                              f"C盘仅 {src_fc} 个文件。\n"
                              f"已记录未完成事务，下次启动程序会自动续传。")
        except Exception as e:
            # 验证异常时保守中止，不继续删 D 盘（可能导致 C 盘数据不完整时误删 D 盘备份）
            log_error_with_reason("还原完整性验证异常",
                f"异常: {e}", f"还原: {src_path}")
            for p in pending_r:
                if p.get("src") == src_path:
                    p["stage"] = "integrity_failed"
                    p["error"] = f"验证异常: {e}"
            save_all(self.cfg)
            self._unmark_restoring(src_path)
            return False, (f"还原完整性验证异常，已中止以保护数据: {e}\n"
                          f"已记录未完成事务，下次启动会自动续传。")

        # ===== 步骤5：删除 D 盘冗余数据（事务安全）=====
        # C 盘数据已通过完整性验证，可以安全删除 D 盘原数据释放空间
        # 删除失败不影响还原成功（C 盘数据已完整），只警告
        # ⚠️ 关键：只清空目标文件夹里面的内容，保留目标文件夹本身！
        #   原代码用 rd /s /q D:\dev\android\sdk 会把整个 sdk 文件夹都删了，
        #   导致下次迁移要重新创建目录结构，而且破坏了 D:\dev\android\ 的目录层级。
        #   正确做法：清空 dst 里的所有条目，保留 dst 文件夹本身。
        self._emit_log("migrate", f"  🗑 正在清空目标盘冗余数据: {os.path.basename(dst)}...")
        d_cleanup_ok, d_cleanup_err = self._cleanup_dir_contents(str(dst))
        if d_cleanup_ok:
            log.info(f"还原: 已清空 D 盘冗余数据（保留目录本身）{dst}")
            self._emit_log("migrate", f"  ✓ 目标盘冗余数据已清空（保留目录本身）: {os.path.basename(dst)}")
        else:
            # 删除失败不阻断事务（C 盘数据已完整），仅记录警告
            log_error_with_reason("还原后D盘清理失败",
                d_cleanup_err, f"还原: {src_path} (D盘={dst})")
            self._emit_log("warn",
                f"  ⚠️ 目标盘冗余数据清空失败（下次启动自动重试）: {os.path.basename(dst)} - {d_cleanup_err[:60]}")
            # 标记 stage，启动恢复时会重试清理
            # ⚠️ 修复 N3：清理失败时保留事务 + stage=d_cleanup_failed，不覆盖为 d_cleaned
            # 否则 recover 的 d_cleanup_failed 分支成死代码，D 盘冗余数据永久残留
            for p in pending_r:
                if p.get("src") == src_path:
                    p["stage"] = "d_cleanup_failed"
                    p["error"] = d_cleanup_err
            save_all(self.cfg)
            self._unmark_restoring(src_path)
            self._maybe_clean_vss()
            return True, (f"还原成功: {src.name} 数据已放回C盘，"
                          f"但目标盘冗余数据清空失败，下次启动会自动重试清理。\n"
                          f"错误详情: {d_cleanup_err[:200]}")

        # 清理成功：更新 stage 为 d_cleaned 并完成事务
        for p in pending_r:
            if p.get("src") == src_path:
                p["stage"] = "d_cleaned"
        save_all(self.cfg)

        # ===== 步骤6：完成事务，清理记录 =====
        self.cfg["migrated"] = [m for m in self.cfg["migrated"] if m["src"] != src_path]
        pending_r[:] = [p for p in pending_r if p.get("src") != src_path]
        save_all(self.cfg)
        log.info(f"还原成功: {src}")
        log_link_operation("删除链接(还原)", src_path, dst, "数据放回C盘")
        # 走到这里时 d_cleanup_ok 必为 True（False 已在上方 return）
        self._emit_log("migrate", f"  ✅ 还原完成: {src.name}（数据已放回 C 盘，目标盘冗余已清空）")
        self._unmark_restoring(src_path)
        self._maybe_clean_vss()
        return True, f"还原成功: {src.name} 数据已放回C盘，D盘冗余数据已清空（保留目录本身）"

    def restore_dev_env_data(self, d_data, default_c):
        """还原开发环境数据：无迁移记录但 D 盘有数据 → 把数据搬回 C 盘默认路径。

        场景：用户手动把数据放到 D 盘并设了环境变量，未通过本工具迁移
        （ui_devenv 反向搬数据）。P6 收敛到 migrator，修复 UI 层直接调复制命令
        的架构违规（无 pending 事务/无错误翻译/无进度上报）。

        与 restore() 同构：写 pending_restores（断电可恢复）→ 引擎复制
        （mirror 含 purge + verify=hash 强制校验）→ 文件数完整性验证 →
        清理 D 盘原数据（保留目录本身）→ 移除事务。
        事务条目 {src: default_c, dst: d_data, type: "dev_env_reverse"}，
        src/dst 语义与 restore() 一致，recover_pending_restores 无需改动即可恢复。

        :param d_data: D 盘数据目录（数据源）
        :param default_c: C 盘默认路径（还原目标）
        :return: (ok: bool, msg: str) 与 restore() 同构
        """
        d_data = str(d_data)
        default_c = str(default_c)
        if default_c.startswith("\\\\?\\"):
            default_c = default_c[4:]

        # 前置校验
        if not os.path.exists(d_data):
            return False, f"D 盘数据不存在: {d_data}"
        if os.path.exists(default_c) and not os.path.isdir(default_c):
            return False, f"C 盘目标路径不是目录: {default_c}"
        # 路径包含校验（防 mirror purge 自吞：default_c ⊆ d_data 或反之）
        try:
            common = os.path.commonpath([d_data, default_c])
            if common == d_data or common == default_c:
                return False, (f"源/目标路径存在包含关系，已中止以保护数据: "
                               f"{d_data} / {default_c}")
        except ValueError:
            pass  # 不同盘符等，无包含关系

        # 写 pending 事务（断电恢复依据；与 restore() 条目语义一致）
        pending_r = self.cfg.setdefault("pending_restores", [])
        pending_r[:] = [p for p in pending_r if p.get("src") != default_c]
        pending_r.append({
            "src": default_c,
            "dst": d_data,
            "stage": "started",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "dev_env_reverse",
        })
        save_all(self.cfg)

        # 创建 C 盘父目录
        try:
            os.makedirs(default_c, exist_ok=True)
        except OSError as e:
            return False, f"创建 C 盘目标目录失败: {default_c} ({e})"

        # 引擎复制（mirror 含 purge + verify=hash 强制校验，删 D 盘数据前必须过校验）
        self._emit_log("migrate", f"  📦 反向搬数据: {os.path.basename(d_data)} → {default_c}...")
        rc = self._run_copy_with_progress(d_data, default_c, action_label="反向搬数据")
        if rc >= 8 or rc == _CANCELLED_RC:
            for p in pending_r:
                if p.get("src") == default_c:
                    p["stage"] = "rustcopy_failed"
                    p["error"] = f"复制返回码 {rc}"
            save_all(self.cfg)
            short_log, long_msg = self._format_copy_fail(rc, d_data, "反向搬数据")
            self._emit_log("error", short_log)
            return False, long_msg

        # 复制成功，更新 stage
        for p in pending_r:
            if p.get("src") == default_c:
                p["stage"] = "rustcopy_done"
        save_all(self.cfg)

        # 文件数完整性验证（与 restore/recover 口径一致）
        try:
            src_count = self._count_files_fast(d_data)
            dst_count = self._count_files_fast(default_c)
        except Exception as e:
            log_error_with_reason("反向搬数据完整性验证异常", str(e), f"d_data={d_data}")
            for p in pending_r:
                if p.get("src") == default_c:
                    p["stage"] = "integrity_failed"
                    p["error"] = f"验证异常: {e}"
            save_all(self.cfg)
            return False, (f"反向搬数据完整性验证异常，已中止以保护数据: {e}\n"
                           f"已记录未完成事务，下次启动会自动续传。")
        if src_count > 0 and dst_count < src_count:
            for p in pending_r:
                if p.get("src") == default_c:
                    p["stage"] = "integrity_failed"
                    p["error"] = f"文件数不足: C={dst_count} D={src_count}"
            save_all(self.cfg)
            return False, (f"反向搬数据完整性验证失败: 目标 {dst_count}/{src_count} 文件。\n"
                           f"已记录未完成事务，下次启动会自动续传。")

        # 清理 D 盘原数据（只清空内容保留目录本身，事务安全；与 restore 一致）
        self._emit_log("migrate", f"  🗑 正在清空 D 盘原数据: {os.path.basename(d_data)}...")
        d_ok, d_err = self._cleanup_dir_contents(d_data)
        if not d_ok:
            # 清理失败不阻断事务（C 盘数据已完整），标记 stage 供启动恢复重试
            log_error_with_reason("反向搬数据D盘清理失败", d_err, f"d_data={d_data}")
            self._emit_log("warn", f"  ⚠️ D 盘原数据清空失败（下次启动自动重试）: {d_err[:60]}")
            for p in pending_r:
                if p.get("src") == default_c:
                    p["stage"] = "d_cleanup_failed"
                    p["error"] = d_err
            save_all(self.cfg)
            return True, (f"反向搬数据完成: 数据已搬回 {default_c}\n"
                          f"但 D 盘原数据清空失败，下次启动会自动重试清理。\n"
                          f"错误详情: {d_err[:200]}")

        # 清理成功：stage=d_cleaned → 移除事务（与 restore 的收尾顺序一致）
        for p in pending_r:
            if p.get("src") == default_c:
                p["stage"] = "d_cleaned"
        save_all(self.cfg)
        pending_r[:] = [p for p in pending_r if p.get("src") != default_c]
        save_all(self.cfg)
        log.info(f"反向搬数据完成: {d_data} -> {default_c}")
        self._emit_log("migrate", f"  ✅ 反向搬数据完成: {os.path.basename(d_data)} → {default_c}")
        return True, f"反向搬数据完成: {dst_count} 个文件已搬回 {default_c}，D 盘原数据已清空（保留目录本身）"

    def recover_pending_restores(self):
        """启动时扫描未完成的还原事务，自动恢复

        事务阶段处理：
        - started: D 盘数据完整，C 盘还是符号链接，重新开始还原
        - delete_c_failed: 重试删除 C 盘符号链接/目录
        - c_cleaned: C 盘已删，D 盘完整，复制引擎续传
        - rustcopy_failed/integrity_failed: 复制引擎续传（/MIR 幂等）
        - rustcopy_done: 验证已通过，进入 D 盘清理
        - d_cleanup_failed: C 盘数据已完整，重试删除 D 盘冗余数据
        - d_cleaned: 全部完成，清理记录

        失败次数控制：同 recover_pending_migrations，>= 2 次停止自动恢复。
        所有工具通用，不针对特定工具。
        """
        results = []
        pending_r = self.cfg.get("pending_restores", [])
        if not pending_r:
            return results

        log.info(f"发现 {len(pending_r)} 个未完成还原事务，开始恢复...")
        for p in list(pending_r):
            # 程序退出时取消恢复循环，避免杀一个复制引擎又启动新的
            if self._recover_cancel_requested:
                log.info("还原恢复循环被取消（程序退出），剩余事务留到下次启动")
                break
            src = p.get("src", "")
            dst = p.get("dst", "")
            stage = p.get("stage", "")
            # P7:旧版 stage 名兼容(还原路径虽无 unknown_stage 兜底,归一化保持口径一致)
            if stage in ("robocopy_done", "robocopy_failed"):
                p["stage"] = "rustcopy_done" if stage == "robocopy_done" else "rustcopy_failed"
                stage = p["stage"]
            if not src or not dst:
                continue

            # 失败次数检查：>= 2 次不再自动恢复，把决策权交给用户
            fail_count = p.get("fail_count", 0)
            if fail_count >= 2:
                last_error = p.get("last_error", "未知原因")
                log.warning(f"跳过自动恢复（已失败 {fail_count} 次）: {src} <- {dst}, "
                            f"stage={stage}, 上次错误: {last_error}")
                results.append((src, "user_decision_required",
                    f"已失败 {fail_count} 次，停止自动恢复。原因: {last_error}。"
                    f"请关闭占用程序后手动还原，或联系支持。"))
                continue

            log.info(f"恢复还原事务: {src} <- {dst}, stage={stage}, fail_count={fail_count}")
            self._mark_restoring(src)
            try:
                # d_cleanup_failed / d_cleaned 阶段：C 盘数据已完整，
                # 只需要重试删除目标盘冗余数据，无需再复制
                if stage in ("d_cleanup_failed", "d_cleaned"):
                    if not os.path.exists(dst):
                        # 目标盘已无数据，事务完成
                        self.cfg["migrated"] = [m for m in self.cfg["migrated"] if m["src"] != src]
                        pending_r[:] = [x for x in pending_r if x.get("src") != src]
                        results.append((src, "completed", "目标盘冗余数据已清理，还原事务完成"))
                        continue
                    # 重试清空目标盘数据（保留目录本身）
                    d_ok, d_err = self._cleanup_dir_contents(dst)
                    if d_ok or not os.path.exists(dst):
                        self.cfg["migrated"] = [m for m in self.cfg["migrated"] if m["src"] != src]
                        pending_r[:] = [x for x in pending_r if x.get("src") != src]
                        results.append((src, "completed", "目标盘冗余数据已清空，还原事务完成"))
                    else:
                        # 目标盘清理失败不阻断（C 盘数据已完整），但记一次失败次数
                        # 避免无限重试目标盘清理（可能是权限问题）
                        self._incr_pending_fail(p, f"目标盘冗余数据清空失败: {d_err}")
                        results.append((src, "d_cleanup_retry_failed",
                            f"目标盘冗余数据清空仍失败: {dst}（不阻断，C盘数据已完整）"))
                    continue

                # P7 审查修复: rustcopy_done = 复制成功已写 stage、但完整性验证前断电的窗口期。
                # 此时 C 盘数据可能完整也可能不完整，绝不能直接按"未完成"删除 C 盘再重拷：
                # - C 盘文件数 >= 目标盘（或目标盘已空）→ 数据完整，只需清理目标盘冗余即可提交
                # - 否则 → C 盘是残缺数据，落入下方"其他阶段"分支删除残片后从目标盘续传
                if stage == "rustcopy_done":
                    try:
                        c_fc = self._count_files_fast(src)
                        d_fc = self._count_files_fast(dst)
                    except Exception:
                        c_fc, d_fc = 0, 1  # 统计异常按"不完整"处理，走续传
                    if d_fc == 0:
                        # 目标盘已空，事务实际已完成
                        self.cfg["migrated"] = [m for m in self.cfg["migrated"] if m["src"] != src]
                        pending_r[:] = [x for x in pending_r if x.get("src") != src]
                        results.append((src, "completed", "C盘数据完整，还原事务完成"))
                        continue
                    if c_fc > 0 and c_fc >= d_fc:
                        # C 盘数据完整，清理目标盘冗余（与 d_cleanup_failed 分支同构）
                        d_ok, d_err = self._cleanup_dir_contents(dst)
                        if d_ok or not os.path.exists(dst):
                            self.cfg["migrated"] = [m for m in self.cfg["migrated"] if m["src"] != src]
                            pending_r[:] = [x for x in pending_r if x.get("src") != src]
                            results.append((src, "completed", "C盘数据完整，目标盘冗余已清理，还原事务完成"))
                        else:
                            # 目标盘清理失败不阻断（C 盘数据已完整），记一次失败次数避免无限重试
                            self._incr_pending_fail(p, f"目标盘冗余数据清空失败: {d_err}")
                            results.append((src, "d_cleanup_retry_failed",
                                f"目标盘冗余数据清空仍失败: {dst}（不阻断，C盘数据已完整）"))
                        continue
                    # C 盘数据不完整 → 落入下方"其他阶段"分支（删残片 → 从目标盘续传）
                    log.warning(f"stage=rustcopy_done 但 C 盘数据不完整(src={c_fc}/dst={d_fc})，"
                                f"删除残缺数据后从目标盘续传")

                # 其他阶段（started/delete_c_failed/c_cleaned/rustcopy_failed/integrity_failed）：
                # 先处理 C 盘的符号链接/真实目录
                if is_symlink(src):
                    # C 盘还是符号链接，删除它
                    try:
                        os.rmdir(src)
                    except OSError:
                        os.unlink(src)
                    log.info(f"恢复: 删除符号链接 {src}")
                elif os.path.isdir(src):
                    # C 盘是真实目录（可能是 delete_c_failed 重试，或上次复制引擎部分完成）
                    # 用三级兜底删除（rd /s /q → rmtree → rename），与 restore() 主流程一致
                    # ⚠️ 审查修复: 删除 C 盘目录前必须验证目标盘有数据（N12 同款保护）
                    # 否则目标盘数据丢失/盘符变更时删掉 C 盘残片 = 双端丢失
                    try:
                        d_fc = self._count_files_fast(dst)
                    except Exception:
                        d_fc = 0
                    if d_fc == 0:
                        err_msg = (f"目标盘无数据（{dst}），拒绝删除C盘目录以防止数据丢失。"
                                   f"请检查目标盘是否已连接/数据是否完整。")
                        self._incr_pending_fail(p, err_msg)
                        results.append((src, "dst_empty_refused", err_msg))
                        continue
                    log.info(f"恢复: C盘是真实目录，尝试删除: {src}")
                    c_deleted = False
                    c_err = ""
                    try:
                        c_deleted, c_rd_err = self._safe_rd(src)
                        if not c_deleted:
                            c_err = f"rd /s /q 后仍存在: {c_rd_err}"
                    except Exception as e:
                        c_err = f"rd异常: {e}"
                    if not c_deleted:
                        try:
                            shutil.rmtree(src)
                            c_deleted = True
                        except Exception as e:
                            c_err += f"; rmtree: {e}"
                            # rename 兜底
                            try:
                                bak_path = src + "._cdrive_bak"
                                if os.path.exists(bak_path):
                                    shutil.rmtree(bak_path, ignore_errors=True)
                                os.rename(src, bak_path)
                                log.info(f"恢复: C盘目录被占用，已重命名为 {bak_path}")
                                c_deleted = True
                            except Exception as e2:
                                c_err += f"; rename: {e2}"
                    if not c_deleted:
                        self._incr_pending_fail(p, f"删除C盘目录失败: {c_err}")
                        results.append((src, "delete_c_retry_failed",
                            f"删除C盘目录仍失败（可能文件被占用）: {c_err[:80]}"))
                        continue

                # 检查目标盘数据
                if not os.path.exists(dst):
                    results.append((src, "error", f"目标盘数据不存在: {dst}，无法还原"))
                    pending_r[:] = [x for x in pending_r if x.get("src") != src]
                    continue

                # 复制引擎续传
                log.info(f"续传复制: {dst} -> {src}")
                self._emit_log("migrate", f"  ⏳ 续传还原数据: {os.path.basename(src)}...")
                rc = self._run_copy_with_progress(dst, src, action_label="续传还原")
                if rc >= 8 or rc == _CANCELLED_RC:
                    if rc == _CANCELLED_RC:
                        # 取消：不增加 fail_count，不写"返回码 -1"
                        err_msg = "用户取消续传还原"
                        self._record_pending_cancel(p, err_msg)
                        results.append((src, "rustcopy_retry_failed",
                            err_msg + "，下次启动会自动续传"))
                    else:
                        # 失败：附加诊断原因（如文件被占用、权限不足等），便于用户定位问题
                        diag = getattr(self, "_last_copy_fail_reason", None) or {}
                        if diag.get("reason"):
                            err_msg = f"复制续传仍失败（返回码 {rc}）: {diag['reason']}"
                        else:
                            err_msg = f"复制续传仍失败（返回码 {rc}）"
                        self._incr_pending_fail(p, err_msg)
                        results.append((src, "rustcopy_retry_failed",
                            err_msg + "，请关闭占用程序后重启程序"))
                    continue

                # 完整性验证
                try:
                    src_fc = self._count_files_fast(src)
                    dst_fc = self._count_files_fast(dst)
                except Exception:
                    src_fc, dst_fc = 1, 1
                if dst_fc > 0 and src_fc < dst_fc:
                    err_msg = f"完整性验证仍失败(C={src_fc}, D={dst_fc})"
                    self._incr_pending_fail(p, err_msg)
                    results.append((src, "integrity_still_failed",
                        err_msg + "，请关闭占用程序后重启程序"))
                    continue

                # 验证通过，清空 D 盘冗余数据（保留目录本身）
                d_ok, d_err = self._cleanup_dir_contents(dst)

                # 完成事务（无论 D 盘清理是否成功，C 盘数据已完整）
                self.cfg["migrated"] = [m for m in self.cfg["migrated"] if m["src"] != src]
                pending_r[:] = [x for x in pending_r if x.get("src") != src]
                if d_ok or not os.path.exists(dst):
                    results.append((src, "completed",
                        f"还原续传完成 ({src_fc} 文件已放回C盘，D盘冗余已清空)"))
                else:
                    results.append((src, "completed_warn",
                        f"还原续传完成 ({src_fc} 文件已放回C盘)，但D盘冗余数据清空失败: {d_err[:60]}"))
            except Exception as e:
                results.append((src, "error", f"恢复异常: {e}"))
            finally:
                self._unmark_restoring(src)

        save_all(self.cfg)
        return results

    def scan_appdata(self, progress_cb=None):
        """扫描监控目录（6 个关键目录 + 当前用户目录）的所有一级子目录（不按阈值过滤，不跳过symlink）
        progress_cb(current, total, dir_name) 用于更新进度条

        实现说明：
          - 优先使用 MFT 扫描器（由 utils.get_mft_scanner() 返回）
            MFT 模式下大小计算为 O(1) 查预计算缓存，任意深度目录都准确
          - MFT 未加载或路径不在当前卷时自动回退到 os.walk
          - 不管底层套多少层子目录，一级子目录的大小都包含所有后代文件
          - 用户目录下动态排除已监控子目录（AppData 等）与系统特殊文件夹（scan_dirs 模块）
        """
        # 获取全局 MFT 扫描器单例（可能为 None 或未加载）
        from utils import get_mft_scanner
        mft_scanner = get_mft_scanner()
        use_mft = (mft_scanner is not None and mft_scanner._loaded
                   and mft_scanner.is_mft_mode)

        results = []
        scan_dirs = get_scan_dirs()
        # 用户目录一级子目录的动态排除集合：已监控 base（AppData\Local/Roaming 等）
        # + 系统特殊文件夹（Known Folder API 解析，不硬编码目录名）
        _user_dir_monitored = get_monitored_base_norms(scan_dirs)
        _user_dir_known = get_known_folder_paths()
        candidates = []
        for base_path, label in scan_dirs:
            if not base_path or not os.path.exists(base_path):
                continue
            try:
                for entry in os.listdir(base_path):
                    full_path = os.path.join(base_path, entry)
                    # 只扫一级子目录，不扫孙目录
                    if not os.path.isdir(full_path):
                        continue
                    # 用户目录下：动态排除已在监控列表的子目录（如 AppData\Local）
                    # 与系统特殊文件夹（桌面/文档/下载等），避免重复与误迁移
                    if label == USER_LABEL and is_user_dir_excluded(
                            norm_path(full_path), _user_dir_monitored, _user_dir_known):
                        continue
                    # 跳过已建立软链接的文件夹（已迁移过）
                    if is_symlink(full_path):
                        continue
                    # 通用跳过：目录非空且所有子项都是符号链接
                    # （如 Android 目录下只剩 Sdk 符号链接，说明内容已全部迁移）
                    # 空目录不跳过（可能是有意义但暂未填充的目录）
                    try:
                        sub_entries = os.listdir(full_path)
                    except Exception:
                        sub_entries = None
                    if sub_entries is not None and len(sub_entries) > 0:
                        all_symlink = True
                        for sub in sub_entries:
                            sub_path = os.path.join(full_path, sub)
                            # P6 修复:os.path.islink 对 Junction 返回 False,
                            # 用 is_symlink(st_reparse_tag)兼容两种链接
                            if not is_symlink(sub_path):
                                all_symlink = False
                                break
                        if all_symlink:
                            continue
                    candidates.append((full_path, entry, label))
            except Exception as e:
                log.debug(f"scan_appdata 遍历 {base_path} 时异常: {e}")

        total = len(candidates)
        t_size_total = 0.0
        t_desc_total = 0.0
        # desc 缓存（从 config.json 读取，避免每次扫描都重新识别软件描述）
        # 缓存未命中的目录返回空字符串，扫描完成后由 _async_fill_empty_desc 异步补全
        desc_cache = self.cfg.get("desc_cache", {}) if hasattr(self, 'cfg') else {}
        desc_hit_count = 0
        # 开发环境已配置的 C 盘源路径集合（用于待迁移区橙色提示）
        dev_env_paths = build_dev_env_paths(getattr(self, 'cfg', None))
        for i, (full_path, entry, label) in enumerate(candidates):
            try:
                # 优先用 MFT 计算大小（O(1)，任意深度都准确）
                _t0 = time.time()
                if use_mft:
                    try:
                        size = mft_scanner.get_dir_size_mft(full_path)
                    except Exception:
                        size = get_dir_size_fast(full_path)
                else:
                    size = get_dir_size_fast(full_path)
                t_size_total += time.time() - _t0
                # 软件描述：优先查缓存，未命中返回空字符串（异步补全）
                _t0 = time.time()
                desc = desc_cache.get(full_path, "")
                if desc:
                    desc_hit_count += 1
                else:
                    desc = ""  # 未命中缓存，留空等异步补全
                t_desc_total += time.time() - _t0
                is_link = is_symlink(full_path)
                link_target = get_symlink_target(full_path) if is_link else ""
                # 检查是否被开发环境迁移区配置过（橙色提示）
                fp_norm = full_path.replace("\\\\?\\", "").lower().rstrip("\\")
                dev_cfg = dev_env_paths.get(fp_norm)
                # 记录目录 mtime，供智能刷新增量对比（mtime 未变化则不重算大小）
                try:
                    cur_mtime = os.path.getmtime(full_path)
                except Exception:
                    cur_mtime = 0
                results.append({"path": full_path, "name": entry,
                    "location": label, "size_mb": size, "desc": desc,
                    "is_symlink": is_link, "link_target": link_target,
                    "dev_env_configured": dev_cfg is not None,
                    "dev_env_target": dev_cfg.get("target_path", "") if dev_cfg else "",
                    "dev_env_name": dev_cfg.get("name", "") if dev_cfg else "",
                    "dev_env_drive": dev_cfg.get("target_drive", "") if dev_cfg else "",
                    "mtime": cur_mtime,
                })
            except Exception as e:
                log.debug(f"scan_appdata 处理目录 {entry} 时异常: {e}")
            # 每个目录处理完成后才更新进度（表示"已完成N个"，而非"开始处理第N个"）
            if progress_cb:
                progress_cb(i + 1, total, entry)
        if progress_cb:
            progress_cb(total, total, "完成")
        results.sort(key=lambda x: x["size_mb"], reverse=True)
        # 记录各阶段耗时到日志
        log.info(f"scan_appdata 内部统计: 候选 {total} 个, "
                 f"大小计算 {t_size_total:.2f} 秒, 描述查缓存 {t_desc_total:.2f} 秒 "
                 f"(命中 {desc_hit_count}/{total})")
        return results

    def scan_migrated(self, force_recalc_size=False):
        """扫描已迁移记录的状态
        状态说明：
        - OK：正常，符号链接有效且指向正确目标
        - BROKEN：断链，C盘路径存在但不是符号链接（被软件覆盖）
        - MISSING：丢失，C盘路径不存在（被误删）
        - TARGET_GONE：目标丢失，符号链接还在但目标盘数据不存在

        :param force_recalc_size: True 时强制重算所有记录的大小（get_dir_size_fast 遍历目标盘）
                                  False 时优先用 config 中已存的 size_mb（启动时用，避免遍历 68GB）
        """
        results = []
        for m in self.cfg["migrated"]:
            src, dst = m["src"], m["dst"]
            is_link = is_symlink(src)
            target = get_symlink_target(src) if is_link else ""
            # 规范化比较：去掉 \\?\ 前缀，统一小写，统一去掉末尾反斜杠
            def norm(p):
                p = p.lower().rstrip("\\").replace("\\\\?\\", "").replace("\\\\?\\UNC\\", "\\\\").replace("/", "\\")
                return p
            target_norm = norm(target) if target else ""
            dst_norm = norm(dst)
            if force_recalc_size:
                # 用户点"刷新已迁移"按钮：重算大小（目标在其他盘，走 os.walk）
                # 大小为 0 表示目标不存在或无法访问
                try:
                    size = get_dir_size_fast(dst) if os.path.exists(dst) else 0
                except Exception:
                    size = 0
            else:
                # 启动时不计算大小（get_dir_size_fast会遍历整个目录树，68GB要几十秒）
                # 优先用config中已存的size_mb，没有就标0，后台异步刷新时再计算
                size = m.get("size_mb", 0) if (is_link and target_norm == dst_norm) else 0
            # 判断状态
            if is_link and target_norm == dst_norm and os.path.exists(dst):
                status = "OK"
            elif is_link and (not os.path.exists(dst)):
                status = "TARGET_GONE"
            elif is_link and target_norm != dst_norm:
                status = "BROKEN"
            elif not os.path.exists(src):
                status = "MISSING"
            else:
                # src存在但不是符号链接（被软件覆盖为真实目录）
                status = "BROKEN"
            results.append({
                "src": src, "dst": dst, "time": m.get("time", ""),
                "is_symlink": is_link, "target": target, "size_mb": size,
                "status": status,
                # 动态查 desc_cache 获取最新说明（修复旧记录中 desc 为 basename 的问题）
                "desc": _get_migrated_desc(src, self.cfg) or m.get("desc", "")
            })
        # 补充扫描文件系统上实际存在的symlink（不依赖config.json记录）
        existing_srcs = set(r["src"].lower().rstrip("\\") for r in results)
        # 已记录到链接日志的src集合（去重，避免每次扫描都重复写入）
        logged_set = set(s.lower().rstrip("\\") for s in self.cfg.get("logged_symlinks", []))
        new_logged = []  # 本次新记录的链接（待写入config）
        # 系统自带junction黑名单（Windows为兼容XP创建的重定向链接，非用户迁移）
        # 这些链接target指向同用户/系统目录下的其他位置，不是用户通过本工具迁移的
        SYSTEM_JUNCTION_NAMES = {
            "application data", "local settings", "my documents",
            "history", "temporary internet files", "cookies",
            "sendto", "recent", "nethood", "printhood",
            "templates", "start menu", "「开始」菜单", "桌面",
            "documents", "favorites", "local appdata",
            "printhood", "appdata",
        }
        _scan_dirs = [
            os.environ.get("LOCALAPPDATA", ""),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
            os.environ.get("APPDATA", ""),
            r"C:\Program Files",
            r"C:\Program Files (x86)",
            r"C:\ProgramData",
            # 当前用户目录：已迁移目录在 C 盘是符号链接，也需补进已迁移表
            os.environ.get("USERPROFILE", ""),
        ]
        for base_path in _scan_dirs:
            if not base_path or not os.path.exists(base_path):
                continue
            try:
                for entry in os.listdir(base_path):
                    full_path = os.path.join(base_path, entry)
                    if not os.path.isdir(full_path):
                        continue
                    if not is_symlink(full_path):
                        continue
                    # 只补录符号链接（本工具迁移产物，_create_dir_link /D 优先）
                    # 过滤 junction：手动建的目录联接（.hermes → G:\AI\... 等）和
                    # 系统 XP 兼容链接都不是本工具迁移产物，补录会干扰用户选择
                    # （用户可能误点"还原"把目标盘数据复制回 C 盘）
                    if is_junction(full_path):
                        continue
                    # 过滤系统自带junction（XP兼容链接，非用户迁移）
                    if entry.lower() in SYSTEM_JUNCTION_NAMES:
                        continue
                    if full_path.lower().rstrip("\\") in existing_srcs:
                        continue
                    target = get_symlink_target(full_path)
                    # 启动时不计算大小，避免遍历大目录卡死（后台异步刷新时再算）
                    size = 0
                    if target and os.path.exists(target):
                        status = "OK"
                    else:
                        status = "TARGET_GONE"
                    results.append({
                        "src": full_path, "dst": target, "time": "",
                        "is_symlink": True, "target": target, "size_mb": size,
                        "status": status,
                        "desc": _get_migrated_desc(full_path, self.cfg)
                    })
                    existing_srcs.add(full_path.lower().rstrip("\\"))
                    # 记录到链接日志（去重：只在首次发现时写入）
                    src_key = full_path.lower().rstrip("\\")
                    if src_key not in logged_set:
                        log_link_operation("扫描发现(已存在)", full_path, target or "(无目标)",
                            f"状态:{status} 大小:{size}MB")
                        logged_set.add(src_key)
                        new_logged.append(full_path)
            except Exception as e:
                log.debug(f"scan_migrated 处理条目时异常: {e}")
        # 如果有新记录的链接，保存到config（避免下次重复写入）
        if new_logged:
            self.cfg.setdefault("logged_symlinks", []).extend(new_logged)
            save_all(self.cfg)
        return results

    @link_fix_locked
    def fix_broken_link(self, src_path, dst_path):
        """修复断链的符号链接 - 保留C盘新数据，合并到目标盘后重建链接
        步骤：1.通过 Rust 引擎将C盘新数据合并到目标盘(mode=copy) 2.删除C盘目录 3.创建符号链接

        H3 修复:与后台 _auto_fix_link 共用 _link_fix_lock 互斥(见 utils.link_fix_locked),
        避免两个引擎作业并发写同一目标目录、互相覆盖取消失效。
        """
        src = Path(src_path)
        dst = Path(dst_path)
        if not src.exists():
            # C盘路径不存在，直接创建链接（符号链接 /D 优先）
            ok, lerr = self._create_dir_link(str(src), str(dst))
            if ok:
                log_link_operation("修复链接(缺失)", str(src), str(dst), "C盘路径不存在，直接创建链接")
                return True, "C盘路径不存在，已直接创建链接"
            log_error_with_reason("创建链接失败", lerr, f"修复链接(缺失): {src_path} -> {dst_path}")
            return False, f"创建链接失败: {lerr}"
        if is_symlink(src_path):
            return True, "符号链接已存在，无需修复"
        # C盘是真实目录（被软件覆盖），需合并数据后重建链接
        # 检查目标盘是否存在
        if dst.is_absolute():
            dst_root = dst.anchor
            if not os.path.exists(dst_root):
                log_error_with_reason("目标盘不存在", context=f"修复链接: {src_path} -> {dst_path}")
                return False, f"目标盘不存在: {dst_root}（请检查目标盘是否已连接）"
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, FileNotFoundError) as e:
            log_error_with_reason("目标目录创建失败",
                f"无法创建目录: {e}", f"修复链接: {src_path} -> {dst_path}")
            return False, f"目标目录创建失败: {dst.parent}\n错误: {e}"
        # 1. 通过 Rust 引擎合并 C盘新数据到目标盘(mode=copy,不删除目标盘已有数据)
        # P4:复制调用已替换为引擎 mode="copy"(=/E 等价,不含 purge)
        # fix_broken_link 是低频小数据量场景,不走 _run_copy_with_progress dispatcher
        # 引擎 mode="copy"(=/E 等价,不含 purge)
        rc = self._run_engine_with_progress(
            str(src), str(dst), action_label="合并",
            mode="copy", purge_enabled=False,
        )
        if rc >= 8 or rc == _CANCELLED_RC:
            # _run_engine_with_progress 已填充 _last_copy_fail_reason
            diag = getattr(self, "_last_copy_fail_reason", None) or {}
            err_detail = f"合并数据失败（返回码 {rc}）"
            if diag.get("reason"):
                err_detail += f": {diag['reason']}"
            if diag.get("suggestion"):
                err_detail += f"\n建议：{diag['suggestion']}"
            if diag.get("file"):
                err_detail += f"\n失败文件：{diag['file']}"
            log_error_with_reason("合并数据失败", f"返回码: {rc}", f"修复链接: {src_path} -> {dst_path}")
            return False, err_detail
        # 2. 删除C盘真实目录
        try:
            shutil.rmtree(str(src))
        except Exception as e:
            log_error_with_reason("删除C盘目录失败", str(e), f"修复链接: {src_path}")
            return False, f"删除C盘目录失败: {e}"
        # 3. 创建链接（符号链接 /D 优先）
        ok, lerr = self._create_dir_link(str(src), str(dst))
        if not ok:
            # 创建失败，尝试把数据复制回来(回滚:目标盘→C盘)
            # P4:复制调用已替换为引擎 mode="copy"(回滚场景,不 purge)
            try:
                self._run_engine_with_progress(
                    str(dst), str(src), action_label="回滚",
                    mode="copy", purge_enabled=False,
                )
            except Exception:
                pass  # 回滚失败不阻断错误上报,主错误是"创建链接失败"
            log_error_with_reason("创建链接失败", lerr, f"修复链接: {src_path} -> {dst_path}")
            return False, f"创建链接失败: {lerr}"
        log_link_operation("修复链接(断链)", str(src), str(dst), "合并C盘新数据后重建链接")
        return True, f"修复成功: 已合并C盘新数据并重建链接: {src.name} -> {dst}"

    @link_fix_locked
    def rebuild_all_links(self, username_map=None, progress_cb=None):
        """重装系统后批量重建所有符号链接

        遍历 migrated 记录，检查 C 盘 src 路径：
        - 如果 src 已是符号链接 → 跳过（已正常）
        - 如果 src 是真实目录 → 合并数据到 dst 后重建链接（断链修复）
        - 如果 src 不存在 → 直接创建符号链接
        - 如果 dst 目标数据不存在 → 标记为"目标丢失"，跳过

        H3 修复:与 _auto_fix_link/fix_broken_link 共用 _link_fix_lock 互斥,
        避免批量重建与后台周期修复并发时双引擎作业写同一批目标目录。

        :param username_map: 可选的用户名映射 dict，如 {"aaa": "newname"}
                             用于重装系统后用户名变更的场景
        :param progress_cb: 可选的进度回调 fn(current, total, msg)
        :return: (rebuilt_count, skipped_count, failed_count, details)
                 details: [(src, dst, status, msg), ...]
        """
        migrated = self.cfg.get("migrated", [])
        if not migrated:
            return 0, 0, 0, []

        total = len(migrated)
        rebuilt = 0
        skipped = 0
        failed = 0
        details = []

        for i, m in enumerate(migrated):
            src = m.get("src", "")
            dst = m.get("dst", "")
            if not src or not dst:
                continue

            # 用户名映射：重装系统后用户名可能变了
            actual_src = src
            if username_map:
                for old_name, new_name in username_map.items():
                    if old_name in actual_src:
                        actual_src = actual_src.replace(
                            f"\\{old_name}\\", f"\\{new_name}\\")
                        break
                # 更新 config 中的 src 路径
                if actual_src != src:
                    m["src"] = actual_src
                    src = actual_src

            # 规范化路径
            src = src.replace("\\\\?\\", "").replace("/", "\\")
            dst = dst.replace("\\\\?\\", "").replace("/", "\\")

            if progress_cb:
                progress_cb(i + 1, total, f"[{i+1}/{total}] {os.path.basename(src)}")

            # 检查目标盘数据是否存在
            if not os.path.exists(dst):
                failed += 1
                details.append((src, dst, "target_gone", f"目标盘数据不存在: {dst}"))
                self._emit_log("error", f"  ❌ 目标盘数据不存在: {os.path.basename(src)} → {dst}")
                continue

            # 情况1：src 已是符号链接且指向正确目标 → 跳过
            if is_symlink(src):
                cur_target = get_symlink_target(src)
                if cur_target:
                    cur_target = cur_target.replace("\\\\?\\", "")
                    if os.path.normpath(cur_target).lower() == os.path.normpath(dst).lower():
                        skipped += 1
                        details.append((src, dst, "already_ok", "符号链接已存在且正确"))
                        continue
                    else:
                        # 指向错误目标，删除重建
                        try:
                            if os.path.isdir(src):
                                os.rmdir(src)
                            else:
                                os.unlink(src)
                        except Exception as e:
                            failed += 1
                            details.append((src, dst, "del_old_link_failed", str(e)))
                            self._emit_log("error", f"  ❌ 删除旧符号链接失败: {os.path.basename(src)} - {e}")
                            continue
                else:
                    # 断链符号链接，删除重建
                    try:
                        if os.path.isdir(src):
                            os.rmdir(src)
                        else:
                            os.unlink(src)
                    except Exception as e:
                        failed += 1
                        details.append((src, dst, "del_old_link_failed", str(e)))
                        self._emit_log("error", f"  ❌ 删除断链符号链接失败: {os.path.basename(src)} - {e}")
                        continue

            # 情况2：src 是真实目录（被软件覆盖）→ 合并数据后重建
            if os.path.exists(src) and not is_symlink(src):
                self._emit_log("migrate", f"  🔄 合并 C 盘新数据到目标盘: {os.path.basename(src)}")
                # P4:复制调用已替换为 Rust 引擎 mode="copy"(合并,不 purge)
                rc = self._run_engine_with_progress(
                    src, dst, action_label="合并", mode="copy", purge_enabled=False,
                )
                if rc >= 8 or rc == _CANCELLED_RC:
                    # _run_engine_with_progress 已填充 _last_copy_fail_reason
                    diag = getattr(self, "_last_copy_fail_reason", None) or {}
                    merge_err = f"返回码 {rc}"
                    if diag.get("reason"):
                        merge_err += f" - {diag['reason']}"
                    failed += 1
                    details.append((src, dst, "merge_failed", merge_err))
                    self._emit_log("error", f"  ❌ 合并数据失败: {os.path.basename(src)} - {merge_err}")
                    continue
                # 删除 C 盘真实目录
                try:
                    self._safe_rd(src)
                    if os.path.exists(src):
                        shutil.rmtree(src, ignore_errors=True)
                except Exception as e:
                    failed += 1
                    details.append((src, dst, "delete_failed", str(e)))
                    self._emit_log("error", f"  ❌ 删除 C 盘目录失败: {os.path.basename(src)} - {e}")
                    continue

            # 情况3：src 不存在 → 直接创建符号链接
            # 确保父目录存在
            parent = os.path.dirname(src)
            if parent and not os.path.exists(parent):
                try:
                    os.makedirs(parent, exist_ok=True)
                except Exception as e:
                    failed += 1
                    details.append((src, dst, "mkdir_failed", str(e)))
                    self._emit_log("error", f"  ❌ 创建父目录失败: {os.path.basename(src)} - {e}")
                    continue

            # 创建链接（符号链接 /D 优先）
            ok, lerr = self._create_dir_link(src, dst)
            if ok:
                rebuilt += 1
                details.append((src, dst, "rebuilt", "链接已重建"))
                log_link_operation("重装系统重建链接", src, dst, "rebuild_all_links")
                self._emit_log("migrate", f"  ✅ 重建链接: {os.path.basename(src)} → {dst}")
            else:
                failed += 1
                details.append((src, dst, "mklink_failed", lerr))
                self._emit_log("error", f"  ❌ 创建链接失败: {os.path.basename(src)} - {lerr}")

        # 保存更新后的 config（用户名映射可能修改了 src 路径）
        save_all(self.cfg)

        summary = f"重建 {rebuilt} 个，跳过 {skipped} 个，失败 {failed} 个"
        self._emit_log("migrate", f"📋 链接重建完成: {summary}")
        return rebuilt, skipped, failed, details