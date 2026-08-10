# -*- coding: utf-8 -*-
"""开发环境快照模块（仿 GitHub commit 功能）

功能：
- 保存当前所有开发工具的配置快照（环境变量值 + 配置记录 + 状态缓存）
- 支持备注（commit message）
- 最多保留 500 个快照，超出自动删除最旧的（但首个原始快照永不被删）
- 支持从快照恢复：把环境变量还原到快照时的状态

存储位置：BASE_DIR/dev_env_snapshots/YYYYMMDD_HHMMSS.json
每个快照文件独立，方便单独查看/恢复/删除。
"""

import os
import json
import uuid
import winreg
from datetime import datetime
from pathlib import Path
# 复用 config._atomic_write_json(8.8 评审:消除重复实现;config 仅标准库依赖无回环)
from config import _atomic_write_json

# 快照上限：非首个快照最多保留 500 个，超出自动删除最旧的
# 首个原始快照永不被删（受保护），所以实际最多可能存在 501 个
MAX_SNAPSHOTS = 500

# 首个原始快照文件名后缀（便于辨别）
_INITIAL_SUFFIX = "_initial"
# 隐藏的原始快照完整备份文件（系统隐藏属性保护，删了首个可从此恢复）
_ORIGINAL_BACKUP_FILE = ".original_snapshot.bak"
# 快照标记存储文件（独立于快照本身，避免修改受保护的首个快照）
_MARKS_FILE = "snapshot_marks.json"


# ── 首个原始快照的弱保护：隐藏受保护的操作系统文件 ──────────────────────────────
# FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM = 0x02 | 0x04 = 0x06
# 资源管理器默认不显示系统文件（即使用户开了"显示隐藏文件"也不显示，
# 需额外开启"显示受保护的操作系统文件"才显示，且有警告弹窗）。
# 0 提权、0 杀软风险、0 解锁麻烦，设置/取消就一行 SetFileAttributesW。
_HIDDEN_SYSTEM = 0x06
_ATTR_NORMAL = 0x80


def _set_system_hidden(filepath):
    """设置文件为"隐藏受保护的操作系统文件"属性"""
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(filepath), _HIDDEN_SYSTEM)
    except Exception:
        pass


def _unset_system_hidden(filepath):
    """清除隐藏/系统属性，恢复为普通文件"""
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(filepath), _ATTR_NORMAL)
    except Exception:
        pass


def is_system_hidden(filepath):
    """检查文件是否具有隐藏+系统属性"""
    try:
        import ctypes
        attr = ctypes.windll.kernel32.GetFileAttributesW(str(filepath))
        if attr < 0 or attr == 0xFFFFFFFF:  # INVALID_FILE_ATTRIBUTES
            return False
        return (attr & _HIDDEN_SYSTEM) == _HIDDEN_SYSTEM
    except Exception:
        return False


# ── 原始快照备份/恢复（隐藏备份文件，删了首个可从此恢复）─────────────────────
def _write_original_backup(snapshot_data):
    """把首个原始快照的完整 JSON 写入隐藏备份文件（系统隐藏属性保护）

    备份文件内容 = 完整快照 JSON（含 UUID、时间戳、环境变量、配置记录等全部字段）
    删了首个 .json 后，可从此备份完整恢复，保证"原始首个"身份的连续性。
    """
    try:
        backup_file = _snapshots_dir() / _ORIGINAL_BACKUP_FILE
        _atomic_write_json(backup_file, snapshot_data)
        _set_system_hidden(backup_file)
    except Exception:
        pass


