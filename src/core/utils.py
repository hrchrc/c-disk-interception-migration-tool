#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具函数 - 目录大小、符号链接、PE版本信息、lnk快捷方式、注册表匹配"""

import os
import ctypes
import threading
import functools
import logging
from pathlib import Path

# 与主项目共用日志通道（main.py 配置的 'CDriveRelocator' handler）
log = logging.getLogger('CDriveRelocator')

# 全局 MFT 扫描器单例（由 main.py 启动时注入）
# 未注入时 get_dir_size_fast 自动回退到 os.walk
_mft_scanner = None


def link_fix_locked(fn):
    """H3 修复:链接修复互斥锁装饰器。

    后台 _auto_fix_link(每 30 秒周期)与手动 fix_broken_link 并发时,
    两个引擎作业会写同一目标目录且互相覆盖 _engine_for_cancel(取消失效);
    加锁保证同一时刻只有一个修复作业。

    锁对象优先取 self._link_fix_lock(Migrator 实例),其次 self.migrator._link_fix_lock
    (MonitorWorker 等持有 migrator 引用的宿主),都没有则退化不加锁(兼容旧代码)。
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        lock = getattr(self, "_link_fix_lock", None)
        if lock is None:
            mig = getattr(self, "migrator", None)
            lock = getattr(mig, "_link_fix_lock", None)
        if lock is None:
            return fn(self, *args, **kwargs)
        with lock:
            return fn(self, *args, **kwargs)
    return wrapper

def set_mft_scanner(scanner):
    """注入全局 MftScanner 单例（由 main.py 启动时调用）

    替换前若已存在旧单例，先关闭其内部 MFT 卷句柄，避免反复清缓存重新加载
    导致卷句柄泄漏至进程退出。
    """
    global _mft_scanner
    old = _mft_scanner
    if old is not None and old is not scanner:
        try:
            old.close()
        except Exception as e:
            log.debug("忽略异常: %s", e)
    _mft_scanner = scanner

def get_mft_scanner():
    """获取全局 MftScanner 单例（可能为 None）"""
    return _mft_scanner

def get_dir_size_fast(path):
    """快速获取目录大小（MB，保留 1 位小数）

    优先使用 MFT 扫描器（O(1) 查预计算缓存，任意深度目录都准确），
    MFT 未加载或路径不在当前卷时自动回退到 os.walk。
    """
    # 优先用 MFT 单例
    scanner = _mft_scanner
    if scanner is not None and scanner._loaded:
        try:
            return scanner.get_dir_size_mft(path)
        except Exception:
            pass  # 出错则回退到 os.walk
    # os.walk 兜底
    try:
        total = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except Exception as e:
                    log.debug("忽略异常: %s", e)
        # 保留 6 位小数（最小到 1 字节），避免小目录被 round 成 0.0 显示为"0B"
        return round(total / 1024 / 1024, 6)
    except Exception as e:
        try:
            from config import log_error_with_reason
            log_error_with_reason("未知错误", str(e), f"get_dir_size_fast: {path}")
        except Exception as e:
            log.debug("忽略异常: %s", e)
        return 0

# ========== 云同步占位符检测 ==========
# OneDrive/坚果云等云盘用"占位文件"（RECALL/OFFLINE 属性位）减少本地占用；
# 复制引擎按普通文件读取时会触发强制下载（hydration），弱网/离线时
# 拖慢迁移甚至报错。迁移前检测并提示用户。
# 属性位常量（WinNT.h）：OFFLINE=0x1000 / RECALL_ON_OPEN=0x40000 /
# RECALL_ON_DATA_ACCESS=0x400000
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_PLACEHOLDER_FLAGS = (_FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN
                      | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
_PLACEHOLDER_SCAN_LIMIT = 200  # 命中上限：防止超大目录扫描拖慢迁移


def count_cloud_placeholder_files(root, limit=_PLACEHOLDER_SCAN_LIMIT):
    """统计目录树中的云同步占位文件数（OneDrive 等）

    用 GetFileAttributesW 逐文件查属性位（RECALL/OFFLINE），命中达 limit
    即提前返回——该检查只在迁移前执行一次（后台线程），不能拖慢迁移。
    返回 (count, 首个命中示例路径)；无命中返回 (0, "")；异常返回 (0, "")。
    """
    import ctypes
    try:
        get_attrs = ctypes.windll.kernel32.GetFileAttributesW
        get_attrs.argtypes = [ctypes.c_wchar_p]
        get_attrs.restype = ctypes.c_uint32
    except Exception:
        return 0, ""
    count = 0
    example = ""
    try:
        for _dirpath, _dirnames, filenames in os.walk(root):
            for f in filenames:
                full = os.path.join(_dirpath, f)
                try:
                    attrs = get_attrs(full)
                except Exception:
                    continue
                if attrs != 0xFFFFFFFF and (attrs & _PLACEHOLDER_FLAGS):
                    count += 1
                    if not example:
                        example = full
                    if count >= limit:
                        return count, example
    except Exception:
        return count, example
    return count, example

def is_symlink(path):
    """检测路径是否为符号链接/junction/重解析点
    os.path.islink 只检测符号链接，不检测 junction
    Windows 目录链接有三种：符号链接、junction、重解析点，都需要跳过
    用 os.lstat 的 S_ISLNK + reparse point 标记位检测，避免 ctypes 栈溢出
    """
    try:
        if os.path.islink(path):
            return True
        # 检测 junction：os.lstat 的 st_file_attributes 包含 reparse point 标记
        st = os.lstat(path)
        # FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        # os.lstat 在 Windows 上 st_reparse_tag 非零表示是重解析点
        if hasattr(st, 'st_reparse_tag') and st.st_reparse_tag != 0:
            return True
        return False
    except Exception:
        return False

def is_junction(path):
    """判断路径是否为 junction（目录联接，IO_REPARSE_TAG_MOUNT_POINT=0xA0000003）

    与符号链接（IO_REPARSE_TAG_SYMLINK=0xA000000C）的区别：
    junction 只能指向本地卷目录，常见于系统 XP 兼容链接（Local Settings 等）
    和手动跨盘联接（如 .local → G:\\AI\\...）；
    本工具迁移链接是 /D 符号链接优先（migrator._create_dir_link），
    按此区分可过滤"非本工具迁移产物"的 junction。
    """
    try:
        st = os.lstat(path)
        return getattr(st, 'st_reparse_tag', 0) == 0xA0000003
    except Exception:
        return False

def get_symlink_target(path):
    try:
        target = os.readlink(path)
        # 去除Windows扩展路径前缀 \\?\ 和 \\?\UNC\
        prefix_unc = "\\\\?\\UNC\\"
        prefix_norm = "\\\\?\\"
        if target.lower().startswith(prefix_unc.lower()):
            target = "\\\\" + target[len(prefix_unc):]
        elif target.lower().startswith(prefix_norm.lower()):
            target = target[len(prefix_norm):]
        return target
    except Exception:
        return ""


def is_system_path(path):
    """判断路径是否为Windows系统重要文件/目录"""
    try:
        bs = chr(92)
        p = path.lower().replace(bs, "/")
        prefixes = [
            "c:/program files/windows", "c:/program files (x86)/windows",
            "c:/program files/microsoft", "c:/program files (x86)/microsoft",
            "c:/program files/common files", "c:/program files (x86)/common files",
            "c:/program files/reference assemblies", "c:/program files (x86)/reference assemblies",
            "c:/program files/internet explorer", "c:/program files (x86)/internet explorer",
            "c:/program files/windowspowershell", "c:/program files (x86)/windowspowershell",
            "c:/program files/windowsapps", "c:/program files/modifiablewindowsapps",
            "c:/programdata/microsoft", "c:/programdata/windows",
            "c:/programdata/package cache", "c:/programdata/desktop",
            "c:/programdata/start menu", "c:/programdata/templates",
        ]
        for prefix in prefixes:
            if p.startswith(prefix):
                return True
        # 注意：用户级 AppData\Local\Microsoft\Windows 下是 INetCache/Explorer
        # 等用户级缓存，迁走（符号链接透明）不影响系统，不再标 [系统]
        # 关键词只保留真系统位置在监控目录范围内的：
        # - windows defender / windows security：C:\ProgramData\Microsoft\...（监控内）
        # - windowsapps：C:\Program Files\WindowsApps（监控内）
        # system32/drivers/driverstore/servicing 的真系统位置都在 C:\Windows 下
        # （不在监控目录范围，且删除层已整体保护）；待迁移区命中它们只会
        # 误标用户目录里的同名缓存/模拟目录（如 .wine\...\system32），故不列入
        keywords = ["windows defender", "windows security", "windowsapps"]
        for kw in keywords:
            if kw in p:
                return True
        return False
    except Exception:
        return False


def _read_lnk_target(lnk_path):
    """读取lnk快捷方式的目标路径（用win32com，不依赖PowerShell）"""
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(lnk_path)
        target = shortcut.Targetpath
        if target and target.lower().endswith('.exe'):
            return target
    except Exception as e:
        log.debug("忽略异常: %s", e)
    return ""


def _is_generic_registry_text(text):
    """过滤注册表 DisplayName 中的通用/无意义描述（不应作为软件名返回）
    例：'Install Additional Tools for Node.js'、'Python 3.11.9 pip Bootstrap'、
        '{1D653E80-...}'、'Microsoft Visual C++ ...'、'WinRT Intellisense ...'
    """
    if not text:
        return True
    t = text.strip()
    if not t or len(t) < 2:
        return True
    tl = t.lower()
    # GUID 模式
    import re
    if re.match(r'^\{[0-9A-Fa-f\-]+\}', t):
        return True
    # 版本号开头模式（如 "3.11.9 ..."）
    if re.match(r'^[0-9]+\.[0-9]+\.[0-9]+', t) and len(t) < 30:
        return True
    # 系统/工具链组件描述
    GENERIC_REGISTRY_PATTERNS = [
        'install additional tools for',
        'pip bootstrap',
        'microsoft visual c++',
        'winrt intellisense',
        'windows software development kit',
        'windows driver package',
        'microsoft .net framework',
        'microsoft .net core',
        'windows sdk',
        'addon for visual studio',
        'extension for visual studio',
        'redistributable',
        'runtime -',
        'additional runtime',
        'minimum runtime',
        'language pack',
        'windowsai',
        'machine learning',
    ]
    for p in GENERIC_REGISTRY_PATTERNS:
        if p in tl:
            return True
    return False


# ===== 注册表数据源快照 + 路径索引 =====
# 回退版每次调用全量枚举 Uninstall 注册表（3 个根键），异步补全数百目录时重复
# 枚举数百次拖慢补全。修复：首次调用构建一次快照 + InstallLocation 路径
# 索引（线程安全），后续调用纯内存匹配。
_REG_SNAPSHOT = None          # [(sub_name, display_name, install_location), ...]
_REG_SNAPSHOT_LOCK = threading.Lock()


def _get_registry_snapshot():
    """惰性构建注册表卸载项快照（线程安全，仅首次真实枚举）"""
    global _REG_SNAPSHOT
    if _REG_SNAPSHOT is not None:
        return _REG_SNAPSHOT
    with _REG_SNAPSHOT_LOCK:
        if _REG_SNAPSHOT is not None:
            return _REG_SNAPSHOT
        snapshot = []
        _build_failed = False
        try:
            import winreg
            roots = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            ]
            for root, subkey in roots:
                try:
                    key = winreg.OpenKey(root, subkey)
                except OSError:
                    continue
                try:
                    i = 0
                    while True:
                        try:
                            sub_name = winreg.EnumKey(key, i)
                            i += 1
                        except OSError:
                            break
                        try:
                            sub_key = winreg.OpenKey(key, sub_name)
                        except OSError:
                            continue
                        try:
                            display_name = ""
                            install_loc = ""
                            try:
                                display_name, _ = winreg.QueryValueEx(sub_key, "DisplayName")
                            except OSError:
                                pass
                            try:
                                install_loc, _ = winreg.QueryValueEx(sub_key, "InstallLocation")
                            except OSError:
                                pass
                            if display_name:
                                snapshot.append((str(sub_name), str(display_name), str(install_loc or "")))
                        finally:
                            winreg.CloseKey(sub_key)
                finally:
                    winreg.CloseKey(key)
        except Exception as e:
            log.debug("忽略异常: %s", e)
            _build_failed = True
        if _build_failed:
            # 构建失败不缓存，下次调用重试（避免永久缓存残缺数据）
            return []
        _REG_SNAPSHOT = snapshot
        return snapshot


_REG_LOC_PREFIX = None        # {install_loc 各级真前缀_lower: [display_name, ...]}
_REG_LOC_EXACT = None         # {install_loc 自身_lower: [display_name, ...]}
_REG_LOC_INDEX_LOCK = threading.Lock()


def _build_registry_loc_index():
    """构建 InstallLocation 路径索引（自身 + 全部祖先前缀），双向匹配 O(1) 查询"""
    global _REG_LOC_PREFIX, _REG_LOC_EXACT
    if _REG_LOC_EXACT is not None:
        return _REG_LOC_PREFIX, _REG_LOC_EXACT
    with _REG_LOC_INDEX_LOCK:
        if _REG_LOC_EXACT is not None:
            return _REG_LOC_PREFIX, _REG_LOC_EXACT
        prefix, exact = {}, {}
        for _sub_name, _display_name, _install_loc in _get_registry_snapshot():
            if not _install_loc or not _display_name:
                continue
            loc = _install_loc.lower().rstrip("\\").replace("\\\\", "\\")
            if not loc:
                continue
            exact.setdefault(loc, []).append(_display_name)
            parts = loc.split("\\")
            cur = ""
            for part in parts:
                cur = cur + "\\" + part if cur else part
                if cur != loc:
                    prefix.setdefault(cur, []).append(_display_name)
        _REG_LOC_PREFIX, _REG_LOC_EXACT = prefix, exact
    return _REG_LOC_PREFIX, _REG_LOC_EXACT


def _match_registry_uninstall(dir_path, dir_name):
    """从注册表卸载项匹配软件说明（最准确）
    遍历HKLM和HKCU的Uninstall键，双向匹配InstallLocation或DisplayName
    过滤系统组件描述（如 "Install Additional Tools for Node.js"）
    性能：快照 + 路径索引匹配（首次构建后纯内存 O(1)，不再每次全量枚举注册表）
    """
    try:
        snapshot = _get_registry_snapshot()
        # 待匹配的路径小写形式（规范化，去掉尾斜杠）
        path_lower = dir_path.lower().rstrip("\\").replace("\\\\", "\\")
        # 通用词目录名黑名单：避免厂商容器目录/通用词目录被注册表反查错配到具体产品
        # 例：Adobe → 匹配 "Adobe Creative Cloud"，Google → 匹配任何含 google 的软件
        #     Mozilla → 匹配 "Mozilla Firefox"，Netease → 匹配 "网易 Et"
        #     CrashDumps → 匹配 "Malwarebytes"（恶意软件字节）
        # 这些目录应走第19层厂商容器判定或兜底，不应被注册表反查绑定到具体产品
        _GENERIC_DIR_NAMES_FOR_REGISTRY = {
            'adobe', 'google', 'mozilla', 'tencent', 'netease', 'ncsoft',
            'nvidia', 'amd', 'intel', 'ibm', 'oracle', 'apple', 'microsoft',
            'vmware', 'topaz labs llc', 'saerasoft', 'feelfish', 'wegame',
            'gtarcade', 'spiritrealmworkshop', 'xiumaster', 'steam++',
            'tencent', 'netease', 'ourplayer', 'cef', 'comms', 'cache',
            'crashdumps', 'crashrpt', 'squirreltemp', 'package cache',
            'chromeextensioncache', 'temporary internet files',
            'codebuddyextension', 'workbuddy', 'docker-secrets-engine',
            'vedetector', 'chromedevtoolsmcp', 'hermes', 'mongodbcompass',
            'webview2', 'microsoft_corporation', 'apps', 'programs',
            'packages', 'temp', 'tmp', 'data', 'logs', 'log',
        }
        # 厂商容器目录 + 通用容器目录：完全跳过注册表反查（包括 InstallLocation 父目录匹配）
        # 原因：Adobe 下有 AdobeCreativeCloud 等多个子产品，InstallLocation 父目录匹配
        #       会错配到第一个子产品（如 Adobe Creative Cloud），但 Adobe 是容器目录，
        #       应该走第19层 _detect_vendor_container 识别为"Adobe 容器目录"或子产品分拆
        # 通用容器目录（Programs/Package Cache 等）同理：
        #       Programs 下有 Ollama/Edge 等多个子产品，InstallLocation 父目录匹配
        #       会错配到 Ollama version 0.32.0 等具体产品
        _VENDOR_CONTAINER_DIRS = {
            'adobe', 'google', 'mozilla', 'tencent', 'netease', 'ncsoft',
            'nvidia', 'amd', 'intel', 'ibm', 'oracle', 'apple',
            'vmware', 'topaz labs llc', 'saerasoft', 'feelfish', 'wegame',
            'gtarcade', 'spiritrealmworkshop', 'xiumaster', 'steam++',
            'ourplayer', 'microsoft_corporation',
            # 通用容器目录（多软件共用，InstallLocation 父目录匹配会错配）
            'programs', 'package cache', 'apps', 'packages',
            'common files', 'windowsapps',
        }
        is_vendor_container = dir_name.lower() in _VENDOR_CONTAINER_DIRS
        # 短词（< 4 字符）和厂商容器目录跳过子键名匹配（避免子串误匹配）
        # 仅允许 InstallLocation 路径匹配（更精确）
        skip_subname_match = (
            (len(dir_name) < 4)
            or (dir_name.lower() in _GENERIC_DIR_NAMES_FOR_REGISTRY)
        )
        # 收集所有匹配结果，返回最精确的（与全量枚举版逐条等价的取最长逻辑）
        best_match = ""
        best_score = 0
        if not is_vendor_container:
            # InstallLocation 双向匹配走索引（等价原逻辑，见 _build_registry_loc_index）：
            #   情况1: path == install_loc 或 path 是 install_loc 的前缀 → prefix/exact 索引命中
            #   情况2: path 是 install_loc 的子目录 → install_loc 是 path 的祖先 → exact 索引查祖先链
            _prefix_idx, _exact_idx = _build_registry_loc_index()
            for _dn in _prefix_idx.get(path_lower, ()):
                if len(_dn) > best_score:
                    best_match, best_score = _dn, len(_dn)
            for _dn in _exact_idx.get(path_lower, ()):
                if len(_dn) > best_score:
                    best_match, best_score = _dn, len(_dn)
            _cur = os.path.dirname(path_lower)
            while _cur and _cur != path_lower:
                for _dn in _exact_idx.get(_cur, ()):
                    if len(_dn) > best_score:
                        best_match, best_score = _dn, len(_dn)
                _parent = os.path.dirname(_cur)
                if _parent == _cur or not _parent:
                    break
                _cur = _parent
        # 子键名包含 dir_name 匹配（子串无法索引，保留快照遍历；命中目录少）
        if not skip_subname_match and dir_name and len(dir_name) >= 4:
            for _sub_name, _display_name, _install_loc in snapshot:
                if dir_name.lower() in _sub_name.lower():
                    if len(_display_name) > best_score:
                        best_match, best_score = _display_name, len(_display_name)
        # 过滤系统组件描述（如 "Install Additional Tools for Node.js"）
        if _is_generic_registry_text(best_match):
            return ""
        return best_match
    except Exception as e:
        log.debug("忽略异常: %s", e)
    return ""

def get_exe_version_info(exe_path):
    """读取exe的PE版本信息 - 纯win32api实现，不依赖PowerShell

    注意：原 ctypes 方案在指针解引用时存在 segfault 风险（trans_array 越界），
    且 Python 的 try-except 无法捕获 C 层崩溃，已禁用。
    改为仅使用 win32api（pywin32），其内部有完整的异常处理。
    """
    # 仅使用 win32api 方案（pywin32 已安装，内部异常会被 Python 捕获）
    try:
        import win32api
        val = win32api.GetFileVersionInfo(exe_path, "\\VarFileInfo\\Translation")
        if val:
            lang_id, codepage = val[0]
            for field in ["ProductName", "FileDescription", "CompanyName"]:
                try:
                    sub_block = f"\\StringFileInfo\\{lang_id:04X}{codepage:04X}\\{field}"
                    text = win32api.GetFileVersionInfo(exe_path, sub_block)
                    if text and text.strip():
                        return text.strip()
                except Exception:
                    continue
    except ImportError:
        # pywin32未安装属于环境配置问题，不是真正的错误，静默返回
        pass
    except Exception:
        # PE版本信息读取失败是常见情况（mingw64/开源工具exe无版本信息），不写入错误日志
        pass
    return ""


def _get_exe_version_ctypes(exe_path):
    """用ctypes直接调用Win32 API读取PE版本信息（不依赖pywin32和PowerShell）"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    version = ctypes.WinDLL("version", use_last_error=True)

    # GetFileVersionInfoSizeW
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    version.VerQueryValueW.restype = wintypes.BOOL

    # 1. 获取版本信息大小
    dummy = wintypes.DWORD(0)
    size = version.GetFileVersionInfoSizeW(exe_path, ctypes.byref(dummy))
    if size == 0:
        return ""
    # 2. 分配缓冲区并读取版本信息
    buf = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(exe_path, 0, size, buf):
        return ""
    # 3. 查询Translation
    p_val = ctypes.c_void_p()
    val_len = wintypes.UINT(0)
    if not version.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(p_val), ctypes.byref(val_len)):
        return ""
    # 读取lang_id和codepage
    trans_array = ctypes.cast(p_val, ctypes.POINTER(ctypes.c_uint16))
    lang_id = trans_array[0]
    codepage = trans_array[1]
    # 4. 查询ProductName/FileDescription/CompanyName
    for field in ["ProductName", "FileDescription", "CompanyName"]:
        sub_block = f"\\StringFileInfo\\{lang_id:04X}{codepage:04X}\\{field}"
        p_str = ctypes.c_void_p()
        str_len = wintypes.UINT(0)
        if version.VerQueryValueW(buf, sub_block, ctypes.byref(p_str), ctypes.byref(str_len)) and str_len.value > 0:
            text = ctypes.wstring_at(p_str.value, str_len.value - 1)
            if text and text.strip():
                return text.strip()
    return ""


