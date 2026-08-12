#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置管理 - 常量、配置加载/保存、日志设置"""

import os
import sys
import json
import time
import shutil
import logging
import ctypes
import copy
from pathlib import Path
from datetime import datetime

APP_NAME = "C盘拦迁器"
APP_VERSION = "0.03"

# #28:配置文件版本号(config.json/state.json 各自独立版本,结构变更时递增,
# 加载时检查:文件版本高于程序支持→警告降级加载;低于→执行迁移扩展点)
CONFIG_VERSION = 2  # 2026-08-12: 新增 user_dir_notify_enabled 配置字段
STATE_VERSION = 2  # 2026-08-12: 新增 dst_index 状态字段

# 单实例 Mutex handle（main() 中创建，_restart_app 中释放）
# 放在 config 模块而非 main 模块，因为 ui_lifecycle 已经 import config，
# 避免「import main」在 PyInstaller 打包模式下可能拿不到正确模块对象的问题
SINGLE_INSTANCE_MUTEX_HANDLE = None

# 项目根目录：源码模式=src的上一级（本文件位于 src/core/config.py，需向上3级），exe模式=exe所在目录
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent.parent.parent

CONFIG_DIR = BASE_DIR
LOG_DIR = BASE_DIR / "logs"
# 静态 JSON 数据统一放 src/data/（随源码分发，只读）
# 运行时可写数据放 BASE_DIR（config.json/state.json/logs/ 等）
DATA_DIR = BASE_DIR / "data"
# 自动创建子目录（首次运行/exe部署时）
LOG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ========== JSON 文件存储分工说明 ==========
# 程序运行时使用 3 个 JSON 文件，职责严格分离，不允许交叉存储：
#
# 1. config.json  —— 用户设置（可手动编辑，非敏感）
#    存：g_root / scan_interval / auto_migrate / size_threshold / ai_recognize / whitelist
#    特点：用户可读可改，清空缓存不影响，不含 API Key
#
# 2. state.json   —— 程序运行时状态（程序自维护，用户不应手动编辑）
#    分两类：
#    (a) 关键持久数据（不可丢失，丢失会导致迁移记录/未完成事务丢失）：
#        - migrated          已迁移记录（src/dst/size/desc/time）
#        - pending_migrations 未完成迁移事务（断电/崩溃恢复依据）
#        - pending_restores   未完成还原事务（断电/崩溃恢复依据）
#        - dev_env_configured 开发环境配置记录（环境变量/配置命令/卸载辅助）
#    (b) 可重建缓存（丢失可重新生成，不影响数据安全）：
#        - scan_cache          待迁移扫描结果
#        - scan_cache_time     扫描时间戳
#        - desc_cache          软件描述缓存
#        - dev_env_status_cache 开发环境工具状态缓存
#        - logged_symlinks     链接日志去重列表
#        - blocked_processes   拦截记录
#
# 3. ai_keys.json —— AI API Key（敏感，独立存储，清空缓存不影响）
#    存：各 AI 平台（zhipu/siliconflow/deepseek/xfyun/qwen/ernie/groq）的 API Key
#    特点：与 config.json 分离，避免被误删/误传，清空缓存不影响
#
# 其他文件（非 JSON 配置）：
# - logs/app.log          应用日志
# - logs/监控日志.log      监控拦截日志
# - logs/错误日志.log       错误日志（含人话原因）
# - logs/链接记录日志.log    符号链接操作记录
# - logs/识别记录.json      软件识别记录（debug 用）
# - src/data/software_dict.json 软件识别字典（静态只读数据，随源码分发）
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"  # 运行时状态文件（与 config.json 分离）
LOG_FILE = LOG_DIR / "app.log"
LINK_LOG_FILE = LOG_DIR / "链接记录日志.log"
# 词典目录：打包模式优先 exe 内 _MEIPASS/data（打进 exe），缺失回退 exe 同级 data/；源码模式 = src/data
if getattr(sys, "frozen", False):
    _meipass = getattr(sys, "_MEIPASS", None)
    if _meipass and (Path(_meipass) / "data").is_dir():
        _DICT_DIR = Path(_meipass) / "data"
    else:
        _DICT_DIR = Path(sys.executable).resolve().parent / "data"
else:
    _DICT_DIR = Path(__file__).resolve().parent.parent / "data"
SOFTWARE_DICT_FILE = _DICT_DIR / "software_dict.json"
# 程序图标：打包模式优先 exe 内 _MEIPASS/ico，缺失回退 exe 同级 ico/；源码模式 = 项目根/ico
if getattr(sys, "frozen", False):
    _meipass2 = getattr(sys, "_MEIPASS", None)
    if _meipass2 and (Path(_meipass2) / "ico").is_dir():
        _ICO_DIR = Path(_meipass2) / "ico"
    else:
        _ICO_DIR = Path(sys.executable).resolve().parent / "ico"