def _read_original_backup():
    """读取隐藏备份文件的完整快照 JSON
    :return: dict（完整快照数据）或 None（无备份）
    """
    try:
        backup_file = _snapshots_dir() / _ORIGINAL_BACKUP_FILE
        if backup_file.exists():
            with open(backup_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _restore_from_backup():
    """从隐藏备份文件恢复首个原始快照

    场景：启动时检测到首个 _initial 缺失，从备份完整恢复（不是重新生成）。
    恢复后的 .json 文件与原始首个完全一致（UUID、时间戳、内容全部不变）。

    :return: (成功?, 恢复的文件名 or 错误信息)
    """
    try:
        backup_data = _read_original_backup()
        if not backup_data:
            return False, "无原始快照备份"

        snap_dir = _snapshots_dir()
        # 用备份中的时间戳作为文件名（保持与原始首个一致）
        ts = backup_data.get("timestamp", datetime.now().strftime("%Y%m%d_%H%M%S"))
        filename = f"{ts}{_INITIAL_SUFFIX}.json"
        filepath = snap_dir / filename
        # 若文件已存在（理论不应，但防御性处理），加序号
        if filepath.exists():
            for i in range(1, 100):
                filename = f"{ts}{_INITIAL_SUFFIX}_{i}.json"
                filepath = snap_dir / filename
                if not filepath.exists():
                    break

        _atomic_write_json(filepath, backup_data)
        _set_system_hidden(filepath)
        return True, filename
    except Exception as e:
        return False, str(e)


def has_original_backup():
    """检查是否存在原始快照备份文件（供 UI 判断是恢复还是首次创建）"""
    try:
        return (_snapshots_dir() / _ORIGINAL_BACKUP_FILE).exists()
    except Exception:
        return False


# ── 快照标记管理（星标 + 自定义标签，独立存储，不修改快照文件）──────────────────
def _load_marks():
    """加载快照标记字典 {filename: {"starred": bool, "tag": str}}"""
    try:
        marks_file = _snapshots_dir() / _MARKS_FILE
        if marks_file.exists():
            with open(marks_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _save_marks(marks):
    """保存快照标记字典"""
    try:
        marks_file = _snapshots_dir() / _MARKS_FILE
        _atomic_write_json(marks_file, marks)
    except Exception:
        pass


def set_snapshot_mark(filename, starred=None, tag=None):
    """设置快照标记
    :param starred: True/False/None（None=不修改）
    :param tag: 字符串/None（None=不修改，""=清除标签）
    :return: (成功?, 消息)
    """
    try:
        marks = _load_marks()
        if filename not in marks:
            marks[filename] = {"starred": False, "tag": ""}
        if starred is not None:
            marks[filename]["starred"] = starred
        if tag is not None:
            marks[filename]["tag"] = tag
        _save_marks(marks)
        return True, "已更新标记"
    except Exception as e:
        return False, str(e)


def get_snapshot_mark(filename):
    """获取单个快照的标记"""
    return _load_marks().get(filename, {"starred": False, "tag": ""})


def clear_snapshot_mark(filename):
    """清除单个快照的全部标记"""
    try:
        marks = _load_marks()
        if filename in marks:
            del marks[filename]
            _save_marks(marks)
        return True, "已清除标记"
    except Exception as e:
        return False, str(e)


def _write_readme(snap_dir):
    """在快照目录写一份 README.txt，说明首个原始快照的保护机制"""
    readme_path = snap_dir / "README.txt"
    # 若已存在且内容已是新版（含"恢复机制"关键词），跳过
    if readme_path.exists():
        try:
            with open(readme_path, "r", encoding="utf-8") as f:
                old = f.read()
            if "恢复机制" in old and "attrib -S -H" not in old:
                return  # 已是新版，无需重写
        except Exception:
            pass
    content = """==========================================================
开发环境快照目录（dev_env_snapshots）
==========================================================

本目录存放 C盘拦迁器 自动/手动 保存的开发环境配置快照。
每个 .json 文件是一个独立快照，文件名即时间戳（YYYYMMDD_HHMMSS）。

----------------------------------------------------------
快照内容
----------------------------------------------------------
每个快照仅保存开发环境迁移区的状态：
  • env_values        - 所有相关环境变量的当前值
  • configured_records - 已配置工具的记录（dev_env_configured）
  • original_dirs     - 原始目录结构（未迁移前的 C 盘路径状态）

不保存（不属于开发环境迁移区）：
  • migrated_records  - 迁移记录（属于已迁移区，符号链接 src→dst 路径）
  • scan_cache        - 扫描缓存（属于全局待迁移区）

----------------------------------------------------------
首个原始快照（文件名带 _initial 后缀）
----------------------------------------------------------
带 _initial 后缀的是"首个原始快照"，记录软件第一次在本机
运行时的开发环境状态，是"还原到初始状态"的最终底线。

恢复机制：即使该文件被删除，软件下次启动时会从隐藏备份
文件 .original_snapshot.bak 完整恢复（内容/UUID/时间戳全部
不变，不是重新生成）。手动新建的快照永远不会带 _initial 后缀。
----------------------------------------------------------
"""
    try:
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass


def _snapshots_dir():
    """返回快照目录路径（BASE_DIR/dev_env_snapshots/）
    会自动创建目录
    """
    # 找 BASE_DIR：源码模式下是 src 的上一级，exe 模式下是 exe 所在目录
    try:
        import sys
        if getattr(sys, 'frozen', False):
            base = Path(sys.executable).parent
        else:
            # 本文件位于 src/core/dev_env_snapshot.py，向上3级到项目根目录
            base = Path(__file__).parent.parent.parent
    except Exception:
        base = Path(__file__).parent.parent.parent
    snap_dir = base / "dev_env_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    return snap_dir


def _read_user_env_var(name):
    """读取用户级环境变量的当前值（用于快照保存当前状态）
    :return: 值字符串，不存在返回 None
    """
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ)
        try:
            value, _ = winreg.QueryValueEx(key, name)
            return value
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _set_user_env_var(name, value):
    """设置用户级环境变量（用于恢复快照）"""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, name, 0, winreg.REG_EXPAND_SZ, value)
        finally:
            winreg.CloseKey(key)
        # 广播环境变量变化
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", 0x2, 1000, None)
        return True, ""
    except Exception as e:
        return False, str(e)