# ========== 特征文件检测 + 关联安装目录 ==========

# 厂商容器目录黑名单（多函数共用）：
# 这些目录名是厂商容器目录，含有多个子产品，不应被直接当作软件名返回，
# 也不应被注册表/WMI/已安装索引反查错配到具体产品。
# 例：Adobe → 不应被绑定为 "Adobe Flash Player" 或 "Adobe Creative Cloud"
#     Google → 不应被绑定为 "Google Chrome"
#     Mozilla → 不应被绑定为 "Mozilla Firefox"
# 这些目录应该走第19层 _detect_vendor_container 识别为"厂商容器目录"或子产品分拆
_VENDOR_CONTAINER_DIRS_FOR_FEATURE = {
    'adobe', 'google', 'mozilla', 'tencent', 'netease', 'ncsoft',
    'nvidia', 'amd', 'intel', 'intel corporation', 'ibm', 'oracle', 'apple',
    'vmware', 'topaz labs llc', 'saerasoft', 'feelfish', 'wegame',
    'gtarcade', 'spiritrealmworkshop', 'xiumaster', 'steam++',
    'ourplayer', 'microsoft_corporation',
    # 2026-07-20 新增（返回空字符串的厂商容器目录）
    'bytedance', 'openai', 'purpledome', 'reckfeng', 'sentry', 'softdeluxe',
    # 通用容器目录（多软件共用，应走第19层厂商容器判定，不应绑定到具体产品）
    # Programs: LocalAppData\Programs 下有多个软件（如 Edge/GLM-PC 等）
    # Package Cache: 多个 MSI 安装包缓存目录
    'programs', 'package cache',
}

# 通用词目录名黑名单（多函数共用，用于子键名/索引匹配时跳过）：
# 这些目录名过于通用，按子串匹配会命中大量无关软件
# 例：Cache → 命中 "Malwarebytes"（含 cache 子串），Comms → 命中 "Microsoft Sway"
_GENERIC_SHORT_NAMES_FOR_FEATURE = {
    'cache', 'caches', 'logs', 'log', 'temp', 'tmp', 'data', 'saved',
    'apps', 'programs', 'packages', 'common', 'common files',
    'microsoft', 'windows', 'program files', 'program files (x86)',
    'programdata', 'appdata', 'local', 'roaming', 'system32',
    'config', 'settings', 'update', 'updater', 'upgrade',
    'default', 'user data', 'crashdumps', 'downloads',
    'oem links', 'internet explorer', 'windows defender',
    'reference assemblies', 'package cache', 'winsxs',
    'squirreltemp', 'installlog', 'microsoft devdiv',
    # Windows/Electron/Chromium 内部通用目录名
    'comms', 'cef', 'chromeextensioncache', 'crashrpt',
    'crashreport', 'crashreporter', 'crashpad',
    'temporary internet files', 'ebwebview', 'gpucache',
    'shader cache', 'nodedata', 'shared_proto_db',
    'dawncache', 'dawnwebgpucache',
    'webview2', 'microsoft_corporation',
    # 厂商容器目录（同上）
    'tencent', 'netease', 'ncsoft', 'wegame', 'gtarcade',
    'topaz labs llc', 'vmware', 'spiritrealmworkshop',
    'saerasoft', 'feelfish', 'xiumaster', 'steam++',
    'codebuddyextension', 'workbuddy',
    'docker-secrets-engine', 'vedetector',
    'chromedevtoolsmcp', 'hermes', 'mongodbcompass',
}

# 特征文件 → 软件说明映射（基于文件扩展名/文件名，不是硬编码软件名）
# 检测目录内的文件特征推断软件用途，通用方案，对所有软件生效
def _detect_by_file_features(dir_path, dir_name):
    """基于文件/子目录特征推断软件说明（通用，不依赖特定电脑）

    策略（按优先级）：
    1. 子目录名推断（如 Softdeluxe/Free Download Manager → "Free Download Manager"）
    2. 已知文件模式（如 *.whl → Python包, package.json → Node.js）
    3. 文件名推断（如 chfs.setting.ini → CHFS, amr.ini → Auto Macro Recorder）
    4. 确认识别后返回，不确定返回空
    """
    try:
        entries = os.listdir(dir_path)
    except Exception:
        return ""

    files_lower = [e.lower() for e in entries if not os.path.isdir(os.path.join(dir_path, e))]
    dirs_lower = [e.lower() for e in entries if os.path.isdir(os.path.join(dir_path, e))]
    dirs_orig = [e for e in entries if os.path.isdir(os.path.join(dir_path, e))]
    files_orig = [e for e in entries if not os.path.isdir(os.path.join(dir_path, e))]

    # === Level 0: dir_name 本身就是产品名（§3.6 目录名推断） ===
    # 当目录名本身符合产品名格式（有大写字母或空格，且非通用词）时，直接返回
    # 适用于：带空格或混合大小写的目录名等
    # 厂商容器目录（Adobe/Google/Mozilla 等）跳过此层，应走第19层厂商容器识别
    GENERIC_DIR_NAMES = {
        'cache', 'caches', 'logs', 'log', 'temp', 'tmp', 'data', 'saved',
        'apps', 'programs', 'packages', 'common', 'common files',
        'microsoft', 'windows', 'program files', 'program files (x86)',
        'programdata', 'appdata', 'local', 'roaming', 'system32',
        'config', 'settings', 'update', 'updater', 'upgrade',
        'default', 'user data', 'crashdumps', 'downloads',
        'oem links', 'internet explorer', 'windows defender',
        'reference assemblies', 'package cache', 'winsxs',
        'squirreltemp', 'installlog', 'microsoft devdiv',
    }
    # 子目录名黑名单：Electron/Chromium 内部缓存目录名，不作为软件名返回
    # 函数级常量，多处引用（Level 1 子目录推断 + Level 3 Electron 特征检测）
    SKIP_SUBDIRS = {
        'cache', 'caches', 'logs', 'log', 'temp', 'tmp', 'data', 'saved',
        'blob_storage', 'code cache', 'dawncache', 'dawnwebgpucache',
        'dawngraphitecache', 'local storage', 'session storage',
        'dictionaries', 'crashpad', 'crashpad database', 'crashpad metrics',
        'crashpad metadata', 'shared_proto_db', 'gpucache', 'storage',
        'default', 'master', 'main', 'config', 'settings', 'apps',
        'upgrade', 'updater', 'update', 'highquality', 'userdata',
        'user data', 'ebwebview', 'browser', 'renderer', 'extensions', 'plugins',
        'nodedata', 'shaders', 'textures', 'models', 'assets', 'build', 'dist',
        'db_backups', 'log_archives', 'generatedfiles',
        'updatestore', 'spatialstore', 'providerassemblies',
        'gadgets', 'unsentcrashreports', 'nccrdata',
        'ark apis', 'arks', 'grpccache',
        'qone', 'ml', 'ov',
        # Electron 内部通用子目录（其exe描述是通用词如 Network/Common/Shared/Partitions 等）
        'network', 'common', 'shared', 'service', 'helper',
        'setup', 'installer', 'uninstaller',
        'partitions', 'shared dictionary', 'app preferences',
        'preferences', 'local state', 'local storage',
        'session storage', 'indexeddb',
        'gpu cache', 'shader cache', 'code cache',
    }
    import re as _re
    dn = dir_name.strip()
    dl = dn.lower()
    # 厂商容器目录（Adobe/Google/Mozilla/NCSOFT 等）跳过 Level 0
    # 这些目录应走第19层 _detect_vendor_container，识别为"厂商容器目录"或子产品分拆
    # 避免 Adobe 直接返回 "Adobe" 被 _enhance_with_location 套通用位置模板
    if dl in _VENDOR_CONTAINER_DIRS_FOR_FEATURE:
        pass  # 跳过 Level 0，继续走 Level 1+
    elif dn and len(dn) >= 5 and dl not in GENERIC_DIR_NAMES:
        has_upper = any(c.isupper() for c in dn)
        has_space = ' ' in dn
        # 跳过版本号格式
        if not _re.match(r'^[\d]+[-._][\d]+', dl):
            # 跳过纯小写（通用目录名通常全小写）
            if not dn.islower():
                if has_upper or has_space:
                    # 跳过反向域名格式（由专门的包名识别层处理）
                    if not (dl.startswith('com.') or dl.startswith('org.')
                            or dl.startswith('io.') or dl.startswith('cn.')
                            or dl.startswith('dev.')):
                        return dn

    # === Level 1: 子目录名推断（子目录名本身就是软件名） ===
    # 仅在父目录是通用目录名时触发，避免厂商容器目录的子目录被误识别为软件名
    # 例：cache/MyApp/ → "MyApp"（父目录 cache 是通用目录名，触发）
    #     VendorContainer/ProductA/ → 不触发（父目录 VendorContainer 非通用目录名）
    if dl in GENERIC_DIR_NAMES or len(dn) < 5:
        for d in dirs_orig:
            dl_sub = d.lower()
            if dl_sub in SKIP_SUBDIRS or len(dl_sub) < 3:
                continue
            # 跳过纯数字目录、随机GUID目录
            if dl_sub.isdigit() or dl_sub.replace('-','').replace('_','').isdigit():
                continue
            # 跳过看起来像版本号的
            import re
            if re.match(r'^[\d]+[-._][\d]+', dl_sub):
                continue
            # 子目录名很可能是软件名
            if len(d) > 4 and not any(c in d for c in ['\n', '\r']):
                # 如果子目录名里有空格/大写字母，很可能是产品名
                has_upper = any(c.isupper() for c in d)
                has_space = ' ' in d
                if has_upper or has_space:
                    # 确认不是系统目录名
                    if dl_sub not in ('microsoft', 'windows', 'program files', 'common files',
                                  'internet explorer', 'windows defender', 'system32',
                                  'reference assemblies', 'package cache'):
                        return d

    # === Level 2: 文件名推断 ===
    # [已注释-硬编码文件名→软件名映射，待用§3.5文件指纹表替代]
    # # CHFS
    # if 'chfs.setting.ini' in files_lower or 'chfs.setting' in files_lower:
    #     return "CHFS HTTP文件服务器"
    # # Auto Macro Recorder
    # if 'amr.ini' in files_lower:
    #     return "Auto Macro Recorder"
    # # Sidebar Diagnostics
    # if 'settings.json' in files_lower and dir_name.lower().startswith('sidebar'):
    #     return "Sidebar Diagnostics"
    # # Aomei Backupper
    # if 'comn.ini' in files_lower and 'aomei' in dir_name.lower():
    #     return "AOMEI Backupper"
    # if 'install.ini' in files_lower and 'aomei' in dir_name.lower():
    #     return "AOMEI Partition Assistant"
    # # Audyssey audio tuning
    # if 'apoheadphonetuning.audy' in files_lower or 'apospeakertuning.audy' in files_lower:
    #     return "Audyssey 音频调校数据"
    # # Firebase
    # if 'heartbeats-[default]' in files_lower and 'firebase' in dir_name.lower():
    #     return "Firebase 心跳数据"
    # # CareUEyes
    # if 'setting_v2.dat' in files_lower and 'careueyes' in dir_name.lower():
    #     return "CareUEyes 护眼软件"
    # # Ultralytics
    # if 'settings.yaml' in files_lower and 'ultralytics' in dir_name.lower():
    #     return "Ultralytics YOLO 配置"
    # # GitHub CLI / Desktop
    # if 'github' in dir_name.lower() and '.dead' in files_lower:
    #     return "GitHub Desktop"
    # # FLiNG Trainer (game trainer framework)
    # if 'trainersettings.ini' in files_lower and 'fling' in dir_name.lower():
    #     return "FLiNG Trainer 游戏修改器"
    # # comfyui
    # if 'config.json' in files_lower and 'comfyui' in dir_name.lower():
    #     return "ComfyUI AI图像工作流"
    # # clash
    # if any('clash' in f for f in files_lower + dirs_lower) and 'clash' in dir_name.lower():
    #     return "Clash 代理工具"
    # # novel-box
    # if 'novel' in dir_name.lower() and 'box' in dir_name.lower():
    #     return "小说盒子/Novel Box"

    # === Level 3: 已知特征文件模式 ===
    # Python
    has_whl = any(f.endswith('.whl') for f in files_lower)
    has_pip_exe = any(f in ('pip.exe', 'pip3.exe', 'pip3.12.exe', 'pip3.11.exe') for f in files_lower)
    has_python_exe = any(f.startswith('python') and f.endswith('.exe') for f in files_lower)
    if has_pip_exe or (has_whl and 'pip' in dir_name.lower()):
        return "pip Python 包管理器"
    if has_whl and dir_name.lower() in ('uv', 'pdm', 'poetry', 'pipenv'):
        return f"{dir_name} Python 包管理器"
    if has_python_exe:
        return "Python 解释器"

    # Node.js
    has_pkg_json = 'package.json' in files_lower
    has_node_modules = 'node_modules' in dirs_lower
    has_npm_exe = any(f in ('npm.exe', 'npx.exe', 'pnpm.exe', 'yarn.exe') for f in files_lower)
    if has_npm_exe or (dir_name.lower() in ('npm', 'npm-cache', 'pnpm', 'yarn') and (has_pkg_json or has_node_modules)):
        return f"{dir_name} Node.js 包管理器"
    if has_pkg_json and not has_node_modules:
        return "Node.js 项目"

    # VSCode
    if 'extensions.json' in files_lower or any(f.endswith('.vsix') for f in files_lower):
        return "VSCode 扩展"
    if dir_name.lower() == 'code' and has_pkg_json:
        return "Visual Studio Code"

    # Go
    if 'go.exe' in files_lower or 'go.mod' in files_lower:
        return "Go 语言工具链"

    # Steam
    if 'steam.exe' in files_lower:
        return "Steam 游戏平台"
    if 'steamapps' in dirs_lower:
        return "Steam 游戏库"

    # Docker
    if 'docker.exe' in files_lower:
        return "Docker 容器引擎"
    if 'volumes' in dirs_lower and 'containers' in dirs_lower:
        return "Docker 容器与数据卷"

    # 浏览器特征
    if 'bookmarks' in files_lower and 'preferences' in files_lower:
        return "Chromium 系浏览器配置"
    if 'places.sqlite' in files_lower:
        return "Firefox 浏览器配置"

    # Electron应用通用特征
    has_blob = 'blob_storage' in dirs_lower
    has_cache = 'cache' in dirs_lower
    has_prefs = 'preferences' in files_lower
    has_local_state = 'local state' in files_lower
    if has_prefs or has_local_state:
        if has_blob and has_cache:
            # Electron app data dir - try subdir name first
            for d in dirs_orig:
                dl = d.lower()
                if dl not in SKIP_SUBDIRS and len(dl) >= 3 and not dl.isdigit():
                    return d
            return "Electron 应用"

    # 日志特征 — 只有目录完全是日志时返回
    log_count = sum(1 for f in files_lower if f.endswith('.log'))
    if log_count > 3:
        has_other = any(
            f.endswith('.exe') or f.endswith('.dll') or f.endswith('.so')
            or f.endswith('.json') or f.endswith('.ini') or f.endswith('.conf')
            or f.endswith('.cfg') or f.endswith('.bin') or f.endswith('.dat')
            for f in files_lower
        )
        if not has_other:
            return "应用运行日志"

    # 不确定返回空
    return ""

def _find_related_install(dir_path, dir_name, known_software_dirs=None):
    """关联安装目录：从已识别的安装目录反推数据目录的软件名
    例如：AppData\\Local\\uv 找不到，但注册表识别到uv.exe装在别处
    或者父目录名能匹配到已识别的软件

    :param known_software_dirs: KNOWN_SOFTWARE_DIRS 字典（可选，用于关联）
    :return: 关联到的软件名，失败返回空
    """
    try:
        dl = dir_name.lower()

        # 厂商容器目录完全跳过此层（避免 Adobe → Adobe Flash Player 等错配）
        # 这些目录应走第19层 _detect_vendor_container
        if dl in _VENDOR_CONTAINER_DIRS_FOR_FEATURE:
            return ""
        # 通用词目录跳过子键名匹配（避免 Cache → Malwarebytes 等子串误匹配）
        # 但允许从 KNOWN_SOFTWARE_DIRS 反查和 InstallLocation 同名匹配
        skip_subname_match = (
            (len(dir_name) < 4)
            or (dl in _GENERIC_SHORT_NAMES_FOR_FEATURE)
        )

        # 1. 从目录名直接匹配已知软件（用已学习的词典）
        # 短词和通用词跳过（避免 "data" 命中含 data 的词典 key）
        if known_software_dirs and not skip_subname_match:
            for key, desc in known_software_dirs.items():
                if key in dl and not _is_vague_desc_static(desc):
                    return desc

        # 2. 查注册表有没有该软件的安装记录（InstallLocation）
        # 数据目录虽然没有InstallLocation，但同名软件的安装目录可能有
        try:
            import winreg
            roots = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            ]
            for root, subkey in roots:
                try:
                    key = winreg.OpenKey(root, subkey)
                except OSError:
                    continue
                try:
                    i = 0
                    while True:
                        try:
                            sub_name = winreg.EnumKey(key, i)
                            i += 1
                        except OSError:
                            break
                        # 子键名包含目录名（如 {UV-xxx} 或 Python相关）
                        # 短词和通用词跳过避免误匹配：
                        # Adobe → Adobe Flash Player 34 ActiveX
                        # Google → Google Chrome
                        # Mozilla → Mozilla Firefox
                        # Cache → Malwarebytes（恶意软件字节）
                        if (not skip_subname_match) and (dl in sub_name.lower()) and (len(dl) >= 4):
                            try:
                                sub_key = winreg.OpenKey(key, sub_name)
                                try:
                                    display_name, _ = winreg.QueryValueEx(sub_key, "DisplayName")
                                    if display_name:
                                        return display_name
                                finally:
                                    winreg.CloseKey(sub_key)
                            except OSError:
                                continue
                finally:
                    winreg.CloseKey(key)
        except Exception as e:
            log.debug("忽略异常: %s", e)

        # 3. 扫描常见安装位置有没有同名目录（Program Files等）
        # 厂商容器目录跳过此扫描（Adobe 在 Program Files\Adobe 下有多子产品，但根目录读 exe 无意义）
        install_prefixes = [
            os.environ.get("ProgramFiles", "C:\\Program Files"),
            os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
        ]
        for prefix in install_prefixes:
            if not prefix:
                continue
            candidate = os.path.join(prefix, dir_name)
            if os.path.exists(candidate) and candidate.lower() != dir_path.lower():
                # 同名安装目录存在，读PE信息
                # 过滤通用/系统组件PE文本（如 "Microsoft® Windows® Operating System"）
                try:
                    for item in os.listdir(candidate):
                        if item.lower().endswith('.exe'):
                            info = get_exe_version_info(os.path.join(candidate, item))
                            if info and not _is_generic_pe_text(info):
                                return info
                            break
                except Exception as e:
                    log.debug("忽略异常: %s", e)

        return ""
    except Exception:
        return ""


