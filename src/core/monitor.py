#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后台监控线程 - watchdog文件系统监控 + 安装器进程检测 + 自动修复符号链接"""

import os
import sys
import shutil
import subprocess
import time
import threading
from pathlib import Path
from datetime import datetime

from PySide6.QtCore import Signal, QObject

import logging
log = logging.getLogger('CDriveRelocator')
from config import log_link_operation, log_error_with_reason, save_config
from utils import is_symlink, is_junction, get_symlink_target, get_dir_size_fast, link_fix_locked
from scan_dirs import (get_scan_dirs, get_monitored_base_norms, get_known_folder_paths,
                       is_user_dir_excluded, USER_LABEL)
from migrator import build_dev_env_paths

# subprocess 隐藏控制台窗口标志（避免弹黑窗）
_NO_WINDOW_FLAGS = 0x08000000

# ========== 异步扫描线程 ==========

def ensure_mft_loaded(progress_cb=None, log_prefix=""):
    """确保 MFT 索引已加载（模块级共用函数）

    ScanWorker 和 SmartScanWorker 都调用此函数，避免 MFT 加载逻辑重复。
    若已加载（全局单例存在），直接返回；否则创建并加载 MftScanner。
    加载失败时创建降级 scanner（_fallback=True），后续走 os.walk。
    """
    from utils import get_mft_scanner
    scanner = get_mft_scanner()
    if scanner is not None:
        return scanner  # 已加载
    import time
    t0 = time.time()
    try:
        from fast_scan import MftScanner
        scanner = MftScanner("C")
        scanner.load(progress_cb=progress_cb)
        from utils import set_mft_scanner
        set_mft_scanner(scanner)
        log.info(f"{log_prefix}MFT 索引加载完成: 文件={scanner.file_count}, 目录={scanner.dir_count}, "
                 f"耗时 {time.time()-t0:.2f} 秒")
        return scanner
    except Exception as e:
        log.warning(f"{log_prefix}MFT 加载失败，将使用 os.walk 兜底: {e}")
        try:
            from fast_scan import MftScanner
            from utils import set_mft_scanner
            fallback_scanner = MftScanner("C")
            fallback_scanner._fallback = True
            fallback_scanner._loaded = True
            set_mft_scanner(fallback_scanner)
            return fallback_scanner
        except Exception:
            return None


class ScanWorker(QObject):
    """异步扫描 - 不阻塞UI
    进度通过共享变量传递（主线程QTimer轮询），避免跨线程信号队列堆积导致进度条不动
    """
    progress_signal = Signal(str)       # 进度文本（低频，只用于状态栏文字）
    finished_signal = Signal(list, list)  # (migrated, scanned)
    error_signal = Signal(str)

    def __init__(self, migrator):
        super().__init__()
        self.migrator = migrator
        # 进度共享变量：主线程QTimer每100ms轮询读取，无需加锁（简单数值赋值在CPython中原子）
        self.current = 0
        self.total = 0
        self.dir_name = ""
        self._last_emit_pct = -1  # 状态栏文字更新频率控制
        # MFT 加载阶段标志（用于让进度条显示加载过程）
        self.mft_loading = False
        self.mft_current = 0
        self.mft_total = 0
        self.mft_message = ""
        # 扫描耗时记录
        self.scan_start_time = 0.0
        self.scan_elapsed = 0.0

    def run(self):
        # 后台线程中使用win32com（lnk读取）必须初始化COM
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception as e:
            log.debug("忽略异常: %s", e)
        try:
            import time
            self.scan_start_time = time.time()

            # 首次刷新时加载 MFT 索引（若未加载）
            self.progress_signal.emit("首次刷新：正在加载 MFT 索引...")
            self.mft_loading = True
            try:
                def mft_progress(current, total, message):
                    self.mft_current = current
                    self.mft_total = total
                    self.mft_message = message
                ensure_mft_loaded(progress_cb=mft_progress, log_prefix="[ScanWorker] ")
            finally:
                self.mft_loading = False

            # 记录 scan_migrated 和 scan_appdata 各阶段耗时
            t_migrated_start = time.time()
            self.progress_signal.emit("扫描已迁移目录...")
            migrated = self.migrator.scan_migrated()
            t_migrated_done = time.time()
            log.info(f"scan_migrated 完成: {len(migrated)} 条, 耗时 {t_migrated_done-t_migrated_start:.2f} 秒")
            self.progress_signal.emit(f"已迁移: {len(migrated)} 个，正在扫描C盘大目录...")

            def progress_cb(current, total, dir_name):
                # 只更新共享变量，不发射跨线程信号（主线程QTimer会轮询读取）
                self.current = current
                self.total = total
                self.dir_name = dir_name

            t_scan_start = time.time()
            scanned = self.migrator.scan_appdata(progress_cb=progress_cb)
            t_scan_done = time.time()
            log.info(f"scan_appdata 完成: {len(scanned)} 条, 耗时 {t_scan_done-t_scan_start:.2f} 秒")

            total_mb = sum(s["size_mb"] for s in scanned)
            self.scan_elapsed = time.time() - self.scan_start_time
            # 写入日志文件（logs/app.log）
            mft_mode = ""
            try:
                from utils import get_mft_scanner
                _s = get_mft_scanner()
                if _s is not None and _s.is_mft_mode:
                    mft_mode = " [MFT 模式]"
                elif _s is not None:
                    mft_mode = " [os.walk 模式]"
            except Exception as e:
                log.debug("忽略异常: %s", e)
            log.info(f"扫描完成{mft_mode}: 待迁移 {len(scanned)} 个 ({total_mb:.0f} MB), "
                     f"已迁移 {len(migrated)} 个, 耗时 {self.scan_elapsed:.2f} 秒")
            self.progress_signal.emit(
                f"扫描完成: 待迁移 {len(scanned)} 个 ({total_mb:.0f} MB), 耗时 {self.scan_elapsed:.2f} 秒")
            self.finished_signal.emit(migrated, scanned)
        except Exception as e:
            log_error_with_reason("扫描失败", str(e), "ScanWorker.run")
            self.error_signal.emit(str(e))
        finally:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception as e:
                log.debug("忽略异常: %s", e)


class SmartScanWorker(QObject):
    """智能刷新待迁移表 - 后台线程执行
    策略：listdir收集一级子目录，对比旧表格路径复用旧数据，仅新增目录计算大小
    进度通过共享变量传递（主线程QTimer轮询），避免跨线程信号队列堆积
    """
    progress_signal = Signal(str)                      # 进度文本（低频）
    finished_signal = Signal(list)                     # scanned results
    error_signal = Signal(str)

    def __init__(self, migrator, old_entries):
        """
        :param migrator: Migrator实例（用于读取已迁移记录）
        :param old_entries: dict - 旧表格缓存 {规范化路径(正斜杠小写): {"size_mb": float, "desc": str, "orig_path": str}}
        """
        super().__init__()
        self.migrator = migrator
        self.old_entries = old_entries
        # 进度共享变量：主线程QTimer轮询读取
        self.current = 0
        self.total = 0
        self.dir_name = ""

    @staticmethod
    def _norm_path(p):
        """规范化路径用于比较：正斜杠 + 小写 + 去尾斜杠"""
        return p.lower().replace("\\", "/").rstrip("/")
    @staticmethod
    def _is_vague_desc_fallback(desc):
        """判断说明是否笼统（本地副本，避免频繁跨模块导入）"""
        if not desc:
            return True
        if "相关" in desc:
            return True
        vague_words = ["应用数据", "缓存数据", "临时文件", "日志文件", "配置/设置数据"]
        if desc in vague_words:
            return True
        return False

    def run(self):
        # 后台线程中使用win32com（lnk读取）必须初始化COM，否则引发异常导致UI绘制冲突
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception as e:
            log.debug("忽略异常: %s", e)
        try:
            import time
            t_start = time.time()
            self.progress_signal.emit("智能扫描中...")
            # 确保 MFT 索引已加载（若未加载，加载需要 3-5 秒）
            # 不加载 MFT 会导致 get_dir_size_fast 走 os.walk 兜底，423 个目录会很慢
            self.progress_signal.emit("正在加载 MFT 索引...")
            ensure_mft_loaded(log_prefix="[SmartScanWorker] ")
            # 已迁移路径集合（规范化）
            migrated_srcs = set()
            for m in self.migrator.cfg.get("migrated", []):
                migrated_srcs.add(self._norm_path(m.get("src", "")))

            scan_dirs = get_scan_dirs()
            # 用户目录一级子目录的动态排除集合：已监控 base（AppData\Local/Roaming 等）
            # + 系统特殊文件夹（Known Folder API 解析，不硬编码目录名）
            _user_dir_monitored = get_monitored_base_norms(scan_dirs)
            _user_dir_known = get_known_folder_paths()

            # 第一阶段：listdir收集所有一级子目录（不计算大小，极快）
            candidates = []
            for base_path, label in scan_dirs:
                if not base_path or not os.path.exists(base_path):
                    continue
                try:
                    for entry in os.listdir(base_path):
                        full_path = os.path.join(base_path, entry)
                        if not os.path.isdir(full_path):
                            continue
                        # 用户目录下：动态排除已在监控列表的子目录（如 AppData\Local）
                        # 与系统特殊文件夹（桌面/文档/下载等），避免重复与误迁移
                        if label == USER_LABEL and is_user_dir_excluded(
                                self._norm_path(full_path), _user_dir_monitored, _user_dir_known):
                            continue
                        if is_symlink(full_path):
                            continue
                        if self._norm_path(full_path) in migrated_srcs:
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
                    log.debug("忽略异常: %s", e)

            total = len(candidates)
            new_count = 0
            reused_count = 0
            results = []
            t_size_total = 0.0
            t_desc_total = 0.0
            # desc 缓存（从 config.json 读取，避免每次扫描都重新识别软件描述）
            desc_cache = self.migrator.cfg.get("desc_cache", {}) if hasattr(self.migrator, 'cfg') else {}
            desc_hit_count = 0
            # 开发环境已配置的 C 盘源路径索引（与 scan_appdata 一致，[已配置] 橙色标记）
            dev_env_paths = build_dev_env_paths(getattr(self.migrator, 'cfg', None))

            # 第二阶段：对每个目录判断是否需要重新计算大小
            # 增量策略：对比目录 mtime，未变化直接复用旧 size_mb，不调 get_dir_size_fast
            # 只有新增目录和 mtime 变化的目录才重算大小（MFT O(1) 或 os.walk）
            reused_size_count = 0  # mtime 未变化，直接复用旧 size（跳过 IO）
            recalced_count = 0     # mtime 变化或新目录，重算大小
            for i, (full_path, entry, label) in enumerate(candidates):
                try:
                    # 更新共享进度变量（主线程QTimer轮询读取）
                    self.current = i + 1
                    self.total = total
                    self.dir_name = entry
                    norm = self._norm_path(full_path)
                    old = self.old_entries.get(norm)
                    # 获取当前目录 mtime（极快，一次 stat 调用）
                    try:
                        cur_mtime = os.path.getmtime(full_path)
                    except Exception:
                        cur_mtime = 0
                    if old:
                        # 旧目录：对比 mtime 决定是否重算大小
                        old_mtime = old.get("mtime", 0)
                        if cur_mtime and old_mtime and cur_mtime == old_mtime:
                            # mtime 未变化 → 直接复用旧 size，不调 get_dir_size_fast
                            size_mb = old.get("size_mb", 0)
                            reused_size_count += 1
                        else:
                            # mtime 变化或无旧 mtime → 重算大小
                            _t0 = time.time()
                            size_mb = get_dir_size_fast(full_path)
                            t_size_total += time.time() - _t0
                            recalced_count += 1
                        # desc 优先查缓存，缓存未命中用旧 desc，都无则空（异步补全）
                        _t0 = time.time()
                        desc = desc_cache.get(full_path, "") or old.get("desc", "")
                        if desc:
                            desc_hit_count += 1
                        t_desc_total += time.time() - _t0
                        reused_count += 1
                    else:
                        # 新目录：计算大小（MFT O(1)），desc 查缓存
                        _t0 = time.time()
                        size_mb = get_dir_size_fast(full_path)
                        t_size_total += time.time() - _t0
                        _t0 = time.time()
                        desc = desc_cache.get(full_path, "")
                        if desc:
                            desc_hit_count += 1
                        t_desc_total += time.time() - _t0
                        new_count += 1
                        recalced_count += 1
                    # 匹配开发环境已配置（规范化与 scan_appdata 一致：去 \\?\、小写、去尾 \）
                    fp_norm = full_path.replace("\\\\?\\", "").lower().rstrip("\\")
                    dev_cfg = dev_env_paths.get(fp_norm)
                    results.append({
                        "path": full_path,
                        "name": entry,
                        "location": label,
                        "size_mb": size_mb,
                        "desc": desc,
                        "mtime": cur_mtime,  # 记录 mtime 供下次增量对比
                        # 开发环境已配置标记（与 scan_appdata 字段一致，智能刷新时橙色标记也能亮）
                        "dev_env_configured": dev_cfg is not None,
                        "dev_env_target": dev_cfg.get("target_path", "") if dev_cfg else "",
                        "dev_env_name": dev_cfg.get("name", "") if dev_cfg else "",
                        "dev_env_drive": dev_cfg.get("target_drive", "") if dev_cfg else "",
                    })
                except Exception as e:
                    log.debug("忽略异常: %s", e)

            elapsed = time.time() - t_start
            # 检测 MFT 模式
            mft_mode = ""
            try:
                from utils import get_mft_scanner
                _s = get_mft_scanner()
                if _s is not None and _s.is_mft_mode:
                    mft_mode = " [MFT 模式]"
            except Exception as e:
                log.debug("忽略异常: %s", e)
            log.info(f"智能扫描完成{mft_mode}: 共{total}项 (复用{reused_count} 新增{new_count}), "
                     f"大小计算 {t_size_total:.2f} 秒 (mtime复用{reused_size_count} 重算{recalced_count}), "
                     f"描述查缓存 {t_desc_total:.2f} 秒 "
                     f"(命中 {desc_hit_count}/{total}), 总耗时 {elapsed:.2f} 秒")
            self.progress_signal.emit(
                f"智能扫描完成: 共{total}项 (复用{reused_count} 新增{new_count}), 耗时 {elapsed:.2f} 秒")
            self.scan_elapsed = elapsed  # 供主线程读取
            self.finished_signal.emit(results)
        except Exception as e:
            log_error_with_reason("智能扫描失败", str(e), "SmartScanWorker.run")
            self.error_signal.emit(str(e))
        finally:
            # 释放COM
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception as e:
                log.debug("忽略异常: %s", e)

