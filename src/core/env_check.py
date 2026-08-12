# -*- coding: utf-8 -*-
"""环境自检：启动时/手动诊断系统环境能力，提前暴露环境相关隐患。

背景(2026-08-09)：RecycleBin(IFileOperation CLSID 未注册)、无缓冲 I/O
非对齐 truncate、符号链接权限等环境问题，此前只能等用户实际迁移时踩坑
(updater 事故:测试全在同盘/热缓存/对齐文件的"理想组合"上跑，
跨盘 IOCP + 非对齐尾部 + 冷启动的环境组合从未覆盖)。

本模块提供探测函数：
- 全部只读，或仅在 tempfile 临时目录中做低副作用探测并立即自清理
- 绝不触碰用户数据目录
- 启动自检只跑快速项(约几十毫秒)；VSS 还原点占用(慢)仅在手动「环境诊断」时跑

结果格式：(name, status, detail)
- status: "ok" 正常 / "warn" 可用但有降级 / "fail" 不可用(需处理)
"""

import ctypes
import os
import shutil
import tempfile
import winreg
from pathlib import Path

# IFileOperation 的 CLSID（purge 软删除首选路径；未注册则降级 SHFileOperationW/硬删）
CLSID_FILE_OPERATION = "{3AD05575-8857-4850-8278-1054B1BFCD31}"
_RECYCLE_CLSID_KEY = r"Software\Classes\CLSID\%s" % CLSID_FILE_OPERATION


def check_admin():
    """是否管理员权限（符号链接 /D、进程拦截等需要）。"""
    try:
        from config import is_admin
        if is_admin():
            return ("管理员权限", "ok", "已以管理员身份运行")
        return ("管理员权限", "warn", "非管理员：符号链接 /D 可能创建失败（有 Junction 兜底）")
    except Exception as e:
        return ("管理员权限", "fail", f"检测异常: {e}")


def check_engine_exe():
    """Rust 复制引擎 exe 是否存在（迁移/校验/MFT 索引的核心）。

    复用 migrate_engine._locate_engine（含 PyInstaller 打包场景处理：
    打包后 __file__ 指向解包临时目录，引擎 exe 在 _MEIPASS/exe 同目录的 bin/）。
    """
    try:
        from migrate_engine import MigrateEngine
        path = MigrateEngine._locate_engine()
        if path and os.path.isfile(path):
            size = os.path.getsize(path)
            if size > 100_000:
                return ("复制引擎", "ok", f"{os.path.basename(path)} ({size // 1024}KB)")
            return ("复制引擎", "warn", f"{os.path.basename(path)} 存在但大小异常({size}B)")
        return ("复制引擎", "fail", "未找到 rust-migrate-engine.exe（迁移将不可用）")
    except Exception as e:
        return ("复制引擎", "fail", f"检测异常: {e}")


def check_recycle_bin():
    """回收站软删除可用性（purge 删目标盘冗余文件时用）。

    判定链：
    1. IFileOperation 类已注册（HKCR\\CLSID）→ 完整软删除
    2. 否则 shell32 导出 SHFileOperationW → 兼容软删除（引擎 recycle_via_shfileop）
    3. 都不可用 → purge 降级硬删除（误删不可还原）
    """
    try:
        # 1. IFileOperation CLSID 注册检查（无副作用）
        clsid_ok = False
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _RECYCLE_CLSID_KEY):
                clsid_ok = True
        except OSError:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RECYCLE_CLSID_KEY):
                    clsid_ok = True
            except OSError:
                clsid_ok = False
        # 2. shell32 导出 SHFileOperationW（无副作用）
        shfileop_ok = False
        try:
            shfileop_ok = hasattr(ctypes.windll.shell32, "SHFileOperationW")
        except Exception:
            shfileop_ok = False
        if clsid_ok:
            return ("回收站软删除", "ok", "IFileOperation 可用（完整软删除）")
        if shfileop_ok:
            return ("回收站软删除", "warn",
                    "IFileOperation 类未注册（系统精简/注册表被清理），"
                    "已用 SHFileOperationW 兼容路径")
        return ("回收站软删除", "fail",
                "IFileOperation 未注册且 SHFileOperationW 不可用，purge 将降级硬删除")
    except Exception as e:
        return ("回收站软删除", "fail", f"检测异常: {e}")