def _is_vague_desc_static(desc):
    """静态版敷衍说明判断（utils.py内部使用，避免循环导入）"""
    if not desc:
        return True
    import re
    desc = re.sub(r'^\[[^\]]*\]\s*', '', desc).strip()
    # 中文 2 字软件名是有效软件名，不能判为 vague
    # 纯英文/数字才用 < 4 阈值
    has_cn = bool(re.search(r'[\u4e00-\u9fff]', desc))
    if has_cn:
        if len(desc) < 2:
            return True
    else:
        if len(desc) < 4:
            return True
    if "相关" in desc:
        return True
    vague_words = ["应用数据", "缓存数据", "临时文件", "日志文件", "配置/设置数据"]
    if desc in vague_words:
        return True
    return False


def _tokenize_with_camel(name):
    """带驼峰分词的 token 切分（需保留原始大小写）
    示例：
      "MyApp" → {"my", "app"}
      "My App Launcher" → {"my", "app", "launcher"}
      "MyBrowser" → {"my", "browser"}
      "myapp" → {"myapp"}（全小写无法分词）
      "My Studio Code" → {"my", "studio", "code"}
      中文字符串原样保留不分词
    """
    import re as _re_tok
    if not name:
        return set()
    # 1. 先按 `[\s\-_\.]+` 切分
    raw_tokens = [t for t in _re_tok.split(r'[\s\-_\.]+', name) if t]
    # 2. 对每个 token 做驼峰分词
    result = set()
    for tok in raw_tokens:
        # 仅对纯 ASCII 字母数字 token 做驼峰分词
        if tok and tok.isascii() and not tok.isdigit():
            # 驼峰分词：在大写字母前插入分隔符
            # "MyApp" → "My App" → ["My", "App"]
            # "ABApp" → "A B App"（连续大写视为缩写，但这里简化处理）
            parts = _re_tok.sub(r'([a-z])([A-Z])', r'\1 \2', tok)
            # 再次处理连续大写后跟小写的情况 "ABCode" → "AB Code"
            parts = _re_tok.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', parts)
            for p in parts.split():
                if p:
                    result.add(p.lower())
        else:
            # 中文或其他：直接小写加入
            result.add(tok.lower())
    return result - {''}


# ========== 动态已安装软件索引（冷启动，替代被清空的内置词典）==========
# 核心思路：启动时扫描本机实际安装的软件，建立 {目录名小写: 真实产品名} 映射
# 数据目录(无exe)用目录名匹配此索引，拿到真实软件名，而不是伪造"目录名+本地数据"

_INSTALLED_INDEX = None
_INSTALLED_INDEX_LOCK = threading.Lock()