# ========== 后台监控线程 ==========

class MonitorWorker(QObject):
    """实时监控 - watchdog文件系统监控 + 安装器进程检测"""
    log_signal = Signal(str, str)
    finished_signal = Signal()
    new_dir_signal = Signal(str, float)
    alert_signal = Signal(str, str)      # (title, message)
    # 用户目录写入提醒（右下角自定义气泡，不参与安装拦截）
    user_dir_alert_signal = Signal(str, str)  # (title, message)
    installer_signal = Signal(str)       # 安装器进程名
    # 系统级安装器拦截信号：(name, pid, exe, cmdline_str)
    # 主线程收到后弹窗询问用户：放行+信任 / 拒绝终止 / 稍后迁移
    installer_confirm_signal = Signal(str, int, str, str, str)  # name, pid, exe, cmdline, hit_keyword

    # ========== 智能拦截关键词分级 ==========
    # 设计理念：从"猜进程名"改为"看实际行为"
    # 强关键词：进程名含这些词几乎100%是安装器，启动即杀（配合5秒窗口）
    # 弱关键词：辅助进程常见词，单独命中不主动杀，等写入行为验证
    #
    # 误杀的根本原因：helper/updater/launcher 既是安装器辅助进程的特征，
    # 也是浏览器/聊天软件的常驻辅助进程特征。靠进程名无法区分，必须看行为。
    INSTALLER_STRONG_KEYWORDS = [
        'setup', 'installer', 'install',  # 注意：install 会匹配 installer，保留以保证单独 install 也能命中
        'wizard', 'bootstrap', 'agentsetup',
    ]
    # 弱关键词：不主动杀，只记录到"可疑进程池"，由 watchdog 写入行为触发反查
    INSTALLER_WEAK_KEYWORDS = [
        'updater', 'unzip', 'deploy', 'package',
        'launcher', 'config', 'assistant', 'helper',
    ]
    # 兼容旧代码：合并后的关键词列表（仅供 _is_installer_process 等历史逻辑使用）
    INSTALLER_KEYWORDS = INSTALLER_STRONG_KEYWORDS + INSTALLER_WEAK_KEYWORDS

    # 明确的系统级安装器进程名（第一阶段直接匹配，误杀风险低）
    # 只拦截真正的系统级安装器，脚本类（cmd/powershell/vbs/bat 等）默认放行
    # msiexec 需配合 _is_user_triggered_msiexec 排除系统服务
    PACKAGE_MANAGER_PROCS = {
        'winget.exe', 'choco.exe', 'scoop.exe', 'scoop.cmd',
        'msiexec.exe',
        # 扩展：常见包管理器/安装器进程名
        'inno_setup.exe', 'iscc.exe',   # Inno Setup
        'nsis.exe', 'makensis.exe',     # NSIS
        '7z.exe', '7zip.exe',           # 7-Zip 自解压
        'unrar.exe',                    # WinRAR 自解压
    }

    # 脚本类进程拦截已回退（脚本拦截无解决方案价值，无法保证下次装到非C盘）
    # 保留空集占位，避免后续引用报错；后续可考虑独立"开发环境路径迁移"功能页
    SCRIPT_PROCS_NEED_CMDLINE = set()
    # 默认白名单：包含这些词的进程不杀（系统更新等）
    # 恢复 helper/launcher/config/assistant 关键词后，需把已知非安装器的辅助进程加入白名单避免误杀
    DEFAULT_WHITELIST = [
        {"keyword": "windowsupdate", "desc": "Windows系统更新服务"},
        {"keyword": "windows defender", "desc": "Windows Defender杀毒"},
        {"keyword": "microsoftedgeupdate", "desc": "Edge浏览器更新"},
        {"keyword": "microsoft update", "desc": "微软更新服务"},
        {"keyword": "wuauclt", "desc": "Windows自动更新客户端"},
        {"keyword": "trustedinstaller", "desc": "Windows安装器信任服务"},
        {"keyword": "tiworker", "desc": "Windows更新工作进程"},
        {"keyword": "usoclient", "desc": "Windows更新协调器"},
        {"keyword": "dism", "desc": "Windows部署映像服务"},
        {"keyword": "system32", "desc": "Windows系统目录"},
        {"keyword": "microsoft\\windows", "desc": "微软Windows子目录"},
        {"keyword": "software\\distribution", "desc": "Windows更新下载目录"},
        {"keyword": "antivirus", "desc": "杀毒软件通用关键词"},
        {"keyword": "security", "desc": "安全软件通用关键词"},
        {"keyword": "defender", "desc": "Windows Defender"},
        {"keyword": "mcafee", "desc": "McAfee杀毒"},
        {"keyword": "norton", "desc": "诺顿杀毒"},
        {"keyword": "kaspersky", "desc": "卡巴斯基杀毒"},
        {"keyword": "avast", "desc": "Avast杀毒"},
        {"keyword": "avg", "desc": "AVG杀毒"},
        {"keyword": "bitdefender", "desc": "Bitdefender杀毒"},
        # 误杀防护：恢复 helper/launcher 等关键词后，需排除已知非安装器的辅助进程
        {"keyword": "steamwebhelper", "desc": "Steam浏览器辅助进程"},
        {"keyword": "identity_helper", "desc": "Edge身份验证辅助进程"},
        {"keyword": "crashhelper", "desc": "崩溃报告辅助进程（Firefox等）"},
        {"keyword": "gpuhelper", "desc": "GPU辅助进程（Chrome/Edge等）"},
        {"keyword": "cefhelper", "desc": "CEF框架辅助进程"},
        {"keyword": "browserhelper", "desc": "浏览器辅助进程"},
        {"keyword": "shellextension", "desc": "Shell扩展辅助进程"},
        {"keyword": "platform_experience_helper", "desc": "Chrome平台体验辅助进程"},
        {"keyword": "microsoftedge", "desc": "Edge浏览器相关进程"},
        {"keyword": "mozilla", "desc": "Mozilla/Firefox相关进程"},
        {"keyword": "google\\chrome", "desc": "Chrome浏览器相关进程"},
        {"keyword": "ffmpeghelper", "desc": "FFmpeg辅助进程（视频解码）"},
        # 以下为日志分析新增（2026-07-29）：含 helper 但非安装器的辅助进程
        {"keyword": "parfait-helper", "desc": "Parfait软件辅助进程"},
        {"keyword": "360seclogonhelper", "desc": "360安全登录辅助进程"},
        {"keyword": "qqocrhelper", "desc": "QQ OCR文字识别辅助进程"},
        {"keyword": "cleanhelper", "desc": "清理辅助进程"},
        {"keyword": "ksyshelper", "desc": "驱动精灵辅助进程"},
        # === 国内外通用软件更新服务（2026-07-29 补充）===
        # 国际软件更新服务
        {"keyword": "googleupdate", "desc": "Google软件更新服务"},
        {"keyword": "googlecrashhandler", "desc": "Google崩溃报告"},
        {"keyword": "adobeupdate", "desc": "Adobe软件更新"},
        {"keyword": "adobearm", "desc": "Adobe Reader更新"},
        {"keyword": "oracle", "desc": "Oracle Java更新"},
        {"keyword": "javaupdate", "desc": "Java更新服务"},
        {"keyword": "appleupdate", "desc": "Apple软件更新（iTunes/QuickTime）"},
        {"keyword": "softwareupdate", "desc": "通用软件更新服务"},
        {"keyword": "autoupdate", "desc": "通用自动更新服务"},
        # 国内常用软件更新/服务
        {"keyword": "tencent", "desc": "腾讯软件（QQ/微信等）"},
        {"keyword": "wechat", "desc": "微信相关进程"},
        {"keyword": "qqupdate", "desc": "QQ更新服务"},
        {"keyword": "baidu", "desc": "百度软件相关进程"},
        {"keyword": "alibaba", "desc": "阿里软件相关进程"},
        {"keyword": "alipay", "desc": "支付宝相关进程"},
        {"keyword": "taobao", "desc": "淘宝相关进程"},
        {"keyword": "dingtalk", "desc": "钉钉相关进程"},
        {"keyword": "wpscloud", "desc": "WPS云服务"},
        {"keyword": "kingsoft", "desc": "金山软件相关进程"},
        {"keyword": "sogou", "desc": "搜狗输入法相关进程"},
        {"keyword": "360update", "desc": "360软件更新"},
        {"keyword": "360tray", "desc": "360安全卫士托盘"},
        {"keyword": "360sd", "desc": "360杀毒"},
        {"keyword": "qhsafe", "desc": "360安全组件"},
        {"keyword": "tencentdl", "desc": "腾讯下载组件"},
        {"keyword": "tenio", "desc": "腾讯游戏组件"},
        {"keyword": "teshelp", "desc": "腾讯游戏辅助进程"},
        # 常见游戏平台/更新服务
        {"keyword": "steam.exe", "desc": "Steam主程序"},
        {"keyword": "epicgames", "desc": "Epic Games平台"},
        {"keyword": "battle.net", "desc": "暴雪战网"},
        {"keyword": "riotclient", "desc": "Riot客户端"},
        {"keyword": "gfe", "desc": "GeForce Experience"},
        # 常见开发工具更新/服务（非安装器）
        {"keyword": "code.exe", "desc": "VS Code主程序"},
        {"keyword": "codeupdate", "desc": "VS Code更新"},
        {"keyword": "jetbrains", "desc": "JetBrains IDE相关"},
        {"keyword": "jetbrainsupdater", "desc": "JetBrains更新"},
        # 系统/驱动更新类
        {"keyword": "nvidia", "desc": "NVIDIA驱动相关"},
        {"keyword": "amd", "desc": "AMD驱动相关"},
        {"keyword": "intel", "desc": "Intel驱动相关"},
        {"keyword": "realtek", "desc": "Realtek声卡驱动"},
        {"keyword": "driver", "desc": "驱动通用关键词"},
    ]

    def __init__(self, migrator, interval=60, threshold=50, auto_migrate=False):
        super().__init__()
        self.migrator = migrator
        self.interval = interval
        self.threshold = threshold
        self.auto_migrate = auto_migrate
        self.running = True
        self.known_dirs = set()
        # 用户目录写入提醒开关（右下角气泡；气泡点"不再提醒"或界面开关可热更新）
        self.user_dir_notify_enabled = migrator.cfg.get("user_dir_notify_enabled", True)
        # 已提醒过新建的用户目录一级子目录（规范化路径集合，每个目录只提醒一次）
        self._user_dir_seen = set()
        self._last_periodic = 0
        self._observer = None
        # 从config加载白名单（支持用户自定义）
        # 关键修复：DEFAULT_WHITELIST 是系统级不可杀白名单，必须始终生效
        # 旧逻辑 cfg.get("whitelist", DEFAULT_WHITELIST) 在 whitelist 为空列表时返回 []
        # 导致默认白名单全部失效（crashhelper/steamwebhelper 等被误拦截）
        # 正确逻辑：默认白名单（排除用户主动删除的）+ 用户自定义白名单合并
        user_whitelist = migrator.cfg.get("whitelist", []) or []
        # 用户删除的默认白名单关键词（重启后保持删除状态）
        removed_default_kws = set(
            kw.lower() for kw in migrator.cfg.get("removed_default_whitelist", []) or []
        )
        # 过滤掉用户删除的默认白名单条目
        effective_default = [
            w for w in self.DEFAULT_WHITELIST
            if (w.get("keyword", "").lower() not in removed_default_kws)
        ]
        self.whitelist = list(effective_default) + list(user_whitelist)
        # 暂停询问决策池：{pid: {"event": threading.Event, "decision": str|None}}
        self._pending_decisions = {}
        # 实例级锁：保护 _pending_decisions 的复合操作（注册/读取/清理）
        # 必须复用同一把锁，不能用 `with threading.Lock()` 每次新建（那样互斥形同虚设）
        self._pending_lock = threading.Lock()
        # 防止同一进程短时间内重复弹窗
        self._asked_pids = {}  # {pid: last_ask_ts}
        # 可疑进程池：弱关键词（helper/updater 等）进程记录于此
        # 不主动杀，等 watchdog 检测到 C 盘写入行为时反查此池
        # 格式：{pid: {"name":..., "exe":..., "create_time":..., "record_time":...}}
        self._suspicious_procs = {}
        # 最近启动进程缓存：主循环每 0.3 秒更新一次所有进程信息
        # watcher 触发 _kill_install_related_procs 时直接查缓存（<1ms），
        # 不再重新枚举376个进程（原 100-500ms 延迟的根源）
        # 格式：{pid: {"name":..., "exe":..., "create_time":..., "cmdline":..., "record_time":...}}
        self._recent_procs = {}
        # 最近创建的新目录（5秒窗口），用于文件写入兜底监控
        # 格式：{dir_path_lower: (create_time, killed_triggered)}
        self._recent_new_dirs = {}
        # 线程锁：保护 _suspicious_procs / _recent_procs / _recent_new_dirs
        # 6个watcher线程 + 主循环并发访问这三个字典，不加锁会导致
        # RuntimeError: dictionary changed size during iteration 或数据损坏
        self._suspicious_lock = threading.Lock()
        self._recent_lock = threading.Lock()
        self._dirs_lock = threading.Lock()

    def _save_blocked_processes(self):
        """统一保存 blocked_processes 到 state.json

        关键修复：blocked_processes 是 STATE_FIELDS（存入 state.json），
        不是 CONFIG_FIELDS。旧代码误用 save_config 只保存 CONFIG_FIELDS，
        导致 blocked_processes 永远不会被持久化到磁盘，重启后丢失。

        中危修复：改用读-改-写模式，只更新 blocked_processes 字段，
        不用内存中的 migrated/scan_cache 等字段全量覆盖 state.json，
        避免与迁移线程并发写入时覆盖对方未保存的更新。
        """
        try:
            from config import save_state, STATE_FILE
            import json as _json
            # 读取磁盘上最新的 state.json（避免用内存中可能过时的 migrated/scan_cache 覆盖）
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    disk_state = _json.load(f)
            except Exception:
                disk_state = {}
            # 只更新 blocked_processes，其他字段保持磁盘上的最新值
            disk_state["blocked_processes"] = self.migrator.cfg.get("blocked_processes", [])
            save_state(disk_state)
            return True
        except Exception as e:
            try:
                self.log_signal.emit("error", f"save_state 失败: {e}")
            except Exception as e:
                log.debug("忽略异常: %s", e)
            return False

    def _start_dir_watchers(self):
        """用 ReadDirectoryChangesW 监控 6 个关键目录，毫秒级响应目录创建

        替代 _periodic_check 轮询（1 秒延迟），实现"目录创建瞬间触发 kill"。

        原理：ReadDirectoryChangesW 是 Windows 内核级目录变化通知，目录创建瞬间
        内核就会推送事件，不需要轮询。非递归监控（recursive=False）只看一级子目录，
        不会像 watchdog 那样因递归监控大目录导致 CPU 100%。

        每个目录一个守护线程，同步阻塞等待（不占 CPU）。
        """
        import ctypes
        from ctypes import wintypes

        # 实时拦截监控目录：6 个关键目录（不含用户目录——用户目录新建不是安装行为，
        # 由独立 watcher 只做提醒，见下方"用户目录监控"）
        watch_dirs = [p for p, _ in get_scan_dirs(include_user=False)]

        # FILE_NOTIFY_INFORMATION 结构
        class FILE_NOTIFY_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("NextEntryOffset", wintypes.DWORD),
                ("Action", wintypes.DWORD),
                ("FileNameLength", wintypes.DWORD),
                # FileName 是变长 WCHAR[]，紧跟在结构后面
            ]

        FILE_LIST_DIRECTORY = 0x0001
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002   # 目录创建/删除/重命名
        FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001  # 文件创建/删除/重命名（兜底：目录创建瞬间的文件写入）
        FILE_ACTION_ADDED = 0x00000001               # 文件/目录创建
        FILE_ACTION_REMOVED = 0x00000002             # 文件/目录删除
        FILE_ACTION_MODIFIED = 0x00000003            # 文件修改（写入）
        FILE_ACTION_RENAMED_OLD_NAME = 0x00000004    # 重命名（旧名）
        FILE_ACTION_RENAMED_NEW_NAME = 0x00000005    # 重命名（新名）

        kernel32 = ctypes.windll.kernel32

        def watch_one_dir(directory, recursive=False, user_mode=False):
            """监控单个目录（守护线程函数）

            :param directory: 监控目录
            :param recursive: False=只监控一级子目录（性能优，主目录用），
                              True=递归监控所有子目录（关键安全目录用，如 Startup）
            :param user_mode: True=用户目录模式：新建目录只弹右下角提醒，
                              不参与安装拦截（不 kill、不写拦截记录、不弹安装警告）
            """
            if not directory or not os.path.exists(directory):
                return
            try:
                # 打开目录句柄
                handle = kernel32.CreateFileW(
                    directory,
                    FILE_LIST_DIRECTORY,
                    FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                    None,
                    OPEN_EXISTING,
                    FILE_FLAG_BACKUP_SEMANTICS,
                    None,
                )
                if handle == -1 or handle == 0:
                    self.log_signal.emit("error", f"ReadDirectoryChangesW 打开失败: {directory}")
                    return

                buffer = ctypes.create_string_buffer(8192)
                bytes_returned = wintypes.DWORD()

                while self.running:
                    try:
                        # 阻塞等待目录变化（同步调用，不占 CPU）
                        # 同时监控目录创建和文件创建/修改
                        # FILE_NOTIFY_CHANGE_DIR_NAME: 目录创建/删除/重命名（主力：安装器建目录）
                        # FILE_NOTIFY_CHANGE_FILE_NAME: 文件创建/删除/重命名（兜底：安装器往已存在目录写文件）
                        success = kernel32.ReadDirectoryChangesW(
                            handle,
                            buffer,
                            8192,
                            recursive,  # recursive 参数化：主目录 False，关键安全目录 True
                            FILE_NOTIFY_CHANGE_DIR_NAME | FILE_NOTIFY_CHANGE_FILE_NAME,
                            ctypes.byref(bytes_returned),
                            None,
                            None,
                        )
                        if not success or bytes_returned.value == 0:
                            continue

                        # 解析 buffer，提取所有事件
                        offset = 0
                        while offset < bytes_returned.value:
                            try:
                                entry = ctypes.cast(
                                    ctypes.addressof(buffer) + offset,
                                    ctypes.POINTER(FILE_NOTIFY_INFORMATION)
                                ).contents
                                # FileName 紧跟在结构后面，长度为 FileNameLength 字节
                                file_name_ptr = ctypes.cast(
                                    ctypes.addressof(entry) + 12,  # 3 个 DWORD = 12 字节
                                    ctypes.c_wchar_p
                                )
                                name_len = entry.FileNameLength // 2
                                try:
                                    name = file_name_ptr.value[:name_len] if file_name_ptr.value else ""
                                except Exception:
                                    name = ""
                                if name:
                                    full_path = os.path.join(directory, name)
                                    # 事件分类处理
                                    if entry.Action in (FILE_ACTION_ADDED, FILE_ACTION_RENAMED_NEW_NAME):
                                        if user_mode:
                                            # 用户目录模式：只做右下角提醒，不参与安装拦截
                                            try:
                                                self._on_user_dir_event(full_path, os.path.isdir(full_path))
                                            except Exception as e:
                                                self.log_signal.emit("error",
                                                    f"用户目录 watcher 处理 {full_path} 失败: {e}")
                                        # 目录创建或重命名 → 主力触发
                                        elif os.path.isdir(full_path):
                                            try:
                                                self._on_dir_created(full_path)
                                            except Exception as e:
                                                self.log_signal.emit("error",
                                                    f"watcher 处理目录 {full_path} 失败: {e}")
                                        # 文件创建 → 检查是否在"最近5秒新建的目录"下
                                        elif os.path.isfile(full_path):
                                            self._on_file_in_new_dir(full_path)
                                    elif entry.Action == FILE_ACTION_MODIFIED:
                                        # 文件修改（写入）→ 检查是否在"最近5秒新建的目录"下
                                        if user_mode:
                                            # 用户目录直接文件写入：仅日志，不打扰（高频）
                                            self._on_user_dir_event(full_path, False)
                                        elif os.path.isfile(full_path):
                                            self._on_file_in_new_dir(full_path)
                                    elif entry.Action in (FILE_ACTION_REMOVED, FILE_ACTION_RENAMED_OLD_NAME):
                                        # 文件/目录删除或重命名 → 触发删除拦截
                                        # 修复漏洞：原代码完全不处理删除事件，导致病毒可随意删除文件
                                        if not user_mode:
                                            try:
                                                self._on_file_removed(full_path)
                                            except Exception as e:
                                                self.log_signal.emit("error",
                                                    f"watcher 处理删除 {full_path} 失败: {e}")
                                # 下一个事件
                                if entry.NextEntryOffset == 0:
                                    break
                                offset += entry.NextEntryOffset
                            except Exception:
                                break
                    except Exception as e:
                        if self.running:
                            try:
                                self.log_signal.emit("error", f"watcher {directory} 异常: {e}")
                            except Exception as e:
                                log.debug("忽略异常: %s", e)
                        break

                try:
                    kernel32.CloseHandle(handle)
                except Exception as e:
                    log.debug("忽略异常: %s", e)
            except Exception as e:
                self.log_signal.emit("error", f"watcher 启动失败 {directory}: {e}")

        # 启动 6 个守护线程，各监控一个目录（recursive=False，性能优）
        main_count = 0
        for d in watch_dirs:
            if d and os.path.exists(d):
                t = threading.Thread(target=watch_one_dir, args=(d, False), daemon=True)
                t.start()
                main_count += 1

        # 用户目录监控（不参与安装拦截）：一级子目录新建 → 右下角气泡提醒
        user_profile = os.environ.get("USERPROFILE", "")
        if user_profile and os.path.exists(user_profile):
            t = threading.Thread(target=watch_one_dir, args=(user_profile, False, True), daemon=True)
            t.start()
            main_count += 1

        # 关键安全目录：递归监控（recursive=True），捕获深层子目录的删除/篡改行为
        # 这些目录文件少、层级深，recursive=True 不会导致 CPU 爆炸
        # 修复漏洞：原 recursive=False 无法监控深层子目录，导致 Startup 等目录删除行为漏报
        critical_dirs = [
            os.path.join(os.environ.get("APPDATA", ""),
                         r"Microsoft\Windows\Start Menu\Programs\Startup"),
            os.path.join(os.environ.get("ProgramData", ""),
                         r"Microsoft\Windows\Start Menu\Programs\Startup"),
        ]
        crit_count = 0
        for d in critical_dirs:
            if d and os.path.exists(d):
                t = threading.Thread(target=watch_one_dir, args=(d, True), daemon=True)
                t.start()
                crit_count += 1

        self.log_signal.emit("init",
            f"已启动 {main_count} 个主目录监控（recursive=False）+ "
            f"{crit_count} 个关键安全目录监控（recursive=True，含 Startup）")

    def run(self):
        # 1. 用 ReadDirectoryChangesW 监控目录创建（毫秒级响应）
        #    + psutil 进程拦截（0.3 秒轮询，捕获安装器进程名）
        self.log_signal.emit("init", "启动监控（ReadDirectoryChangesW 目录监控 + psutil 进程拦截）")
        self.log_signal.emit("init", "目录监控：6 个关键目录，毫秒级响应目录创建")
        self._observer = None  # 不再使用watchdog

        # 2. 轻量初始化已知目录（只listdir收集目录名，不获取大小/说明，避免遍历大目录卡死）
        self.log_signal.emit("init", "初始化已知目录...")
        init_scan_dirs = [p for p, _ in get_scan_dirs(include_user=False)]
        for base_path in init_scan_dirs:
            if not base_path or not os.path.exists(base_path):
                continue
            try:
                for entry in os.listdir(base_path):
                    full_path = os.path.join(base_path, entry)
                    if os.path.isdir(full_path):
                        self.known_dirs.add(full_path)
            except Exception as e:
                log.debug("忽略异常: %s", e)
        for m in self.migrator.scan_migrated():
            self.known_dirs.add(m["src"])
        self.log_signal.emit("init", f"已知 {len(self.known_dirs)} 个目录，初始化完成")

        # 3. 不再使用WMI（会导致CPU 100%空转），改用psutil轮询
        #    psutil轮询优化：只查name（无需打开句柄），匹配后再查exe/cmdline
        self.log_signal.emit("init", "进程拦截：psutil轮询模式（0.3秒间隔，优化版）")

        # 4. 启动 ReadDirectoryChangesW 目录监控（毫秒级响应，替代 _periodic_check 轮询）
        self._start_dir_watchers()

        # 5. 主循环：psutil 进程拦截 + _periodic_check 兜底（30秒，防止 watcher 漏事件）
        # 进程拦截：0.3 秒间隔（CreateToolhelp32Snapshot < 10ms，0.3 秒间隔无压力）
        # 目录兜底：30 秒间隔（watcher 是主力，_periodic_check 只是备份，避免频繁 listdir）
        last_process_check = 0
        last_dirs_cleanup = 0
        PERIODIC_INTERVAL = 30  # 兜底扫描间隔（watcher 是主力）
        PROCESS_CHECK_INTERVAL = 0.3
        DIRS_CLEANUP_INTERVAL = 10  # _recent_new_dirs 清理间隔
        while self.running:
            try:
                now = time.time()
                # 每 0.3 秒检测一次安装器进程
                if now - last_process_check >= PROCESS_CHECK_INTERVAL:
                    last_process_check = now
                    self._check_installer_processes()
                # 每 30 秒做一次兜底目录检查（防止 watcher 漏事件）
                if now - self._last_periodic >= PERIODIC_INTERVAL:
                    self._last_periodic = now
                    self._periodic_check()
                # 每 10 秒清理一次 _recent_new_dirs（防止内存泄漏）
                # 正常情况下 _on_file_in_new_dir 会清理，但如果5秒内没有
                # 文件写入事件触发，_recent_new_dirs 中的记录不会被清理
                if now - last_dirs_cleanup >= DIRS_CLEANUP_INTERVAL:
                    last_dirs_cleanup = now
                    try:
                        with self._dirs_lock:
                            expired = [k for k, v in self._recent_new_dirs.items()
                                      if now - v[0] > 5]
                            for k in expired:
                                self._recent_new_dirs.pop(k, None)
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
            except Exception as e:
                # 防止异常导致监控线程崩溃
                try:
                    self.log_signal.emit("error", f"监控循环异常: {type(e).__name__}: {e}")
                except Exception as e:
                    log.debug("忽略异常: %s", e)
            time.sleep(0.1)

        # 清理
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception as e:
                log.debug("忽略异常: %s", e)
        self.finished_signal.emit()

    def _on_file_in_new_dir(self, file_path):
        """文件写入兜底监控：检测到文件创建/修改时，检查是否在"最近5秒新建的目录"下

        核心场景：安装器先创建目录 C:\\Program Files\\XXX\\，然后立即往里写文件。
        如果目录创建瞬间的 kill 失败（如权限不足），文件写入事件作为兜底再次触发 kill。

        关键防抖：
        1. 只处理"最近5秒新建目录"下的文件写入，不处理已有目录的文件写入
           （如浏览器写 Cache、QQ 写聊天记录等），避免 CPU 爆炸和误杀
        2. 每个新建目录5秒内只触发一次 kill（目录级去重）
           安装器往一个目录写100个文件，只触发1次 kill，不重复枚举进程
        """
        try:
            now = time.time()
            file_dir = os.path.dirname(file_path).lower().rstrip("\\")
            # 线程安全：先检查，再决定是否处理（加锁在 _trigger_kill_for_dir 中）
            with self._dirs_lock:
                # 清理过期的 _recent_new_dirs 记录（超过5秒的）
                expired = [k for k, v in self._recent_new_dirs.items()
                          if now - v[0] > 5]
                for k in expired:
                    self._recent_new_dirs.pop(k, None)
                # 检查文件所在目录是否在 _recent_new_dirs 中
                if file_dir not in self._recent_new_dirs:
                    return  # 不在新建目录下，忽略（浏览器缓存等正常写入）
                # 目录级去重：标记此目录已触发过 kill，5秒内不再重复触发
                # _recent_new_dirs 的值格式：(create_time, killed_triggered)
                create_time, killed_triggered = self._recent_new_dirs[file_dir]
                if killed_triggered:
                    return  # 此目录已触发过 kill，不再重复
                # 标记为已触发
                self._recent_new_dirs[file_dir] = (create_time, True)
            # 在新建目录下首次检测到文件写入 → 触发 kill（锁外执行，避免长时间持锁）
            self.log_signal.emit("warn",
                f"新目录下检测到文件写入(兜底触发): {file_path}")
            try:
                killed = self._kill_install_related_procs(file_dir)
            except Exception as e:
                killed = []
                self.log_signal.emit("error", f"文件写入兜底 kill 异常: {e}")
        except Exception as e:
            try:
                self.log_signal.emit("error", f"_on_file_in_new_dir 异常: {e} path={file_path}")
            except Exception as e:
                log.debug("忽略异常: %s", e)

    def _on_file_removed(self, path):
        """文件/目录删除或重命名触发 - 审计记录 + 告警（不 kill）

        设计决策（基于实测）：
        原计划在删除事件触发时调用 _kill_install_related_procs 拦截恶意删除，
        但实测发现两个根本性问题导致 kill 逻辑无意义：

        1. ReadDirectoryChangesW 是"事后通知"——文件已被删除后才收到事件，
           无法阻止删除行为本身，只能事后 kill 进程防止继续删。
        2. _kill_install_related_procs 的判据是为"安装器"设计的（强关键词、
           exe 在 %TEMP%、可疑进程池），病毒/木马进程名千变万化，
           命中不了判据，kill 不到目标。

        因此本函数定位为"审计 + 告警"：
        - 记录删除行为到监控日志（warn 级别）
        - 记录到 blocked_processes（审计日志，出问题能查）
        - 不调用 kill 逻辑（避免误杀系统进程）
        - 不弹 alert（避免正常删除文件时弹窗骚扰用户）

        保留此函数的价值：
        - 用户可从监控日志查看"何时何文件被删"
        - blocked_processes 留下完整审计轨迹
        - 如未来发现可疑删除模式，可基于此扩展
        """
        # 步骤1：基础检查（排除正常清理行为，避免审计日志被污染）
        try:
            path_lower = path.lower()
            # 本工具日志目录下的文件删除不记录
            if "\\logs\\" in path_lower and "cdriverelocator" in path_lower:
                return
            # 还原操作中的路径跳过（本工具自身在删符号链接/复制数据，不是恶意删除）
            restoring_set = set(self.migrator.cfg.get("restoring_in_progress", []))
            if any(path_lower == s.lower() or path_lower.startswith(s.lower() + "\\")
                   for s in restoring_set):
                return
            # Windows 临时文件（~开头、.tmp 后缀）不记录
            base_name = os.path.basename(path)
            if base_name.startswith("~") or base_name.lower().endswith(".tmp"):
                return
            # .log 文件不记录（日志轮转正常删除）
            if path_lower.endswith(".log") or path_lower.endswith(".log.1"):
                return
        except Exception as e:
            try:
                self.log_signal.emit("error", f"_on_file_removed 步骤1(基础检查)异常: {e}")
            except Exception as e:
                log.debug("忽略异常: %s", e)
            return

        # 步骤2：记录删除行为到监控日志（审计用途）
        try:
            msg = f"检测到删除行为: {path}"
            self.log_signal.emit("warn", msg)
        except Exception as e:
            try:
                self.log_signal.emit("error", f"_on_file_removed 步骤2(日志emit)异常: {e}")
            except Exception as e:
                log.debug("忽略异常: %s", e)

        # 步骤3：记录到 blocked_processes（审计日志）
        # 不 kill、不弹 alert —— 避免误杀系统进程 + 避免正常删除时骚扰用户
        try:
            blocked = self.migrator.cfg.setdefault("blocked_processes", [])
            blocked.append({
                "name": os.path.basename(path),
                "pid": 0,
                "exe": path,
                "cmdline": "",
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source": "file_removed"  # 标记来源是删除/重命名监控
            })
            if len(blocked) > 200:
                self.migrator.cfg["blocked_processes"] = blocked[-200:]
                blocked = self.migrator.cfg["blocked_processes"]
            self._save_blocked_processes()
            self.log_signal.emit("installer",
                f"已记录删除行为到拦截日志: {os.path.basename(path)} (共{len(blocked)}条)")
        except Exception as e:
            try:
                self.log_signal.emit("error", f"写入blocked_processes失败(删除): {e}")
            except Exception as e:
                log.debug("忽略异常: %s", e)

    def _on_user_dir_event(self, path, is_dir=False):
        """用户目录一级子目录事件 → 右下角气泡提醒（不参与安装拦截）

        只提醒"一级子目录新建"（AI 工具等新目录吃 C 盘的典型信号），
        每个目录只提醒一次（_user_dir_seen 去重，防重复弹窗）；
        用户目录下的直接文件写入/删除事件不打扰用户（高频），仅由日志记录。

        限制（ReadDirectoryChangesW recursive=False）：
        深层孙目录写入（如 .ollama\\models\\xxx）不产生事件，
        膨胀可见性由待迁移区扫描兜底。
        """
        try:
            # 文件写入/删除事件（高频，且 recursive=False 下只涉及用户目录直接文件）
            # 直接忽略，只关心一级子目录新建
            if not is_dir:
                return
            # 新建 junction 不算数据膨胀（链接实体 0 字节，数据在目标盘），不提醒
            if is_junction(path):
                return
            if not self.user_dir_notify_enabled:
                return
            user_profile = os.environ.get("USERPROFILE", "")
            if not user_profile or not path:
                return
            # 只看用户目录一级子目录的新建（parent 恰好等于用户目录）
            norm = path.replace("\\", "/").lower().rstrip("/")
            up_norm = user_profile.replace("\\", "/").lower().rstrip("/")
            if os.path.dirname(path).replace("\\", "/").lower().rstrip("/") != up_norm:
                return
            if norm in self._user_dir_seen:
                return
            self._user_dir_seen.add(norm)
            self.log_signal.emit("new", f"用户目录新建目录: {path}")
            self.user_dir_alert_signal.emit(
                "用户目录写入提醒",
                f"检测到用户目录 {user_profile} 下新建目录：\n{path}\n\n"
                f"可在待迁移区查看并迁移，或点击气泡\"不再提醒\"关闭")
        except Exception as e:
            log.debug("忽略异常: %s", e)

    def _on_dir_created(self, path):
        """目录创建瞬间触发 - 立即弹窗
        无论是否开启拦截，检测到安装行为都记录到 blocked_processes

        关键修复：
        1. blocked_processes 是 STATE_FIELDS，必须用 save_state 持久化（旧代码误用 save_config 导致丢失）
        2. 每一步独立 try/except，避免单点异常导致整个函数被外层吞掉
        3. 添加详细调试日志，定位失败点
        """
        # 步骤1：基础检查（不会抛异常，但防御性 try/except）
        try:
            if path in self.known_dirs:
                return
            if is_symlink(path):
                return
            self.known_dirs.add(path)
            dir_name = os.path.basename(path)
            # 记录到 _recent_new_dirs，供文件写入兜底监控使用（5秒窗口）
            # 值格式：(create_time, killed_triggered)，初始 killed_triggered=False
            with self._dirs_lock:
                self._recent_new_dirs[path.lower().rstrip("\\")] = (time.time(), False)
        except Exception as e:
            self.log_signal.emit("error", f"_on_dir_created 步骤1(基础检查)异常: {e} path={path}")
            return

        # 步骤2：立即记录"新目录创建"日志（不等任何后续操作）
        try:
            msg = f"新目录创建: {path}"
            self.log_signal.emit("new", msg)
        except Exception as e:
            self.log_signal.emit("error", f"_on_dir_created 步骤2(日志emit)异常: {e}")

        # 步骤3：获取目录大小（可能慢，独立 try/except）
        size = 0.0
        try:
            size = get_dir_size_fast(path)
            # new_dir_signal 需要float参数，强制转换避免类型错误
            self.new_dir_signal.emit(path, float(size))
        except Exception as e:
            self.log_signal.emit("error", f"_on_dir_created 步骤3(获取大小)异常: {e} path={path}")

        # 步骤4：判断安装行为并弹窗（独立 try/except，避免alert失败影响后续记录）
        try:
            parent = os.path.dirname(path)
            # 修复 H10-①：原 endswith("Programs") 会命中 ...\Local\Programs
            # VS Code/微信等正常安装到 %LOCALAPPDATA%\Programs 会被误判为安装行为并 kill
            # 改为精确匹配：parent 必须就是 "Program Files"/"Program Files (x86)"，
            # 或者是系统级 Programs 目录（C:\Programs 这种少见但合法的安装根）
            # 用户级 %LOCALAPPDATA%\Programs 不再触发自动拦截（那是正常安装位置）
            parent_norm = parent.lower().rstrip("\\")
            is_install_behavior = (
                parent_norm in ("c:\\program files", "c:\\program files (x86)")
                or parent_norm == "c:\\programs"
            )
            if is_install_behavior:
                # 自动拦截开关检查：关闭时只发轻量 alert 提示用户，绝不 kill 进程、不清理目录
                # （kill/清理是"拦截"的核心动作，必须受 auto_migrate 开关控制）
                if not self.auto_migrate:
                    self.log_signal.emit("warn",
                        f"检测到安装行为(未拦截): {path}  自动拦截已关闭")
                    self.alert_signal.emit(
                        "检测到软件安装（未拦截）",
                        f"安装目录下新建: {dir_name}\n路径: {path}\n\n"
                        f"自动拦截已关闭，未杀进程、未清理目录。\n"
                        f"如需拦截，请开启「自动拦截」开关。"
                    )
                else:
                    # 用户要求：工作的一瞬间就干掉，先 kill 再弹窗
                    # 先 kill 所有最近启动的非系统进程，再发 alert
                    try:
                        killed = self._kill_install_related_procs(path)
                    except Exception as e:
                        killed = []
                        self.log_signal.emit("error", f"安装触发 kill 异常: {e}")

                    # kill 后立即清理残留目录（安装器在 kill 前可能已写入部分文件）
                    # 复用 migrator.py 的三级删除策略：rd /s /q → shutil.rmtree → 重命名 .bak
                    residue_removed = False
                    try:
                        if os.path.exists(path):
                            residue_removed = self._cleanup_residue(path)
                            if residue_removed:
                                self.log_signal.emit("kill",
                                    f"已清理安装残留目录: {path}")
                                # 从 known_dirs 移除（路径已被删除或重命名）
                                self.known_dirs.discard(path)
                    except Exception as e:
                        self.log_signal.emit("error", f"清理残留目录异常: {e}")

                    # kill 完成后再发 alert（避免 alert 弹窗阻塞 kill）
                    if killed:
                        extra = f"\n已清理残留文件: {'是' if residue_removed else '否（需手动删除）'}"
                        self.alert_signal.emit(
                            "已拦截安装行为！",
                            f"触发目录: {path}\n已 kill {len(killed)} 个进程{extra}\n\n"
                            f"进程已被强制终止，残留文件已清理，防止继续向 C 盘安装。"
                        )
                    else:
                        self.alert_signal.emit(
                            "检测到软件安装！",
                            f"安装目录下新建: {dir_name}\n路径: {path}\n\n"
                            f"未找到可疑进程（可能已退出）。"
                        )
            elif parent == r"C:\ProgramData":
                self.alert_signal.emit(
                    "ProgramData新目录",
                    f"ProgramData下新建: {dir_name}\n路径: {path}\n\n可能是软件在写入共享数据。"
                )
            elif size >= self.threshold:
                self.alert_signal.emit(
                    "C盘发现大目录",
                    f"{path}\n大小: {size} MB\n\n可右键选择'迁移到指定位置'"
                )
        except Exception as e:
            self.log_signal.emit("error", f"_on_dir_created 步骤4(弹窗)异常: {e}")

        # 步骤5：记录到 blocked_processes（供白名单"从拦截日志添加"使用）
        # 关键修复：blocked_processes 是 STATE_FIELDS，必须用 save_state 持久化
        # 旧代码用 save_config(self.migrator.cfg) 只保存 CONFIG_FIELDS，blocked_processes 不会被写入文件
        try:
            blocked = self.migrator.cfg.setdefault("blocked_processes", [])
            blocked.append({
                "name": dir_name,
                "pid": 0,  # 文件系统监控没有PID，用0标记
                "exe": path,
                "cmdline": "",  # 文件系统监控无cmdline
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source": "dir_created"  # 标记来源是文件系统监控
            })
            # 只保留最近200条
            if len(blocked) > 200:
                self.migrator.cfg["blocked_processes"] = blocked[-200:]
                blocked = self.migrator.cfg["blocked_processes"]
            # 修复：blocked_processes 是 STATE_FIELDS，用 _save_blocked_processes 持久化到 state.json
            self._save_blocked_processes()
            # 调试日志：确认已写入blocked_processes
            self.log_signal.emit("installer", f"已记录新目录到拦截日志: {dir_name} (共{len(blocked)}条)")
        except Exception as e:
            self.log_signal.emit("error", f"写入blocked_processes失败(目录): {e}")

    def _kill_install_related_procs(self, trigger_path):
        """检测到安装目录新建时，立即杀掉正在写入的进程（零延迟优化版）

        核心优化：查缓存优先，避免每次枚举376个进程（原100-500ms延迟）
        - 第一优先级：查 _suspicious_procs 可疑进程池（<1ms）
        - 第二优先级：查 _recent_procs 缓存中的强关键词进程（<1ms+少量psutil）
        - 第三优先级：兜底枚举（只在以上都没找到时，100-500ms）

        判定 kill 的条件（任一命中即 kill）：
        1. 可疑进程池中的弱关键词进程，有写入行为（write_count >= 1）
        2. 强关键词进程（setup/install 等），最近30秒启动
        3. exe 在 %TEMP% 下（自解压安装器特征）
        4. exe 在 trigger_path 下（安装器装到目标目录）
        5. 命令行引用 trigger_path（兜底枚举时才查）
        """
        import psutil
        import time as _time
        now = _time.time()
        killed = []
        temp_dir = os.environ.get("TEMP", "").lower().rstrip("\\")
        trigger_lower = (trigger_path or "").lower().rstrip("\\")

        # 系统关键进程和本工具自身不杀（按名字快速过滤）
        SYSTEM_WHITELIST = (
            "python.exe", "pythonw.exe", "cdriverelocator", "c盘拦迁器",
            "c-drive-guard", "trae", "code.exe",
            "rust-migrate-engine",  # P6:本工具的 Rust 复制引擎子进程
            "explorer.exe", "dwm.exe", "csrss.exe", "winlogon.exe",
            "services.exe", "lsass.exe", "wininit.exe", "smss.exe",
            "svchost.exe", "spoolsv.exe", "fontdrvhost.exe", "sihost.exe",
            "taskhostw.exe", "ctfmon.exe", "conhost.exe",
            "taskkill.exe",
            "system", "registry", "idle", "memory compression",
            "lsaiso.exe", "wudfhost.exe", "vmms.exe", "vmcompute.exe",
            "dashost.exe", "wmiprvse.exe", "searchindexer.exe", "audiodg.exe",
            "runtimebroker.exe", "backgroundtaskhost.exe", "searchhost.exe",
            "searchui.exe", "searchapp.exe", "textinputhost.exe", "chsime.exe",
            "useroobebroker.exe", "helppane.exe", "prevhost.exe", "comppkgsrv.exe",
            "dllhost.exe", "msedgewebview2.exe",
        )

        # ===== 第一优先级：查可疑进程池（<1ms）=====
        # 这些是弱关键词进程（helper/updater 等），已在主循环中记录
        # 验证写入行为：write_count >= 1 即杀（1字节也杀）
        # 线程安全：拷贝一份后释放锁，避免 kill 过程中长时间持锁
        with self._suspicious_lock:
            suspicious_snapshot = list(self._suspicious_procs.items())
        for pid, info in suspicious_snapshot:
            try:
                # 过期进程跳过（超过120秒）
                if now - info.get("record_time", 0) > 120:
                    continue
                name = info.get("name", "")
                exe = info.get("exe", "")
                name_lower = name.lower()
                # 排除系统进程
                if any(h in name_lower for h in SYSTEM_WHITELIST):
                    continue
                # 排除白名单进程
                if self._is_in_whitelist(name_lower, exe.lower(), ''):
                    continue
                # 验证写入行为（对比记录时的增量，非累计量）
                proc = psutil.Process(pid)
                init_wb = info.get("init_write_bytes", -1)
                has_write, write_reason = self._verify_write_behavior(proc, init_wb)
                if has_write:
                    self._do_kill_installer(proc, name, pid, exe,
                        f"可疑进程有写入行为({write_reason})", killed)
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                log.debug("忽略异常: %s", e)
            except Exception as e:
                log.debug("忽略异常: %s", e)

        # ===== 第二优先级：查 _recent_procs 缓存中的强关键词进程 =====
        # _recent_procs 由主循环每0.3秒更新，包含所有进程的 pid+name
        # 只对强关键词进程查 psutil（很少，<5个），避免枚举所有进程
        # 线程安全：拷贝一份后释放锁
        with self._recent_lock:
            recent_snapshot = list(self._recent_procs.items())
        for pid, info in recent_snapshot:
            try:
                name = info.get("name", "")
                name_lower = name.lower()
                # 只处理强关键词进程（setup/install 等）
                hit_strong = False
                for kw in self.INSTALLER_STRONG_KEYWORDS:
                    if kw in name_lower:
                        hit_strong = True
                        break
                if not hit_strong:
                    continue
                # 排除系统进程
                if any(h in name_lower for h in SYSTEM_WHITELIST):
                    continue
                # 查 psutil 获取 exe/create_time（只对强关键词进程查，很少）
                proc = psutil.Process(pid)
                create_t = proc.create_time()
                # 30秒窗口外的跳过
                if now - create_t > 30:
                    continue
                exe = (proc.exe() or '').lower()
                # 排除系统目录
                if exe.startswith('c:\\windows') or '\\system32\\' in exe or '\\syswow64\\' in exe:
                    continue
                # 排除白名单
                try:
                    cmdline_str = ' '.join(proc.cmdline()).lower()
                except Exception:
                    cmdline_str = ""
                if self._is_in_whitelist(name_lower, exe, cmdline_str):
                    continue
                # 卸载行为不拦截
                if self._is_uninstall_cmdline(cmdline_str):
                    continue
                # kill 强关键词进程
                self._do_kill_installer(proc, name, pid, exe,
                    f"强关键词+最近30秒启动", killed)
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                log.debug("忽略异常: %s", e)
            except Exception as e:
                log.debug("忽略异常: %s", e)

        # ===== 第三优先级：兜底枚举（总是执行）=====
        # 处理既不在可疑进程池、也不是强关键词的写入者（如 exe 在 %TEMP% 下的自解压安装器）
        # 不用 if not killed：前两级可能杀了 helper 但没杀主安装器，兜底要继续找
        try:
            for proc in psutil.process_iter(['pid', 'name', 'exe', 'create_time']):
                try:
                    info = proc.info
                    name = info.get('name') or ''
                    exe = (info.get('exe') or '').lower()
                    create_t = info.get('create_time') or 0
                    pid = info.get('pid')
                    if not pid or not name:
                        continue
                    name_lower = name.lower()
                    # 排除系统关键进程
                    if any(h in name_lower for h in SYSTEM_WHITELIST):
                        continue
                    # 排除 C:\Windows 下的进程
                    if exe.startswith('c:\\windows') or '\\system32\\' in exe or '\\syswow64\\' in exe:
                        continue
                    # 只处理最近60秒启动的进程（老进程不可能是新安装的触发者）
                    if create_t and now - create_t > 60:
                        continue
                    # 行为判据1：exe 在 %TEMP% 下
                    if temp_dir and exe.startswith(temp_dir):
                        if self._is_in_whitelist(name_lower, exe, ''):
                            continue
                        self._do_kill_installer(proc, name, pid, exe,
                            "exe在临时目录(兜底枚举)", killed)
                        continue
                    # 行为判据2：exe 在 trigger_path 下
                    # 修复 H10-③：判据2/3原无白名单检查，会误杀白名单进程
                    if trigger_lower and exe.startswith(trigger_lower):
                        if self._is_in_whitelist(name_lower, exe, ''):
                            continue
                        self._do_kill_installer(proc, name, pid, exe,
                            "exe在安装目标目录(兜底枚举)", killed)
                        continue
                    # 行为判据3：命令行引用 trigger_path
                    try:
                        cmdline_str = ' '.join(proc.cmdline()).lower()
                    except Exception:
                        cmdline_str = ""
                    if trigger_lower and trigger_lower in cmdline_str:
                        if self._is_in_whitelist(name_lower, exe, cmdline_str):
                            continue
                        self._do_kill_installer(proc, name, pid, exe,
                            "命令行引用目标路径(兜底枚举)", killed)
                        continue
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as e:
            self.log_signal.emit("error", f"_kill_install_related_procs 兜底枚举异常: {e}")

        if killed:
            names = ', '.join(f"{n}({p})" for n, p, _ in killed[:10])
            try:
                self.alert_signal.emit(
                    "已拦截安装行为！",
                    f"触发目录: {trigger_path}\n已 kill {len(killed)} 个安装器进程:\n{names}\n\n"
                    f"进程已被强制终止，防止继续向 C 盘安装。"
                )
            except Exception as e:
                log.debug("忽略异常: %s", e)
        else:
            self.log_signal.emit("warn",
                f"安装行为触发但未找到可疑进程(可能已退出): {trigger_path}")
        return killed

    def _verify_write_behavior(self, proc, init_write_bytes=-1):
        """验证进程是否有实际写入行为（行为驱动的核心判据）

        修复 H10-②：原代码用 io.write_bytes（进程累计写入量）判据，
        helper 进程（浏览器/QQ/Steam）运行期间累计写配置超 4KB 即被误杀。
        现改为对比增量：当前 write_bytes - 记录时 init_write_bytes >= 4KB 才判为写入。
        这样只有"被记录为可疑后才新写入 4KB+"的进程才会被杀，
        排除了历史累计写入的干扰。

        :param proc: psutil.Process 实例
        :param init_write_bytes: 记录到可疑池时的初始 write_bytes（-1 表示获取失败）
        :return: (has_write: bool, reason: str)
        """
        try:
            io = proc.io_counters()
            if init_write_bytes < 0:
                # 初始值获取失败，保守不杀（避免误杀）
                return False, ""
            delta = io.write_bytes - init_write_bytes
            if delta >= 4096:
                return True, f"write_delta={delta // 1024}KB(累计{io.write_bytes // 1024}KB)"
            return False, ""
        except Exception:
            return False, ""

    def _do_kill_installer(self, proc, name, pid, exe, reason, killed_list):
        """执行 kill 安装器进程的公共逻辑（避免代码重复）

        :param proc: psutil.Process 实例
        :param name: 进程名
        :param pid: PID
        :param exe: exe 路径
        :param reason: kill 原因（用于日志）
        :param killed_list: 已 kill 列表（追加到此列表）
        """
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=2,
                           creationflags=_NO_WINDOW_FLAGS)
            killed_list.append((name, pid, exe))
            self.log_signal.emit("kill",
                f"安装行为触发 kill: {name} (PID:{pid}) 原因={reason} exe={exe}")
        except Exception as e:
            self.log_signal.emit("error", f"kill {name}(PID:{pid}) 失败: {e}")
        # 从可疑进程池中移除已处理的 PID
        try:
            self._suspicious_procs.pop(pid, None)
        except Exception as e:
            log.debug("忽略异常: %s", e)

    def _cleanup_residue(self, path):
        """kill 后清理安装器残留目录（三级删除策略，参考 migrator.py）

        安装器在 kill 前可能已经写入部分文件，需要立即清理避免残留。
        策略（按速度从快到慢，逐级兜底）：
        1. rd /s /q - Windows 原生命令，最快，不进回收站
        2. shutil.rmtree - Python 兜底
        3. 重命名为 .bak - 文件被占用时绕过占用，由后台 monitor 线程清理

        返回 True 表示已删除（或已重命名待后台清理），False 表示清理失败
        """
        if not path or not os.path.exists(path):
            return True
        if not os.path.isdir(path):
            # 文件残留（非目录），直接 unlink
            try:
                os.remove(path)
                return True
            except Exception as e:
                self.log_signal.emit("error", f"清理残留文件失败: {e} path={path}")
                return False

        # 策略1: rd /s /q（最快，不进回收站）
        try:
            subprocess.run(["cmd", "/c", "rd", "/s", "/q", path],
                           capture_output=True, timeout=3,
                           creationflags=_NO_WINDOW_FLAGS)
            if not os.path.exists(path):
                return True
        except subprocess.TimeoutExpired:
            self.log_signal.emit("warn", f"rd /s /q 超时: {path}")
        except Exception as e:
            self.log_signal.emit("error", f"rd清理异常: {e}")

        # 策略2: shutil.rmtree（Python 兜底）
        try:
            shutil.rmtree(path)
            if not os.path.exists(path):
                return True
        except Exception as e:
            self.log_signal.emit("error", f"rmtree清理失败: {e}")

        # 策略3: 重命名为 .bak（文件被占用时的最终兜底）
        # 重命名后即使原路径仍存在，watcher 也会把它当新目录重新处理
        try:
            bak_path = path + "._install_residue_bak"
            # 清理旧的 .bak
            if os.path.exists(bak_path):
                try:
                    shutil.rmtree(bak_path, ignore_errors=True)
                except Exception as e:
                    log.debug("忽略异常: %s", e)
            os.rename(path, bak_path)
            self.log_signal.emit("warn",
                f"残留目录被占用，已重命名为 {os.path.basename(bak_path)}，将由后台清理")
            return True
        except Exception as e:
            self.log_signal.emit("error",
                f"重命名失败，残留未清理: {e} path={path}")
            return False

    def _on_file_created(self, path):
        """文件创建 - 检测安装器"""
        parent = os.path.dirname(path)
        if not parent.endswith("Programs"):
            return
        try:
            size = os.path.getsize(path)
        except Exception:
            size = 0
        if size > 1024 * 1024:  # >1MB
            size_mb = round(size / 1024 / 1024, 1)
            self.alert_signal.emit(
                "检测到安装行为！",
                f"Programs目录下新文件: {os.path.basename(path)}\n大小: {size_mb} MB\n路径: {path}"
            )
            self.log_signal.emit("install", f"安装文件: {path} ({size_mb} MB)")

    def _check_installer_processes(self):
        """检测安装器进程启动 - 使用Windows原生API（CreateToolhelp32Snapshot）
        比psutil.process_iter快10倍，不需要打开进程句柄

        两类拦截策略：
        1. INSTALLER_KEYWORDS 命中（setup/install/updater 等）→ 直接 kill（原逻辑）
        2. PACKAGE_MANAGER_PROCS 命中（winget/choco/scoop/msiexec）→ 暂停 + 询问用户

        注：脚本类（cmd/powershell/npm/pip/cargo/go/wsl/docker 等）默认放行，
        无法保证下次安装到非 C 盘，拦截无解决方案价值。
        后续可在独立"开发环境路径迁移"功能页统一配置环境变量。
        """
        try:
            import ctypes
            from ctypes import wintypes

            # 定义结构体和常量
            TH32CS_SNAPPROCESS = 0x00000002
            MAX_PATH = 260

            class PROCESSENTRY32(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * MAX_PATH),
                ]

            kernel32 = ctypes.windll.kernel32
            snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snapshot == -1:
                return

            pe = PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32)

            # 第一阶段：扫描所有进程，分类收集
            # strong_matches: 强关键词命中（setup/install 等，走 kill 流程）
            # weak_matches: 弱关键词命中（helper/updater 等，只记录不杀，等写入行为验证）
            # pkg_matches: PACKAGE_MANAGER_PROCS 命中（走暂停询问流程）
            #
            # 同时维护 _recent_procs 缓存：记录所有进程的 pid+name
            # watcher 触发 _kill_install_related_procs 时直接查缓存（<1ms），
            # 不再每次重新枚举376个进程（原 100-500ms 延迟的根源）
            strong_matches = []
            weak_matches = []
            pkg_matches = []
            # 重置 _recent_procs（每0.3秒全量更新一次）
            new_recent = {}
            # now 必须在进程枚举循环前定义，供 new_recent[pid]["record_time"] 使用
            # 原先定义在 1525 行，导致 1490 行 NameError 被 except: pass 吞掉，
            # 强/弱关键词/包管理器三层分类全部失效
            now = time.time()
            if kernel32.Process32First(snapshot, ctypes.byref(pe)):
                while True:
                    try:
                        name = pe.szExeFile.decode('utf-8', errors='ignore')
                        name_lower = name.lower()
                        pid = pe.th32ProcessID
                        # 维护 _recent_procs 缓存（只存 pid+name，不查 psutil，零开销）
                        new_recent[pid] = {"name": name, "record_time": now}
                        # 1) 强关键词匹配（setup/install/installer 等 → 直接 kill 流程）
                        hit_strong = False
                        for kw in self.INSTALLER_STRONG_KEYWORDS:
                            if kw in name_lower:
                                strong_matches.append((name, pid))
                                hit_strong = True
                                break
                        if hit_strong:
                            pass  # 已分类为强关键词
                        else:
                            # 2) 弱关键词匹配（helper/updater 等 → 只记录，不主动杀）
                            # 这些词既是安装器辅助进程特征，也是浏览器/聊天软件辅助进程特征
                            # 靠进程名无法区分，改为由 watchdog 写入行为触发反查
                            for kw in self.INSTALLER_WEAK_KEYWORDS:
                                if kw in name_lower:
                                    weak_matches.append((name, pid))
                                    break
                            else:
                                # 3) 系统级安装器进程名（误杀风险低，直接走暂停询问）
                                if name_lower in self.PACKAGE_MANAGER_PROCS:
                                    pkg_matches.append((name, pid))
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                    if not kernel32.Process32Next(snapshot, ctypes.byref(pe)):
                        break
            kernel32.CloseHandle(snapshot)
            # 更新 _recent_procs 缓存（原子替换，供 watcher 触发时查询）
            with self._recent_lock:
                self._recent_procs = new_recent

            # 第二阶段：psutil 查详情（exe/cmdline/create_time）
            import psutil
            import time as _time
            now = _time.time()

            # 处理 strong_matches（强关键词，直接 kill）
            # 限制最多检查 15 个进程，避免大量进程阻塞主循环
            # 优先检查 PID 大的（通常是最新启动的进程）
            strong_matches.sort(key=lambda x: x[1], reverse=True)
            checked_count = 0
            MAX_CHECK = 15
            for name, pid in strong_matches:
                if checked_count >= MAX_CHECK:
                    break
                try:
                    proc = psutil.Process(pid)
                    # 先查 create_time（快），5 秒窗口外的直接跳过，不调用 exe()/cmdline()
                    # 5 秒窗口：开机后长时间运行的进程（如旧版 setup.exe 残留）直接跳过
                    create_t = proc.create_time()
                    if now - create_t > 5:
                        continue
                    checked_count += 1
                    exe = proc.exe() or ''
                    if 'windows' in exe.lower() or 'system32' in exe.lower():
                        continue
                    cmdline = proc.cmdline()
                    # 卸载行为不拦截（卸载器进程名可能含 install/setup 关键词）
                    cmdline_str = ' '.join(cmdline) if cmdline else ''
                    if self._is_uninstall_cmdline(cmdline_str):
                        continue
                    if not self._is_installer_process(name, exe, cmdline):
                        continue
                    # 信任列表检查（避免杀掉用户已信任的安装器）
                    if self._is_trusted(name, cmdline_str):
                        continue
                    # 防止短时间重复弹窗（同一 PID 60 秒内只问一次）
                    last_ask = self._asked_pids.get(pid, 0)
                    if now - last_ask < 60:
                        continue
                    self._asked_pids[pid] = now
                    # H10-④：强关键词改为走暂停询问流程（而非直接杀），
                    # 用户可选择允许/拒绝/迁移，60秒超时自动 kill（_suspend_and_ask 内部处理）
                    threading.Thread(
                        target=self._suspend_and_ask,
                        args=(name, pid, exe, cmdline_str, f"强关键词匹配: {name}"),
                        daemon=True
                    ).start()
                except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                    log.debug("忽略异常: %s", e)

            # 处理 weak_matches（弱关键词，只记录到可疑进程池，不主动杀）
            # 这些进程不杀，但记录 pid + create_time + exe，供 watchdog 写入行为触发时反查
            # 核心思路：helper/updater 既可能是辅助进程也可能是安装器辅助进程
            #   靠进程名无法区分，改为等它们真的写入 C 盘受保护目录时再杀
            for name, pid in weak_matches:
                try:
                    proc = psutil.Process(pid)
                    create_t = proc.create_time()
                    # 只记录最近 60 秒内启动的弱关键词进程（老进程不可能是新安装的触发者）
                    if now - create_t > 60:
                        continue
                    exe = proc.exe() or ''
                    # 排除系统进程和白名单进程（快速过滤）
                    name_lower = name.lower()
                    if any(h in name_lower for h in (
                        "python.exe", "pythonw.exe", "explorer.exe", "svchost.exe",
                        "cdriverelocator", "c盘拦迁器", "trae", "code.exe")):
                        continue
                    if 'windows' in exe.lower() or 'system32' in exe.lower():
                        continue
                    # 白名单进程不记录
                    if self._is_in_whitelist(name_lower, exe.lower(), ''):
                        continue
                    # 记录到可疑进程池：{pid: {"name":..., "exe":..., "create_time":...}}
                    # 60 秒后自动过期（由 _kill_install_related_procs 查询时过滤）
                    # 修复 H10-②：记录初始 write_bytes，验证时对比增量而非累计量
                    # 否则 helper 进程历史写超 4KB（如浏览器/QQ累计写配置）会被误杀
                    try:
                        init_write = psutil.Process(pid).io_counters().write_bytes
                    except Exception:
                        init_write = -1  # 获取失败时标记，验证时跳过 write 判据
                    with self._suspicious_lock:
                        self._suspicious_procs[pid] = {
                            "name": name,
                            "exe": exe,
                            "create_time": create_t,
                            "record_time": now,
                            "init_write_bytes": init_write,
                        }
                except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                    log.debug("忽略异常: %s", e)
                except Exception as e:
                    log.debug("忽略异常: %s", e)
            # 清理过期的可疑进程记录（超过 120 秒的），避免字典无限增长
            with self._suspicious_lock:
                expired_pids = [p for p, info in list(self._suspicious_procs.items())
                               if now - info.get("record_time", 0) > 120]
                for p in expired_pids:
                    try:
                        del self._suspicious_procs[p]
                    except Exception as e:
                        log.debug("忽略异常: %s", e)

            # 处理 pkg_matches（系统级安装器，暂停询问流程）
            for name, pid in pkg_matches:
                try:
                    proc = psutil.Process(pid)
                    exe = proc.exe() or ''
                    name_lower = name.lower()
                    # 排除系统路径（msiexec 例外，需配合 _is_user_triggered_msiexec）
                    if 'system32' in exe.lower() and 'msiexec' not in name_lower:
                        continue
                    # 检测窗口从 10 秒延长到 60 秒
                    # msiexec 等系统级安装器可能运行较长时间（大软件安装）
                    create_t = proc.create_time()
                    age = now - create_t
                    if age > 60:
                        continue
                    cmdline = proc.cmdline()
                    cmdline_str = ' '.join(cmdline) if cmdline else ''

                    # 卸载行为不拦截（卸载是删除软件，不会在C盘装新东西）
                    if self._is_uninstall_cmdline(cmdline_str):
                        continue

                    # msiexec 特殊处理：排除系统服务（父进程是 services.exe）
                    if 'msiexec' in name_lower:
                        if not self._is_user_triggered_msiexec(proc):
                            continue

                    # 信任列表检查
                    if self._is_trusted(name, cmdline_str):
                        continue

                    # 防止短时间重复弹窗（同一 PID 60 秒内只问一次）
                    last_ask = self._asked_pids.get(pid, 0)
                    if now - last_ask < 60:
                        continue
                    self._asked_pids[pid] = now

                    # 走暂停询问流程（在新线程中执行，不阻塞监控主循环）
                    # 命中关键字：包管理器进程名精确匹配（PACKAGE_MANAGER_PROCS）
                    threading.Thread(
                        target=self._suspend_and_ask,
                        args=(name, pid, exe, cmdline_str, f"包管理器进程名匹配: {name}"),
                        daemon=True
                    ).start()
                except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                    log.debug("忽略异常: %s", e)
        except ImportError as e:
            log.debug("忽略异常: %s", e)

    def _is_user_triggered_msiexec(self, proc):
        """判断 msiexec 是否用户触发（非系统服务）
        Windows Installer 服务 msiexec.exe 的父进程是 services.exe
        用户触发的 msiexec /i 的父进程通常是 explorer/cmd/explorer 等
        """
        try:
            parent = proc.parent()
            if parent is None:
                return True  # 无法判断，默认拦
            parent_name = parent.name().lower()
            # 父进程是服务管理器 → 系统服务，不拦
            if parent_name == 'services.exe':
                return False
            return True
        except Exception:
            return True  # 无法判断，默认拦

    # 卸载关键词：命令行含这些词说明是卸载行为，不拦截
    # 卸载是删除软件，不会在 C 盘装新东西，所以放行
    UNINSTALL_KEYWORDS = (
        'uninstall', '/uninstall', '/x ', '/x"', '/x`',
        '/remove', 'remove ', '-uninstall', '--uninstall',
        '/uninstall_', 'unins000', 'unins001',  # Inno Setup 卸载器
        '/quiet /uninstall', '/qn /x',  # msiexec 静默卸载
    )

    def _is_uninstall_cmdline(self, cmdline_str):
        """检测命令行是否是卸载行为
        卸载不拦截：卸载是删除软件，不会在C盘装新东西
        """
        if not cmdline_str:
            return False
        cmd_lower = cmdline_str.lower()
        for ukw in self.UNINSTALL_KEYWORDS:
            if ukw in cmd_lower:
                return True
        return False

    def _is_trusted(self, name, cmdline_str):
        """检查进程是否在信任列表中（复用 whitelist 字段，避免双份存储）
        whitelist 格式：[{"keyword": "winget", "desc": "..."}, ...]
        匹配逻辑：keyword 在 (name + cmdline_str) 中即视为信任（不区分大小写）
        """
        # 统一用 self.whitelist（已合并 DEFAULT_WHITELIST + 用户自定义）
        # 避免用户白名单非空时默认白名单丢失
        wl = self.whitelist
        name_lower = name.lower()
        cmd_lower = (cmdline_str or '').lower()
        combined = name_lower + ' ' + cmd_lower

        for w in wl:
            if not isinstance(w, dict):
                continue
            kw = (w.get("keyword") or "").lower()
            if not kw:
                continue
            if kw in combined:
                return True
        return False

    def _is_in_whitelist(self, name_lower, exe_lower, cmdline_str):
        """检查进程是否命中白名单（供可疑进程池过滤使用）
        :param name_lower: 进程名（已小写）
        :param exe_lower: exe路径（已小写）
        :param cmdline_str: 命令行原始字符串
        :return: True 表示在白名单中，不应视为可疑
        """
        combined = name_lower + ' ' + exe_lower + ' ' + (cmdline_str or '').lower()
        for w in self.whitelist:
            if not isinstance(w, dict):
                continue
            kw = (w.get("keyword") or "").lower()
            if not kw:
                continue
            if kw in combined:
                return True
        return False

    def set_decision(self, pid, decision):
        """主线程回传用户决策
        :param pid: 进程 PID
        :param decision: "allow" / "kill" / "migrate_later"
        """
        with self._pending_lock:
            entry = self._pending_decisions.get(pid)
            if entry is None:
                return
            entry["decision"] = decision
            entry["event"].set()

    def _suspend_and_ask(self, name, pid, exe, cmdline_str, hit_keyword=""):
        """暂停进程并询问用户决策（在独立线程中执行，避免阻塞监控主循环）
        流程：suspend → 发信号给主线程弹窗 → 等待 60 秒 → 执行决策

        :param hit_keyword: 命中的规则/关键字描述（显示在弹窗中，帮助用户判断）
        自动拦截开关控制：
        - 开启：暂停进程 + 弹窗询问
        - 关闭：不暂停，只弹提示告知用户检测到安装行为
        """
        import psutil

        # 自动拦截开关检查
        if not self.auto_migrate:
            # 自动拦截已关闭，不暂停进程，只记录日志和提示
            self.log_signal.emit("warn",
                f"检测到系统级安装器(未拦截): {name} (PID:{pid})  自动拦截已关闭")
            self.installer_signal.emit(name)
            self.alert_signal.emit(
                "检测到安装行为（未拦截）",
                f"进程: {name}\nPID: {pid}\n路径: {exe}\n命令: {cmdline_str}\n\n"
                f"自动拦截已关闭，未暂停进程。\n"
                f"如需拦截，请开启「自动拦截」开关。\n"
                f"稍后可在「待迁移」表手动迁移该目录。"
            )
            # 记录到 blocked_processes
            try:
                blocked = self.migrator.cfg.setdefault("blocked_processes", [])
                blocked.append({
                    "name": name, "pid": pid, "exe": exe,
                    "cmdline": cmdline_str,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "installer_detected_no_intercept",
                    "decision": "no_intercept"
                })
                if len(blocked) > 200:
                    self.migrator.cfg["blocked_processes"] = blocked[-200:]
                # 修复：blocked_processes 是 STATE_FIELDS，用 _save_blocked_processes 持久化
                self._save_blocked_processes()
            except Exception as e:
                log.debug("忽略异常: %s", e)
            return

        try:
            proc = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return

        # 1. 尝试暂停进程（失败不 return，继续弹倒计时框让用户决策）
        suspend_ok = False
        try:
            proc.suspend()
            suspend_ok = True
            self.log_signal.emit("warn",
                f"已暂停安装进程: {name} (PID:{pid})，等待用户决策...")
        except Exception as e:
            # 暂停失败 → 记录日志，但仍弹倒计时框让用户决策（可 kill）
            self.log_signal.emit("error",
                f"暂停进程失败 {name}: {e}（将仅弹窗不暂停，仍可拒绝终止）")

        # 2. 注册决策池
        event = threading.Event()
        with self._pending_lock:
            self._pending_decisions[pid] = {"event": event, "decision": None}

        # 3. 发信号给主线程弹窗（附带 suspend_ok 让 UI 显示状态）
        self.installer_confirm_signal.emit(name, pid, exe, cmdline_str, hit_keyword)

        # 4. 等待决策（60 秒超时）
        decision = None
        if event.wait(timeout=60):
            with self._pending_lock:
                decision = self._pending_decisions.get(pid, {}).get("decision")
        if decision is None:
            decision = "timeout"

        # 5. 清理决策池
        with self._pending_lock:
            self._pending_decisions.pop(pid, None)

        # 6. 执行决策
        try:
            if decision == "allow":
                if suspend_ok:
                    try:
                        proc.resume()
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                self.log_signal.emit("init", f"用户放行: {name} (PID:{pid})")
            elif decision == "kill":
                try:
                    proc.kill()
                    self.log_signal.emit("kill", f"用户拒绝并终止: {name} (PID:{pid})")
                    self.alert_signal.emit(
                        "已终止安装进程",
                        f"进程: {name}\nPID: {pid}\n\n进程已被用户拒绝并终止。"
                    )
                except Exception as e:
                    if suspend_ok:
                        try:
                            proc.resume()
                        except Exception as e:
                            log.debug("忽略异常: %s", e)
                    self.log_signal.emit("error", f"终止失败已放行: {name}: {e}")
            elif decision == "migrate_later":
                if suspend_ok:
                    try:
                        proc.resume()
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                self.log_signal.emit("warn",
                    f"用户选择稍后迁移: {name} (PID:{pid})，请在「待迁移」表右键迁移")
                self.alert_signal.emit(
                    "安装已放行",
                    f"进程: {name}\nPID: {pid}\n\n"
                    f"安装完成后，请到「待迁移」标签页找到新目录，"
                    f"右键选择「迁移到指定位置」将其搬到其他盘。"
                )
            else:  # timeout
                if suspend_ok:
                    try:
                        proc.resume()
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                self.log_signal.emit("warn",
                    f"60秒未决策，自动放行: {name} (PID:{pid})")
                self.alert_signal.emit(
                    "无法拦截安装进程",
                    f"进程: {name}\nPID: {pid}\n\n"
                    f"60 秒内未做出决策，已自动放行。\n"
                    f"请稍后在「待迁移」表手动迁移该目录。"
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            log.debug("忽略异常: %s", e)
        # 同时记录到 blocked_processes（供白名单"从拦截日志添加"使用）
        try:
            blocked = self.migrator.cfg.setdefault("blocked_processes", [])
            blocked.append({
                "name": name, "pid": pid, "exe": exe,
                "cmdline": cmdline_str,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source": "script_intercept",
                "decision": decision
            })
            if len(blocked) > 200:
                self.migrator.cfg["blocked_processes"] = blocked[-200:]
            # 修复：blocked_processes 是 STATE_FIELDS，用 _save_blocked_processes 持久化
            self._save_blocked_processes()
        except Exception as e:
            log.debug("忽略异常: %s", e)

    def _is_installer_process(self, name, exe, cmdline):
        """判断是否是安装器进程（通用）"""
        name_lower = (name or '').lower()
        exe_lower = (exe or '').lower()
        # 先检查隐藏白名单 - 本工具自身+系统关键进程不杀
        HIDDEN_WHITELIST = [
            "pythonw.exe",      # 本工具运行进程
            "python.exe",       # 本工具开发模式
            "rust-migrate-engine.exe",  # P6:本工具的 Rust 复制引擎子进程(会写 C 盘数据,不能误杀)
            "c盘拦迁器",         # 本工具打包后名称（v0.04+）
            "c盘拦迁器",       # 本工具旧名称（兼容）
            "c-drive-guard",   # 本工具英文名（旧）
            "cdriverelocator",
            "cdriverelocatorian",   # 本工具英文名（新）
            "c-drive-guardian",
            "explorer.exe",     # 资源管理器
            "dwm.exe",          # 桌面窗口管理器
            "csrss.exe",        # 客户端服务器运行时
            "winlogon.exe",     # Windows登录
            "services.exe",     # 服务管理器
            "lsass.exe",        # 本地安全机构
            "wininit.exe",      # Windows启动
            "smss.exe",         # 会话管理器
            "svchost.exe",      # 服务主机
            "spoolsv.exe",      # 打印后台
            "fontdrvhost.exe",  # 字体驱动
            "sihost.exe",       # Shell图标
            "taskhostw.exe",    # 任务主机
            "ctfmon.exe",       # 输入法
            "conhost.exe",      # 控制台主机
            "taskkill.exe",     # 本工具用taskkill杀进程
        ]
        for hidden in HIDDEN_WHITELIST:
            if hidden in name_lower or hidden in exe_lower:
                return False
        # 先检查白名单 - 系统更新/杀毒软件不杀
        all_text = name_lower + ' ' + exe_lower
        if cmdline:
            for arg in cmdline:
                if arg:
                    all_text += ' ' + arg.lower()
        for wl in self.whitelist:
            kw = wl.get("keyword", "") if isinstance(wl, dict) else str(wl)
            if kw and kw in all_text:
                return False
        # 进程名/exe路径/命令行包含安装器特征词
        for kw in self.INSTALLER_KEYWORDS:
            if kw in name_lower or kw in exe_lower:
                return True
        if cmdline:
            for arg in cmdline:
                if arg and any(kw in arg.lower() for kw in self.INSTALLER_KEYWORDS):
                    return True
        return False

    def _kill_installer(self, name, pid, exe):
        """杀掉安装器进程 - 受auto_migrate开关控制
        无论是否开启拦截，都先记录到blocked_processes供白名单管理引用
        """
        # 先记录到 blocked_processes（无论是否拦截都记录，供"从拦截日志添加"使用）
        try:
            blocked = self.migrator.cfg.setdefault("blocked_processes", [])
            # 尝试获取 cmdline 供白名单"按命令关键词添加"使用
            try:
                import psutil
                cmdline_str = ' '.join(psutil.Process(pid).cmdline())
            except Exception:
                cmdline_str = ""
            blocked.append({"name": name, "pid": pid, "exe": exe,
                            "cmdline": cmdline_str,
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "source": "process_detected"})
            # 只保留最近200条
            if len(blocked) > 200:
                self.migrator.cfg["blocked_processes"] = blocked[-200:]
            # 修复：blocked_processes 是 STATE_FIELDS，用 _save_blocked_processes 持久化
            self._save_blocked_processes()
            # 调试日志：确认已写入blocked_processes
            self.log_signal.emit("installer", f"已记录到拦截日志: {name} (共{len(blocked)}条)")
        except Exception as e:
            self.log_signal.emit("error", f"写入blocked_processes失败: {e}")

        if not self.auto_migrate:
            # 自动拦截已关闭，只记录日志不杀进程
            self.log_signal.emit("warn", f"检测到安装器(未拦截): {name} (PID:{pid})  自动拦截已关闭")
            self.installer_signal.emit(name)
            return
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=3,
                creationflags=_NO_WINDOW_FLAGS)
            self.log_signal.emit("kill", f"已终止安装器进程: {name} (PID:{pid})")
            self.alert_signal.emit(
                "已拦截安装器！",
                f"进程: {name}\nPID: {pid}\n路径: {exe}\n\n进程已被强制终止，防止向C盘安装。"
            )
        except Exception as e:
            self.log_signal.emit("error", f"终止进程失败 {name}: {e}")
            self.alert_signal.emit(
                "拦截失败",
                f"进程: {name} (PID:{pid})\n终止失败: {e}\n\n请手动关闭该安装器。"
            )

    def _start_wmi_monitor(self):
        """已弃用 - WMI会导致CPU 100%空转，改用psutil轮询"""
        return

    def _periodic_check(self):
        r"""定期轻量检查 - 只比较目录列表，发现新目录后才获取大小
        不调用scan_appdata（会遍历6个目录获取大小，C:\Program Files下大目录会卡死CPU）
        """
        try:
            # 轻量扫描：只listdir收集目录列表，不获取大小
            scan_bases = [p for p, _ in get_scan_dirs(include_user=False)]
            current_set = set()
            for base_path in scan_bases:
                if not base_path or not os.path.exists(base_path):
                    continue
                try:
                    for entry in os.listdir(base_path):
                        full_path = os.path.join(base_path, entry)
                        if os.path.isdir(full_path):
                            current_set.add(full_path)
                except Exception as e:
                    log.debug("忽略异常: %s", e)

            # 发现新目录
            new_dirs = current_set - self.known_dirs
            for new_dir in new_dirs:
                # 通用：调用 _on_dir_created 处理新目录
                # _on_dir_created 内部会：
                # 1. 发 log_signal("new", ...) 记录日志
                # 2. 发 new_dir_signal 通知 UI
                # 3. 判断是否是安装行为（Programs/Program Files 下新建）→ 发 alert_signal 弹窗
                # 4. 写入 blocked_processes（供白名单"从拦截日志添加"使用）
                # 注意：_on_dir_created 内部会 add 到 known_dirs，这里不再重复 add
                try:
                    self._on_dir_created(new_dir)
                except Exception as e:
                    # 兜底：即使 _on_dir_created 失败，也要记录日志和发信号
                    # 每条 emit 独立 try/except，避免一条失败影响其他
                    try:
                        size = get_dir_size_fast(new_dir)
                        size = float(size)  # new_dir_signal 需要 float 类型
                    except Exception:
                        size = 0.0
                    try:
                        self.log_signal.emit("new", f"发现新目录(兜底): {new_dir} ({size} MB)")
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                    try:
                        self.new_dir_signal.emit(new_dir, size)
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                    try:
                        self.log_signal.emit("error", f"_on_dir_created 处理 {new_dir} 失败: {type(e).__name__}: {e}")
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
            self.known_dirs = current_set
            for m in self.migrator.cfg.get("migrated", []):
                self.known_dirs.add(m.get("src", ""))
            # 清理之前修复时重命名的备份目录（._cdrive_bak后缀）
            # 这些目录是软件文件被占用时无法删除而重命名的，软件关闭后可以删除
            # 通用逻辑：遍历所有 migrated 记录的 src 同目录，查找 *._cdrive_bak
            # （备份目录和原 src 在同一父目录下，如 Android\Sdk._cdrive_bak 和 Android\Sdk）
            for m in self.migrator.cfg.get("migrated", []):
                src = m.get("src", "")
                if not src:
                    continue
                src_norm = src.replace("\\\\?\\", "").rstrip("\\")
                parent = os.path.dirname(src_norm)
                if not parent or not os.path.isdir(parent):
                    continue
                try:
                    for entry in os.listdir(parent):
                        if entry.endswith("._cdrive_bak"):
                            bak_path = os.path.join(parent, entry)
                            try:
                                shutil.rmtree(bak_path)
                                self.log_signal.emit("fix",
                                    f"已清理备份目录: {entry}（软件已关闭，备份目录已删除）")
                                log.info(f"清理备份目录: {bak_path}")
                            except Exception:
                                pass  # 文件仍被占用，等下次再试
                except Exception as e:
                    log.debug("忽略异常: %s", e)

            # 清理安装拦截时重命名的残留目录（._install_residue_bak 后缀）
            # 这些目录是 kill 安装器后部分文件被占用无法删除而重命名的，软件关闭后可删除
            # 扫描所有监控目录下的 ._install_residue_bak 残留
            cleanup_scan_dirs = [p for p, _ in get_scan_dirs(include_user=False)]
            for base_path in cleanup_scan_dirs:
                if not base_path or not os.path.isdir(base_path):
                    continue
                try:
                    for entry in os.listdir(base_path):
                        if entry.endswith("._install_residue_bak"):
                            bak_path = os.path.join(base_path, entry)
                            try:
                                shutil.rmtree(bak_path, ignore_errors=True)
                                if not os.path.exists(bak_path):
                                    self.log_signal.emit("fix",
                                        f"已清理拦截残留目录: {entry}（占用进程已退出）")
                                    log.info(f"清理拦截残留: {bak_path}")
                            except Exception:
                                pass  # 仍被占用，等下次再试
                except Exception as e:
                    log.debug("忽略异常: %s", e)
            # 检查符号链接是否被破坏 - 自动修复（对付无视符号链接在C盘真实目录安装的软件）
            # 跳过正在还原中的 src（避免与 _RestoreDataWorker 抢资源，防止"自动修复"
            # 把正在复制回 C 盘的数据再次迁回 D 盘）
            restoring_set = set(self.migrator.cfg.get("restoring_in_progress", []))
            for m in self.migrator.cfg.get("migrated", []):
                src = m.get("src", "")
                dst = m.get("dst", "")
                if not src or not dst:
                    continue
                if is_symlink(src):
                    continue  # 链接正常，跳过
                # 正在还原中的路径跳过自动修复
                if src in restoring_set:
                    continue
                # 符号链接被覆盖或路径不存在 → 自动修复
                # 把C盘新数据合并到目标盘，删除C盘真实目录，重建符号链接
                dir_name = os.path.basename(src)
                if os.path.exists(src):
                    self.log_signal.emit("warn",
                        f"检测到符号链接被覆盖: {dir_name}（软件无视链接在C盘创建了真实目录），正在自动修复...")
                else:
                    self.log_signal.emit("warn",
                        f"检测到符号链接丢失: {dir_name}（C盘路径不存在），正在自动修复...")
                success, msg = self._auto_fix_link(src, dst)
                if success:
                    self.log_signal.emit("fix",
                        f"自动修复成功: {dir_name} - {msg}（数据已回到目标盘，C盘已重建符号链接）")
                else:
                    self.log_signal.emit("error",
                        f"自动修复失败: {dir_name} - {msg}（下次检查时会自动重试）")
        except Exception as e:
            log_error_with_reason("修复异常", str(e), "MonitorWorker._periodic_check")
            self.log_signal.emit("error", f"检查异常: {e}")

    def stop(self):
        self.running = False

    @link_fix_locked
    def _auto_fix_link(self, src_path, dst_path):
        """自动修复被覆盖的符号链接 - 比 fix_broken_link 更健壮
        处理软件无视符号链接在C盘创建真实目录的情况：
        1. 通过 Rust 引擎把C盘新数据合并到目标盘(mode=copy)
        2. 删除C盘真实目录（如果文件被占用，尝试重命名为._cdrive_bak）
        3. 创建符号链接
        4. 后台清理重命名的旧目录

        H3 修复:与手动 fix_broken_link 共用 _link_fix_lock 互斥(见 utils.link_fix_locked),
        避免后台周期修复与手动修复并发时两个引擎作业写同一目标。
        """
        try:
            # 去掉 dst 的 \\?\ 前缀
            if dst_path.startswith("\\\\?\\"):
                dst_path = dst_path[4:]

            # 情况1: C盘路径不存在，直接创建链接（Junction 优先）
            if not os.path.exists(src_path):
                ok, lerr = self.migrator._create_dir_link(src_path, dst_path)
                if ok:
                    log_link_operation("自动修复(缺失)", src_path, dst_path, "C盘路径不存在，直接创建链接")
                    return True, "C盘路径不存在，已直接创建链接"
                log_error_with_reason("创建链接失败", lerr, f"自动修复(缺失): {src_path} -> {dst_path}")
                return False, f"创建链接失败: {lerr}"

            # 已经是符号链接，无需修复
            if is_symlink(src_path):
                return True, "符号链接已存在"

            # 情况2: C盘是真实目录（被软件覆盖），需合并数据后重建链接
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)

            # 1. 通过 Rust 引擎合并C盘新数据到目标盘(mode=copy,保留目标盘已有数据)
            # P4:复制调用已替换为引擎 mode="copy"(=/E 等价,不含 purge)
            rc = self.migrator._run_engine_with_progress(
                src_path, dst_path, action_label="自动修复",
                mode="copy", purge_enabled=False,
            )
            if rc >= 8 or rc == -1:  # _CANCELLED_RC = -1
                diag = getattr(self.migrator, "_last_copy_fail_reason", None) or {}
                err_detail = f"返回码 {rc}"
                if diag.get("reason"):
                    err_detail += f": {diag['reason']}"
                log_error_with_reason("合并数据失败", f"返回码:{rc}", f"自动修复(覆盖): {src_path} -> {dst_path}")
                return False, f"合并数据失败({err_detail})"

            # 2. 删除C盘真实目录（文件可能被占用，需多种策略）
            delete_ok = False
            try:
                shutil.rmtree(src_path)
                delete_ok = True
            except Exception as e:
                log.debug("忽略异常: %s", e)

            # 2b. 如果rmtree失败（文件被占用），尝试重命名后创建链接
            if not delete_ok:
                bak_path = src_path + "._cdrive_bak"
                try:
                    # 先删除旧的备份目录（如果存在）
                    if os.path.exists(bak_path):
                        try:
                            shutil.rmtree(bak_path)
                        except Exception as e:
                            log.debug("忽略异常: %s", e)
                    os.rename(src_path, bak_path)
                    delete_ok = True
                    # 标记需要后台清理（下次检查时会尝试删除）
                    self.log_signal.emit("warn",
                        f"目录被占用，已重命名为备份: {os.path.basename(bak_path)}，将在软件关闭后清理")
                except Exception as e:
                    log_error_with_reason("删除C盘目录失败且重命名也失败", str(e), f"自动修复(覆盖): {src_path}")
                    return False, f"删除C盘目录失败且重命名也失败: {e}（文件可能被占用，等软件关闭后自动重试）"

            # 3. 创建链接（Junction 优先）
            ok, lerr = self.migrator._create_dir_link(src_path, dst_path)
            if not ok:
                # 创建链接失败，尝试把数据复制回来(回滚:目标盘→C盘)
                # P4:复制调用已替换为引擎 mode="copy"(回滚场景,不 purge)
                try:
                    self.migrator._run_engine_with_progress(
                        dst_path, src_path, action_label="回滚",
                        mode="copy", purge_enabled=False,
                    )
                except Exception:
                    pass  # 回滚失败不阻断错误上报,主错误是"创建链接失败"
                log_error_with_reason("创建链接失败", lerr, f"自动修复(覆盖): {src_path} -> {dst_path}")
                return False, f"创建链接失败: {lerr}"

            log_link_operation("自动修复(覆盖)", src_path, dst_path,
                "软件无视链接在C盘创建真实目录，已合并数据并重建链接")
            return True, "已合并C盘新数据到目标盘并重建符号链接"

        except Exception as e:
            log_error_with_reason("修复异常", str(e), f"自动修复: {src_path} -> {dst_path}")
            return False, f"修复异常: {e}"