def check_symlink_permission():
    """符号链接创建权限（临时目录低副作用探测，立即自清理）。

    程序有 Junction 兜底（无需权限），失败仅影响符号链接首选路径。
    """
    tmp_root = None
    link_path = None
    try:
        tmp_root = tempfile.mkdtemp(prefix="cdrive_diag_")
        real_dir = os.path.join(tmp_root, "real")
        os.makedirs(real_dir)
        link_path = os.path.join(tmp_root, "link")
        os.symlink(real_dir, link_path, target_is_directory=True)
        # 验证链接可用
        if os.path.islink(link_path):
            return ("符号链接权限", "ok", "可创建目录符号链接（管理员/开发者模式）")
        return ("符号链接权限", "warn", "链接创建后验证失败")
    except (OSError, NotImplementedError) as e:
        return ("符号链接权限", "warn",
                f"创建失败（{getattr(e, 'winerror', None) or e}）——程序有 Junction 兜底，可正常迁移")
    except Exception as e:
        return ("符号链接权限", "warn", f"检测异常: {e}")
    finally:
        # 自清理：链接 → 临时目录（仅删本次创建的临时路径）
        try:
            if link_path and os.path.lexists(link_path):
                if os.path.isdir(link_path) and not os.path.islink(link_path):
                    shutil.rmtree(link_path)  # 理论不会到这（链接才创建）
                else:
                    os.rmdir(link_path)
        except Exception:
            pass
        try:
            if tmp_root and os.path.isdir(tmp_root):
                shutil.rmtree(tmp_root)
        except Exception:
            pass


def check_target_drive(g_root):
    """目标盘（g_root）：存在/可写/扇区大小（GetDiskFreeSpaceW 逻辑扇区）。"""
    try:
        if not g_root:
            return ("目标盘", "warn", "未配置 g_root")
        drive = str(g_root)
        if not os.path.exists(drive):
            return ("目标盘", "fail", f"{drive} 不存在（盘符未连接？）")
        # 可写性：临时目录写删自清理
        tmp = None
        try:
            tmp = tempfile.mkdtemp(prefix="cdrive_diag_", dir=drive)
            probe = os.path.join(tmp, "probe.tmp")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
        finally:
            if tmp and os.path.isdir(tmp):
                shutil.rmtree(tmp)
        # 扇区大小（GetDiskFreeSpaceW）
        sector = 0
        try:
            GetDiskFreeSpaceW = ctypes.windll.kernel32.GetDiskFreeSpaceW
            GetDiskFreeSpaceW.argtypes = [
                ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
            ]
            spc = ctypes.c_uint32(); bps = ctypes.c_uint32()
            fc = ctypes.c_uint32(); tc = ctypes.c_uint32()
            ok = GetDiskFreeSpaceW(drive, ctypes.byref(spc), ctypes.byref(bps),
                                   ctypes.byref(fc), ctypes.byref(tc))
            if ok:
                sector = bps.value
        except Exception:
            pass
        detail = f"{drive} 可写"
        if sector:
            detail += f"，逻辑扇区 {sector}B"
        # 文件系统类型：非 NTFS 目标盘有静默风险
        # - FAT32：单文件最大 4GB，大文件迁移中途失败
        # - exFAT：无 ACL / 硬链接 / 稀疏文件支持，引擎保留的属性会丢失
        fs_name = ""
        try:
            GetVolumeInformationW = ctypes.windll.kernel32.GetVolumeInformationW
            _vol = ctypes.create_unicode_buffer(64)
            _fs = ctypes.create_unicode_buffer(32)
            # pathlib 取盘符根（含尾反斜杠，Windows API 要求）
            _root = str(Path(drive).anchor)
            _ok = GetVolumeInformationW(_root, _vol, 64, None, None, None, _fs, 32)
            if _ok:
                fs_name = _fs.value
        except Exception:
            pass
        if fs_name and fs_name.upper() != "NTFS":
            _fs_upper = fs_name.upper()
            if _fs_upper == "FAT32":
                _fs_warn = "FAT32 单文件最大 4GB，大文件迁移会失败"
            else:
                _fs_warn = "无 NTFS 的 ACL/硬链接/稀疏文件支持，部分属性会丢失"
            return ("目标盘", "warn",
                    f"{drive} 是 {fs_name} 文件系统（{_fs_warn}），"
                    f"建议使用 NTFS 格式的磁盘")
        return ("目标盘", "ok", detail)
    except Exception as e:
        return ("目标盘", "fail", f"检测异常: {e}")


def run_fast_checks(g_root=None):
    """启动快速自检（几十毫秒级，不含 VSS 慢查询）。

    :return: [(name, status, detail), ...]
    """
    results = [
        check_admin(),
        check_engine_exe(),
        check_recycle_bin(),
        check_symlink_permission(),
    ]
    if g_root:
        results.append(check_target_drive(g_root))
    return results


def run_full_checks(g_root=None):
    """完整诊断（含 VSS 还原点占用，PowerShell 慢查询，仅手动诊断时调用）。"""
    results = run_fast_checks(g_root)
    try:
        from migrator import query_vss_usage
        count, used_mb = query_vss_usage()
        if count > 0:
            results.append(("还原点占用", "warn", f"{count} 个，约 {used_mb}MB（可在设置开启自动清理）"))
        else:
            results.append(("还原点占用", "ok", "无卷影副本"))
    except Exception as e:
        results.append(("还原点占用", "warn", f"查询失败: {e}"))
    return results