def _delete_user_env_var(name):
    """删除用户级环境变量（用于恢复快照时清理多余变量）"""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass  # 不存在也算成功
        finally:
            winreg.CloseKey(key)
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", 0x2, 1000, None)
        return True, ""
    except Exception as e:
        return False, str(e)


def create_snapshot(configured_records, env_var_names, note="",
                    migrated_records=None, scan_cache=None, original_dirs=None):
    """创建一个新快照（仅保存开发环境迁移区的状态）

    范围说明：
      ✅ 保存：环境变量值、已配置工具记录（dev_env_configured）
      ✅ 保存：原始目录结构（original_dirs，未迁移前的 C 盘路径状态）
      ❌ 不保存：符号链接记录（migrated，属于已迁移区，不是开发环境迁移区）
      ❌ 不保存：扫描缓存（scan_cache，属于全局待迁移区，不是开发环境迁移区）
    原因：用户明确要求快照只覆盖"开发环境迁移区"，不掺已迁移区/待迁移区的数据。
          migrated_records 和 scan_cache 参数保留仅为向后兼容，不再写入新快照。

    :param configured_records: state.json 中的 dev_env_configured 字典
    :param env_var_names: 所有相关环境变量名列表（从 TOOLS 收集），用于保存当前值
    :param note: 用户备注（commit message）
    :param migrated_records: 已废弃，保留参数仅为向后兼容，不再写入快照
    :param scan_cache: 已废弃，保留参数仅为向后兼容，不再写入快照
    :param original_dirs: 原始目录结构列表（collect_original_dir_structure() 返回值）
    :return: (成功?, 快照文件名 or 错误信息)
    """
    try:
        # 收集当前所有相关环境变量的值
        env_values = {}
        for name in env_var_names:
            env_values[name] = _read_user_env_var(name)

        snapshot = {
            "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "created_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": note,
            "env_values": env_values,  # {变量名: 当前值或None}
            "configured_records": configured_records,  # dev_env_configured 的副本
            "original_dirs": original_dirs or [],  # 原始目录结构（未迁移前的 C 盘路径状态）
            # 注：migrated_records 和 scan_cache 不保存
            # （属于已迁移区/待迁移区，不是开发环境迁移区）
        }

        # 判断是否是首个快照：必须同时满足两个条件
        # 1. 目录无任何快照 .json（排除标记存储文件）
        # 2. 无隐藏备份文件 .original_snapshot.bak
        # 条件2防止"用户删了首个快照后手动新建快照"被误判为首个 → 错误加 _initial 后缀
        # 有备份说明原始首个已被删，应从备份恢复，绝不能新建 _initial
        snap_dir = _snapshots_dir()
        no_json = not any(p.name != _MARKS_FILE for p in snap_dir.glob("*.json"))
        no_backup = not has_original_backup()
        is_first = no_json and no_backup

        # 首个原始快照：文件名加 _initial 后缀 + 生成 UUID
        if is_first:
            snapshot["uuid"] = str(uuid.uuid4())
            filename = f"{snapshot['timestamp']}{_INITIAL_SUFFIX}.json"
        else:
            filename = f"{snapshot['timestamp']}.json"
        filepath = snap_dir / filename
        # 如果同一秒已存在（极少见），加序号
        if filepath.exists():
            for i in range(1, 100):
                if is_first:
                    filename = f"{snapshot['timestamp']}{_INITIAL_SUFFIX}_{i}.json"
                else:
                    filename = f"{snapshot['timestamp']}_{i}.json"
                filepath = snap_dir / filename
                if not filepath.exists():
                    break

        _atomic_write_json(filepath, snapshot)

        # 写 README.txt 说明权限保护机制（首次创建目录时）
        _write_readme(snap_dir)

        # 首个原始快照：设为"隐藏受保护的操作系统文件" + 写完整备份
        if is_first:
            _set_system_hidden(filepath)
            _write_original_backup(snapshot)

        # 清理超出上限的旧快照（但首个原始快照永不被删）
        _cleanup_old_snapshots(protect_first=True)

        return True, filename
    except Exception as e:
        return False, str(e)


