#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描目录列表（单一数据源）

待迁移区扫描目录从 6 个扩展为 7 个（新增当前用户目录 %USERPROFILE%），
所有使用点（migrator / monitor / fast_scan）统一从这里取列表，
避免此前多处各自硬编码列表导致"改一处漏一处"。

用户目录纳入后需要动态排除两类一级子目录（禁止硬编码目录名，其他电脑
的目录结构可能不同）：
1. 已在监控列表里的 base 目录（如 AppData\\Local、AppData\\Roaming）——
   排除集合直接从 get_scan_dirs() 自身计算得出，若其他电脑把用户目录下
   更多子目录加进监控，也会自动排除，无需改动；
2. Windows 系统特殊文件夹（桌面/文档/下载/图片等）——用 Known Folder
   API（SHGetKnownFolderPath）动态解析真实路径，中文系统（"桌面"）等
   任何语言/重定向情况都正确，不依赖目录名。
"""
import ctypes
import os
from ctypes import wintypes

# 用户目录在"位置"列的显示标签
USER_LABEL = "User"

# 需要从用户目录扫描中排除的系统特殊文件夹 FOLDERID（微软标准 GUID 常量）
# 解析失败（该文件夹在系统上不存在）的条目自动跳过，不影响其他条目
_KNOWN_FOLDER_IDS = [
    "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",  # 桌面
    "FDD39AD0-238F-46AF-ADB4-6C85480369C7",  # 文档
    "374DE290-123F-4565-9164-39C4925E467B",  # 下载
    "33E28130-4E1E-4676-835A-98395C3BC3BB",  # 图片
    "4BD8D571-6D19-48D3-BE97-422220080E43",  # 音乐
    "18989B1D-99B5-455B-841C-AB7C74E4DDFC",  # 视频
    "31C0DD25-9439-4F12-BF41-7FF4EDA38722",  # 3D 对象
    "1777F761-68AD-4D8A-87BD-30B759FA33DD",  # 收藏夹
    "BFB9D5E0-C6A9-404C-B2B2-AE6DB6AF4968",  # 链接
    "7D1D3A04-DEBB-4115-95CF-2F29DA2920DA",  # 搜索
    "4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4",  # 游戏存档
    "56784854-C6CB-462B-8169-88E350ACB882",  # 联系人
]

# Known Folder 解析结果缓存（同一进程内路径不会变）
_known_folder_cache = None


def norm_path(path):
    """规范化路径：剥 Win32 扩展路径前缀、小写、反斜杠转正斜杠、去尾部斜杠

    与 monitor._norm_path 同规约，另兼容 Win32 扩展路径前缀（\\\\?\\ 或 \\\\?\\UNC\\），
    避免 Known Folder API 返回带前缀的路径时与扫描路径匹配不上。
    """
    if not path:
        return ""
    p = path.replace("\\\\?\\UNC\\", "\\\\").replace("\\\\?\\", "")
    return p.replace("\\", "/").lower().rstrip("/")


def get_scan_dirs(include_user=True):
    """待迁移区扫描目录列表 [(路径, label), ...]

    :param include_user: 是否包含当前用户目录（实时拦截监控场景传 False，
        避免用户目录下的目录创建被当作安装行为处理）
    """
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    appdata = os.environ.get("APPDATA", "")
    dirs = [
        (local_appdata, "Local"),
        (os.path.join(local_appdata, "Programs"), "Programs"),
        (appdata, "Roaming"),
        (r"C:\Program Files", "Program Files"),
        (r"C:\Program Files (x86)", "Program Files (x86)"),
        (r"C:\ProgramData", "ProgramData"),
    ]
    if include_user:
        user_profile = os.environ.get("USERPROFILE", "")
        if user_profile:
            dirs.append((user_profile, USER_LABEL))
    return dirs


def get_monitored_base_norms(scan_dirs=None):
    """所有监控 base 路径的规范化集合

    用于用户目录扫描时动态排除"已经在监控列表里"的一级子目录
    （如 AppData\\Local、AppData\\Roaming），不硬编码目录名。
    """
    if scan_dirs is None:
        scan_dirs = get_scan_dirs()
    return {norm_path(p) for p, _ in scan_dirs if p}


class _GUID(ctypes.Structure):
    """Windows GUID 结构（供 SHGetKnownFolderPath 使用）"""
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid_from_string(guid_str):
    """把 "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX" 解析为 _GUID"""
    parts = guid_str.split("-")
    g = _GUID()
    g.Data1 = int(parts[0], 16)
    g.Data2 = int(parts[1], 16)
    g.Data3 = int(parts[2], 16)
    raw = bytes.fromhex(parts[3] + parts[4])
    for i in range(8):
        g.Data4[i] = raw[i]
    return g


def get_known_folder_paths():
    """通过 Windows Known Folder API 动态解析系统特殊文件夹真实路径（规范化集合）

    - 不硬编码目录名（中文系统的"桌面"等也能正确解析）
    - 解析失败（本机不存在该文件夹）的条目自动跳过
    - 已被用户重定向到其他盘的目录不在用户目录下，天然不会误排除
    - 结果带进程内缓存
    """
    global _known_folder_cache
    if _known_folder_cache is not None:
        return _known_folder_cache
    result = set()
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p)]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long  # HRESULT
        for guid_str in _KNOWN_FOLDER_IDS:
            try:
                g = _guid_from_string(guid_str)
                p = ctypes.c_wchar_p()
                hr = shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p))
                if hr == 0 and p.value:
                    result.add(norm_path(p.value))
                if p.value:
                    try:
                        ctypes.windll.ole32.CoTaskMemFree(p)
                    except Exception:
                        pass
            except Exception:
                continue
    except Exception:
        pass
    _known_folder_cache = result
    return result


def is_user_dir_excluded(normed_path, monitored_norms, known_norms):
    """用户目录一级子目录是否应排除（完全动态，无硬编码目录名）

    排除三类：
    1. 已在监控列表里的 base 目录本身（如 AppData\\Local、AppData\\Roaming）
    2. 已监控 base 的祖先目录（如 AppData 包含 AppData\\Local，扫描它会
       重复列出 Local 的全部内容）
    3. 系统特殊文件夹（Known Folder API 解析）
    """
    if normed_path in monitored_norms or normed_path in known_norms:
        return True
    # 祖先目录检查：一级子目录下若还有监控 base，扫描它会重复列出已监控内容
    for m in monitored_norms:
        if m.startswith(normed_path + "/"):
            return True
    return False