else:
    _ICO_DIR = Path(__file__).resolve().parent.parent.parent / "ico"
APP_ICON_FILE = _ICO_DIR / "C盘拦迁器图标.ico"
RECOGNITION_LOG_FILE = LOG_DIR / "识别记录.json"
MONITOR_LOG_FILE = LOG_DIR / "监控日志.log"
ERROR_LOG_FILE = LOG_DIR / "错误日志.log"
# AI API Key 独立存储文件（与 config.json 分离，清空缓存不影响 Key）
# 避免敏感 Key 混在 config.json 中被误删或误传
AI_KEYS_FILE = BASE_DIR / "ai_keys.json"
G_ROOT = "D:\\"

# 配置字段集合（存入 config.json，用户可手动编辑）
# 仅放用户设置类字段，不放任何运行时状态或缓存
CONFIG_FIELDS = {
    "g_root", "scan_interval", "auto_migrate", "size_threshold",
    "auto_clean_vss", "ai_recognize", "whitelist", "removed_default_whitelist",
    "verify_hash", "copy_threads", "copy_threads_auto",  # P5/P10 用户选项:复制校验开关/线程数(须落盘保存)
    "user_dir_notify_enabled",  # 用户目录写入提醒（右下角气泡）
    "config_version",  # #28:配置文件版本号(结构变更时递增,加载时检查)
}

# 状态字段集合（存入 state.json，程序自己维护，用户不应手动编辑）
# 包含关键持久数据（migrated/pending_*/dev_env_configured）和可重建缓存
STATE_FIELDS = {
    # === 关键持久数据（不可丢失）===
    "migrated",            # 已迁移记录（src/dst/size/desc/time）
    "pending_migrations",  # 未完成迁移事务（断电/崩溃恢复依据，必须持久化）
    "pending_restores",    # 未完成还原事务（断电/崩溃恢复依据，必须持久化）
    "dev_env_configured",  # 开发环境配置记录（环境变量/配置命令/卸载辅助）
    # === 可重建缓存（丢失可重新生成）===
    "scan_cache",          # 待迁移扫描结果
    "scan_cache_time",     # 扫描时间戳
    "desc_cache",          # 软件描述缓存
    "dev_env_status_cache",  # 开发环境工具状态缓存（启动时快速加载）
    "logged_symlinks",     # 链接日志去重列表
    "blocked_processes",   # 拦截记录
    "deleted_links",       # 删除链接恢复线索 [{src, dst, time, file_count, size_mb}]
    "dst_index",           # 已迁移目标目录轻量索引（跨盘校对值，后台构建，可重建）
    "state_version",       # #28:状态文件版本号(结构变更时递增,加载时检查)
}

DEFAULT_CONFIG = {
    "config_version": CONFIG_VERSION,  # #28:配置文件版本号
    "language": "zh_cn",  # 界面语言（zh_cn/en_us，i18n 语言包）
    "g_root": G_ROOT,
    "scan_interval": 60,
    "auto_migrate": False,
    "size_threshold": 50,
    "verify_hash": True,   # P5 用户选项:复制后 BLAKE3 哈希校验(顶部设置区"复制校验"开关)
    "copy_threads": 12,    # P5 用户选项:复制/校验线程数(手动输入,上限=CPU 逻辑线程数)
    "copy_threads_auto": True,  # P10:线程数自动分级(按 CPU 逻辑线程数低/中/高端,默认开)
    "auto_clean_vss": False,  # 迁移/还原后自动清理 VSS 卷影副本（2026-08-09 改默认关：会删除系统所有还原点，软迁移不依赖它，开启需用户明确选择）
    "user_dir_notify_enabled": True,  # 用户目录写入提醒（右下角气泡，默认开；气泡点"不再提醒"或界面开关可关闭）
    "whitelist": [],  # 用户白名单（放行的安装命令）
    "removed_default_whitelist": [],  # 用户删除的默认白名单关键词（重启后不再生效）
    # AI 联网识别配置（API Key 不在此处存储，独立保存到 ai_keys.json）
    "ai_recognize": {
        "platform": "zhipu",          # 平台：zhipu/siliconflow/deepseek/xfyun/qwen/ernie/groq
        "enabled": False,             # 是否启用 AI 识别
        "auto_fill_on_scan": False,   # 扫描时自动补全空描述（默认关闭，避免首扫太慢）
        "batch_size": 20,             # 每批识别数量（建议 10-50）
        "last_used_at": ""            # 上次使用时间
        # 注意：api_keys 已迁移到 ai_keys.json 独立文件，清空缓存不影响
    }
}