def create_initial_snapshot(configured_records, env_var_names, note="", original_dirs=None):
    """真正首次创建首个原始快照（带 _initial 后缀 + UUID + 隐藏属性 + 完整备份）

    仅用于：无原始快照备份（真正首次运行）时创建首个。
    重新生成场景应调用 _restore_from_backup() 从备份恢复（内容、UUID、时间戳全部一致）。

    :return: (成功?, 快照文件名 or 错误信息)
    """
    try:
        env_values = {}
        for name in env_var_names:
            env_values[name] = _read_user_env_var(name)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_uuid = str(uuid.uuid4())
        snapshot = {
            "timestamp": ts,
            "created_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": note,
            "env_values": env_values,
            "configured_records": configured_records,
            "original_dirs": original_dirs or [],
            "uuid": snap_uuid,
        }

        snap_dir = _snapshots_dir()
        filename = f"{ts}{_INITIAL_SUFFIX}.json"
        filepath = snap_dir / filename
        # 同秒冲突时加序号
        if filepath.exists():
            for i in range(1, 100):
                filename = f"{ts}{_INITIAL_SUFFIX}_{i}.json"
                filepath = snap_dir / filename
                if not filepath.exists():
                    break

        _atomic_write_json(filepath, snapshot)

        _write_readme(snap_dir)
        _set_system_hidden(filepath)
        _write_original_backup(snapshot)
        _cleanup_old_snapshots(protect_first=True)
        return True, filename
    except Exception as e:
        return False, str(e)


def _list_snapshot_files():
    """列出所有快照文件（排除标记存储文件等非快照 .json）"""
    snap_dir = _snapshots_dir()
    return sorted(
        f for f in snap_dir.glob("*.json") if f.name != _MARKS_FILE
    )