def _build_installed_index():
    r"""构建通用软件反向索引（纯系统自举，不依赖任何内置词典，跨电脑通用）

    策略：把 Windows 自带的安装信息拆解为 {路径片段 - 软件名} 的多级映射：
      1. 注册表 Uninstall - InstallLocation 每一级目录都映射
         C:\Program Files\SomeVendor\AppName - "appname"-"AppName", "somevendor"-"AppName"
      2. WMI InstalledWin32Program - 同上
      3. Services - 服务名 - DisplayName 映射
      4. App Paths - exe名 - 从目标exe读PE信息
      5. Program Files 扫描（2层深度）- 每个exe的PE ProductName - 路径片段映射
      6. 文件指纹 - {特征文件或扩展名 - 软件类别}（通用，不依赖特定电脑）

    结果：任何数据目录（哪怕没有exe），只要目录名能匹配到索引中的任一路径片段，
    就能直接拿到真实软件名，无需联网。

    返回：{path_segment_lower: software_name}
    """
    index = {}

    def _add_path_segments(full_path, display_name):
        """把安装路径的每一级目录名都反向映射到软件名"""
        if not full_path or not display_name:
            return
        parts = full_path.replace('/', '\\').rstrip('\\').split('\\')
        for part in parts:
            p = part.lower().strip()
            if not p or len(p) < 2:
                continue
            # 去版本号（如 app-1.2.3 - app）
            import re
            clean = re.sub(r'[-_]\d[\d.]*$', '', p)
            skip_dirs = {
                'c:', 'd:', 'e:', 'f:',
                'program files', 'program files (x86)', 'programdata',
                'users', 'windows', 'appdata', 'local', 'roaming',
                'microsoft', 'common', 'common files'
            }
            if clean not in skip_dirs and len(clean) >= 2:
                if clean not in index or len(display_name) > len(index.get(clean, '')):
                    index[clean] = display_name
            if p not in skip_dirs and p not in index:
                index[p] = display_name

    # === 1. 注册表 Uninstall ===
    try:
        import winreg
        roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for root, subkey in roots:
            try:
                key = winreg.OpenKey(root, subkey)
            except OSError:
                continue
            try:
                i = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(key, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        sub_key = winreg.OpenKey(key, sub_name)
                        try:
                            display_name = install_loc = ''
                            try:
                                display_name, _ = winreg.QueryValueEx(sub_key, "DisplayName")
                            except OSError as e:
                                log.debug("忽略异常: %s", e)
                            try:
                                install_loc, _ = winreg.QueryValueEx(sub_key, "InstallLocation")
                            except OSError as e:
                                log.debug("忽略异常: %s", e)
                            if display_name:
                                index[display_name.lower()] = display_name
                                if install_loc:
                                    _add_path_segments(install_loc, display_name)
                        finally:
                            winreg.CloseKey(sub_key)
                    except OSError:
                        continue
            finally:
                winreg.CloseKey(key)
    except Exception as e:
        log.debug("忽略异常: %s", e)

    # === 1.5. DisplayName关键词索引（用于无InstallLocation的软件） ===
    # 很多注册表项只有DisplayName没有InstallLocation
    # 从DisplayName提取关键词也加入索引
    try:
        import winreg, re
        for root, subkey in roots:
            try:
                key = winreg.OpenKey(root, subkey)
            except OSError:
                continue
            try:
                i = 0
                while True:
                    try:
                        sn = winreg.EnumKey(key, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        sk = winreg.OpenKey(key, sn)
                        try:
                            dn = ''
                            try:
                                dn, _ = winreg.QueryValueEx(sk, 'DisplayName')
                            except OSError as e:
                                log.debug("忽略异常: %s", e)
                            if dn and len(dn) >= 3:
                                # 从DisplayName提取关键单词（如 "Microsoft Visual Studio 2022" →
                                # "visual","studio","2022","visual studio","visualstudio"）
                                words = re.split(r'[\s\-_/()（）]+', dn.lower())
                                # 去无用词
                                skip = {'microsoft', 'windows', 'for', 'the', 'and', 'or',
                                        'version', 'edition', 'x64', 'x86', '64-bit', '32-bit',
                                        'update', 'runtime', 'redistributable', 'package',
                                        'driver', 'component', 'service', 'tools', 'tool',
                                        'application', 'software', 'system', 'core',
                                        'client', 'server', 'support', 'framework',
                                        'library', 'libraries', 'sdk', 'extension',
                                        'language', 'pack',
                                        # 公司后缀和通用商业词
                                        'inc', 'corp', 'corporation', 'ltd', 'co', 'gmbh',
                                        'llc', 'limited', 'international', 'technologies',
                                        'group', 'holdings', 'solutions', 'enterprises',
                                        'studio', 'studios', 'interactive', 'entertainment',
                                        'games', 'game', 'online', 'development',
                                        'com', 'net', 'org', 'www'}
                                meaningful = [w for w in words if w and len(w) >= 2
                                              and w not in skip and not w.isdigit()
                                              and w != 'c++']
                                if meaningful:
                                    # 也存组合词
                                    for w in meaningful:
                                        if w not in index:
                                            index[w] = dn
                                    # 前两个词组合（如 "visual studio"）
                                    if len(meaningful) >= 2:
                                        combo = ' '.join(meaningful[:2])
                                        if combo not in index or len(dn) < len(index[combo]):
                                            index[combo] = dn
                                    # 前三个词
                                    if len(meaningful) >= 3:
                                        combo3 = ' '.join(meaningful[:3])
                                        if combo3 not in index or len(dn) < len(index[combo3]):
                                            index[combo3] = dn
                        finally:
                            winreg.CloseKey(sk)
                    except OSError:
                        continue
            finally:
                winreg.CloseKey(key)
    except Exception as e:
        log.debug("忽略异常: %s", e)

    # === 2. WMI InstalledWin32Program ===
    try:
        import win32com.client
        wmi = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        svc = wmi.ConnectServer(".", "root/cimv2")
        try:
            items = svc.ExecQuery(
                "SELECT Name, InstallLocation FROM Win32_InstalledWin32Program"
            )
        except Exception:
            items = []
        for item in items:
            try:
                name = item.Name or ''
                install_loc = ''
                try:
                    install_loc = item.InstallLocation or ''
                except Exception as e:
                    log.debug("忽略异常: %s", e)
                if name:
                    index[name.lower()] = name
                    if install_loc:
                        _add_path_segments(install_loc, name)
            except Exception:
                continue
    except Exception as e:
        log.debug("忽略异常: %s", e)

    # === 3. Windows Services ===
    try:
        import win32com.client
        wmi_svc = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        svc_conn = wmi_svc.ConnectServer(".", "root/cimv2")
        try:
            svc_items = svc_conn.ExecQuery(
                "SELECT Name, DisplayName, PathName FROM Win32_Service"
            )
        except Exception:
            svc_items = []
        for svc in svc_items:
            try:
                svc_name = svc.Name or ''
                display = svc.DisplayName or ''
                path = ''
                try:
                    path = svc.PathName or ''
                except Exception as e:
                    log.debug("忽略异常: %s", e)
                if svc_name and display:
                    index[svc_name.lower()] = display
                if path:
                    exe_dir = os.path.dirname(path.strip('"').strip("'"))
                    if exe_dir:
                        _add_path_segments(exe_dir, display if display else svc_name)
            except Exception:
                continue
    except Exception as e:
        log.debug("忽略异常: %s", e)

    # === 4. App Paths 注册表 ===
    try:
        import winreg
        ap_roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        ]
        for root, subkey in ap_roots:
            try:
                key = winreg.OpenKey(root, subkey)
            except OSError:
                continue
            try:
                i = 0
                while True:
                    try:
                        exe_name = winreg.EnumKey(key, i)
                        i += 1
                    except OSError:
                        break
                    if exe_name.lower().endswith('.exe'):
                        try:
                            sub_key = winreg.OpenKey(key, exe_name)
                            try:
                                default_val = ''
                                try:
                                    default_val, _ = winreg.QueryValueEx(sub_key, '')
                                except OSError as e:
                                    log.debug("忽略异常: %s", e)
                                if default_val and os.path.exists(default_val):
                                    _add_path_segments(default_val, exe_name[:-4])
                            finally:
                                winreg.CloseKey(sub_key)
                        except OSError as e:
                            log.debug("忽略异常: %s", e)
            finally:
                winreg.CloseKey(key)
    except Exception as e:
        log.debug("忽略异常: %s", e)

    # === 5. Program Files 深度扫描（2层，读所有exe的PE元数据）===
    install_roots = []
    for env_key in ["ProgramFiles", "ProgramFiles(x86)"]:
        pf = os.environ.get(env_key, "")
        if pf and os.path.isdir(pf):
            install_roots.append(pf)
    la = os.environ.get("LOCALAPPDATA", "")
    if la:
        lp = os.path.join(la, "Programs")
        if os.path.isdir(lp):
            install_roots.append(lp)

    for root_dir in install_roots:
        try:
            for top_entry in os.listdir(root_dir):
                top_path = os.path.join(root_dir, top_entry)
                if not os.path.isdir(top_path):
                    continue
                top_lower = top_entry.lower()
                # 检查根目录exe
                found = False
                try:
                    for item in os.listdir(top_path):
                        if item.lower().endswith('.exe'):
                            info = get_exe_version_info(os.path.join(top_path, item))
                            if info:
                                index[top_lower] = info
                                _add_path_segments(top_path, info)
                                found = True
                                break
                except Exception as e:
                    log.debug("忽略异常: %s", e)
                # 深入一层子目录
                if not found:
                    try:
                        for sub_entry in os.listdir(top_path):
                            sub_path = os.path.join(top_path, sub_entry)
                            if not os.path.isdir(sub_path):
                                continue
                            sub_lower = sub_entry.lower()
                            try:
                                for item in os.listdir(sub_path):
                                    if item.lower().endswith('.exe'):
                                        info = get_exe_version_info(os.path.join(sub_path, item))
                                        if info:
                                            if sub_lower not in index or len(info) > len(index.get(sub_lower, '')):
                                                index[sub_lower] = info
                                            if top_lower not in index:
                                                index[top_lower] = info
                                            _add_path_segments(sub_path, info)
                                            found = True
                                            break
                            except Exception as e:
                                log.debug("忽略异常: %s", e)
                            if found:
                                break
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
                # 深入二层子目录（第3层PE扫描）
                if not found:
                    try:
                        for sub_entry in os.listdir(top_path):
                            sub_path = os.path.join(top_path, sub_entry)
                            if not os.path.isdir(sub_path):
                                continue
                            for sub2_entry in os.listdir(sub_path):
                                sub2_path = os.path.join(sub_path, sub2_entry)
                                if not os.path.isdir(sub2_path):
                                    continue
                                sub2_lower = sub2_entry.lower()
                                try:
                                    for item in os.listdir(sub2_path):
                                        if item.lower().endswith('.exe'):
                                            info = get_exe_version_info(os.path.join(sub2_path, item))
                                            if info:
                                                if sub2_lower not in index or len(info) > len(index.get(sub2_lower, '')):
                                                    index[sub2_lower] = info
                                                if top_lower not in index:
                                                    index[top_lower] = info
                                                _add_path_segments(sub2_path, info)
                                                found = True
                                                break
                                except Exception as e:
                                    log.debug("忽略异常: %s", e)
                                if found:
                                    break
                            if found:
                                break
                    except Exception as e:
                        log.debug("忽略异常: %s", e)
        except Exception as e:
            log.debug("忽略异常: %s", e)

    # === 6. UWP / MSIX 应用包注册（HKCU AppModel Repository） ===
    try:
        import winreg
        # 当前用户的UWP应用包
        try:
            pkg_key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages"
            )
            try:
                i = 0
                while True:
                    try:
                        pkg_name = winreg.EnumKey(pkg_key, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        sub = winreg.OpenKey(pkg_key, pkg_name)
                        try:
                            display = ''
                            try:
                                display, _ = winreg.QueryValueEx(sub, "DisplayName")
                            except OSError as e:
                                log.debug("忽略异常: %s", e)
                            path = ''
                            try:
                                path, _ = winreg.QueryValueEx(sub, "PackageRootFolder")
                            except OSError as e:
                                log.debug("忽略异常: %s", e)
                            if display and path:
                                _add_path_segments(path, display)
                            elif display:
                                index[display.lower()] = display
                        finally:
                            winreg.CloseKey(sub)
                    except OSError:
                        continue
            finally:
                winreg.CloseKey(pkg_key)
        except OSError as e:
            log.debug("忽略异常: %s", e)
    except Exception as e:
        log.debug("忽略异常: %s", e)

    # === 7. Steam 游戏库扫描 ===
    # Steam 游戏不在注册表 Uninstall 里，需读 appmanifest_*.acf 获取游戏名
    # 支持 Steam 库分散在多个磁盘的场景
    try:
        import re as _re_steam, winreg as _wr_steam
        steam_install_dirs = []
        # 1) 从注册表找 Steam 主目录
        try:
            _k = _wr_steam.OpenKey(_wr_steam.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam")
            try:
                _sp, _ = _wr_steam.QueryValueEx(_k, "SteamPath")
                if _sp and os.path.isdir(_sp):
                    steam_install_dirs.append(_sp.replace('/', '\\'))
            finally:
                _wr_steam.CloseKey(_k)
        except OSError as e:
            log.debug("忽略异常: %s", e)

        for steam_dir in steam_install_dirs:
            steamapps = os.path.join(steam_dir, "steamapps")
            if not os.path.isdir(steamapps):
                continue
            # 2) 读 libraryfolders.vdf 获取所有库目录（含 D:/E:/F: 等其他盘的 Steam 库）
            # VDF 格式: "path" "F:\\steam"（用 "path" 键匹配，不是 "数字"）
            libs = [steamapps]
            lfv = os.path.join(steamapps, "libraryfolders.vdf")
            if os.path.exists(lfv):
                try:
                    with open(lfv, "r", encoding="utf-8", errors="ignore") as _f:
                        _content = _f.read()
                    # 匹配 "path" "xxx" 对（VDF 格式中每个库的路径字段）
                    for _m in _re_steam.finditer(r'"path"\s+"([^"]+)"', _content):
                        _p = _m.group(1).replace("\\\\", "\\")
                        if _p and os.path.isdir(_p):
                            libs.append(os.path.join(_p, "steamapps"))
                except Exception as e:
                    log.debug("忽略异常: %s", e)
            # 3) 遍历每个库目录的 appmanifest_*.acf
            for lib in libs:
                if not os.path.isdir(lib):
                    continue
                try:
                    for fn in os.listdir(lib):
                        if not (fn.startswith("appmanifest_") and fn.endswith(".acf")):
                            continue
                        acf_path = os.path.join(lib, fn)
                        try:
                            with open(acf_path, "r", encoding="utf-8", errors="ignore") as _f:
                                _content = _f.read()
                            _nm = _re_steam.search(r'"name"\s+"([^"]+)"', _content)
                            _im = _re_steam.search(r'"installdir"\s+"([^"]+)"', _content)
                            if _nm and _im:
                                _name = _nm.group(1)
                                _install_dir = _im.group(1)
                                # 用 installdir 作为 key（这是 Steam 安装目录名）
                                _key_lower = _install_dir.lower()
                                if _key_lower not in index or len(_name) > len(index.get(_key_lower, '')):
                                    index[_key_lower] = _name
                                # 也加名字本身的反向映射
                                _name_lower = _name.lower()
                                if _name_lower not in index:
                                    index[_name_lower] = _name
                        except Exception:
                            continue
                except Exception as e:
                    log.debug("忽略异常: %s", e)
    except Exception as e:
        log.debug("忽略异常: %s", e)

    # === 8. 扩展文件特征扫描（用于数据目录的无exe精确识别） ===
    # 增加更多通用文件指纹模式

    return index


def _get_installed_index():
    """获取已安装软件索引（惰性构建，线程安全，构建一次后缓存）
    返回 {dir_name_lower: real_product_name}
    """
    global _INSTALLED_INDEX
    if _INSTALLED_INDEX is not None:
        return _INSTALLED_INDEX
    with _INSTALLED_INDEX_LOCK:
        if _INSTALLED_INDEX is None:
            try:
                _INSTALLED_INDEX = _build_installed_index()
            except Exception:
                _INSTALLED_INDEX = {}
    return _INSTALLED_INDEX


def _match_installed_index(dir_name):
    """用目录名匹配动态已安装软件索引，返回真实产品名，失败返回空
    匹配策略：精确匹配优先，其次长关键字包含匹配（过滤笼统名）
    """
    try:
        if not dir_name or len(dir_name) < 2:
            return ""
        index = _get_installed_index()
        if not index:
            return ""
        dl = dir_name.lower()
        # 厂商容器目录完全跳过此层（避免 Adobe/Tencent 等被错配到具体产品）
        # 例：Tencent → StreamingService，cn.org.localagent.desktop → Docker Desktop
        # 这些目录应走第19层 _detect_vendor_container
        if dl in _VENDOR_CONTAINER_DIRS_FOR_FEATURE:
            return ""
        # 反向域名包名（com.*/cn.*/dev.*/org.*/io.*）跳过此层
        # 例：com.pot-app.desktop 不应按子串匹配命中 "Docker Desktop"（含 desktop 子串）
        import re as _re_idx
        if _re_idx.match(r'^(com|org|io|cn|dev)\.', dl):
            return ""
        # 通用词目录跳过子串匹配（避免 Cache/Comms 等命中大量无关软件）
        skip_substring_match = (
            (len(dir_name) < 4)
            or (dl in _GENERIC_SHORT_NAMES_FOR_FEATURE)
        )
        if skip_substring_match:
            # 仅允许精确匹配（已上方检查过 dl in index）
            return ""
        # 精确匹配
        if dl in index:
            return index[dl]
        # 包含匹配（长key优先，避免短key误匹配）
        # 约束：key 必须是 dl 的子串（单向），且 key 长度 ≥ 4
        # 避免 "container" 匹配 "vendorcontainer" 这种双向子串误命中
        # 通用词黑名单：这些词作为 key 时跳过（会误匹配很多无关目录名）
        _GENERIC_KEY_BLACKLIST = {
            'container', 'session', 'user', 'install', 'backend',
            'plugin', 'service', 'update', 'cache', 'temp', 'logs',
            'data', 'app', 'application', 'tools', 'tool', 'kit',
            'sdk', 'runtime', 'core', 'display', 'audio', 'driver',
            'virtual', 'shadow', 'watch', 'telemetry', 'frameview',
            'messagebus', 'system', 'local', 'state', 'plugins',
            'hd', 'aiuser', 'localsystem', 'nvapp', 'nvdlisr',
            'ngcctnrsvc', 'fvsvc', 'physx', 'installer', 'installer2',
            'installercore', 'backend', 'frameviewsdk',
            # 厂商容器目录作为 key 时也跳过（避免 adobe → Adobe Flash Player）
            'adobe', 'google', 'mozilla', 'tencent', 'netease', 'ncsoft',
            'nvidia', 'amd', 'intel', 'ibm', 'oracle', 'apple',
            'vmware', 'steam++', 'wegame',
        }
        for key in sorted(index.keys(), key=len, reverse=True):
            if len(key) < 4:
                continue
            if key in _GENERIC_KEY_BLACKLIST:
                continue
            if key in dl:
                name = index[key]
                if not _is_vague_desc_static(name):
                    return name
        return ""
    except Exception:
        return ""


# ========== winget 内置软件数据库（从 winget-pkgs 仓库提取）==========
# 启动时一次性加载到内存，提供 {package_id_lower: {"name":..., "publisher":..., "desc":...}}
_WINGET_DB_CACHE = None

def _load_winget_db():
    """惰性加载 winget 软件数据库"""
    global _WINGET_DB_CACHE
    if _WINGET_DB_CACHE is not None:
        return _WINGET_DB_CACHE
    try:
        import json as _json
        import sys as _sys
        # 词典目录：打包模式优先 exe 内 _MEIPASS/data（打进 exe），缺失回退 exe 同级 data/；源码模式 = src/data
        # （本文件位于 src/core/utils.py，向上1级到 src/，再进 data/）
        if getattr(_sys, "frozen", False):
            _meipass = getattr(_sys, "_MEIPASS", None)
            if _meipass and os.path.isdir(os.path.join(_meipass, "data")):
                _data_dir = os.path.join(_meipass, "data")
            else:
                _data_dir = os.path.join(os.path.dirname(_sys.executable), "data")
        else:
            _data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        db_path = os.path.join(_data_dir, "winget_software_db.json")
        if os.path.exists(db_path):
            with open(db_path, "r", encoding="utf-8") as f:
                _WINGET_DB_CACHE = _json.load(f)
        else:
            _WINGET_DB_CACHE = {}
    except Exception:
        _WINGET_DB_CACHE = {}
    return _WINGET_DB_CACHE


_WINGET_NAME_INDEX = None  # name_lower → (name, desc, type) 反向索引缓存
_WINGET_NAME_INDEX_LOCK = None


def _i18n_en():
    """当前界面是否为英文模式（延迟导入避免循环依赖）。"""
    try:
        from i18n import current_language
        return current_language() == "en_us"
    except Exception:
        return False


def _tr_text(text):
    """界面文案翻译（延迟导入，识别描述渲染处统一调用）。"""
    try:
        from i18n import tr
        return tr(text)
    except Exception:
        return text


def _winget_triple(pkg_id, db=None):
    """从 pkg_id 取 (name, desc, type) 三元组。

    #5 瘦身:索引只存 name_lower → pkg_id(不再复制 desc/type 字符串),
    查询时从这里反查原 db,避免 19000 条双份浪费内存。
    i18n:英文界面模式优先取 winget db 自带的 desc_en 英文字段,
    无英文字段时回退中文 desc。
    """
    try:
        if db is None:
            db = _load_winget_db()
        entry = db.get(pkg_id)
        if entry:
            desc = entry.get("desc", "")
            if _i18n_en():
                desc = entry.get("desc_en") or desc
            return (entry.get("name", ""), desc, entry.get("type", ""))
    except Exception as e:
        log.debug("忽略异常: %s", e)
    return ()


def _build_winget_name_index():
    """构建 winget DB 的 name → 三元组 反向索引（一次性构建，后续 O(1) 查询）
    避免 _lookup_winget_by_display_name 每次扫 19000 条 3 遍导致扫描极慢
    """
    global _WINGET_NAME_INDEX, _WINGET_NAME_INDEX_LOCK
    if _WINGET_NAME_INDEX is not None:
        return _WINGET_NAME_INDEX
    if _WINGET_NAME_INDEX_LOCK is None:
        import threading as _t
        _WINGET_NAME_INDEX_LOCK = _t.Lock()
    with _WINGET_NAME_INDEX_LOCK:
        if _WINGET_NAME_INDEX is not None:
            return _WINGET_NAME_INDEX
        try:
            db = _load_winget_db()
            idx = {}
            if db:
                for pkg_id, entry in db.items():
                    name = entry.get("name", "")
                    if name:
                        # #5 瘦身:只存 pkg_id,desc/type 用时从原 db 反查
                        idx[name.lower()] = pkg_id
            _WINGET_NAME_INDEX = idx
        except Exception:
            _WINGET_NAME_INDEX = {}
    return _WINGET_NAME_INDEX


# ---- 策略3 后缀索引 + 策略4 token 预计算 ----
# _match_winget_db 策略3/4 每次调用全遍历 19421 条（策略4 还逐条重新驼峰分词），
# 高频识别时累计耗时显著。索引一次性构建（约 0.06s），查询变 O(1)。
_WINGET_SUFFIX_INDEX = None       # {pkg_id最后一段: [pkg_id, ...]}
_WINGET_SUFFIX_INDEX_LOCK = None
_WINGET_NAME_TOKENS = None        # {pkg_id: tokens}，策略4 预分词缓存
_WINGET_NAME_TOKENS_LOCK = None


def _build_winget_suffix_index():
    """构建 pkg_id 后缀索引（{最后一段: [pkg_id, ...]}），惰性一次性构建，线程安全"""
    global _WINGET_SUFFIX_INDEX, _WINGET_SUFFIX_INDEX_LOCK
    if _WINGET_SUFFIX_INDEX is not None:
        return _WINGET_SUFFIX_INDEX
    if _WINGET_SUFFIX_INDEX_LOCK is None:
        import threading as _t
        _WINGET_SUFFIX_INDEX_LOCK = _t.Lock()
    with _WINGET_SUFFIX_INDEX_LOCK:
        if _WINGET_SUFFIX_INDEX is not None:
            return _WINGET_SUFFIX_INDEX
        db = _load_winget_db()
        idx = {}
        if db:
            for pkg_id in db:
                if "." in pkg_id:
                    idx.setdefault(pkg_id.rsplit(".", 1)[-1], []).append(pkg_id)
        _WINGET_SUFFIX_INDEX = idx
    return _WINGET_SUFFIX_INDEX


def _build_winget_name_tokens():
    """预计算 winget DB 所有 name 的驼峰分词（策略4 复用，不再每次对 19421 条重新分词）"""
    global _WINGET_NAME_TOKENS, _WINGET_NAME_TOKENS_LOCK
    if _WINGET_NAME_TOKENS is not None:
        return _WINGET_NAME_TOKENS
    if _WINGET_NAME_TOKENS_LOCK is None:
        import threading as _t
        _WINGET_NAME_TOKENS_LOCK = _t.Lock()
    with _WINGET_NAME_TOKENS_LOCK:
        if _WINGET_NAME_TOKENS is not None:
            return _WINGET_NAME_TOKENS
        db = _load_winget_db()
        cache = {}
        if db:
            for pkg_id, entry in db.items():
                name = entry.get("name", "")
                if name:
                    cache[pkg_id] = _tokenize_with_camel(name)
        _WINGET_NAME_TOKENS = cache
    return _WINGET_NAME_TOKENS


# ---- 策略4 token 倒排索引 ----
# 原策略4 每次调用全遍历 19421 条做集合运算（纯 Python，GIL 串行，
# 高频识别时耗时显著）。
# 倒排索引 {token: set(pkg_id)}：查询时对 dir_tokens 取集合交集，
# 候选通常几个~几十个，且交集数学上等价于原"dir_tokens ⊆ name_tokens"条件。
_WINGET_TOKEN_INDEX = None       # {token: set(pkg_id, ...)}
_WINGET_TOKEN_INDEX_LOCK = None
_WINGET_PKG_ORDER = None         # {pkg_id: db 顺序号}，选最短 name 同长取先（与全遍历版一致）


def _build_winget_token_index():
    """构建 winget name token 倒排索引 + pkg 顺序号（惰性一次性，线程安全）"""
    global _WINGET_TOKEN_INDEX, _WINGET_TOKEN_INDEX_LOCK, _WINGET_PKG_ORDER
    if _WINGET_TOKEN_INDEX is not None:
        return _WINGET_TOKEN_INDEX, _WINGET_PKG_ORDER
    if _WINGET_TOKEN_INDEX_LOCK is None:
        import threading as _t
        _WINGET_TOKEN_INDEX_LOCK = _t.Lock()
    with _WINGET_TOKEN_INDEX_LOCK:
        if _WINGET_TOKEN_INDEX is not None:
            return _WINGET_TOKEN_INDEX, _WINGET_PKG_ORDER
        tok_cache = _build_winget_name_tokens()
        idx = {}
        order = {}
        for i, pkg_id in enumerate(tok_cache):
            order[pkg_id] = i
            for tok in tok_cache[pkg_id]:
                idx.setdefault(tok, set()).add(pkg_id)
        _WINGET_TOKEN_INDEX = idx
        _WINGET_PKG_ORDER = order
    return _WINGET_TOKEN_INDEX, _WINGET_PKG_ORDER


def _lookup_winget_by_display_name(display_name):
    """用 DisplayName 反查 winget DB，返回三元组 (name, desc, type)，失败返回空元组

    用于注册表/WMI/PE 命中后增强描述：
      - 注册表/WMI 返回的 DisplayName 通常是英文名（如 "Discord" / "GitHub Desktop"）
      - PE 文件的 ProductName 也可能是英文名
      - 用这个名称反查 winget DB 拿到中文 desc 和 78 类 type
      - 命中后走 type × position 矩阵生成自然描述（避免机翻"DisplayName 本地数据"）

    匹配策略（按优先级）：
      1. PackageName 完全等于 display_name（大小写不敏感）
      2. PackageName 以 display_name 开头（如 "Discord" → "Discord - Chat"）
      3. display_name 包含在 PackageName 中（token 子集，display_name 至少 4 字符）

    :param display_name: 注册表/WMI/PE 返回的 DisplayName 或 ProductName
    :return: (name, desc, type) 三元组，未命中返回空元组 ()
    """
    try:
        if not display_name or len(display_name) < 2:
            return ()
        idx = _build_winget_name_index()
        if not idx:
            return ()
        dl = display_name.lower().strip()
        if not dl:
            return ()
        # 过滤通用词（避免 "Microsoft" / "Update" 等返回错误结果）
        _GENERIC_DISPLAY_NAMES = {
            'microsoft', 'windows', 'update', 'updater', 'cache', 'config',
            'data', 'logs', 'log', 'temp', 'tmp', 'common', 'shared',
            'program', 'application', 'app', 'tool', 'tools', 'kit',
            'runtime', 'sdk', 'core', 'engine', 'editor', 'viewer',
            'player', 'reader', 'manager', 'client', 'server', 'studio',
            'portable', 'launcher',
        }
        if dl in _GENERIC_DISPLAY_NAMES:
            return ()
        # 1. 精确匹配（O(1) 字典查询，替代 O(19000) 线性扫描）
        if dl in idx:
            return _winget_triple(idx[dl])
        # 2. PackageName 以 display_name 开头（如 "Discord" → "Discord - Chat"）
        if len(dl) >= 4:
            for name_lower, pkg_id in idx.items():
                if name_lower.startswith(dl):
                    return _winget_triple(pkg_id)
        # 3. display_name 包含 PackageName（display_name 至少 4 字符）
        if len(dl) >= 4:
            for name_lower, pkg_id in idx.items():
                if dl in name_lower and len(name_lower) <= len(dl) + 8:
                    return _winget_triple(pkg_id)
        return ()
    except Exception:
        return ()



def _match_winget_db(dir_name):
    """用目录名匹配 winget 内置数据库，返回三元组 (name, desc, type)，失败返回空元组
    匹配策略（按优先级，前 3 个是精确匹配，第 4 个是兜底扩展匹配）：
    1. 目录名直接匹配 PackageIdentifier（如 "publisher.appname" → "AppName"）
    2. 目录名完全等于 PackageName（大小写不敏感，如 "appname" → 匹配 name="AppName" 的包）
    3. 目录名匹配 PackageIdentifier 后缀（如 "App" → 命中 "Publisher.App" 包的 name）
       PackageIdentifier 格式为 Publisher.SoftwareName，取最后一段比较
    4. 兜底：dir_name 的所有单词都出现在 PackageName 中（token 子集匹配）
       适用于 "App" → "App Desktop"、"My Studio Code" → "Publisher My Studio Code"
       约束：dir_name 至少 4 字符 或 最长 token >= 4，避免 "go"/"set" 等短词误匹配

    返回值：
      成功：(name, desc, type) 三元组，desc 为功能描述，type 为 78 类分类之一
      失败：空元组 ()，调用方用 if winget_result: 判断真值
    """
    try:
        if not dir_name or len(dir_name) < 2:
            return ()
        db = _load_winget_db()
        if not db:
            return ()
        dl = dir_name.lower()
        # 通用词目录名黑名单：这些目录名通常是系统/应用通用目录名，
        # 不应作为软件名匹配到 winget DB 的具体软件
        # 例：Local\Temp 不应匹配 "Core Temp"，C:\Windows 不应匹配 "v2RayTun"（pkg_id 后缀 windows）
        #     C:\Program Files\Microsoft 不应匹配任何软件
        # 检查时机：所有策略之前（包括精确匹配策略 1）
        _GENERIC_DIR_NAMES_FOR_WINGET = {
            'temp', 'tmp', 'cache', 'caches', 'logs', 'log', 'data',
            'apps', 'programs', 'packages', 'common', 'common files',
            'microsoft', 'windows', 'program files', 'program files (x86)',
            'programdata', 'appdata', 'local', 'roaming', 'system32',
            'config', 'settings', 'update', 'updater', 'upgrade',
            'default', 'user data', 'crashdumps', 'downloads',
            'application data', 'history', 'temporary',
            'google', 'apple', 'oracle', 'intel',
            'adobe', 'mozilla', 'nvidia', 'amd', 'ibm',
            'package cache', 'winsxs', 'assembly', 'driverstore',
            'install', 'installed', 'installation',
            'help', 'documentation', 'docs', 'doc',
            'media', 'video', 'audio', 'image', 'images', 'picture',
            'fonts', 'lang', 'language', 'locale', 'locales',
            'plugins', 'plugin', 'extension', 'extensions',
            'themes', 'theme', 'skin', 'skins',
            'backup', 'backups', 'old', 'archive',
            'bin', 'lib', 'libs', 'src', 'include',
            'public', 'private', 'shared', 'static',
            'desktop', 'documents', 'music', 'pictures', 'videos',
            'favorites', 'links', 'sandbox', 'installer', 'installers',
            'internet explorer', 'windows defender', 'reference assemblies',
            'drivers', 'system', 'runtime', 'sdk', 'core',
            'service', 'services', 'engine', 'editor', 'viewer',
            'player', 'reader', 'manager', 'client', 'server',
            'studio', 'portable', 'launcher',
            'app', 'application', 'tool', 'tools', 'kit',
            # Windows 自带虚拟化功能（不应匹配第三方 Hyper-V 管理工具）
            'hyper-v', 'hyperv', 'wsl', 'wsl2',
            # 其他常见系统目录
            'onedrive', 'microsoft edge', 'microsoft office',
            'windowsapps', 'modifiablewindowsapps',
            'store', 'stores', 'wallet', 'wallets',
            # 补充：Windows/Electron/Chromium 内部通用目录名（不应匹配具体软件）
            # 这些是浏览器/Electron 应用运行时产生的内部缓存目录，不是软件名
            'comms', 'squirreltemp', 'cef', 'chromeextensioncache',
            'crashrpt', 'crashreport', 'crashreporter', 'crashpad',
            'temporary internet files',
            'ebwebview', 'gpucache', 'shader cache',
            'nodedata', 'shared_proto_db',
            'dawncache', 'dawnwebgpucache',
            # 厂商容器目录（应走第19层厂商容器判定，不应绑定具体产品）
            'tencent', 'netease', 'ncsoft', 'wegame', 'gtarcade',
            'topaz labs llc', 'vmware', 'spiritrealmworkshop',
            'saerasoft', 'feelfish', 'xiumaster', 'steam++',
            'codebuddyextension', 'workbuddy',
            'docker-secrets-engine', 'vedetector',
            'chromedevtoolsmcp', 'hermes', 'mongodbcompass',
        }
        # 单 token 目录名（按空格/连字符/下划线/点切分后只剩 1 个 token）且是通用词时跳过所有匹配策略
        import re as _re_gen
        raw_dir_tokens_for_check = set(_re_gen.split(r'[\s\-_\.]+', dl)) - {''}
        if len(raw_dir_tokens_for_check) == 1 and dl in _GENERIC_DIR_NAMES_FOR_WINGET:
            return ()
        # 整个目录名（含连字符的多 token 通用词）也检查
        if dl in _GENERIC_DIR_NAMES_FOR_WINGET:
            return ()
        # 1. 精确匹配 PackageIdentifier（key 已是小写）
        if dl in db:
            entry = db[dl]
            name = entry.get("name", "")
            if name and not _is_vague_desc_static(name):
                return (name, entry.get("desc", ""), entry.get("type", ""))
        # 2. 目录名完全等于 PackageName（用 _WINGET_NAME_INDEX 索引 O(1) 查询）
        #    索引由 _build_winget_name_index() 构建并缓存（name_lower → pkg_id），
        #    此前 _lookup_winget_by_display_name 已复用，_match_winget_db 也应复用，
        #    避免 _match_winget_db 每次扫 19000 条只为找一个 name==dl 的包
        idx = _build_winget_name_index()
        if idx:
            pkg = idx.get(dl)
            if pkg:
                # #5 瘦身:索引只存 pkg_id,三元组从这里反查原 db
                triple = _winget_triple(pkg)
                # 索引构建时未过滤 vague 名，此处取出后仍需校验（与原遍历逻辑一致）
                if triple and not _is_vague_desc_static(triple[0]):
                    return triple
        # 3. 目录名等于 PackageIdentifier 最后一段（点号后的部分）
        #    索引化：后缀索引 O(1) 取候选（同后缀包通常 1-3 个），避免每次全遍历 19421 条
        best_entry = None
        cands = _build_winget_suffix_index().get(dl, ())
        for pkg_id in cands:
            entry = db.get(pkg_id) or {}
            name = entry.get("name", "")
            if not name or _is_vague_desc_static(name):
                continue
            if best_entry is None or len(name) > len(best_entry.get("name", "")):
                best_entry = entry
        if best_entry:
            return (best_entry.get("name", ""),
                    best_entry.get("desc", ""),
                    best_entry.get("type", ""))
        # 4. 兜底：token 子集匹配（dir_name 所有单词都出现在 PackageName 中）
        # 使用驼峰分词：解决 PascalCase 目录名无法匹配带空格的软件名的问题
        # 反向域名包名（com.*/cn.*/dev.*/org.*/io.*）跳过此策略：
        # 例：cn.org.localagent.desktop 不应按 token 子集匹配命中 "Docker Desktop"
        #     com.pot-app.desktop 不应命中 "Docker Desktop"（pkg_id 后缀 desktop 误匹配）
        #     反向域名包名走第7层专门处理，winget 仅做精确匹配（策略 1/2/3）
        import re as _re_tok
        if _re_tok.match(r'^(com|org|io|cn|dev)\.', dir_name.lower()):
            return ()
        dir_tokens = _tokenize_with_camel(dir_name)  # 传入原始大小写以支持驼峰分词
        if not dir_tokens:
            return ()
        max_token_len = max(len(t) for t in dir_tokens)
        # 短目录名单 token 不做 token 匹配（避免 "go"/"set" 等通用短词误匹配）
        # 阈值 >=3 字符，允许 OBS/IDE 等合理缩写通过
        if len(dl) < 3 and max_token_len < 3:
            return ()
        # 单 token dir_name 时，extra token 必须是已知后缀词（命名惯例约束）
        # 避免 "MyApp" 误匹配 "MyApp Meet"（Meet 不是后缀词）
        # 允许 "MyApp" → "MyApp Desktop"（Desktop 是后缀词）
        # 注：drive/cloud/photos 等是软件功能词不是命名后缀，不放入集合
        # 移除 'core'：'core' 是常见产品名词（如 Core Temp, CorelDraw），易误匹配
        _SUFFIX_TOKENS = {
            'studio', 'desktop', 'engine', 'launcher', 'code', 'editor',
            'portable', 'client', 'manager', 'reader', 'player', 'viewer',
            'pro', 'plus', 'office', 'browser', 'messenger', 'chat',
            'mail', 'music', 'video',
            'for', 'the', 'app', 'application', 'tools', 'tool', 'kit',
            'sdk', 'runtime', 'service', 'services',
        }
        # 单 token 判断：用原始切分（不含驼峰分词）判断，
        # 因为 PascalCase 目录名驼峰分词后变多 token，不应按单 token 约束
        raw_dir_tokens = set(_re_tok.split(r'[\s\-_\.]+', dl)) - {''}
        is_single_token = len(raw_dir_tokens) == 1
        token_best_entry = None
        tok_cache = _build_winget_name_tokens()
        # 倒排索引查询：候选 = 含全部 dir_tokens 的包（集合交集），
        # 数学上等价于原"dir_tokens ⊆ name_tokens"全遍历条件；候选通常几个~几十个。
        # 排序保持 db 顺序（选最短 name 同长取先，与全遍历版结果一致）
        _inv, _order = _build_winget_token_index()
        _cand = None
        for _tok in dir_tokens:
            _s = _inv.get(_tok)
            if not _s:
                _cand = None
                break
            _cand = _s if _cand is None else (_cand & _s)
            if not _cand:
                break
        for pkg_id in (sorted(_cand, key=_order.get) if _cand else ()):
            entry = db.get(pkg_id) or {}
            name = entry.get("name", "")
            if not name or _is_vague_desc_static(name):
                continue
            name_tokens = tok_cache.get(pkg_id)
            if not name_tokens:
                continue
            # name_tokens 数量只能比 dir_tokens 多 1-4 个（避免无限制扩展匹配）
            extra = len(name_tokens) - len(dir_tokens)
            if not (1 <= extra <= 4):
                continue
            # 单 token dir_name 时（原始切分），extra token 必须全在后缀词集合里
            if is_single_token:
                extra_tokens = name_tokens - dir_tokens
                if not extra_tokens.issubset(_SUFFIX_TOKENS):
                    continue
            # 选最短 name（最精确匹配）
            if token_best_entry is None or len(name) < len(token_best_entry.get("name", "")):
                token_best_entry = entry
        if token_best_entry:
            return (token_best_entry.get("name", ""),
                    token_best_entry.get("desc", ""),
                    token_best_entry.get("type", ""))
        return ()
    except Exception:
        return ()



# ========== 厂商容器目录判定（通用启发式，不针对特定厂商/软件）==========
# 解决问题：某些厂商在六个大目录下创建以厂商名命名的容器目录，内含多个子产品
#   例：<厂商名>/ 下有 <产品A>/ <产品B>/ 等多个子目录，根目录无 exe
#   此类目录无法用软件名识别（它不是软件，是容器），需特殊处理：
#   - 优先扫描子目录识别具体产品
#   - 识别失败时返回"无法识别：厂商容器目录"，避免联网搜索厂商名产生无关结果
# 判定条件（启发式，通用）：
#   1. 目录下有 ≥2 个子目录
#   2. 根目录无 .exe 文件
#   3. 目录名长度 ≥ 4（避免短词如 "go"/"set"）
#   4. 排除已知系统目录（Packages/Crashpad 等已在前面层处理）
#   5. 目录名是 PascalCase 或单词（首字母大写，避免全小写通用词）

def _is_vendor_container_dir(dir_path, dir_name):
    """启发式判定目录是否为厂商容器目录
    返回 (is_container, subdir_list) —— is_container=True 时 subdir_list 为子目录列表
    """
    try:
        if not dir_name or len(dir_name) < 4:
            return False, []
        # 目录名首字母大写（PascalCase 或单词），避免全小写通用词误判
        if not dir_name[0].isupper():
            return False, []
        # 排除已知的非容器目录模式（含分隔符的多词目录名通常是软件名）
        if any(c in dir_name for c in ['-', '_', '.', ' ']):
            return False, []
        # 读取目录内容
        try:
            entries = os.listdir(dir_path)
        except Exception:
            return False, []
        # 统计子目录和根目录 exe
        subdirs = []
        has_root_exe = False
        for entry in entries:
            try:
                full = os.path.join(dir_path, entry)
                if os.path.isdir(full):
                    subdirs.append(entry)
                elif entry.lower().endswith('.exe'):
                    has_root_exe = True
            except Exception:
                continue
        # 根目录有 exe → 是软件目录，不是容器
        if has_root_exe:
            return False, []
        # 子目录 < 2 → 不是容器
        if len(subdirs) < 2:
            return False, []
        # 排除系统组件目录（Packages/UWP 容器等，已在前面层处理）
        _SYSTEM_CONTAINER_NAMES = {
            "packages", "crashpad", "temp", "cache", "logs",
            "windowsapps", "packagetmp", "localcache", "localstate",
        }
        if dir_name.lower() in _SYSTEM_CONTAINER_NAMES:
            return False, []
        return True, subdirs
    except Exception:
        return False, []


def _detect_vendor_container(dir_path, dir_name):
    """厂商容器目录识别：扫描子目录识别具体产品
    返回识别结果字符串，识别失败返回带原因的"无法识别"说明
    识别策略（优先级）：
    1. 扫描子目录名匹配 KNOWN_SOFTWARE_DIRS
    2. 扫描子目录 PE 版本信息
    3. 都失败：返回"无法识别：厂商容器目录（含 N 个子产品）"
    """
    try:
        is_container, subdirs = _is_vendor_container_dir(dir_path, dir_name)
        # 强厂商容器黑名单（Adobe/Google/Mozilla/Tencent/NCSOFT 等）
        # 即使 _is_vendor_container_dir 启发式未识别（如子目录<2 或有分隔符），
        # 也按目录名黑名单强制按厂商容器目录处理：
        # - 不返回具体子产品的 PE 信息（避免 Adobe → "Adobe 创意云" 错配）
        # - 直接返回"无法识别：厂商容器目录"提示用户进入子目录单独识别
        dl_lower = dir_name.lower()
        in_vendor_set = dl_lower in _VENDOR_CONTAINER_DIRS_FOR_FEATURE
        # 前缀匹配：含分隔符的厂商衍生目录（如 "Mozilla-1de4eec8-..." / "NeteaseWinDev"）
        # 用 startswith 处理厂商前缀 + hash 后缀 / PascalCase 拼接的目录名
        if not in_vendor_set:
            for prefix in ('netease', 'mozilla-', 'tencent', 'adobe-', 'google-'):
                if dl_lower.startswith(prefix) and len(dl_lower) > len(prefix):
                    in_vendor_set = True
                    break
        if not is_container and in_vendor_set:
            try:
                entries = os.listdir(dir_path)
                subdirs = [e for e in entries if os.path.isdir(os.path.join(dir_path, e))]
                is_container = True
            except Exception as e:
                log.debug("忽略异常: %s", e)
        if not is_container:
            return ""
        # 1. 扫描子目录名匹配 KNOWN_SOFTWARE_DIRS
        # 注：强厂商容器黑名单（Adobe/Google/Mozilla 等）不返回具体子产品名
        # 仅对启发式识别的厂商容器目录（非黑名单）才尝试子目录名匹配
        is_strong_vendor = in_vendor_set
        if not is_strong_vendor:
            try:
                from config import KNOWN_SOFTWARE_DIRS
                sorted_keys = sorted(KNOWN_SOFTWARE_DIRS.keys(), key=len, reverse=True)
                skip_keys = {"common", "code", "apps", "data", "cache",
                             "temp", "logs", "backup", "update", "updater", "config",
                             "history", "installer"}
                for subdir in subdirs:
                    subdir_lower = subdir.lower()
                    for k in sorted_keys:
                        if k in skip_keys:
                            continue
                        if k in subdir_lower:
                            desc = KNOWN_SOFTWARE_DIRS[k]
                            if not _is_vague_desc_static(desc):
                                return desc
            except Exception as e:
                log.debug("忽略异常: %s", e)
            # 2. 扫描子目录 PE 版本信息（最多扫 5 个子目录）
            # 注：强厂商容器黑名单跳过此层（避免 Adobe → "Adobe 创意云" 错配）
            scan_count = 0
            for subdir in subdirs:
                if scan_count >= 5:
                    break
                subdir_path = os.path.join(dir_path, subdir)
                try:
                    if not os.path.isdir(subdir_path):
                        continue
                except Exception:
                    continue
                scan_count += 1
                try:
                    for entry in os.listdir(subdir_path):
                        if entry.lower().endswith('.exe'):
                            try:
                                info = get_exe_version_info(os.path.join(subdir_path, entry))
                                if info:
                                    return info
                            except Exception as e:
                                log.debug("忽略异常: %s", e)
                            break  # 每个子目录只试第一个 exe
                except Exception:
                    continue
        # 3. 都失败：返回"无法识别"+原因（不触发联网搜索厂商名，避免无关结果）
        # 优化：含 0 个子产品时，目录名本身即厂商名，返回厂商说明
        if len(subdirs) == 0:
            # 厂商名映射表（容器目录无子产品时直接返回厂商说明）
            VENDOR_NAME_MAP = {
                'adobe':         'Adobe 系列软件容器目录',
                'google':        'Google 系列软件容器目录',
                'nvidia':        'NVIDIA 显卡驱动与配套软件',
                'intel':         'Intel 驱动与工具软件',
                'intel corporation': 'Intel 驱动与工具软件',
                'mozilla':       'Mozilla 系列软件容器目录',
                'tencent':       '腾讯系列软件容器目录',
                'bytedance':     '字节跳动系列软件容器目录',
                'netease':       '网易系列软件容器目录',
                'kingsoft':      '金山系列软件容器目录',
                'alibaba':       '阿里巴巴系列软件容器目录',
                'openai':        'OpenAI 系列软件容器目录',
                'vmware':        'VMware 虚拟机软件',
                'microsoft_corporation': 'Microsoft 系列软件缓存',
                'purpledome':    'PurpleDome 软件',
                'reckfeng':      'Reckfeng 软件',
                'ourplayer':     'OurPlayer 视频播放器',
                'steam++':       'Watt Toolkit（原 Steam++）游戏工具',
                'gtarcade':      'Hoolai 海外游戏平台',
                'ncsoft':        'NCSOFT 韩国游戏公司',
                'softdeluxe':    'SoftDeluxe 软件',
                'saerasoft':     'SaeraSoft 软件',
                'sentry':        'Sentry 错误监控 SDK',
                'feelfish':      'FeelFish 软件',
            }
            vendor_desc = VENDOR_NAME_MAP.get(dl_lower)
            if vendor_desc:
                return vendor_desc
        # 优化：含 1 个子产品时，直接用子产品名反查 winget DB
        # 避免返回"无法识别：厂商容器目录（含 1 个子产品）"这种无信息提示
        if len(subdirs) == 1:
            _sub_name = subdirs[0]
            _sub_winget = _lookup_winget_by_display_name(_sub_name)
            if _sub_winget:
                _sw_name, _sw_desc, _sw_type = _sub_winget
                return _sw_desc or _sw_name
            # winget 未命中，返回厂商 + 子产品名（如"Adobe Photoshop"）
            _vendor_label = VENDOR_NAME_MAP.get(dl_lower, '').split(' ')[0] if dl_lower in VENDOR_NAME_MAP else ''
            if _vendor_label:
                return f"{_vendor_label} {_sub_name}"
            return _sub_name
        return f"无法识别：厂商容器目录（含 {len(subdirs)} 个子产品，建议进入子目录单独识别）"
    except Exception:
        return ""


# ========== 增强识别：WMI / App Paths / 开始菜单lnk / 深层PE扫描 ==========

def _match_wmi_installed(dir_path, dir_name):
    """通过WMI查询已安装软件（比注册表Uninstall更全，覆盖MSI/UWP/Steam等）
    使用win32com调用WMI（不依赖PowerShell），匹配 InstallLocation 或 Name 包含目录名
    返回最佳匹配的软件名，失败返回空字符串
    Win32_InstalledWin32Program 在 Win10+ 可用，失败则返回空
    """
    try:
        import win32com.client
        wmi = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        svc = wmi.ConnectServer(".", "root\\\\cimv2")
        # Win32_InstalledWin32Program 比 Win32_Product 快很多（不触发MSI重新配置）
        try:
            items = svc.ExecQuery(
                "SELECT Name, Version, InstallLocation FROM Win32_InstalledWin32Program"
            )
        except Exception:
            # 旧系统无此类，降级为空
            return ""
        path_lower = dir_path.lower().rstrip("\\").replace("\\\\", "\\")
        path_lower_norm = path_lower + "\\"
        dir_lower = (dir_name or "").lower()
        # 厂商容器目录/通用词目录跳过 WMI 反查（与 _match_registry_uninstall 同源过滤逻辑）
        # 避免 Adobe → Adobe Creative Cloud、Google → Google Chrome 等错配
        _GENERIC_DIR_NAMES_FOR_WMI = {
            'adobe', 'google', 'mozilla', 'tencent', 'netease', 'ncsoft',
            'nvidia', 'amd', 'intel', 'ibm', 'oracle', 'apple', 'microsoft',
            'vmware', 'topaz labs llc', 'saerasoft', 'feelfish', 'wegame',
            'gtarcade', 'spiritrealmworkshop', 'xiumaster', 'steam++',
            'ourplayer', 'cef', 'comms', 'cache', 'crashdumps', 'crashrpt',
            'squirreltemp', 'package cache', 'chromeextensioncache',
            'temporary internet files', 'codebuddyextension', 'workbuddy',
            'docker-secrets-engine', 'vedetector', 'chromedevtoolsmcp',
            'hermes', 'mongodbcompass', 'webview2', 'microsoft_corporation',
            'apps', 'programs', 'packages', 'temp', 'tmp', 'data',
            'logs', 'log', 'node', 'pip',
        }
        _VENDOR_CONTAINER_DIRS_FOR_WMI = {
            'adobe', 'google', 'mozilla', 'tencent', 'netease', 'ncsoft',
            'nvidia', 'amd', 'intel', 'ibm', 'oracle', 'apple',
            'vmware', 'topaz labs llc', 'saerasoft', 'feelfish', 'wegame',
            'gtarcade', 'spiritrealmworkshop', 'xiumaster', 'steam++',
            'ourplayer', 'microsoft_corporation',
        }
        is_vendor_container = dir_lower in _VENDOR_CONTAINER_DIRS_FOR_WMI
        skip_subname_match = (
            (len(dir_lower) < 4)
            or (dir_lower in _GENERIC_DIR_NAMES_FOR_WMI)
        )
        best_match = ""
        best_score = 0
        for item in items:
            try:
                name = item.Name or ""
                install_loc = ""
                try:
                    install_loc = item.InstallLocation or ""
                except Exception as e:
                    log.debug("忽略异常: %s", e)
                if not name:
                    continue
                # 情况1: InstallLocation 双向匹配
                # 厂商容器目录跳过此情况（避免 Adobe → AdobeCreativeCloud 错配）
                if install_loc and not is_vendor_container:
                    install_lower = install_loc.lower().rstrip("\\").replace("\\\\", "\\")
                    if path_lower == install_lower or install_lower.startswith(path_lower_norm):
                        if len(name) > best_score:
                            best_match = name
                            best_score = len(name)
                    elif path_lower.startswith(install_lower + "\\"):
                        if len(name) > best_score:
                            best_match = name
                            best_score = len(name)
                # 情况2: 软件名包含目录名（短词和厂商容器目录跳过避免误匹配）
                if not skip_subname_match and dir_lower and len(dir_lower) >= 4:
                    if dir_lower in name.lower():
                        if len(name) > best_score:
                            best_match = name
                            best_score = len(name)
            except Exception:
                continue
        return best_match
    except Exception:
        return ""


def _match_app_paths(dir_name):
    r"""从注册表 App Paths 匹配软件名
    HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\*.exe
    很多软件会注册App Paths，键名就是exe文件名
    返回匹配的软件名（exe名去掉.exe），失败返回空
    """
    try:
        import winreg
        if not dir_name or len(dir_name) < 3:
            return ""
        dir_lower = dir_name.lower()
        # 厂商容器目录完全跳过此层（避免 Adobe/Google 等被错配到具体产品）
        if dir_lower in _VENDOR_CONTAINER_DIRS_FOR_FEATURE:
            return ""
        # 通用词目录跳过子串匹配（避免 Cache → ChromeCache 等）
        skip_substring_match = (
            (len(dir_name) < 4)
            or (dir_lower in _GENERIC_SHORT_NAMES_FOR_FEATURE)
        )
        roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        ]
        best_match = ""
        for root, subkey in roots:
            try:
                key = winreg.OpenKey(root, subkey)
            except OSError:
                continue
            try:
                i = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(key, i)
                        i += 1
                    except OSError:
                        break
                    # sub_name 形如 "xxx.exe"（exe 文件名）
                    exe_lower = sub_name.lower()
                    if exe_lower.endswith(".exe"):
                        exe_base = exe_lower[:-4]
                        # 精确匹配优先，子串匹配仅对非通用词
                        is_exact = (exe_base == dir_lower)
                        is_substring = (
                            (not skip_substring_match)
                            and (dir_lower in exe_base or exe_base in dir_lower)
                            and len(exe_base) >= 4
                        )
                        if is_exact or is_substring:
                            # 读取默认值或AppName
                            try:
                                sub_key = winreg.OpenKey(key, sub_name)
                                try:
                                    # 优先读 AppName 值
                                    try:
                                        app_name, _ = winreg.QueryValueEx(sub_key, "AppName")
                                        if app_name:
                                            return app_name
                                    except OSError as e:
                                        log.debug("忽略异常: %s", e)
                                    # 兜底用exe名（去掉.exe，首字母大写）
                                    if len(exe_base) > len(best_match):
                                        best_match = exe_base.capitalize()
                                finally:
                                    winreg.CloseKey(sub_key)
                            except OSError as e:
                                log.debug("忽略异常: %s", e)
            finally:
                winreg.CloseKey(key)
        return best_match
    except Exception:
        return ""


def _match_start_menu_lnk(dir_name):
    r"""从开始菜单快捷方式匹配软件名
    扫描 %ProgramData% 和 %AppData% 下的 Start Menu\Programs\*.lnk
    匹配lnk文件名包含目录名的项，返回软件名
    """
    try:
        if not dir_name or len(dir_name) < 3:
            return ""
        dir_lower = dir_name.lower()
        # 厂商容器目录完全跳过此层（避免 Adobe/Google 等被错配到具体产品）
        if dir_lower in _VENDOR_CONTAINER_DIRS_FOR_FEATURE:
            return ""
        # 通用词目录跳过子串匹配（避免 Cache → ChromeCache.lnk 等）
        skip_substring_match = (
            (len(dir_name) < 4)
            or (dir_lower in _GENERIC_SHORT_NAMES_FOR_FEATURE)
        )
        start_menu_dirs = []
        # 系统级开始菜单
        prog_data = os.environ.get("PROGRAMDATA", "")
        if prog_data:
            start_menu_dirs.append(os.path.join(prog_data, "Microsoft", "Windows", "Start Menu", "Programs"))
        # 用户级开始菜单
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            start_menu_dirs.append(os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs"))
        best_match = ""
        for sm_dir in start_menu_dirs:
            if not os.path.isdir(sm_dir):
                continue
            # 递归扫描（限制深度2层）
            for root, dirs, files in os.walk(sm_dir):
                # 限制深度
                rel = os.path.relpath(root, sm_dir)
                if rel.count(os.sep) >= 2:
                    dirs[:] = []
                    continue
                for f in files:
                    if not f.lower().endswith(".lnk"):
                        continue
                    lnk_base = f[:-4].lower()  # 去掉.lnk
                    # 精确匹配优先，子串匹配仅对非通用词
                    is_exact = (lnk_base == dir_lower)
                    is_substring = (
                        (not skip_substring_match)
                        and (dir_lower in lnk_base or lnk_base in dir_lower)
                        and len(lnk_base) >= 4
                    )
                    if is_exact or is_substring:
                        # 读取lnk目标exe的PE信息
                        try:
                            target_exe = _read_lnk_target(os.path.join(root, f))
                            if target_exe:
                                info = get_exe_version_info(target_exe)
                                # 过滤通用/系统组件PE文本（如 "Microsoft® Windows® Operating System"）
                                # node.exe 等用 Microsoft 工具链编译的exe PE CompanyName 会被标记为系统组件
                                if info and not _is_generic_pe_text(info):
                                    return info
                        except Exception as e:
                            log.debug("忽略异常: %s", e)
                        # 兜底用lnk文件名
                        if len(lnk_base) > len(best_match):
                            best_match = f[:-4]
        return best_match
    except Exception:
        return ""


def _is_generic_pe_text(text):
    """过滤 PE 版本信息中的通用/无意义文本（不应作为软件名返回）
    例：'Network'、'User Data'、'Microsoft® Windows® Operating System'、'Shared' 等
    """
    if not text:
        return True
    t = text.strip()
    if not t or len(t) < 3:
        return True
    # 纯通用词（Electron/Chromium 内部子目录名、系统组件描述等）
    GENERIC_PE_TEXTS = {
        'network', 'user data', 'shared', 'common', 'data', 'cache',
        'config', 'settings', 'default', 'main', 'renderer', 'browser',
        'service', 'helper', 'updater', 'update', 'setup', 'installer',
        'uninstaller', 'crashpad', 'crashreport', 'crashreporter',
        'microsoft® windows® operating system',
        'microsoft corporation', 'microsoft', 'windows',
        'google chrome elevation service (googlechromeelevationservice)',
        'google chrome elevation service',
        'googlechromeelevationservice',
        'installshield', 'nullsoft install system', 'nsis',
        'inno setup', 'wix toolset', 'burn',
        # Electron/Chromium 内部模块名（深层PE扫描常读到这些）
        'partitions', 'shared dictionary', 'shared_proto_db',
        'preferences', 'local state', 'local storage',
        'session storage', 'indexeddb', 'storage',
        'gpu cache', 'shader cache', 'code cache',
        'crashpad', 'crashpad database', 'crashpad metrics',
        'app preferences', 'apppreferences',
        'microsoft visual c++ 2005 redistributable',
        'microsoft visual c++ 2013 x64 additional runtime - 12.0.40664',
        'microsoft visual c++ 2013 x64 additional runtime',
        'microsoft visual c++ 2005 redist',
        'microsoft visual c++ 2008 redist',
        'winrt intellisense mobile - en-us',
        'google chrome elevation service',
        # 安装框架描述
        'squirrel', 'squirrel.windows', 'squirreltemp',
        'wix toolset', 'burn',
    }
    if t.lower() in GENERIC_PE_TEXTS:
        return True
    # GUID 或版本号模式
    import re
    if re.match(r'^\{[0-9A-Fa-f\-]+\}', t):
        return True
    if re.match(r'^[0-9]+\.[0-9]+\.[0-9]+', t) and len(t) < 30:
        return True
    # 含 "Microsoft® Windows®" 字样（系统组件描述）
    if 'microsoft® windows®' in t.lower():
        return True
    # 含 "Visual C++" 字样（运行时组件，不是软件本体）
    if 'visual c++' in t.lower() and 'redistributable' in t.lower():
        return True
    if 'visual c++' in t.lower() and 'runtime' in t.lower():
        return True
    # 含 "WinRT Intellisense" 字样（系统组件）
    if 'winrt intellisense' in t.lower():
        return True
    # 含 "Elevation Service" 字样（Google Chrome 等的提权服务）
    if 'elevation service' in t.lower():
        return True
    return False


def _deep_scan_exe_pe(dir_path, max_depth=3, max_count=15):
    """深层递归扫描目录中的exe文件并读取PE版本信息
    比一级子目录扫描更深入（限制深度和数量避免太慢）
    返回第一个能读到PE信息的exe的版本信息，失败返回空
    过滤通用/无意义PE文本（Network、User Data、系统组件描述等）
    """
    try:
        exe_count = 0
        for root, dirs, files in os.walk(dir_path):
            # 限制深度
            rel = os.path.relpath(root, dir_path)
            if rel.count(os.sep) >= max_depth:
                dirs[:] = []
                continue
            # 跳过常见的大型数据子目录 + Electron/Chromium 内部通用子目录
            # Network/User Data/Common/Partitions 等子目录的exe通常是 Electron 内部组件，不是软件本体
            dirs[:] = [d for d in dirs if d.lower() not in (
                "node_modules", ".git", "__pycache__", "venv", ".venv",
                "site-packages", "dist-info", "__pypackages__",
                # Electron/Chromium 内部通用子目录（其exe是 PE 但描述是通用词）
                "network", "user data", "default", "cache", "caches",
                "code cache", "gpucache", "dawncache", "dawnwebgpucache",
                "blob_storage", "local storage", "session storage",
                "crashpad", "crashpad database", "crashpad metrics",
                "crashpad metadata", "shared_proto_db", "storage",
                "ebwebview", "browser", "renderer", "extensions",
                "plugins", "shaders", "textures", "models", "assets",
                "build", "dist", "config", "settings", "logs", "log",
                "temp", "tmp", "data", "saved", "common", "shared",
                "service", "helper", "updater", "update", "setup",
                "installer", "uninstaller",
                # Electron/Chromium 内部模块（PE 描述为通用词）
                "partitions", "shared dictionary", "app preferences",
                "preferences", "local state", "indexeddb",
                "gpu cache", "shader cache",
            )]
            for f in files:
                if f.lower().endswith(".exe"):
                    exe_path = os.path.join(root, f)
                    info = get_exe_version_info(exe_path)
                    if info and not _is_generic_pe_text(info):
                        return info
                    exe_count += 1
                    if exe_count >= max_count:
                        return ""
        return ""
    except Exception:
        return ""


def _scan_subdir_for_exe_pe(dir_path, max_subdirs=10):
    """扫描一级子目录中的exe并读取PE信息（比深层扫描快，只扫一级）
    返回第一个能读到PE信息的exe版本信息，失败返回空
    过滤通用/无意义PE文本（Network、User Data、系统组件描述等）
    """
    # 通用子目录名黑名单：这些子目录的exe通常是 Electron 内部组件，不应作为软件名返回
    GENERIC_SUBDIRS = {
        "network", "user data", "default", "cache", "caches",
        "code cache", "gpucache", "dawncache", "dawnwebgpucache",
        "blob_storage", "local storage", "session storage",
        "crashpad", "crashpad database", "crashpad metrics",
        "crashpad metadata", "shared_proto_db", "storage",
        "ebwebview", "browser", "renderer", "extensions",
        "plugins", "shaders", "textures", "models", "assets",
        "build", "dist", "config", "settings", "logs", "log",
        "temp", "tmp", "data", "saved", "common", "shared",
        "service", "helper", "updater", "update", "setup",
        "installer", "uninstaller",
        # Electron/Chromium 内部模块（PE 描述为通用词）
        "partitions", "shared dictionary", "app preferences",
        "preferences", "local state", "indexeddb",
        "gpu cache", "shader cache",
    }
    try:
        sub_count = 0
        for entry in os.listdir(dir_path):
            if sub_count >= max_subdirs:
                break
            sub_path = os.path.join(dir_path, entry)
            if not os.path.isdir(sub_path):
                continue
            # 跳过通用子目录（Network/User Data 等）
            if entry.lower() in GENERIC_SUBDIRS:
                continue
            sub_count += 1
            try:
                for item in os.listdir(sub_path):
                    if item.lower().endswith(".exe"):
                        exe_path = os.path.join(sub_path, item)
                        info = get_exe_version_info(exe_path)
                        if info and not _is_generic_pe_text(info):
                            return info
                        break
            except Exception as e:
                log.debug("忽略异常: %s", e)
        return ""
    except Exception:
        return ""


# ========== 基于路径位置的智能识别 ==========
# 同一软件在不同位置功能不同，例如某软件:
#   AppData\Local\<软件名>       → <软件名> 本地缓存与配置
#   Program Files\<软件名>       → <软件名> 主程序（64 位）
#   Program Files (x86)\<软件名> → <软件名> 主程序（32 位）

# 6个扫描根目录的位置标签
LOCATION_LABELS = {
    "appdata_local":   "本地缓存与配置",
    "appdata_roaming": "用户配置与账号数据",
    "program_files":   "主程序（64 位）",
    "program_files_x86": "主程序（32 位）",
    "programdata":     "系统级共享数据",
    "programs":        "安装目录",
}


# ========== type × position 矩阵（第5步子任务E）==========
# 来源：winget_software_db.json 的 type 字段（78类分类，覆盖10500条软件）
# 用途：按 (软件类型, 目录位置) 生成差异化的功能+位置描述，替代千篇一律的位置模板
# 覆盖范围：top 30 type，覆盖约71%的软件条目；未命中的 type 走原 _identify_by_location 兜底
# 模板设计原则：
#   1. {sw} 占位符填充真实软件名（如"Discord"）
#   2. 包含具体功能描述（如"通讯软件缓存与聊天记录"），避免"本地数据（缓存/配置）"机器短语
#   3. 含"能不能删"提示（如"清理后需重新登录""不要删""可清理"）
#   4. 同类软件描述格式一致，保证区分度
_TYPE_POSITION_MATRIX = {
    "浏览器": {
        "local":            "{sw} 浏览器网页缓存与 Cookie，清理后需重新登录",
        "roaming":          "{sw} 浏览器书签、历史与密码配置，不要删",
        "program_files":    "{sw} 浏览器主程序（64 位）",
        "program_files_x86":"{sw} 浏览器主程序（32 位）",
        "programdata":      "{sw} 浏览器全局配置",
        "programs":         "{sw} 浏览器安装目录",
    },
    "通讯软件": {
        "local":            "{sw} 通讯软件缓存与聊天记录",
        "roaming":          "{sw} 通讯软件账号配置与好友列表，不要删",
        "program_files":    "{sw} 通讯软件主程序（64 位）",
        "program_files_x86":"{sw} 通讯软件主程序（32 位）",
        "programdata":      "{sw} 通讯软件全局数据",
        "programs":         "{sw} 通讯软件安装目录",
    },
    "开发工具": {
        "local":            "{sw} 开发工具缓存与扩展数据",
        "roaming":          "{sw} 开发工具配置文件与已安装插件",
        "program_files":    "{sw} 开发工具主程序（64 位）",
        "program_files_x86":"{sw} 开发工具主程序（32 位）",
        "programdata":      "{sw} 开发工具全局配置",
        "programs":         "{sw} 开发工具安装目录",
    },
    "代码编辑器": {
        "local":            "{sw} 代码编辑器缓存与扩展数据",
        "roaming":          "{sw} 代码编辑器配置文件与已安装插件",
        "program_files":    "{sw} 代码编辑器主程序（64 位）",
        "program_files_x86":"{sw} 代码编辑器主程序（32 位）",
        "programdata":      "{sw} 代码编辑器全局配置",
        "programs":         "{sw} 代码编辑器安装目录",
    },
    "IDE": {
        "local":            "{sw} IDE 缓存与索引数据",
        "roaming":          "{sw} IDE 配置文件、插件与项目模板",
        "program_files":    "{sw} IDE 主程序（64 位）",
        "program_files_x86":"{sw} IDE 主程序（32 位）",
        "programdata":      "{sw} IDE 全局配置",
        "programs":         "{sw} IDE 安装目录",
    },
    "游戏平台": {
        "local":            "{sw} 游戏平台缓存与封面图，可安全清理",
        "roaming":          "{sw} 游戏平台账号配置与登录信息",
        "program_files":    "{sw} 游戏平台主程序（64 位）",
        "program_files_x86":"{sw} 游戏平台主程序（32 位）",
        "programdata":      "{sw} 游戏平台全局数据",
        "programs":         "{sw} 游戏平台安装目录",
    },
    "游戏工具": {
        "local":            "{sw} 游戏工具缓存数据",
        "roaming":          "{sw} 游戏工具配置数据",
        "program_files":    "{sw} 游戏工具主程序（64 位）",
        "program_files_x86":"{sw} 游戏工具主程序（32 位）",
        "programdata":      "{sw} 游戏工具全局数据",
        "programs":         "{sw} 游戏工具安装目录",
    },
    "云盘同步": {
        "local":            "{sw} 云盘同步缓存与本地副本",
        "roaming":          "{sw} 云盘账号配置",
        "program_files":    "{sw} 云盘客户端主程序（64 位）",
        "program_files_x86":"{sw} 云盘客户端主程序（32 位）",
        "programdata":      "{sw} 云盘全局数据",
        "programs":         "{sw} 云盘客户端安装目录",
    },
    "办公套件": {
        "local":            "{sw} 办公套件缓存与最近文档",
        "roaming":          "{sw} 办公套件用户配置与模板",
        "program_files":    "{sw} 办公套件主程序（64 位）",
        "program_files_x86":"{sw} 办公套件主程序（32 位）",
        "programdata":      "{sw} 办公套件全局数据",
        "programs":         "{sw} 办公套件安装目录",
    },
    "媒体播放": {
        "local":            "{sw} 媒体播放器缓存与字幕",
        "roaming":          "{sw} 媒体播放器用户配置与播放列表",
        "program_files":    "{sw} 媒体播放器主程序（64 位）",
        "program_files_x86":"{sw} 媒体播放器主程序（32 位）",
        "programdata":      "{sw} 媒体播放器全局数据",
        "programs":         "{sw} 媒体播放器安装目录",
    },
    "视频剪辑": {
        "local":            "{sw} 视频剪辑软件缓存与渲染临时文件，可清理",
        "roaming":          "{sw} 视频剪辑软件用户配置与预设",
        "program_files":    "{sw} 视频剪辑软件主程序（64 位）",
        "program_files_x86":"{sw} 视频剪辑软件主程序（32 位）",
        "programdata":      "{sw} 视频剪辑软件全局数据",
        "programs":         "{sw} 视频剪辑软件安装目录",
    },
    "图片处理": {
        "local":            "{sw} 图片处理软件缓存与最近文档",
        "roaming":          "{sw} 图片处理软件用户配置与预设",
        "program_files":    "{sw} 图片处理软件主程序（64 位）",
        "program_files_x86":"{sw} 图片处理软件主程序（32 位）",
        "programdata":      "{sw} 图片处理软件全局数据",
        "programs":         "{sw} 图片处理软件安装目录",
    },
    "音频编辑": {
        "local":            "{sw} 音频编辑软件缓存与波形预览",
        "roaming":          "{sw} 音频编辑软件用户配置与预设",
        "program_files":    "{sw} 音频编辑软件主程序（64 位）",
        "program_files_x86":"{sw} 音频编辑软件主程序（32 位）",
        "programdata":      "{sw} 音频编辑软件全局数据",
        "programs":         "{sw} 音频编辑软件安装目录",
    },
    "安全软件": {
        "local":            "{sw} 安全软件扫描缓存与隔离区",
        "roaming":          "{sw} 安全软件用户配置",
        "program_files":    "{sw} 安全软件主程序（64 位）",
        "program_files_x86":"{sw} 安全软件主程序（32 位）",
        "programdata":      "{sw} 安全软件全局数据",
        "programs":         "{sw} 安全软件安装目录",
    },
    "VPN代理": {
        "local":            "{sw} VPN代理缓存与日志",
        "roaming":          "{sw} VPN代理账号配置",
        "program_files":    "{sw} VPN代理主程序（64 位）",
        "program_files_x86":"{sw} VPN代理主程序（32 位）",
        "programdata":      "{sw} VPN代理全局配置",
        "programs":         "{sw} VPN代理安装目录",
    },
    "终端": {
        "local":            "{sw} 终端缓存与会话记录",
        "roaming":          "{sw} 终端用户配置与主题",
        "program_files":    "{sw} 终端主程序（64 位）",
        "program_files_x86":"{sw} 终端主程序（32 位）",
        "programdata":      "{sw} 终端全局配置",
        "programs":         "{sw} 终端安装目录",
    },
    "数据库管理": {
        "local":            "{sw} 数据库管理工具缓存与连接数据",
        "roaming":          "{sw} 数据库管理工具用户配置与连接信息",
        "program_files":    "{sw} 数据库管理工具主程序（64 位）",
        "program_files_x86":"{sw} 数据库管理工具主程序（32 位）",
        "programdata":      "{sw} 数据库管理工具全局配置",
        "programs":         "{sw} 数据库管理工具安装目录",
    },
    "版本控制": {
        "local":            "{sw} 版本控制工具缓存与仓库数据",
        "roaming":          "{sw} 版本控制工具用户配置",
        "program_files":    "{sw} 版本控制工具主程序（64 位）",
        "program_files_x86":"{sw} 版本控制工具主程序（32 位）",
        "programdata":      "{sw} 版本控制工具全局配置",
        "programs":         "{sw} 版本控制工具安装目录",
    },
    "下载工具": {
        "local":            "{sw} 下载工具缓存与未完成任务，谨慎清理",
        "roaming":          "{sw} 下载工具用户配置",
        "program_files":    "{sw} 下载工具主程序（64 位）",
        "program_files_x86":"{sw} 下载工具主程序（32 位）",
        "programdata":      "{sw} 下载工具全局数据",
        "programs":         "{sw} 下载工具安装目录",
    },
    "文件管理": {
        "local":            "{sw} 文件管理工具缓存与缩略图",
        "roaming":          "{sw} 文件管理工具用户配置",
        "program_files":    "{sw} 文件管理工具主程序（64 位）",
        "program_files_x86":"{sw} 文件管理工具主程序（32 位）",
        "programdata":      "{sw} 文件管理工具全局数据",
        "programs":         "{sw} 文件管理工具安装目录",
    },
    "磁盘工具": {
        "local":            "{sw} 磁盘工具缓存与扫描数据",
        "roaming":          "{sw} 磁盘工具用户配置",
        "program_files":    "{sw} 磁盘工具主程序（64 位）",
        "program_files_x86":"{sw} 磁盘工具主程序（32 位）",
        "programdata":      "{sw} 磁盘工具全局数据",
        "programs":         "{sw} 磁盘工具安装目录",
    },
    "笔记": {
        "local":            "{sw} 笔记软件本地缓存",
        "roaming":          "{sw} 笔记软件账号配置与笔记数据",
        "program_files":    "{sw} 笔记软件主程序（64 位）",
        "program_files_x86":"{sw} 笔记软件主程序（32 位）",
        "programdata":      "{sw} 笔记软件全局数据",
        "programs":         "{sw} 笔记软件安装目录",
    },
    "远程控制": {
        "local":            "{sw} 远程控制软件缓存与会话日志",
        "roaming":          "{sw} 远程控制软件账号配置",
        "program_files":    "{sw} 远程控制软件主程序（64 位）",
        "program_files_x86":"{sw} 远程控制软件主程序（32 位）",
        "programdata":      "{sw} 远程控制软件全局配置",
        "programs":         "{sw} 远程控制软件安装目录",
    },
    "服务器": {
        "local":            "{sw} 服务器软件本地数据",
        "roaming":          "{sw} 服务器软件用户配置",
        "program_files":    "{sw} 服务器软件主程序（64 位）",
        "program_files_x86":"{sw} 服务器软件主程序（32 位）",
        "programdata":      "{sw} 服务器软件全局数据",
        "programs":         "{sw} 服务器软件安装目录",
    },
    "加密解密": {
        "local":            "{sw} 加密解密工具缓存数据",
        "roaming":          "{sw} 加密解密工具用户配置与密钥",
        "program_files":    "{sw} 加密解密工具主程序（64 位）",
        "program_files_x86":"{sw} 加密解密工具主程序（32 位）",
        "programdata":      "{sw} 加密解密工具全局数据",
        "programs":         "{sw} 加密解密工具安装目录",
    },
    "编程语言": {
        "local":            "{sw} 编程语言运行时与包缓存",
        "roaming":          "{sw} 编程语言用户配置",
        "program_files":    "{sw} 编程语言主程序（64 位）",
        "program_files_x86":"{sw} 编程语言主程序（32 位）",
        "programdata":      "{sw} 编程语言全局数据",
        "programs":         "{sw} 编程语言安装目录",
    },
    "AI工具": {
        "local":            "{sw} AI工具缓存与模型数据",
        "roaming":          "{sw} AI工具用户配置与对话历史",
        "program_files":    "{sw} AI工具主程序（64 位）",
        "program_files_x86":"{sw} AI工具主程序（32 位）",
        "programdata":      "{sw} AI工具全局数据",
        "programs":         "{sw} AI工具安装目录",
    },
    "学习工具": {
        "local":            "{sw} 学习工具缓存数据",
        "roaming":          "{sw} 学习工具用户配置与学习记录",
        "program_files":    "{sw} 学习工具主程序（64 位）",
        "program_files_x86":"{sw} 学习工具主程序（32 位）",
        "programdata":      "{sw} 学习工具全局数据",
        "programs":         "{sw} 学习工具安装目录",
    },
    "系统工具": {
        "local":            "{sw} 系统工具缓存数据",
        "roaming":          "{sw} 系统工具用户配置",
        "program_files":    "{sw} 系统工具主程序（64 位）",
        "program_files_x86":"{sw} 系统工具主程序（32 位）",
        "programdata":      "{sw} 系统工具全局数据",
        "programs":         "{sw} 系统工具安装目录",
    },
    "系统监控": {
        "local":            "{sw} 系统监控工具缓存与监控数据",
        "roaming":          "{sw} 系统监控工具用户配置",
        "program_files":    "{sw} 系统监控工具主程序（64 位）",
        "program_files_x86":"{sw} 系统监控工具主程序（32 位）",
        "programdata":      "{sw} 系统监控工具全局数据",
        "programs":         "{sw} 系统监控工具安装目录",
    },
    "网络工具": {
        "local":            "{sw} 网络工具缓存数据",
        "roaming":          "{sw} 网络工具用户配置",
        "program_files":    "{sw} 网络工具主程序（64 位）",
        "program_files_x86":"{sw} 网络工具主程序（32 位）",
        "programdata":      "{sw} 网络工具全局数据",
        "programs":         "{sw} 网络工具安装目录",
    },
    "外设控制": {
        "local":            "{sw} 外设控制软件缓存与配置",
        "roaming":          "{sw} 外设控制软件用户配置",
        "program_files":    "{sw} 外设控制软件主程序（64 位）",
        "program_files_x86":"{sw} 外设控制软件主程序（32 位）",
        "programdata":      "{sw} 外设控制软件全局数据",
        "programs":         "{sw} 外设控制软件安装目录",
    },
    "桌面工具": {
        "local":            "{sw} 桌面工具缓存数据",
        "roaming":          "{sw} 桌面工具用户配置",
        "program_files":    "{sw} 桌面工具主程序（64 位）",
        "program_files_x86":"{sw} 桌面工具主程序（32 位）",
        "programdata":      "{sw} 桌面工具全局数据",
        "programs":         "{sw} 桌面工具安装目录",
    },
    "行业软件": {
        "local":            "{sw} 行业软件缓存与本地数据",
        "roaming":          "{sw} 行业软件用户配置",
        "program_files":    "{sw} 行业软件主程序（64 位）",
        "program_files_x86":"{sw} 行业软件主程序（32 位）",
        "programdata":      "{sw} 行业软件全局数据",
        "programs":         "{sw} 行业软件安装目录",
    },
    "实用工具": {
        "local":            "{sw} 实用工具缓存数据",
        "roaming":          "{sw} 实用工具用户配置",
        "program_files":    "{sw} 实用工具主程序（64 位）",
        "program_files_x86":"{sw} 实用工具主程序（32 位）",
        "programdata":      "{sw} 实用工具全局数据",
        "programs":         "{sw} 实用工具安装目录",
    },
    "容器编排": {
        "local":            "{sw} 容器与镜像数据，删除会丢失容器",
        "roaming":          "{sw} 容器编排用户配置",
        "program_files":    "{sw} 容器引擎主程序（64 位）",
        "program_files_x86":"{sw} 容器引擎主程序（32 位）",
        "programdata":      "{sw} 容器全局配置与镜像缓存",
        "programs":         "{sw} 容器引擎安装目录",
    },
    "密码管理": {
        "local":            "{sw} 密码库本地缓存",
        "roaming":          "{sw} 密码库与加密密钥，不要删",
        "program_files":    "{sw} 密码管理器主程序（64 位）",
        "program_files_x86":"{sw} 密码管理器主程序（32 位）",
        "programdata":      "{sw} 密码管理器全局数据",
        "programs":         "{sw} 密码管理器安装目录",
    },
    "压缩工具": {
        "local":            "{sw} 压缩工具临时数据",
        "roaming":          "{sw} 压缩工具用户配置",
        "program_files":    "{sw} 压缩工具主程序（64 位）",
        "program_files_x86":"{sw} 压缩工具主程序（32 位）",
        "programdata":      "{sw} 压缩工具全局配置",
        "programs":         "{sw} 压缩工具安装目录",
    },
    "搜索工具": {
        "local":            "{sw} 搜索索引与缓存，可清理后重建",
        "roaming":          "{sw} 搜索工具用户配置",
        "program_files":    "{sw} 搜索工具主程序（64 位）",
        "program_files_x86":"{sw} 搜索工具主程序（32 位）",
        "programdata":      "{sw} 搜索工具全局数据",
        "programs":         "{sw} 搜索工具安装目录",
    },
    "游戏引擎": {
        "local":            "{sw} 游戏引擎着色器与编译缓存，可清理",
        "roaming":          "{sw} 游戏引擎项目配置",
        "program_files":    "{sw} 游戏引擎主程序（64 位）",
        "program_files_x86":"{sw} 游戏引擎主程序（32 位）",
        "programdata":      "{sw} 游戏引擎全局数据",
        "programs":         "{sw} 游戏引擎安装目录",
    },
    "输入法": {
        "local":            "{sw} 输入法词库与学习记录",
        "roaming":          "{sw} 输入法用户词库与配置",
        "program_files":    "{sw} 输入法主程序（64 位）",
        "program_files_x86":"{sw} 输入法主程序（32 位）",
        "programdata":      "{sw} 输入法全局词库",
        "programs":         "{sw} 输入法安装目录",
    },
    "虚拟机": {
        "local":            "{sw} 虚拟机配置与磁盘映像，不要删",
        "roaming":          "{sw} 虚拟机用户配置",
        "program_files":    "{sw} 虚拟机主程序（64 位）",
        "program_files_x86":"{sw} 虚拟机主程序（32 位）",
        "programdata":      "{sw} 虚拟机全局配置",
        "programs":         "{sw} 虚拟机安装目录",
    },
    "数据库服务器": {
        "local":            "{sw} 数据库数据文件，不要删",
        "roaming":          "{sw} 数据库服务器用户配置",
        "program_files":    "{sw} 数据库服务器主程序（64 位）",
        "program_files_x86":"{sw} 数据库服务器主程序（32 位）",
        "programdata":      "{sw} 数据库全局配置",
        "programs":         "{sw} 数据库服务器安装目录",
    },
    "桌面美化": {
        "local":            "{sw} 桌面美化工具缓存与主题",
        "roaming":          "{sw} 桌面美化用户配置与主题",
        "program_files":    "{sw} 桌面美化工具主程序（64 位）",
        "program_files_x86":"{sw} 桌面美化工具主程序（32 位）",
        "programdata":      "{sw} 桌面美化全局配置",
        "programs":         "{sw} 桌面美化工具安装目录",
    },
    "驱动": {
        "local":            "{sw} 驱动程序本地数据",
        "roaming":          "{sw} 驱动程序用户配置",
        "program_files":    "{sw} 驱动程序主程序（64 位）",
        "program_files_x86":"{sw} 驱动程序主程序（32 位）",
        "programdata":      "{sw} 驱动程序全局配置",
        "programs":         "{sw} 驱动程序安装目录",
    },
    "固件工具": {
        "local":            "{sw} 固件工具缓存数据",
        "roaming":          "{sw} 固件工具用户配置",
        "program_files":    "{sw} 固件工具主程序（64 位）",
        "program_files_x86":"{sw} 固件工具主程序（32 位）",
        "programdata":      "{sw} 固件工具全局配置",
        "programs":         "{sw} 固件工具安装目录",
    },
    # —— 以下 7 类为 2026-07-20 细化新增 ——
    "运行时": {
        "local":            "{sw} 运行时缓存与临时数据",
        "roaming":          "{sw} 运行时用户配置",
        "program_files":    "{sw} 运行时主程序（64 位）",
        "program_files_x86":"{sw} 运行时主程序（32 位）",
        "programdata":      "{sw} 运行时全局数据",
        "programs":         "{sw} 运行时安装目录",
    },
    "编译器": {
        "local":            "{sw} 编译器缓存与中间产物",
        "roaming":          "{sw} 编译器用户配置",
        "program_files":    "{sw} 编译器主程序（64 位）",
        "program_files_x86":"{sw} 编译器主程序（32 位）",
        "programdata":      "{sw} 编译器全局数据",
        "programs":         "{sw} 编译器安装目录",
    },
    "SDK": {
        "local":            "{sw} SDK 缓存与示例数据",
        "roaming":          "{sw} SDK 用户配置",
        "program_files":    "{sw} SDK 主程序（64 位）",
        "program_files_x86":"{sw} SDK 主程序（32 位）",
        "programdata":      "{sw} SDK 全局数据",
        "programs":         "{sw} SDK 安装目录",
    },
    "自动化脚本": {
        "local":            "{sw} 自动化脚本缓存与日志",
        "roaming":          "{sw} 自动化脚本用户配置",
        "program_files":    "{sw} 自动化脚本主程序（64 位）",
        "program_files_x86":"{sw} 自动化脚本主程序（32 位）",
        "programdata":      "{sw} 自动化脚本全局数据",
        "programs":         "{sw} 自动化脚本安装目录",
    },
    "启动项管理": {
        "local":            "{sw} 启动项管理缓存数据",
        "roaming":          "{sw} 启动项管理用户配置",
        "program_files":    "{sw} 启动项管理主程序（64 位）",
        "program_files_x86":"{sw} 启动项管理主程序（32 位）",
        "programdata":      "{sw} 启动项管理全局配置",
        "programs":         "{sw} 启动项管理安装目录",
    },
    "主题美化": {
        "local":            "{sw} 主题美化缓存与资源",
        "roaming":          "{sw} 主题美化用户配置",
        "program_files":    "{sw} 主题美化主程序（64 位）",
        "program_files_x86":"{sw} 主题美化主程序（32 位）",
        "programdata":      "{sw} 主题美化全局资源",
        "programs":         "{sw} 主题美化安装目录",
    },
    "FTP/SFTP客户端": {
        "local":            "{sw} FTP/SFTP 客户端缓存与临时会话",
        "roaming":          "{sw} FTP/SFTP 客户端站点配置，不要删",
        "program_files":    "{sw} FTP/SFTP 客户端主程序（64 位）",
        "program_files_x86":"{sw} FTP/SFTP 客户端主程序（32 位）",
        "programdata":      "{sw} FTP/SFTP 客户端全局数据",
        "programs":         "{sw} FTP/SFTP 客户端安装目录",
    },
    # —— 以下为 2026-07-20 补齐 28 种缺失模板 ——
    "3D建模": {
        "local":            "{sw} 3D 建模缓存与材质库",
        "roaming":          "{sw} 3D 建模用户配置与工程模板",
        "program_files":    "{sw} 3D 建模主程序（64 位）",
        "program_files_x86":"{sw} 3D 建模主程序（32 位）",
        "programdata":      "{sw} 3D 建模全局资源",
        "programs":         "{sw} 3D 建模安装目录",
    },
    "API工具": {
        "local":            "{sw} API 工具缓存与请求历史",
        "roaming":          "{sw} API 工具用户配置与环境变量",
        "program_files":    "{sw} API 工具主程序（64 位）",
        "program_files_x86":"{sw} API 工具主程序（32 位）",
        "programdata":      "{sw} API 工具全局配置",
        "programs":         "{sw} API 工具安装目录",
    },
    "Markdown编辑器": {
        "local":            "{sw} Markdown 编辑器缓存与扩展",
        "roaming":          "{sw} Markdown 编辑器配置文件",
        "program_files":    "{sw} Markdown 编辑器主程序（64 位）",
        "program_files_x86":"{sw} Markdown 编辑器主程序（32 位）",
        "programdata":      "{sw} Markdown 编辑器全局配置",
        "programs":         "{sw} Markdown 编辑器安装目录",
    },
    "Mod管理": {
        "local":            "{sw} Mod 管理器缓存与下载的 Mod",
        "roaming":          "{sw} Mod 管理器配置文件",
        "program_files":    "{sw} Mod 管理器主程序（64 位）",
        "program_files_x86":"{sw} Mod 管理器主程序（32 位）",
        "programdata":      "{sw} Mod 管理器全局数据",
        "programs":         "{sw} Mod 管理器安装目录",
    },
    "OCR": {
        "local":            "{sw} OCR 识别缓存与临时图片",
        "roaming":          "{sw} OCR 用户配置与语言包",
        "program_files":    "{sw} OCR 主程序（64 位）",
        "program_files_x86":"{sw} OCR 主程序（32 位）",
        "programdata":      "{sw} OCR 全局配置",
        "programs":         "{sw} OCR 安装目录",
    },
    "PDF工具": {
        "local":            "{sw} PDF 工具缓存与最近文档",
        "roaming":          "{sw} PDF 工具用户配置与签名证书",
        "program_files":    "{sw} PDF 工具主程序（64 位）",
        "program_files_x86":"{sw} PDF 工具主程序（32 位）",
        "programdata":      "{sw} PDF 工具全局配置",
        "programs":         "{sw} PDF 工具安装目录",
    },
    "剪贴板": {
        "local":            "{sw} 剪贴板历史记录，可安全清理",
        "roaming":          "{sw} 剪贴板用户配置",
        "program_files":    "{sw} 剪贴板主程序（64 位）",
        "program_files_x86":"{sw} 剪贴板主程序（32 位）",
        "programdata":      "{sw} 剪贴板全局数据",
        "programs":         "{sw} 剪贴板安装目录",
    },
    "动画特效": {
        "local":            "{sw} 动画特效缓存与素材库",
        "roaming":          "{sw} 动画特效用户配置与工程文件",
        "program_files":    "{sw} 动画特效主程序（64 位）",
        "program_files_x86":"{sw} 动画特效主程序（32 位）",
        "programdata":      "{sw} 动画特效全局资源",
        "programs":         "{sw} 动画特效安装目录",
    },
    "卸载工具": {
        "local":            "{sw} 卸载工具缓存与扫描日志",
        "roaming":          "{sw} 卸载工具用户配置",
        "program_files":    "{sw} 卸载工具主程序（64 位）",
        "program_files_x86":"{sw} 卸载工具主程序（32 位）",
        "programdata":      "{sw} 卸载工具全局配置",
        "programs":         "{sw} 卸载工具安装目录",
    },
    "启动器": {
        "local":            "{sw} 启动器缓存与索引数据",
        "roaming":          "{sw} 启动器用户配置与快捷方式",
        "program_files":    "{sw} 启动器主程序（64 位）",
        "program_files_x86":"{sw} 启动器主程序（32 位）",
        "programdata":      "{sw} 启动器全局配置",
        "programs":         "{sw} 启动器安装目录",
    },
    "地图导航": {
        "local":            "{sw} 地图导航缓存与离线地图",
        "roaming":          "{sw} 地图导航用户配置与收藏地点",
        "program_files":    "{sw} 地图导航主程序（64 位）",
        "program_files_x86":"{sw} 地图导航主程序（32 位）",
        "programdata":      "{sw} 地图导航全局数据",
        "programs":         "{sw} 地图导航安装目录",
    },
    "备份恢复": {
        "local":            "{sw} 备份恢复缓存与临时备份",
        "roaming":          "{sw} 备份恢复用户配置与计划任务",
        "program_files":    "{sw} 备份恢复主程序（64 位）",
        "program_files_x86":"{sw} 备份恢复主程序（32 位）",
        "programdata":      "{sw} 备份恢复全局配置",
        "programs":         "{sw} 备份恢复安装目录",
    },
    "天气": {
        "local":            "{sw} 天气应用缓存与历史数据",
        "roaming":          "{sw} 天气应用用户配置与城市列表",
        "program_files":    "{sw} 天气应用主程序（64 位）",
        "program_files_x86":"{sw} 天气应用主程序（32 位）",
        "programdata":      "{sw} 天气应用全局数据",
        "programs":         "{sw} 天气应用安装目录",
    },
    "字体管理": {
        "local":            "{sw} 字体管理缓存与字体预览",
        "roaming":          "{sw} 字体管理用户配置与字体集",
        "program_files":    "{sw} 字体管理主程序（64 位）",
        "program_files_x86":"{sw} 字体管理主程序（32 位）",
        "programdata":      "{sw} 字体管理全局配置",
        "programs":         "{sw} 字体管理安装目录",
    },
    "录屏直播": {
        "local":            "{sw} 录屏直播缓存与录制片段",
        "roaming":          "{sw} 录屏直播用户配置与场景",
        "program_files":    "{sw} 录屏直播主程序（64 位）",
        "program_files_x86":"{sw} 录屏直播主程序（32 位）",
        "programdata":      "{sw} 录屏直播全局配置",
        "programs":         "{sw} 录屏直播安装目录",
    },
    "思维导图": {
        "local":            "{sw} 思维导图缓存与临时数据",
        "roaming":          "{sw} 思维导图用户配置与模板",
        "program_files":    "{sw} 思维导图主程序（64 位）",
        "program_files_x86":"{sw} 思维导图主程序（32 位）",
        "programdata":      "{sw} 思维导图全局配置",
        "programs":         "{sw} 思维导图安装目录",
    },
    "截图工具": {
        "local":            "{sw} 截图工具缓存与截图历史",
        "roaming":          "{sw} 截图工具用户配置与快捷键",
        "program_files":    "{sw} 截图工具主程序（64 位）",
        "program_files_x86":"{sw} 截图工具主程序（32 位）",
        "programdata":      "{sw} 截图工具全局配置",
        "programs":         "{sw} 截图工具安装目录",
    },
    "数据恢复": {
        "local":            "{sw} 数据恢复扫描缓存与临时数据",
        "roaming":          "{sw} 数据恢复用户配置",
        "program_files":    "{sw} 数据恢复主程序（64 位）",
        "program_files_x86":"{sw} 数据恢复主程序（32 位）",
        "programdata":      "{sw} 数据恢复全局配置",
        "programs":         "{sw} 数据恢复安装目录",
    },
    "新闻资讯": {
        "local":            "{sw} 新闻资讯缓存与离线文章",
        "roaming":          "{sw} 新闻资讯用户配置与订阅源",
        "program_files":    "{sw} 新闻资讯主程序（64 位）",
        "program_files_x86":"{sw} 新闻资讯主程序（32 位）",
        "programdata":      "{sw} 新闻资讯全局数据",
        "programs":         "{sw} 新闻资讯安装目录",
    },
    "时钟日历": {
        "local":            "{sw} 时钟日历缓存数据",
        "roaming":          "{sw} 时钟日历用户配置与日程",
        "program_files":    "{sw} 时钟日历主程序（64 位）",
        "program_files_x86":"{sw} 时钟日历主程序（32 位）",
        "programdata":      "{sw} 时钟日历全局数据",
        "programs":         "{sw} 时钟日历安装目录",
    },
    "游戏模拟器": {
        "local":            "{sw} 游戏模拟器缓存与存档数据",
        "roaming":          "{sw} 游戏模拟器用户配置与按键映射",
        "program_files":    "{sw} 游戏模拟器主程序（64 位）",
        "program_files_x86":"{sw} 游戏模拟器主程序（32 位）",
        "programdata":      "{sw} 游戏模拟器全局配置",
        "programs":         "{sw} 游戏模拟器安装目录",
    },
    "电子书": {
        "local":            "{sw} 电子书缓存与下载图书",
        "roaming":          "{sw} 电子书用户配置与书架",
        "program_files":    "{sw} 电子书主程序（64 位）",
        "program_files_x86":"{sw} 电子书主程序（32 位）",
        "programdata":      "{sw} 电子书全局配置",
        "programs":         "{sw} 电子书安装目录",
    },
    "系统优化": {
        "local":            "{sw} 系统优化缓存与扫描日志",
        "roaming":          "{sw} 系统优化用户配置",
        "program_files":    "{sw} 系统优化主程序（64 位）",
        "program_files_x86":"{sw} 系统优化主程序（32 位）",
        "programdata":      "{sw} 系统优化全局配置",
        "programs":         "{sw} 系统优化安装目录",
    },
    "翻译工具": {
        "local":            "{sw} 翻译工具缓存与历史记录",
        "roaming":          "{sw} 翻译工具用户配置与词库",
        "program_files":    "{sw} 翻译工具主程序（64 位）",
        "program_files_x86":"{sw} 翻译工具主程序（32 位）",
        "programdata":      "{sw} 翻译工具全局配置",
        "programs":         "{sw} 翻译工具安装目录",
    },
    "股票财务": {
        "local":            "{sw} 股票财务缓存与行情数据",
        "roaming":          "{sw} 股票财务用户配置与自选股",
        "program_files":    "{sw} 股票财务主程序（64 位）",
        "program_files_x86":"{sw} 股票财务主程序（32 位）",
        "programdata":      "{sw} 股票财务全局数据",
        "programs":         "{sw} 股票财务安装目录",
    },
    "设计原型": {
        "local":            "{sw} 设计原型缓存与素材",
        "roaming":          "{sw} 设计原型用户配置与工程文件",
        "program_files":    "{sw} 设计原型主程序（64 位）",
        "program_files_x86":"{sw} 设计原型主程序（32 位）",
        "programdata":      "{sw} 设计原型全局资源",
        "programs":         "{sw} 设计原型安装目录",
    },
    "邮件客户端": {
        "local":            "{sw} 邮件客户端缓存与附件",
        "roaming":          "{sw} 邮件客户端账号配置与邮件，不要删",
        "program_files":    "{sw} 邮件客户端主程序（64 位）",
        "program_files_x86":"{sw} 邮件客户端主程序（32 位）",
        "programdata":      "{sw} 邮件客户端全局数据",
        "programs":         "{sw} 邮件客户端安装目录",
    },
    "项目管理": {
        "local":            "{sw} 项目管理缓存与任务数据",
        "roaming":          "{sw} 项目管理用户配置与项目文件",
        "program_files":    "{sw} 项目管理主程序（64 位）",
        "program_files_x86":"{sw} 项目管理主程序（32 位）",
        "programdata":      "{sw} 项目管理全局配置",
        "programs":         "{sw} 项目管理安装目录",
    },
    # —— 补齐最后 4 种缺失模板 ——
    "会议软件": {
        "local":            "{sw} 会议软件缓存与录制片段",
        "roaming":          "{sw} 会议软件账号配置与会议记录",
        "program_files":    "{sw} 会议软件主程序（64 位）",
        "program_files_x86":"{sw} 会议软件主程序（32 位）",
        "programdata":      "{sw} 会议软件全局数据",
        "programs":         "{sw} 会议软件安装目录",
    },
    "健康医疗": {
        "local":            "{sw} 健康医疗缓存与历史记录",
        "roaming":          "{sw} 健康医疗用户配置与健康数据",
        "program_files":    "{sw} 健康医疗主程序（64 位）",
        "program_files_x86":"{sw} 健康医疗主程序（32 位）",
        "programdata":      "{sw} 健康医疗全局数据",
        "programs":         "{sw} 健康医疗安装目录",
    },
    "协作平台": {
        "local":            "{sw} 协作平台缓存与同步数据",
        "roaming":          "{sw} 协作平台账号配置与工作空间",
        "program_files":    "{sw} 协作平台主程序（64 位）",
        "program_files_x86":"{sw} 协作平台主程序（32 位）",
        "programdata":      "{sw} 协作平台全局配置",
        "programs":         "{sw} 协作平台安装目录",
    },
    "广告拦截": {
        "local":            "{sw} 广告拦截缓存与过滤规则",
        "roaming":          "{sw} 广告拦截用户配置与白名单",
        "program_files":    "{sw} 广告拦截主程序（64 位）",
        "program_files_x86":"{sw} 广告拦截主程序（32 位）",
        "programdata":      "{sw} 广告拦截全局规则",
        "programs":         "{sw} 广告拦截安装目录",
    },
}


def _detect_position(dir_path):
    """识别目录在 6 个监控位置中的哪一个，返回 position key，失败返回空字符串

    位置 key（与 _TYPE_POSITION_MATRIX 的列键对应）:
      local              AppData\\Local\\<dir>          （非 Programs 子目录）
      roaming            AppData\\Roaming\\<dir>
      program_files      C:\\Program Files\\<dir>
      program_files_x86  C:\\Program Files (x86)\\<dir>
      programdata        C:\\ProgramData\\<dir>
      programs           AppData\\Local\\Programs\\<dir>
    """
    try:
        path_lower = dir_path.lower().replace("/", "\\")
        localappdata = os.environ.get("LOCALAPPDATA", "").lower().replace("/", "\\")
        appdata = os.environ.get("APPDATA", "").lower().replace("/", "\\")
        # programs 必须先判断（是 local 的子目录）
        if localappdata and path_lower.startswith(localappdata + "\\programs\\"):
            return "programs"
        if localappdata and path_lower.startswith(localappdata + "\\"):
            return "local"
        if appdata and path_lower.startswith(appdata + "\\"):
            return "roaming"
        if path_lower.startswith("c:\\program files (x86)\\"):
            return "program_files_x86"
        if path_lower.startswith("c:\\program files\\"):
            return "program_files"
        if path_lower.startswith("c:\\programdata\\"):
            return "programdata"
        return ""
    except Exception:
        return ""


def _generate_type_position_desc(type_val, dir_path, sw_name):
    """按 type × position 矩阵生成差异化描述

    :param type_val: 软件类型（如"浏览器"/"通讯软件"，78类之一）
    :param dir_path: 完整路径，用于识别 position
    :param sw_name: 软件名，用于填充模板的 {sw} 占位符
    :return: 矩阵命中的描述字符串，未命中返回空

    两级命中策略：
      1. type 在 _TYPE_POSITION_MATRIX 中（top 30 type，覆盖71%软件）→ 用具体模板
         如 type="通讯软件" + local → "{sw} 通讯软件缓存与聊天记录"
      2. type 不在矩阵中（剩余 48 type）→ 用通用模板 {sw} {type}{位置语义}
         如 type="剪贴板" + local → "Clippy 剪贴板本地数据"
      3. 都未命中 → 返回空，调用方走原 _identify_by_location 兜底
    """
    try:
        if not type_val or not sw_name:
            return ""
        position = _detect_position(dir_path)
        if not position:
            return ""
        type_matrix = _TYPE_POSITION_MATRIX.get(type_val)
        if type_matrix:
            template = type_matrix.get(position)
            if template:
                # i18n：模板翻译后再填 {sw}（语言包 key 含 {sw} 占位符）
                return _tr_text(template).replace("{sw}", sw_name)
            return ""
        # 通用模板：type 不在 top 30 矩阵中时使用
        # 输出格式 "{sw} {type}{位置语义}"，例如 "Clippy 剪贴板本地数据"
        _GENERIC_TYPE_TEMPLATES = {
            "local":            "{sw} " + type_val + "本地数据",
            "roaming":          "{sw} " + type_val + "用户配置",
            "program_files":    "{sw} " + type_val + "主程序（64 位）",
            "program_files_x86":"{sw} " + type_val + "主程序（32 位）",
            "programdata":      "{sw} " + type_val + "全局数据",
            "programs":         "{sw} " + type_val + "安装目录",
        }
        template = _GENERIC_TYPE_TEMPLATES.get(position)
        if not template:
            return ""
        # i18n：通用模板由"type 词 + 位置后缀"拼接，安全片段替换覆盖
        return _tr_text(template).replace("{sw}", sw_name)
    except Exception:
        return ""


def _identify_by_location(dir_path, dir_name, software_desc=""):
    """基于路径位置识别文件夹功能
    同一软件在不同位置功能不同，结合位置给出更精准说明

    :param dir_path: 完整路径（如 C:\\Users\\xxx\\AppData\\Local\\<软件名>）
    :param dir_name: 目录名（如 <软件名>）
    :param software_desc: 已识别的软件名，可选
    :return: 位置感知的说明字符串，失败返回空

    示例：
      AppData\\Local\\<软件名> + "<软件名>" → "<软件名> 本地数据（缓存/书签/配置）"
      Program Files\\<软件名> + "<软件名>"  → "<软件名> 主程序（64 位）"
    """
    try:
        path_lower = dir_path.lower().replace("/", "\\")
        # 关键修复：没有真实软件名时不拿目录名顶替（否则会产生"<目录名> 本地数据"这种垃圾）
        # 只有特殊系统目录(temp/packages/microsoft/common files等)允许无软件名返回
        sw_name = software_desc  # 不再 else dir_name
        have_sw = bool(sw_name)

        # 判断所在位置
        localappdata = os.environ.get("LOCALAPPDATA", "").lower().replace("/", "\\")
        appdata = os.environ.get("APPDATA", "").lower().replace("/", "\\")

        # AppData\Local\... （Local 下的一级子目录）
        if localappdata and path_lower.startswith(localappdata + "\\"):
            # 特殊文件夹（无需软件名，直接返回）
            if dir_name == "temp":
                return "系统临时缓存文件（可清理）"
            if dir_name == "packages":
                return "应用商店应用数据"
            if dir_name == "microsoft":
                return "Microsoft 系统组件缓存"
            # 判断是否是 Programs 子目录
            if path_lower.startswith(localappdata + "\\programs\\"):
                return f"{sw_name} 安装目录" if have_sw else ""
            # 普通软件本地数据：没有真实软件名就返回空，让上层走联网搜索，不伪造
            return f"{sw_name} 本地缓存与配置" if have_sw else ""

        # AppData\Roaming\...
        if appdata and path_lower.startswith(appdata + "\\"):
            if dir_name == "microsoft":
                return "Microsoft 用户配置"
            return f"{sw_name} 用户配置与账号数据" if have_sw else ""

        # Program Files (x86)\...
        if path_lower.startswith("c:\\program files (x86)\\"):
            if dir_name == "microsoft":
                return "Microsoft 32 位系统组件"
            if dir_name == "common files":
                return "32 位公共组件（共享库/运行时）"
            return f"{sw_name} 主程序（32 位）" if have_sw else ""

        # Program Files\...
        if path_lower.startswith("c:\\program files\\"):
            if dir_name == "microsoft":
                return "Microsoft 64 位系统组件"
            if dir_name == "common files":
                return "64 位公共组件（共享库/运行时）"
            return f"{sw_name} 主程序（64 位）" if have_sw else ""

        # ProgramData\...
        if path_lower.startswith("c:\\programdata\\"):
            if dir_name == "microsoft":
                return "Microsoft 系统级数据"
            if dir_name == "package cache":
                return "软件安装包缓存"
            if dir_name == "windowsapps":
                return "应用商店应用"
            return f"{sw_name} 系统级共享数据" if have_sw else ""

        return ""
    except Exception:
        return ""


# 位置后缀关键词（用于判断desc是否已包含位置信息，避免重复叠加）
_LOCATION_SUFFIX_KEYWORDS = [
    "本地数据", "本地缓存", "主程序", "公共数据", "系统级共享数据",
    "安装目录", "用户配置", "用户配置与账号",
    "系统组件", "公共组件", "缓存", "配置", "数据", "主程序",
    "32 位", "64 位", "32位", "64位", "漫游", "系统级", "安装包",
]

# 具体功能描述关键词：含这些词的 desc 不再叠加通用位置后缀
# 例："123云盘 自动更新组件" 不应再叠加"本地缓存与配置"
_SPECIFIC_FUNCTION_KEYWORDS = [
    "自动更新组件", "自动更新程序", "更新程序", "更新数据", "缓存数据",
    "崩溃报告", "程序崩溃报告", "临时文件", "着色器缓存",
    "运行日志", "应用运行日志", "配置文件", "应用配置文件",
]


def _enhance_with_location(desc, dir_path, dir_name, type_val="", software_name=""):
    """位置感知后处理：对所有识别结果叠加位置信息（通用，不针对特定软件）
    同一软件在不同位置功能不同，统一增强所有识别结果

    :param desc: 已识别的软件说明
    :param dir_path: 完整路径
    :param dir_name: 目录名
    :param type_val: 软件类型（78类之一，可选）。有 type 时按 type × position 矩阵生成差异化描述
    :param software_name: 真实软件名（可选）。矩阵模板使用此名称填充 {sw}，未提供则用 desc
    :return: 叠加位置信息后的说明

    优先级：
      1. 有 type_val 且矩阵命中 → 返回矩阵描述（如"Discord 通讯软件缓存与聊天记录"）
      2. 矩阵未命中 → 走原位置模板（如"Discord 本地数据（缓存/配置）"）

    示例（对所有软件统一生效）：
      某软件 + AppData\\Local\\<dir>       → "<软件名> 本地数据（缓存/配置）"
      某软件 + Program Files\\<dir>        → "<软件名> 主程序（64 位）"
      某软件 + Program Files (x86)\\<dir>  → "<软件名> 主程序（32 位）"
      某软件 + AppData\\Roaming\\<dir>     → "<软件名> 用户配置（漫游数据）"
      某软件 + ProgramData\\<dir>          → "<软件名> 公共数据（系统级）"

    有 type_val 时（如 type="通讯软件"，software_name="Discord"）：
      Discord + AppData\\Local             → "Discord 通讯软件缓存与聊天记录"
      Discord + AppData\\Roaming           → "Discord 通讯软件账号配置与好友列表，不要删"
      Discord + Program Files              → "Discord 通讯软件主程序（64 位）"
    """
    try:
        if not desc:
            return desc

        # "无法识别"开头的说明不加位置后缀（它不是软件名，是失败原因）
        if desc.startswith("无法识别"):
            return desc

        # 如果desc已包含位置关键词，不重复叠加
        desc_check = desc.lower()
        for kw in _LOCATION_SUFFIX_KEYWORDS:
            if kw in desc_check:
                return desc

        # 含具体功能描述关键词（如"自动更新程序"）的不再叠加位置后缀
        # 避免产生"X 自动更新程序 本地数据（缓存/配置）"这种冗余拼接
        for kw in _SPECIFIC_FUNCTION_KEYWORDS:
            if kw in desc:
                return desc

        # 优先：type × position 矩阵（第5步子任务E）
        # 矩阵覆盖 top 30 type，覆盖约71%软件条目；命中后返回差异化描述
        # 矩阵未命中（type 不在 top 30）时使用通用模板兜底，几乎总能命中
        if type_val:
            matrix_desc = _generate_type_position_desc(
                type_val, dir_path, software_name or desc
            )
            if matrix_desc:
                return matrix_desc

        # 兜底：按原位置模板（千篇一律的"X 本地数据（缓存/配置）"）
        # 如果有 software_name，用 software_name 走原位置模板
        # 避免 desc（功能描述）+ 位置后缀的冗余拼接（如"Clippy 剪贴板管理器 本地数据"）
        location_input = software_name if software_name else desc
        location_suffix = _identify_by_location(dir_path, dir_name, location_input)
        if location_suffix and location_suffix != desc:
            # location_suffix 已包含软件名+位置后缀，直接返回（i18n 翻译）
            return _tr_text(location_suffix)

        return desc
    except Exception:
        return desc


def _smart_fallback_desc(dir_path, dir_name, software_desc=""):
    """智能兜底：只在能从目录名特征明确判断功能时返回，否则返回空让上层走联网搜索
    核心原则：禁止用目录名伪造软件名（不产生"camoufox 相关目录"这种垃圾）

    :param dir_path: 完整路径
    :param dir_name: 目录名
    :param software_desc: 已识别的真实软件名（可选，有则拼接，无则只返回纯功能描述）
    :return: 兜底说明字符串，识别不出返回空
    """
    try:
        # 优先用位置识别（需有真实软件名，否则_identify_by_location已返回空）
        location_desc = _identify_by_location(dir_path, dir_name, software_desc)
        if location_desc:
            return location_desc

        dl = dir_name.lower()
        have_sw = bool(software_desc)
        sw = software_desc  # 真实软件名，没有就是空

        # 通用词兜底：基于目录名特征判断功能
        # 有真实软件名则拼接，没有则只返回纯功能描述（不伪造软件名）
        # 改造：去掉机翻"自动更新程序"，改为更自然的"自动更新组件"
        if dl.endswith("-updater") or dl.endswith("_updater") or dl == "updater":
            if have_sw:
                return f"{sw} 自动更新组件"
            # 没有真实软件名，从目录名提取前缀（如 xxx-updater → xxx）
            prefix = dl.replace("-updater", "").replace("_updater", "")
            return f"{prefix} 自动更新组件" if prefix else "软件自动更新组件"
        # 系统组件黑名单（明确的 Windows 系统目录，给固定说明避免联网搜索）
        # 注：这些目录名是 Windows 系统固定使用的，不是软件名
        # 改造：去技术术语（PeerDist/UWP/容器等），用用户能看懂的语言描述
        _SYSTEM_COMPONENTS = {
            # Windows 内容分发缓存（PeerDist 是技术名，用户不懂）
            "peerdistrepub": "Windows 内容分发缓存",
            "publishers": "Windows 内容分发数据",
            # DirectX/图形缓存
            "d3dscache": "DirectX 着色器缓存",
            "d3dcache": "DirectX 缓存",
            # Windows 通知/操作中心
            "connecteddevicesplatform": "Windows 连接设备平台数据",
            "notifications": "Windows 通知数据",
            # 应用商店应用数据（UWP/容器 是技术名，用户不懂）
            "packages": "应用商店应用数据",
            # 系统建议应用
            "suggestapp": "Windows 建议应用数据",
        }
        # 精确匹配系统组件黑名单
        if dl in _SYSTEM_COMPONENTS:
            return _SYSTEM_COMPONENTS[dl]
        # 包管理器状态目录
        if dl == "pnpm-state":
            return "pnpm 包管理器状态"
        if dl == "npm-cache" or dl == ".npm":
            return "npm 包管理器缓存"
        if dl == "yarn" or dl == ".yarn":
            return "yarn 包管理器数据"
        # 崩溃报告相关（扩展版）
        if "crashpad" in dl or "crashdump" in dl or dl == "crashes" or dl == "crashreports":
            return "程序崩溃报告"
        if "temp" in dl:
            return "临时缓存文件"
        if dl == "logs" or dl == "log":
            return f"{sw} 运行日志" if have_sw else "应用运行日志"
        if "log" in dl:
            # 只有目录名确实以log开头或结尾时才认为是日志（避免"ollama"、"blog"被误判）
            if dl.startswith("log") or dl.endswith("log") or "log_" in dl or "_log" in dl:
                return f"{sw} 运行日志" if have_sw else "应用运行日志"
        if dl == "cache" or dl == "caches":
            return f"{sw} 缓存数据" if have_sw else "应用缓存数据"
        if "cache" in dl:
            return f"{sw} 缓存数据" if have_sw else "应用缓存数据"
        if "backup" in dl:
            return f"{sw} 备份文件" if have_sw else "备份文件"
        if "config" in dl or "setting" in dl:
            return f"{sw} 配置文件" if have_sw else "应用配置文件"

        # 反向域名包名（基于实际包名结构，不算伪造）
        # 改造：去掉"（包名: xxx）"技术信息，直接用 app_name 走位置模板
        if (dl.startswith("com.") or dl.startswith("org.") or dl.startswith("io.")
                or dl.startswith("cn.") or dl.startswith("dev.")):
            parts = dl.split(".")
            if len(parts) >= 3:
                app_name = parts[-1]
                # 用 app_name 走位置模板（_identify_by_location 拼接"app_name 本地数据"等）
                if app_name and len(app_name) >= 2:
                    return _identify_by_location(dir_path, dir_name, app_name) or ""

        # 没有真实软件名，不伪造"XX 相关目录"，返回空让上层走联网搜索
        return ""
    except Exception:
        return ""