# 运行时状态默认值（存入 state.json）
DEFAULT_STATE = {
    "state_version": STATE_VERSION,  # #28:状态文件版本号
    "migrated": [],
    "scan_cache": [],
    "scan_cache_time": "",
    "desc_cache": {},  # {路径: desc} 软件描述缓存，避免每次扫描都重新识别
    "logged_symlinks": [],  # 已记录到链接日志的符号链接src路径（去重用）
    "blocked_processes": [],  # 拦截记录
    "deleted_links": [],   # 删除链接恢复线索 [{src, dst, time, file_count, size_mb}]
    "pending_migrations": [],  # 未完成迁移事务（断电/崩溃恢复依据）
    "pending_restores": [],    # 未完成还原事务（断电/崩溃恢复依据）
}

# 全局logger实例（main.py会import这个）
log = None


def _load_software_dict():
    """从 software_dict.json 加载软件识别字典"""
    empty = ({}, {})
    try:
        if not SOFTWARE_DICT_FILE.exists():
            return empty
        with open(SOFTWARE_DICT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        known = data.get("known_software_dirs", {}) or {}
        combo = data.get("combo_map", {}) or {}
        return known, combo
    except Exception:
        return empty


def _record_recognition(dir_path, description, method):
    """把识别到的软件/目录记录到 识别记录.json"""
    try:
        data = {}
        if RECOGNITION_LOG_FILE.exists():
            try:
                with open(RECOGNITION_LOG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data[dir_path] = {
            "description": description,
            "method": method,
            "dir_name": os.path.basename(dir_path),
            "updated_at": ts
        }
        with open(RECOGNITION_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


KNOWN_SOFTWARE_DIRS, COMBO_MAP = _load_software_dict()


def log_link_operation(action, src, dst="", extra=""):
    """记录链接操作到单独的日志文件"""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{action}] {src}"
        if dst:
            line += f" -> {dst}"
        if extra:
            line += f"  | {extra}"
        line += "\n"
        with open(LINK_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
        # #24 修复:链接记录日志加轮转(监控/错误日志都有,此日志此前遗漏,
        # 长期运行无限增长;rotate_log_if_needed 自带 60 秒节流)
        try:
            rotate_log_if_needed(LINK_LOG_FILE)
        except Exception:
            pass
    except Exception:
        pass


def setup_logging():
    """配置日志 - 每7天一个文件，自动删除1年以上日志"""
    global log
    from logging.handlers import TimedRotatingFileHandler
    logger = logging.getLogger("CDriveRelocator")
    logger.setLevel(logging.DEBUG)
    # backupCount=52：保留52周（1年）的日志，每7天一个文件
    # 总占用上限：约 772KB × 52 ≈ 40MB（实际更小，旧日志通常更短）
    fh = TimedRotatingFileHandler(
        str(LOG_FILE), when="D", interval=7, backupCount=52, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    # 重复消息限频：相同消息 2 秒内只写一条（解决异步补全/扫描时
    # [WinError 2] 等同类 DEBUG 异常刷屏 app.log 的问题）
    fh.addFilter(DuplicateLogFilter(interval=2.0))
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(ch)
    _cleanup_old_logs()
    log = logger
    return logger


class DuplicateLogFilter(logging.Filter):
    """重复日志限频过滤器：相同消息在 interval 秒内只放行一次。

    用于 DEBUG 级别高频异常（如异步识别时 WMI/lnk 读取失败反复抛
    [WinError 2]），保留第一条完整信息，后续相同消息静默丢弃，
    避免 app.log 被同类日志刷屏。
    """
    def __init__(self, interval=2.0):
        super().__init__()
        self.interval = interval
        self._last = {}  # message -> last timestamp

    def filter(self, record):
        # 只对 DEBUG 级别限频（INFO 及以上的重要消息不限制）
        if record.levelno > logging.DEBUG:
            return True
        key = record.getMessage()
        now = time.time()
        last = self._last.get(key, 0)
        if now - last < self.interval:
            return False
        self._last[key] = now
        # 控制 dict 大小（上限 500 条，防止长运行后内存膨胀）
        if len(self._last) > 500:
            self._last = {k: v for k, v in self._last.items()
                          if now - v < 60}
        return True


def _cleanup_old_logs():
    """清理1年以上的日志文件（与 backupCount=52 配合）"""
    try:
        import glob
        log_dir = LOG_FILE.parent
        log_name = LOG_FILE.name
        now = time.time()
        one_year_sec = 365 * 24 * 3600
        for f in glob.glob(str(log_dir / (log_name + "*"))):
            try:
                mtime = os.path.getmtime(f)
                if now - mtime > one_year_sec:
                    os.remove(f)
            except Exception:
                pass
    except Exception:
        pass


# 日志大小轮转阈值（10MB），超过则自动轮转保留1个备份
LOG_ROTATE_MAX_SIZE = 10 * 1024 * 1024  # 10MB
# 上次轮转检查时间（避免每次写日志都 stat 文件）
_log_rotate_last_check = 0


def rotate_log_if_needed(file_path):
    """检查日志文件大小，超过阈值则轮转（保留1个备份）

    用于监控日志.log 和错误日志.log（无内置轮转机制的直接追加写入文件）。
    轮转策略：file.log → file.log.1，新文件从空开始。
    保留1个备份，总占用上限 = 2 × LOG_ROTATE_MAX_SIZE = 20MB。

    性能优化：每 60 秒最多检查一次文件大小（stat 调用），
    避免高频写日志时每次 stat 带来的 IO 开销。
    """
    global _log_rotate_last_check
    try:
        now = time.time()
        # 60秒内不重复检查（stat 有 IO 开销）
        if now - _log_rotate_last_check < 60:
            return
        _log_rotate_last_check = now
        p = Path(file_path)
        if not p.exists():
            return
        size = p.stat().st_size
        if size < LOG_ROTATE_MAX_SIZE:
            return
        # 轮转：file.log → file.log.1（覆盖旧备份）
        bak = p.with_suffix(p.suffix + ".1")
        try:
            if bak.exists():
                os.remove(bak)
        except Exception:
            pass
        os.rename(str(p), str(bak))
    except Exception:
        pass


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def load_ai_keys():
    """从 ai_keys.json 加载所有平台的 API Key
    返回 {platform: api_key} 字典。文件不存在或损坏返回空字典。
    API Key 与 config.json 分离存储，清空缓存不会影响 Key。
    """
    try:
        if not AI_KEYS_FILE.exists():
            return {}
        with open(AI_KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}


def save_ai_keys(keys_dict):
    """保存所有平台的 API Key 到 ai_keys.json
    :param keys_dict: {platform: api_key} 字典
    原子写入(H1 修复):与 save_config/save_state 一致走 _atomic_write_json,
    避免断电/崩溃导致 7 平台 API Key 文件残缺。
    """
    try:
        _atomic_write_json(AI_KEYS_FILE, keys_dict)
    except Exception as e:
        log_error_with_reason("AI Key 文件保存失败", str(e), f"save_ai_keys: {AI_KEYS_FILE}")


def load_config():
    """加载配置文件（只含配置字段，状态字段在 state.json）"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # #28:版本号检查(文件高于程序支持→警告降级;低于→迁移扩展点)
            _check_file_version(
                data.get("config_version") if isinstance(data, dict) else None,
                CONFIG_VERSION, "config.json")
            # 只读取配置字段，忽略可能残留的旧状态字段
            for k in cfg:
                if k in data:
                    cfg[k] = data[k]
            # 兼容旧版：把 config.json 里的 api_keys 迁移到独立的 ai_keys.json
            ai = cfg.get("ai_recognize", {})
            if isinstance(ai, dict):
                existing_keys = load_ai_keys() or {}
                migrated = False
                # 旧版 api_keys 字段 → 迁移到独立文件
                if "api_keys" in ai and isinstance(ai.get("api_keys"), dict):
                    for pid, k in ai["api_keys"].items():
                        if k and not existing_keys.get(pid):
                            existing_keys[pid] = k
                            migrated = True
                    del ai["api_keys"]  # 从 config.json 中移除
                # 更旧版单字段 api_key → 迁移到独立文件
                if "api_key" in ai:
                    old_key = ai.get("api_key", "")
                    if old_key and ai.get("platform"):
                        pid = ai["platform"]
                        if not existing_keys.get(pid):
                            existing_keys[pid] = old_key
                            migrated = True
                    del ai["api_key"]
                if migrated:
                    save_ai_keys(existing_keys)
                cfg["ai_recognize"] = ai
            # 首次升级到拆分版：把旧 config.json 中的状态字段迁移到 state.json
            _migrate_state_from_old_config(data)
            return cfg
        except Exception as e:
            log_error_with_reason("配置文件加载失败", str(e), f"load_config: {CONFIG_FILE}")
            # 备份损坏的配置文件（带时间戳，避免多次损坏覆盖），方便用户恢复
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                bak = f"{CONFIG_FILE}.corrupted.{ts}"
                shutil.copy2(CONFIG_FILE, bak)
                log_error_with_reason("配置文件已备份",
                    f"损坏的 config.json 已备份为 {bak}，使用默认配置启动",
                    "load_config")
            except Exception:
                pass
            return copy.deepcopy(DEFAULT_CONFIG)
    return copy.deepcopy(DEFAULT_CONFIG)


def _migrate_state_from_old_config(old_config_data):
    """首次升级时，把旧 config.json 中的状态字段迁移到 state.json
    迁移完成后从 config.json 中移除状态字段，保持 config.json 纯净。
    """
    if not isinstance(old_config_data, dict):
        return
    # 检查是否需要迁移（state.json 不存在 且 旧 config.json 含状态字段）
    if STATE_FILE.exists():
        return  # state.json 已存在，不需要迁移
    has_state_fields = any(k in old_config_data for k in STATE_FIELDS)
    has_deprecated = 'trusted_installers' in old_config_data
    if not has_state_fields and not has_deprecated:
        return
    try:
        # 提取状态字段写入 state.json
        state = DEFAULT_STATE.copy()
        for k in state:
            if k in old_config_data:
                state[k] = old_config_data[k]
        # H2 修复:改用原子写入,避免迁移中途断电产生残缺 state.json
        _atomic_write_json(STATE_FILE, state)
        # 从 config.json 中移除状态字段和废弃字段，重写纯净的 config.json
        clean_cfg = {}
        for k, v in old_config_data.items():
            if k in CONFIG_FIELDS:
                clean_cfg[k] = v
        # 不移除 api_keys 迁移逻辑（上面 load_config 已处理）
        _atomic_write_json(CONFIG_FILE, clean_cfg)
    except Exception as e:
        log_error_with_reason("状态字段迁移失败", str(e), "_migrate_state_from_old_config")


def load_state():
    """加载运行时状态（从 state.json）"""
    state = DEFAULT_STATE.copy()
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # #28:版本号检查(文件高于程序支持→警告降级;低于→迁移扩展点)
            _check_file_version(
                data.get("state_version") if isinstance(data, dict) else None,
                STATE_VERSION, "state.json")
            for k in state:
                if k in data:
                    state[k] = data[k]
        except Exception as e:
            log_error_with_reason("状态文件加载失败", str(e), f"load_state: {STATE_FILE}")
            # 备份损坏的状态文件（带时间戳，避免多次损坏覆盖），方便用户恢复
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                bak = f"{STATE_FILE}.corrupted.{ts}"
                shutil.copy2(STATE_FILE, bak)
                log_error_with_reason("状态文件已备份",
                    f"损坏的 state.json 已备份为 {bak}，使用默认状态启动（pending 事务可能丢失）",
                    "load_state")
            except Exception:
                pass
    return state


def _check_file_version(file_version, current_version, file_desc):
    """#28:配置文件版本号检查。

    文件版本高于程序支持 → 警告并降级加载(不破坏数据,新字段可能丢失);
    文件版本低于当前 → 预留迁移扩展点(当前无历史版本,未来在此做字段迁移)。
    文件无版本号(旧版) → 静默(首次升级自动写入当前版本)。
    """
    if file_version is None:
        return
    if file_version > current_version:
        log_error_with_reason(f"{file_desc}版本高于程序支持",
            f"文件版本 v{file_version} > 程序支持 v{current_version}，"
            f"按当前版本加载，新字段可能丢失。请升级程序。", "config_version_check")
    elif file_version < current_version:
        # 低版本迁移扩展点：未来结构变更时在此按版本逐级迁移
        pass


def _atomic_write_json(file_path, data):
    """原子写入 JSON 文件：先写到唯一 .tmp，再 os.replace 替换原文件
    os.replace 在 Windows 上是原子操作，断电时不会产生残缺 JSON。
    最坏情况：.tmp 写到一半断电 → 原文件完好；replace 后断电 → 新文件已完整。
    使用 PID+线程ID 生成唯一 .tmp 文件名，避免多线程并发写同一文件时冲突损坏。
    """
    import threading
    tmp_path = f"{file_path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(file_path))
    except Exception:
        # 原子写入失败，清理残留的 .tmp 文件
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise


def save_config(cfg):
    """保存配置（只写配置字段到 config.json，状态字段不写入）
    使用原子写入：先写 .tmp 再 os.replace，避免断电导致文件残缺。
    """
    try:
        out = {k: cfg.get(k) for k in CONFIG_FIELDS if k in cfg}
        _atomic_write_json(CONFIG_FILE, out)
    except Exception as e:
        log_error_with_reason("配置文件保存失败", str(e), f"save_config: {CONFIG_FILE}")


def save_state(state):
    """保存运行时状态（只写状态字段到 state.json）
    使用原子写入：先写 .tmp 再 os.replace，避免断电导致 pending 事务丢失。
    """
    try:
        out = {k: state.get(k) for k in STATE_FIELDS if k in state}
        _atomic_write_json(STATE_FILE, out)
    except Exception as e:
        log_error_with_reason("状态文件保存失败", str(e), f"save_state: {STATE_FILE}")


def save_all(cfg):
    """同时保存配置和状态（cfg 是合并后的字典，分别写入 config.json 和 state.json）"""
    save_config(cfg)
    save_state(cfg)


# ========== 错误日志系统 ==========
# 错误类型 → 人话原因映射表（穷举所有可能的错误场景）
# 每条映射包含：错误类型关键字、可能原因（人话）、建议处理方式
ERROR_REASON_MAP = {
    # ===== 迁移相关错误 =====
    "源目录不存在": {
        "reason": "要迁移的C盘目录已被删除或移动，可能软件已卸载或目录路径变更",
        "action": "请检查C盘对应路径是否存在，或刷新待迁移列表重新扫描",
    },
    "复制失败": {
        "reason": "文件复制过程中出错，常见原因：1)文件被软件占用 2)目标盘空间不足 3)权限不足 4)路径过长或含特殊字符",
        "action": "请关闭可能占用该目录的软件，检查目标盘剩余空间，以管理员权限运行本工具",
    },
    "删除源目录失败": {
        "reason": "C盘原目录中有文件正在被使用，无法删除",
        "action": "请关闭所有可能使用该目录的软件（如浏览器、聊天软件等），然后重试。数据已复制到目标盘，不会丢失",
    },
    "创建符号链接失败": {
        "reason": "创建符号链接需要管理员权限，或C盘目录被占用",
        "action": "请以管理员权限运行本工具（右键→以管理员身份运行），并确保C盘目录未被占用",
    },
    "已经是符号链接": {
        "reason": "该目录已经是符号链接，无需重复迁移",
        "action": "无需操作，如需重新迁移请先还原",
    },
    # ===== 还原相关错误 =====
    "找不到迁移记录": {
        "reason": "config.json中没有该目录的迁移记录，可能是手动删除了配置或配置文件损坏",
        "action": "请在已迁移表中检查该记录是否存在，或手动指定目标盘路径后使用重建链接功能",
    },
    "目标盘数据不存在": {
        "reason": "目标盘（如G盘）上的备份数据目录不存在，可能U盘/移动硬盘已拔出，或数据被误删",
        "action": "请检查目标盘是否已连接，确认备份数据目录是否存在。如果数据已丢失，无法还原",
    },
    "删除C盘目录失败": {
        "reason": "C盘真实目录中有文件被占用，无法删除",
        "action": "请关闭所有可能使用该目录的软件，等待几分钟后重试。可尝试在任务管理器中结束相关进程",
    },
    "还原复制失败": {
        "reason": "数据从目标盘复制回C盘时出错，常见原因：1)C盘空间不足 2)文件被占用 3)权限不足",
        "action": "请检查C盘剩余空间，关闭相关软件后重试",
    },
    # ===== 修复链接相关错误 =====
    "合并数据失败": {
        "reason": "将C盘新数据合并到目标盘时出错，复制引擎返回错误码",
        "action": "请关闭可能占用该目录的软件，检查目标盘空间和权限后重试",
    },
    "删除C盘目录失败且重命名也失败": {
        "reason": "C盘目录既无法删除也无法重命名，通常是因为核心文件被系统或软件锁定",
        "action": "请重启电脑后再试，或在任务管理器中结束相关软件进程。下次自动检查时会重试",
    },
    "修复异常": {
        "reason": "修复过程中发生未预期的错误",
        "action": "请查看详细错误信息，并联系开发者反馈。数据安全，不会丢失",
    },
    # ===== 扫描相关错误 =====
    "扫描失败": {
        "reason": "扫描C盘目录时出错，可能是权限不足或目录结构异常",
        "action": "请以管理员权限运行本工具，或稍后重试",
    },
    "智能扫描失败": {
        "reason": "智能刷新扫描时出错，可能是目录权限问题或文件系统异常",
        "action": "请以管理员权限运行，或使用顶部全盘刷新按钮",
    },
    "刷新已迁移表失败": {
        "reason": "读取已迁移记录时出错，可能是config.json格式损坏或文件被占用",
        "action": "请检查config.json是否可正常打开，必要时删除该文件重新配置",
    },
    # ===== 链接操作错误 =====
    "删除链接失败": {
        "reason": "删除符号链接时出错，可能权限不足或链接被系统占用",
        "action": "请以管理员权限运行本工具后重试",
    },
    "重建链接失败": {
        "reason": "重建符号链接时出错，常见原因：1)非管理员权限 2)C盘已有同名目录 3)路径含特殊字符",
        "action": "请以管理员权限运行，确保C盘对应路径已清理",
    },
    # ===== 联网搜索错误 =====
    "联网搜索失败": {
        "reason": "联网获取软件说明时出错，可能是网络连接问题或Wikipedia API不可达",
        "action": "请检查网络连接，或稍后重试。也可手动双击说明列进行编辑",
    },
    # ===== COM初始化错误 =====
    "COM初始化失败": {
        "reason": "后台线程初始化COM组件失败，影响lnk快捷方式和win32api调用",
        "action": "通常不影响核心功能，可忽略。如频繁出现请重启电脑",
    },
    # ===== 进程拦截错误 =====
    "进程拦截异常": {
        "reason": "检测安装器进程时出错，可能是psutil库异常或系统权限不足",
        "action": "请以管理员权限运行以获得完整拦截能力",
    },
    # ===== 配置文件错误 =====
    "配置文件加载失败": {
        "reason": "config.json格式错误或损坏，无法正常解析",
        "action": "将使用默认配置启动，建议备份后删除损坏的config.json",
    },
    "配置文件保存失败": {
        "reason": "config.json写入失败，可能文件被其他程序占用或磁盘空间不足",
        "action": "请关闭可能占用该文件的程序，检查磁盘空间",
    },
    # ===== 软件识别错误 =====
    "软件识别异常": {
        "reason": "识别目录对应的软件时发生错误，可能是PE版本信息读取失败或注册表访问异常",
        "action": "不影响核心功能，可手动编辑说明列",
    },
    # ===== 常见Python异常关键字（技术错误翻译）=====
    "maketrans": {
        "reason": "字符串转换函数str.maketrans参数错误，通常是两个等长字符串参数之间漏了逗号被拼接成一个",
        "action": "检查str.maketrans调用，确保两个字符串参数用逗号分隔，且长度相等",
    },
    "must be a dict": {
        "reason": "str.maketrans只传一个参数时必须是字典，实际传了字符串",
        "action": "检查str.maketrans调用，应为两个等长字符串参数或一个字典",
    },
    "FileNotFoundError": {
        "reason": "文件或目录不存在，路径可能已删除、移动或拼写错误",
        "action": "检查路径是否正确，或刷新列表重新扫描",
    },
    "No such file or directory": {
        "reason": "系统找不到指定文件或目录",
        "action": "检查路径是否正确，确认文件/目录是否存在",
    },
    "PermissionError": {
        "reason": "权限不足，无法访问或修改文件/目录",
        "action": "请以管理员身份运行本工具",
    },
    "Access is denied": {
        "reason": "访问被拒绝，通常是权限不足或文件被占用",
        "action": "请以管理员身份运行，或关闭可能占用该文件的软件",
    },
    "TimeoutError": {
        "reason": "操作超时，网络请求或文件操作在规定时间内未完成",
        "action": "请检查网络连接，或稍后重试",
    },
    "timed out": {
        "reason": "网络请求超时，目标服务器未在规定时间内响应",
        "action": "请检查网络连接，或稍后重试",
    },
    "ConnectionError": {
        "reason": "网络连接错误，无法连接到目标服务器",
        "action": "请检查网络连接是否正常",
    },
    "URLError": {
        "reason": "URL请求错误，可能是网络不通或DNS解析失败",
        "action": "请检查网络连接，或稍后重试",
    },
    "Connection refused": {
        "reason": "连接被拒绝，目标服务器拒绝连接",
        "action": "请稍后重试，或检查目标服务是否可用",
    },
    "Network is unreachable": {
        "reason": "网络不可达，可能未联网或网络配置异常",
        "action": "请检查网络连接是否正常",
    },
    "Name or service not known": {
        "reason": "DNS解析失败，无法解析域名",
        "action": "请检查网络连接和DNS设置",
    },
    "SSL: CERTIFICATE_VERIFY_FAILED": {
        "reason": "SSL证书验证失败，可能是系统时间错误或证书过期",
        "action": "请检查系统时间是否正确，或稍后重试",
    },
    "JSONDecodeError": {
        "reason": "JSON解析失败，配置文件或API返回的数据格式错误",
        "action": "请检查对应JSON文件是否格式正确，必要时备份后删除重建",
    },
    "Expecting value": {
        "reason": "JSON解析失败，通常是因为文件为空或内容不是有效JSON",
        "action": "请检查对应文件内容，必要时备份后删除让程序重建",
    },
    "KeyError": {
        "reason": "字典中找不到指定的键，通常是配置项缺失或数据结构变更",
        "action": "请尝试重置配置文件，或联系开发者反馈",
    },
    "ValueError": {
        "reason": "值错误，传入了不合法的值（如把字符串转数字失败）",
        "action": "请检查输入是否正确，或刷新数据后重试",
    },
    "TypeError": {
        "reason": "类型错误，传入了错误类型的数据（如需要字符串却传了None）",
        "action": "请刷新数据后重试，或联系开发者反馈",
    },
    "AttributeError": {
        "reason": "属性错误，对象没有该属性或方法，可能是库版本不兼容",
        "action": "请检查依赖库版本，或联系开发者反馈",
    },
    "IndexError": {
        "reason": "索引越界，访问了列表/数组中不存在的位置",
        "action": "请刷新数据后重试，或联系开发者反馈",
    },
    "UnicodeDecodeError": {
        "reason": "编码解码失败，文件内容编码与预期不符",
        "action": "程序已做容错处理，通常可忽略",
    },
    "OSError": {
        "reason": "操作系统错误，可能是文件被占用、路径过长或权限不足",
        "action": "请关闭可能占用的软件，以管理员身份运行",
    },
    "ImportError": {
        "reason": "模块导入失败，可能缺少依赖库或库版本不兼容",
        "action": "请安装所需依赖：pip install -r requirements.txt",
    },
    "ModuleNotFoundError": {
        "reason": "找不到指定模块，可能未安装该依赖库",
        "action": "请安装所需依赖：pip install 模块名",
    },
    "CalledProcessError": {
        "reason": "子进程调用返回非零退出码，通常是复制引擎/mklink等命令执行失败",
        "action": "请查看详细错误信息，检查命令参数和权限",
    },
    "MemoryError": {
        "reason": "内存不足，程序无法分配所需内存",
        "action": "请关闭其他占用内存的程序后重试",
    },
    "RecursionError": {
        "reason": "递归调用过深，超过Python最大递归限制",
        "action": "请重启程序，或联系开发者反馈",
    },
    "HTTPError": {
        "reason": "HTTP请求返回错误状态码，如404(不存在)、500(服务器错误)",
        "action": "请稍后重试，或检查请求的目标地址是否正确",
    },
    "404 Not Found": {
        "reason": "请求的资源不存在，URL地址错误或资源已删除",
        "action": "请稍后重试，或检查请求地址",
    },
    "500 Internal Server Error": {
        "reason": "服务器内部错误，目标网站服务异常",
        "action": "请稍后重试",
    },
    "name is not defined": {
        "reason": "变量名未定义，通常是代码中使用了未导入或未声明的变量",
        "action": "请重启程序，或联系开发者反馈",
    },
    "object is not subscriptable": {
        "reason": "对象不支持索引访问，通常是把None当成了列表/字典使用",
        "action": "请刷新数据后重试，或联系开发者反馈",
    },
    "argument must be": {
        "reason": "参数类型错误，传入了不符合要求的类型",
        "action": "请刷新数据后重试，或联系开发者反馈",
    },
    # ===== 通用兜底 =====
    "未知错误": {
        "reason": "发生了未预期的错误",
        "action": "请查看详细错误信息，必要时联系开发者反馈",
    },
}


def _match_error_type(error_msg):
    """从错误消息中匹配最合适的错误类型
    :param error_msg: 原始错误消息字符串
    :return: (error_type, reason_dict) 或 (None, None)
    """
    if not error_msg:
        return None, None
    msg = str(error_msg)
    # 按精确度从高到低匹配（先匹配长关键字，避免误匹配）
    sorted_keys = sorted(ERROR_REASON_MAP.keys(), key=len, reverse=True)
    for key in sorted_keys:
        if key in msg:
            return key, ERROR_REASON_MAP[key]
    return "未知错误", ERROR_REASON_MAP["未知错误"]


# 错误日志去重：进程内同一错误（类型+上下文）只记录一次，避免日志爆炸
_logged_errors = set()


def log_error_with_reason(error_type_or_msg, original_error="", context=""):
    """记录错误到错误日志文件，附带人话原因和建议处理方式
    :param error_type_or_msg: 错误类型关键字或原始错误消息（会自动匹配）
    :param original_error: 原始异常对象的字符串形式（保留英文/技术细节）
    :param context: 错误发生的上下文（如"迁移C:\\Users\\xxx\\AppData"）
    """
    try:
        # 去重：同一错误类型+上下文，本次运行只记录一次
        dedup_key = f"{error_type_or_msg}|{context}"
        if dedup_key in _logged_errors:
            return
        _logged_errors.add(dedup_key)

        # 匹配错误类型
        matched_type, reason_info = _match_error_type(error_type_or_msg)
        is_fallback = False
        if not matched_type:
            matched_type = "未知错误"
            reason_info = ERROR_REASON_MAP["未知错误"]
            is_fallback = True

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"{'='*60}",
            f"[时间] {ts}",
            f"[错误类型] {matched_type}",
        ]
        if context:
            lines.append(f"[发生场景] {context}")
        # 原始错误显示规则：
        # 1. original_error 非空 → 直接显示
        # 2. original_error 为空且 fallback 到"未知错误" → 用 error_type_or_msg 作为原始错误（避免英文原文丢失）
        if original_error:
            lines.append(f"[原始错误] {original_error}")
        elif is_fallback and error_type_or_msg and error_type_or_msg != "未知错误":
            lines.append(f"[原始错误] {error_type_or_msg}")
        lines.append(f"[可能原因] {reason_info['reason']}")
        lines.append(f"[建议处理] {reason_info['action']}")
        lines.append("")  # 空行分隔
        with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        # 日志轮转：超过10MB自动轮转，避免无限增长（每60秒检查一次）
        rotate_log_if_needed(ERROR_LOG_FILE)
    except Exception:
        pass