def list_snapshots():
    """列出所有快照，按时间倒序（最新在前）
    :return: list of dict，每项含 filename/timestamp/created_time/note/
             is_first/is_protected/uuid/starred/tag/env_count/configured_count/original_dirs_count
    """
    files = _list_snapshot_files()
    if not files:
        return []
    # 首个原始快照：有 _initial 后缀的优先；若无（旧版兼容），取排序最靠前的
    first_file = next(
        (f.name for f in files if _INITIAL_SUFFIX in f.stem),
        files[0].name
    )
    marks = _load_marks()
    result = []
    for fp in files:
        is_first = fp.name == first_file
        mark = marks.get(fp.name, {"starred": False, "tag": ""})
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            result.append({
                "filename": fp.name,
                "timestamp": data.get("timestamp", fp.stem),
                "created_time": data.get("created_time", ""),
                "note": data.get("note", ""),
                "is_first": is_first,
                "is_protected": is_system_hidden(str(fp)),
                "uuid": data.get("uuid", ""),
                "starred": mark.get("starred", False),
                "tag": mark.get("tag", ""),
                "env_count": sum(1 for v in data.get("env_values", {}).values() if v is not None),
                "configured_count": len(data.get("configured_records", {})),
                "original_dirs_count": len(data.get("original_dirs", [])),
            })
        except Exception:
            result.append({
                "filename": fp.name,
                "timestamp": fp.stem,
                "created_time": "(读取失败)",
                "note": "",
                "is_first": is_first,
                "is_protected": False,
                "uuid": "",
                "starred": mark.get("starred", False),
                "tag": mark.get("tag", ""),
                "env_count": 0,
                "configured_count": 0,
                "original_dirs_count": 0,
            })
    # 按文件名倒序（文件名含时间戳，倒序=最新在前）
    result.sort(key=lambda x: x["filename"], reverse=True)
    return result


def _safe_snapshot_path(filename):
    """校验 filename 仅是纯文件名（无路径分隔符/.. 越界），返回快照目录内的绝对路径。

    防止 filename 含 ..\\ 等穿越序列导致越界访问快照目录之外的文件。
    :return: (安全路径 Path, 错误消息) —— 路径有效时错误消息为 None
    """
    if not filename or not isinstance(filename, str):
        return None, "文件名为空"
    # 取纯文件名：Path.name 会剥离任何目录部分，仅保留最后一段
    safe_name = Path(filename).name
    if not safe_name or safe_name in (".", ".."):
        return None, "非法文件名"
    snap_dir = _snapshots_dir().resolve()
    filepath = (snap_dir / safe_name).resolve()
    # 包含关系校验：解析后路径必须在快照目录内
    try:
        filepath.relative_to(snap_dir)
    except ValueError:
        return None, "路径越界，拒绝访问"
    return filepath, None


def load_snapshot(filename):
    """加载指定快照的完整数据
    :return: dict 或 None
    """
    try:
        filepath, err = _safe_snapshot_path(filename)
        if err:
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def delete_snapshot(filename):
    """删除指定快照
    :return: (成功?, 消息)
    """
    try:
        snap_dir = _snapshots_dir()
        files = _list_snapshot_files()
        if not files:
            return False, "无快照可删除"
        filepath, err = _safe_snapshot_path(filename)
        if err:
            return False, err
        if not filepath.exists():
            return False, "快照文件不存在"
        # 清除系统隐藏属性（_unset_system_hidden 重置为 NORMAL，含只读位）
        _unset_system_hidden(filepath)
        filepath.unlink()
        # 同步清理标记（不报错就算成功），用纯文件名避免穿越
        clear_snapshot_path = Path(filename).name
        clear_snapshot_mark(clear_snapshot_path)
        # 注：不清理 .original_snapshot_uuid 隐藏文件
        # UUID 文件是"原始首个"身份的永久记录，删首个快照后仍保留，
        # 供下次启动时检测并继承原 UUID 重新生成首个快照。
        return True, f"已删除快照 {Path(filename).name}"
    except Exception as e:
        return False, str(e)


def _cleanup_old_snapshots(protect_first=True):
    """清理超出上限的旧快照
    :param protect_first: True=保护首个原始快照（_initial 后缀或排序最靠前）不被删
    """
    try:
        snap_dir = _snapshots_dir()
        files = _list_snapshot_files()  # 按文件名排序=按时间排序，排除标记文件
        if len(files) <= MAX_SNAPSHOTS:
            return
        # 首个原始快照：_initial 后缀优先；无后缀取排序最靠前（旧版兼容）
        first_file = next(
            (f.name for f in files if _INITIAL_SUFFIX in f.stem),
            files[0].name if files else None
        )
        # 需要删除的数量（超出上限的部分）
        excess = len(files) - MAX_SNAPSHOTS
        deleted = 0
        # 从最旧的开始删，跳过首个原始快照，删够为止
        for fp in files:
            if deleted >= excess:
                break
            if protect_first and fp.name == first_file:
                continue  # 首个原始快照永不被删
            try:
                _unset_system_hidden(fp)
                fp.unlink()
                clear_snapshot_mark(fp.name)
                deleted += 1
            except Exception:
                pass
    except Exception:
        pass


def restore_snapshot(filename, current_env_var_names, restore_migrated=False):
    """从快照恢复环境变量配置
    - 快照中有的变量：还原为快照时的值
    - 当前有但快照中没有的变量：删除（说明是快照后才配置的）
    :param filename: 快照文件名
    :param current_env_var_names: 当前所有相关环境变量名列表
    :param restore_migrated: 已废弃，保留参数仅为向后兼容，不再恢复迁移记录
    :return: (成功?, 详细消息, snapshot_data)
             snapshot_data 含 configured_records/original_dirs，供调用方同步配置记录和符号链接状态，
             避免"环境变量已恢复但配置记录/链接状态仍是当前值"的分裂问题（H7）。
    """
    data = load_snapshot(filename)
    if not data:
        return False, "无法加载快照数据", None

    snap_env = data.get("env_values", {})
    msgs = []
    success_count = 0
    fail_count = 0

    # 1. 还原快照中记录的变量值
    for name, snap_value in snap_env.items():
        if snap_value is None:
            # 快照时该变量不存在 → 删除当前变量（如果存在）
            current_val = _read_user_env_var(name)
            if current_val is not None:
                ok, err = _delete_user_env_var(name)
                if ok:
                    msgs.append(f"✓ 删除环境变量 {name}（快照中不存在）")
                    success_count += 1
                else:
                    msgs.append(f"✗ 删除 {name} 失败: {err}")
                    fail_count += 1
            else:
                msgs.append(f"✓ {name} 已不存在（与快照一致）")
                success_count += 1
        else:
            # 快照中有值 → 设置为快照值
            ok, err = _set_user_env_var(name, snap_value)
            if ok:
                msgs.append(f"✓ 还原 {name} = {snap_value}")
                success_count += 1
            else:
                msgs.append(f"✗ 还原 {name} 失败: {err}")
                fail_count += 1

    # 2. 当前有但快照中完全没有的变量 → 删除（快照后新增的配置）
    for name in current_env_var_names:
        if name not in snap_env:
            current_val = _read_user_env_var(name)
            if current_val is not None:
                ok, err = _delete_user_env_var(name)
                if ok:
                    msgs.append(f"✓ 删除环境变量 {name}（快照后新增的配置）")
                    success_count += 1
                else:
                    msgs.append(f"✗ 删除 {name} 失败: {err}")
                    fail_count += 1

    # 注：不再恢复迁移记录（migrated_records 属于已迁移区，不是开发环境迁移区）
    # H7：返回完整快照数据，供调用方同步 configured_records 和 original_dirs，
    #     避免"环境变量已恢复但配置记录/链接状态未同步"的分裂问题
    summary = f"恢复完成：成功 {success_count} 项，失败 {fail_count} 项"
    return fail_count == 0, summary + "\n\n" + "\n".join(msgs), data


def get_snapshot_count():
    """返回当前快照数量（排除标记存储文件）"""
    try:
        return len(_list_snapshot_files())
    except Exception:
        return 0
