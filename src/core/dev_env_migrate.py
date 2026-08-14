# -*- coding: utf-8 -*-
"""
开发环境路径迁移模块
功能：检测和配置各种开发工具的默认安装路径，把它们改到 D 盘
只配置环境变量/配置文件，不迁移现有文件（安全无风险）

支持的工具类别：
- Node.js 生态：npm 全局/缓存、yarn、pnpm
- Python 生态：pip（特殊提示）、pip 缓存、conda
- Rust 生态：cargo、rustup
- Go 生态：GOPATH/GOCACHE/GOMODCACHE
- .NET 生态：dotnet tools、nuget
- Java 生态：gradle、maven
- Ruby 生态：gem
- Julia：JULIA_DEPOT_PATH
- VS Code：扩展目录
- 特殊工具（环境变量解决不了）：Docker、WSL、Visual Studio
"""

import os
import sys
import json
import shutil
import subprocess
import winreg
import time
import functools
import logging
from pathlib import Path


# Windows 下调用子进程时不弹黑框的标志
# CREATE_NO_WINDOW = 0x08000000
_NO_WINDOW_FLAGS = 0x08000000

# 模块级 logger（main.py 的 setup_logging 会统一配置 handler）
_log = logging.getLogger("CDriveGuard")


# ========== detect/path 进程内缓存 ==========
# 表格刷新时 26+ 个工具的 detect_xxx() 和 path_fn() 会调用 subprocess
# （npm/yarn/pnpm/pip/conda/go/dotnet/gradle/maven/gem/julia/...），每个最多 5 秒超时。
# 用 TTL 缓存避免重复跑 subprocess，刷新表格秒级返回。
# TTL 60 秒：覆盖一次完整的表格刷新周期；apply/unapply/migrate 后会主动清空。
_CACHE_TTL = 60.0
_detect_path_cache = {}  # key: fn.__name__, value: (timestamp, result)


def _cached(fn):
    """带 TTL 的进程内缓存装饰器（用于 detect_xxx / path_fn）

    缓存 key 为函数名（不带参数），所以要求这些函数都是无参的。
    配置变更后通过 clear_detect_path_cache() 主动清空。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        key = fn.__name__
        pair = _detect_path_cache.get(key)
        if pair is not None and time.time() - pair[0] <= _CACHE_TTL:
            return pair[1]
        result = fn(*args, **kwargs)
        _detect_path_cache[key] = (time.time(), result)
        return result
    return wrapper


def clear_detect_path_cache():
    """清空 detect/path 缓存

    在 apply_tool / unapply_tool / migrate_tool_data / unconfigure_tool
    等会改变工具状态的操作后调用，保证下次刷新拿到新结果。
    """
    _detect_path_cache.clear()


# ========== 目录大小缓存（避免 D 盘大目录反复 os.walk） ==========
# key: 目录路径（小写规范化）, value: (size_mb, timestamp)
# TTL 1 小时：目录大小变化不快，迁移/还原后会被主动清空
_SIZE_CACHE_TTL = 3600.0
_size_cache = {}


def get_cached_size(path):
    """获取缓存的目录大小（MB），未命中返回 None"""
    if not path:
        return None
    key = path.replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
    pair = _size_cache.get(key)
    if pair is not None and time.time() - pair[1] < _SIZE_CACHE_TTL:
        return pair[0]
    return None


def set_cached_size(path, size_mb):
    """缓存目录大小"""
    if not path:
        return
    key = path.replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
    _size_cache[key] = (size_mb, time.time())


def clear_size_cache(path=None):
    """清空大小缓存（迁移/还原后调用）

    :param path: 只清该路径的缓存；None 清全部
    """
    if path is None:
        _size_cache.clear()
    else:
        key = path.replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
        _size_cache.pop(key, None)


# 需要校验是否为有效路径的环境变量名（这些变量的值应该是路径，不是任意文本）
# 历史 bug 或外部工具可能把非路径文本写入这些变量（如 "0 MB"），导致 detect/path 误判
_PATH_ENV_VARS = [
    "ANDROID_HOME", "ANDROID_SDK_ROOT",
    "CARGO_HOME", "RUSTUP_HOME",
    "GOPATH", "GOCACHE", "GOMODCACHE",
    "GRADLE_USER_HOME",
    "DOTNET_TOOLS_PATH", "NUGET_PACKAGES",
    "PNPM_HOME",
    "PIP_CACHE_DIR", "PYTHONUSERBASE",
    "GEM_HOME", "GEM_PATH",
    "JULIA_DEPOT_PATH",
    "ELECTRON_CACHE", "CONAN_HOME", "VCPKG_ROOT",
    "TF_PLUGIN_CACHE_DIR",
    "R_LIBS_USER",
]


def cleanup_bad_env_vars():
    """启动时清理坏环境变量

    扫描所有路径类环境变量，值不是有效路径时（如历史 bug 写入的 "0 MB"）：
    1. 从注册表 HKCU\\Environment 删除
    2. 从 os.environ 删除

    :return: list[str]，被清理的变量名列表
    """
    cleaned = []
    try:
        import winreg
        # 先读注册表，确定哪些需要删
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment",
                             0, winreg.KEY_READ)
        reg_values = {}
        try:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                    reg_values[name] = value
                    i += 1
                except OSError:
                    break
        finally:
            winreg.CloseKey(key)

        # 找出值无效的变量
        bad_vars = []
        for name in _PATH_ENV_VARS:
            # 检查注册表值
            reg_val = reg_values.get(name)
            if reg_val is not None and not _is_valid_path(reg_val):
                bad_vars.append((name, reg_val))
                continue
            # 检查 os.environ 值（可能是进程内被污染）
            env_val = os.environ.get(name)
            if env_val is not None and not _is_valid_path(env_val):
                bad_vars.append((name, env_val))

        if not bad_vars:
            return cleaned

        # 删除坏变量
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment",
                             0, winreg.KEY_SET_VALUE)
        try:
            for name, bad_val in bad_vars:
                try:
                    winreg.DeleteValue(key, name)
                    _log.info(f"清理坏环境变量: {name}={bad_val!r}（值不是有效路径，已从注册表删除）")
                    cleaned.append(name)
                except FileNotFoundError:
                    pass  # 注册表没有，只清理 os.environ
                # 同步清理 os.environ
                if name in os.environ:
                    del os.environ[name]
        finally:
            winreg.CloseKey(key)

        # 广播环境变量变化
        if cleaned:
            import ctypes
            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
                0x2, 1000, None)
    except Exception as e:
        _log.error(f"清理坏环境变量失败: {e}")
    return cleaned


def _home(*sub):
    """返回用户主目录（统一反斜杠），可拼接子路径

    替代 os.path.expanduser("~/.xxx")，避免在 Windows 上出现
    "C:\\Users\\aaa/.xxx" 这种正反斜杠混合的路径。
    用法：
        _home()                  → C:\\Users\\aaa
        _home(".gradle")         → C:\\Users\\aaa\\.gradle
        _home(".m2", "settings.xml")  → C:\\Users\\aaa\\.m2\\settings.xml
    """
    base = os.path.expanduser("~")
    return os.path.join(base, *sub) if sub else base


# ========== 工具定义表 ==========
# 每个工具一个 dict，字段：
#   id: 唯一标识
#   name: 显示名
#   category: 类别
#   env_vars: 要设置的环境变量列表 [{name, default_value_template}]
#             template 中 {D} 会被替换成用户选的目标盘
#   config_commands: 配置文件命令列表（如 npm config set）[{cmd_template, desc}]
#   detect: 检测函数名（在下面的 DETECT_FUNCS 里查）
#   current_path_fn: 获取当前路径函数名
#   special: 特殊提示类型（None/ 'pip' / 'docker' / 'wsl' / 'vs'）
#   clean_guide: 清理指引文本

TOOLS = [
    # ===== Node.js 生态 =====
    {
        "id": "npm_global",
        "name": "npm 全局包",
        "category": "Node.js",
        "env_vars": [],
        "config_commands": [
            {"cmd_template": ["npm", "config", "set", "prefix", "{D}\\dev\\nodejs\\npm-global"],
             "desc": "npm 全局包安装路径"},
        ],
        "unconfig_commands": [
            {"cmd_template": ["npm", "config", "delete", "prefix"],
             "desc": "撤销 npm prefix（恢复默认 %APPDATA%\\npm）"},
        ],
        "detect": "detect_npm",
        "current_path_fn": "npm_global_path",
        "special": None,
        "clean_guide": "npm cache clean --force  # 清理缓存\n"
                       "删除 %APPDATA%\\npm 下旧的全局包（确认新路径生效后）",
        "download_url": "https://nodejs.org/zh-cn/download/"
    },
    {
        "id": "npm_cache",
        "name": "npm 缓存",
        "category": "Node.js",
        "env_vars": [],
        "config_commands": [
            {"cmd_template": ["npm", "config", "set", "cache", "{D}\\dev\\nodejs\\npm-cache"],
             "desc": "npm 缓存路径"},
        ],
        "unconfig_commands": [
            {"cmd_template": ["npm", "config", "delete", "cache"],
             "desc": "撤销 npm cache（恢复默认 %LOCALAPPDATA%\\npm-cache）"},
        ],
        "detect": "detect_npm",
        "current_path_fn": "npm_cache_path",
        "special": None,
        "clean_guide": "npm cache clean --force",
        "download_url": "https://nodejs.org/zh-cn/download/"
    },
    {
        "id": "yarn_global",
        "name": "yarn 全局包",
        "category": "Node.js",
        "env_vars": [],
        "config_commands": [
            {"cmd_template": ["yarn", "config", "set", "prefix", "{D}\\dev\\nodejs\\yarn-global"],
             "desc": "yarn 全局包路径"},
            {"cmd_template": ["yarn", "config", "set", "cache-folder", "{D}\\dev\\nodejs\\yarn-cache"],
             "desc": "yarn 缓存路径"},
        ],
        "unconfig_commands": [
            {"cmd_template": ["yarn", "config", "delete", "prefix"],
             "desc": "撤销 yarn prefix"},
            {"cmd_template": ["yarn", "config", "delete", "cache-folder"],
             "desc": "撤销 yarn cache-folder"},
        ],
        "detect": "detect_yarn",
        "current_path_fn": "yarn_global_path",
        "special": None,
        "clean_guide": "yarn cache clean",
        "download_url": "https://classic.yarnpkg.com/lang/en/docs/install/#windows-stable"
    },
    {
        "id": "pnpm_global",
        "name": "pnpm 全局包",
        "category": "Node.js",
        "env_vars": [
            {"name": "PNPM_HOME", "default_value_template": "{D}\\dev\\nodejs\\pnpm-global"},
        ],
        "config_commands": [],
        "unconfig_commands": [],
        "detect": "detect_pnpm",
        "current_path_fn": "pnpm_global_path",
        "special": None,
        "clean_guide": "pnpm store prune  # 清理无用文件",
        "download_url": "https://pnpm.io/installation"
    },

    # ===== Python 生态 =====
    {
        "id": "pip_install",
        "name": "pip 默认安装路径",
        "category": "Python",
        "env_vars": [],
        "config_commands": [],
        "unconfig_commands": [],
        "detect": "detect_pip",
        "current_path_fn": "pip_site_packages_path",
        "special": "pip",  # 特殊提示：pip 装到 site-packages，需 Python 装到 D 盘
        "clean_guide": "⚠️ pip 没有简单的\"改默认路径\"配置。\n"
                       "pip 包默认装到 Python 的 Lib\\site-packages，路径由 Python 安装位置决定。\n"
                       "解决方案（三选一）：\n"
                       "  1. 【推荐】把 Python 本身卸载重装到 D 盘（最彻底）\n"
                       "  2. 用 --user 安装：设置 PYTHONUSERBASE=D:\\dev\\python\\python-user 后 pip install --user xxx\n"
                       "  3. 配置 target：pip config set global.target \"D:\\dev\\python\\pip-target\"（可能影响多版本管理）",
        "download_url": "https://www.python.org/downloads/windows/"
    },
    {
        "id": "pip_cache",
        "name": "pip 缓存",
        "category": "Python",
        "env_vars": [
            {"name": "PIP_CACHE_DIR", "default_value_template": "{D}\\dev\\python\\pip-cache"},
        ],
        "config_commands": [
            {"cmd_template": ["pip", "config", "set", "global.cache-dir", "{D}\\dev\\python\\pip-cache"],
             "desc": "pip 缓存路径"},
        ],
        "unconfig_commands": [
            {"cmd_template": ["pip", "config", "unset", "global.cache-dir"],
             "desc": "撤销 pip cache-dir（恢复默认 %LOCALAPPDATA%\\pip\\Cache）"},
        ],
        "detect": "detect_pip",
        "current_path_fn": "pip_cache_path",
        "special": None,
        "clean_guide": "pip cache purge",
        "download_url": "https://www.python.org/downloads/windows/"
    },
    {
        "id": "conda",
        "name": "conda 包/环境",
        "category": "Python",
        "env_vars": [],
        "config_commands": [
            # .condarc 配置，用 conda config 命令
            {"cmd_template": ["conda", "config", "--add", "envs_dirs", "{D}\\dev\\python\\conda\\envs"],
             "desc": "conda 环境路径"},
            {"cmd_template": ["conda", "config", "--add", "pkgs_dirs", "{D}\\dev\\python\\conda\\pkgs"],
             "desc": "conda 包缓存路径"},
        ],
        "unconfig_commands": [
            {"cmd_template": ["conda", "config", "--remove", "envs_dirs", "{D}\\dev\\python\\conda\\envs"],
             "desc": "移除 conda envs_dirs 自定义路径"},
            {"cmd_template": ["conda", "config", "--remove", "pkgs_dirs", "{D}\\dev\\python\\conda\\pkgs"],
             "desc": "移除 conda pkgs_dirs 自定义路径"},
        ],
        "detect": "detect_conda",
        "current_path_fn": "conda_path",
        "special": None,
        "clean_guide": "conda clean --all  # 清理包缓存\n"
                       "删除 C:\\Users\\xxx\\anaconda3\\envs 下旧环境（确认新路径生效后）",
        "download_url": "https://docs.conda.io/en/latest/miniconda.html"
    },

    # ===== Rust 生态 =====
    {
        "id": "cargo_home",
        "name": "CARGO_HOME (cargo 包)",
        "category": "Rust",
        "env_vars": [
            {"name": "CARGO_HOME", "default_value_template": "{D}\\dev\\rust\\cargo"},
        ],
        "config_commands": [],
        "detect": "detect_cargo",
        "current_path_fn": "cargo_home_path",
        "special": None,
        "clean_guide": "cargo cache -a  # 需先 cargo install cargo-cache\n"
                       "或手动删除 %USERPROFILE%\\.cargo\\registry\\cache",
        "download_url": "https://rustup.rs/"
    },
    {
        "id": "rustup_home",
        "name": "RUSTUP_HOME (rust 工具链)",
        "category": "Rust",
        "env_vars": [
            {"name": "RUSTUP_HOME", "default_value_template": "{D}\\dev\\rust\\rustup"},
        ],
        "config_commands": [],
        "detect": "detect_rustup",
        "current_path_fn": "rustup_home_path",
        "special": None,
        "clean_guide": "rustup toolchain list  # 查看已装工具链\n"
                       "删除 %USERPROFILE%\\.rustup 下旧工具链（确认新路径生效后）",
        "download_url": "https://rustup.rs/"
    },

    # ===== Go 生态 =====
    {
        "id": "gopath",
        "name": "GOPATH (go install 目标)",
        "category": "Go",
        "env_vars": [
            {"name": "GOPATH", "default_value_template": "{D}\\dev\\go\\gopath"},
        ],
        "config_commands": [
            {"cmd_template": ["go", "env", "-w", "GOPATH={D}\\dev\\go\\gopath"],
             "desc": "go env 配置 GOPATH"},
        ],
        "unconfig_commands": [
            {"cmd_template": ["go", "env", "-u", "GOPATH"],
             "desc": "撤销 go env GOPATH（恢复默认 %USERPROFILE%\\go）"},
        ],
        "detect": "detect_go",
        "current_path_fn": "gopath_path",
        "special": None,
        "clean_guide": "go clean -cache  # 清理构建缓存\n"
                       "go clean -modcache  # 清理模块缓存",
        "download_url": "https://go.dev/dl/"
    },
    {
        "id": "gocache",
        "name": "GOCACHE (go 构建缓存)",
        "category": "Go",
        "env_vars": [
            {"name": "GOCACHE", "default_value_template": "{D}\\dev\\go\\build"},
        ],
        "config_commands": [
            {"cmd_template": ["go", "env", "-w", "GOCACHE={D}\\dev\\go\\build"],
             "desc": "go env 配置 GOCACHE"},
        ],
        "unconfig_commands": [
            {"cmd_template": ["go", "env", "-u", "GOCACHE"],
             "desc": "撤销 go env GOCACHE（恢复默认 %LOCALAPPDATA%\\go-build）"},
        ],
        "detect": "detect_go",
        "current_path_fn": "gocache_path",
        "special": None,
        "clean_guide": "go clean -cache",
        "download_url": "https://go.dev/dl/"
    },
    {
        "id": "gomodcache",
        "name": "GOMODCACHE (go 模块缓存)",
        "category": "Go",
        "env_vars": [
            {"name": "GOMODCACHE", "default_value_template": "{D}\\dev\\go\\mod"},
        ],
        "config_commands": [
            {"cmd_template": ["go", "env", "-w", "GOMODCACHE={D}\\dev\\go\\mod"],
             "desc": "go env 配置 GOMODCACHE"},
        ],
        "unconfig_commands": [
            {"cmd_template": ["go", "env", "-u", "GOMODCACHE"],
             "desc": "撤销 go env GOMODCACHE（恢复默认 GOPATH\\pkg\\mod）"},
        ],
        "detect": "detect_go",
        "current_path_fn": "gomodcache_path",
        "special": None,
        "clean_guide": "go clean -modcache",
        "download_url": "https://go.dev/dl/"
    },

    # ===== .NET 生态 =====
    {
        "id": "dotnet_tools",
        "name": "dotnet tools 全局工具",
        "category": ".NET",
        "env_vars": [
            {"name": "DOTNET_TOOLS_PATH", "default_value_template": "{D}\\dev\\dotnet\\tools"},
        ],
        "config_commands": [],
        "detect": "detect_dotnet",
        "current_path_fn": "dotnet_tools_path",
        "special": None,
        "clean_guide": "dotnet tool list -g  # 查看已装工具\n"
                       "dotnet tool uninstall -g <工具名>  # 卸载",
        "download_url": "https://dotnet.microsoft.com/zh-cn/download"
    },
    {
        "id": "nuget_cache",
        "name": "NuGet 包缓存",
        "category": ".NET",
        "env_vars": [
            {"name": "NUGET_PACKAGES", "default_value_template": "{D}\\dev\\dotnet\\nuget"},
        ],
        "config_commands": [],
        "detect": "detect_dotnet",
        "current_path_fn": "nuget_path",
        "special": None,
        "clean_guide": "dotnet nuget locals all --clear",
        "download_url": "https://dotnet.microsoft.com/zh-cn/download"
    },

    # ===== Java 生态 =====
    {
        "id": "gradle_home",
        "name": "GRADLE_USER_HOME",
        "category": "Java",
        "env_vars": [
            {"name": "GRADLE_USER_HOME", "default_value_template": "{D}\\dev\\java\\gradle"},
        ],
        "config_commands": [],
        "detect": "detect_gradle",
        "current_path_fn": "gradle_home_path",
        "special": None,
        "clean_guide": "删除 %USERPROFILE%\\.gradle\\caches 下的构建缓存",
        "download_url": "https://gradle.org/install/"
    },
    {
        "id": "maven_repo",
        "name": "Maven 本地仓库",
        "category": "Java",
        "env_vars": [],
        "config_commands": [],  # 需改 settings.xml，下面有专门函数
        "detect": "detect_maven",
        "current_path_fn": "maven_repo_path",
        "special": None,
        "clean_guide": "删除 %USERPROFILE%\\.m2\\repository 下旧依赖",
        "download_url": "https://maven.apache.org/download.cgi"
    },

    # ===== Ruby 生态 =====
    {
        "id": "gem_home",
        "name": "GEM_HOME/GEM_PATH",
        "category": "Ruby",
        "env_vars": [
            {"name": "GEM_HOME", "default_value_template": "{D}\\dev\\ruby\\gem"},
            {"name": "GEM_PATH", "default_value_template": "{D}\\dev\\ruby\\gem"},
        ],
        "config_commands": [],
        "detect": "detect_gem",
        "current_path_fn": "gem_home_path",
        "special": None,
        "clean_guide": "gem cleanup  # 清理旧版本",
        "download_url": "https://rubyinstaller.org/downloads/"
    },

    # ===== Julia =====
    {
        "id": "julia_depot",
        "name": "JULIA_DEPOT_PATH",
        "category": "Julia",
        "env_vars": [
            {"name": "JULIA_DEPOT_PATH", "default_value_template": "{D}\\dev\\julia"},
        ],
        "config_commands": [],
        "detect": "detect_julia",
        "current_path_fn": "julia_depot_path",
        "special": None,
        "clean_guide": "删除 %USERPROFILE%\\.julia 下旧包",
        "download_url": "https://julialang.org/downloads/"
    },

    # ===== VS Code =====
    {
        "id": "vscode_ext",
        "name": "VS Code 扩展目录",
        "category": "编辑器",
        "env_vars": [
            {"name": "VSCODE_EXTENSIONS", "default_value_template": "{D}\\dev\\editor\\vscode-ext"},
        ],
        "config_commands": [],
        "detect": "detect_vscode",
        "current_path_fn": "vscode_ext_path",
        "special": None,
        "clean_guide": "关闭 VS Code → 删除 %USERPROFILE%\\.vscode\\extensions → 重启\n"
                       "（扩展会重新安装到新路径，需先在 VS Code 里记录已装扩展列表）",
        "download_url": "https://code.visualstudio.com/Download"
    },

    # ===== PHP =====
    {
        "id": "composer_cache",
        "name": "Composer 缓存/主目录",
        "category": "PHP",
        "env_vars": [
            {"name": "COMPOSER_HOME", "default_value_template": "{D}\\dev\\php\\composer"},
            {"name": "COMPOSER_CACHE_DIR", "default_value_template": "{D}\\dev\\php\\composer\\cache"},
        ],
        "config_commands": [],
        "detect": "detect_composer",
        "current_path_fn": "composer_path",
        "special": None,
        "clean_guide": "composer clearcache  # 清理缓存\n"
                       "删除 %LOCALAPPDATA%\\Composer\\Cache 和 %APPDATA%\\Composer 下旧文件",
        "download_url": "https://getcomposer.org/download/"
    },

    # ===== Dart/Flutter =====
    {
        "id": "pub_cache",
        "name": "Dart/Flutter pub 缓存",
        "category": "Dart",
        "env_vars": [
            {"name": "PUB_CACHE", "default_value_template": "{D}\\dev\\dart\\pub-cache"},
        ],
        "config_commands": [
            {"cmd_template": ["dart", "pub", "cache", "set", "{D}\\dev\\dart\\pub-cache"],
             "desc": "pub 缓存路径（dart 命令配置）"},
        ],
        "unconfig_commands": [
            # dart pub cache 没有直接的 unset，删 PUB_CACHE 环境变量即可恢复默认
            # 这里留空，主要靠删除 PUB_CACHE 环境变量回滚
        ],
        "detect": "detect_dart",
        "current_path_fn": "pub_cache_path",
        "special": None,
        "clean_guide": "flutter pub cache clean  # 或 dart pub cache clean",
        "download_url": "https://dart.dev/get-dart"
    },

    # ===== R 语言 =====
    {
        "id": "r_libs",
        "name": "R 语言包库",
        "category": "R",
        "env_vars": [
            # %v 是 R 自动追加版本号的占位符，必须保留
            {"name": "R_LIBS_USER", "default_value_template": "{D}\\dev\\R\\win-library\\%v"},
        ],
        "config_commands": [],
        "detect": "detect_r",
        "current_path_fn": "r_libs_path",
        "special": None,
        "clean_guide": "在 R 里运行 .libPaths() 查看库路径\n"
                       "删除 %USERPROFILE%\\Documents\\R\\win-library 下旧包",
        "download_url": "https://cran.r-project.org/bin/windows/base/"
    },

    # ===== Terraform =====
    {
        "id": "terraform_cache",
        "name": "Terraform 插件缓存",
        "category": "Terraform",
        "env_vars": [
            {"name": "TF_PLUGIN_CACHE_DIR", "default_value_template": "{D}\\dev\\terraform\\plugin-cache"},
        ],
        "config_commands": [],
        "detect": "detect_terraform",
        "current_path_fn": "terraform_cache_path",
        "special": None,
        "clean_guide": "删除 %USERPROFILE%\\.terraform.d\\plugin-cache 下旧插件",
        "download_url": "https://developer.hashicorp.com/terraform/downloads"
    },

    # ===== Haskell =====
    {
        "id": "stack_root",
        "name": "STACK_ROOT (Haskell)",
        "category": "Haskell",
        "env_vars": [
            {"name": "STACK_ROOT", "default_value_template": "{D}\\dev\\haskell\\stack"},
        ],
        "config_commands": [],
        "detect": "detect_stack",
        "current_path_fn": "stack_root_path",
        "special": None,
        "clean_guide": "删除 %LOCALAPPDATA%\\Programs\\stack 下旧文件\n"
                       "stack exec -- ghc-pkg list  # 查看已装包",
        "download_url": "https://docs.haskellstack.org/en/stable/install_and_upgrade/"
    },

    # ===== Scala =====
    {
        "id": "coursier_cache",
        "name": "Coursier 缓存 (Scala/sbt)",
        "category": "Scala",
        "env_vars": [
            {"name": "COURSIER_CACHE", "default_value_template": "{D}\\dev\\scala\\coursier\\cache"},
        ],
        "config_commands": [
            {"cmd_template": ["cs", "config", "cache", "{D}\\dev\\scala\\coursier\\cache"],
             "desc": "coursier 缓存路径"},
        ],
        "unconfig_commands": [
            # coursier 没有直接的 unset 命令，删 COURSIER_CACHE 环境变量即可恢复默认
        ],
        "detect": "detect_coursier",
        "current_path_fn": "coursier_cache_path",
        "special": None,
        "clean_guide": "删除 %LOCALAPPDATA%\\Coursier\\Cache 下旧缓存\n"
                       "sbt 的 ~/.sbt 目录建议保留（配置文件）",
        "download_url": "https://get-coursier.io/docs/cli-installation"
    },

    # ===== OCaml =====
    {
        "id": "opam_root",
        "name": "OPAMROOT (OCaml)",
        "category": "OCaml",
        "env_vars": [
            {"name": "OPAMROOT", "default_value_template": "{D}\\dev\\ocaml\\opam"},
        ],
        "config_commands": [],
        "detect": "detect_opam",
        "current_path_fn": "opam_root_path",
        "special": None,
        "clean_guide": "opam uninstall  # 卸载所有包\n"
                       "删除 %USERPROFILE%\\.opam 下旧 switch",
        "download_url": "https://ocaml.org/install"
    },

    # ===== Nim =====
    {
        "id": "nimble_dir",
        "name": "NIMBLE_DIR (Nim)",
        "category": "Nim",
        "env_vars": [
            {"name": "NIMBLE_DIR", "default_value_template": "{D}\\dev\\nim\\nimble"},
        ],
        "config_commands": [],
        "detect": "detect_nimble",
        "current_path_fn": "nimble_dir_path",
        "special": None,
        "clean_guide": "nimble uninstall  # 卸载包\n"
                       "删除 %USERPROFILE%\\.nimble 下旧包",
        "download_url": "https://nim-lang.org/install.html"
    },

    # ===== Elixir/Erlang =====
    {
        "id": "mix_home",
        "name": "MIX_HOME/HEX_HOME (Elixir)",
        "category": "Elixir",
        "env_vars": [
            {"name": "MIX_HOME", "default_value_template": "{D}\\dev\\elixir\\mix"},
            {"name": "HEX_HOME", "default_value_template": "{D}\\dev\\elixir\\hex"},
        ],
        "config_commands": [],
        "detect": "detect_mix",
        "current_path_fn": "mix_home_path",
        "special": None,
        "clean_guide": "mix archive.uninstall  # 卸载归档\n"
                       "删除 %USERPROFILE%\\.mix 和 %USERPROFILE%\\.hex 下旧文件",
        "download_url": "https://elixir-lang.org/install.html"
    },

    # ===== Swift PM =====
    {
        "id": "swiftpm_config",
        "name": "SwiftPM 配置/缓存",
        "category": "Swift",
        "env_vars": [
            {"name": "SWIFTPM_CONFIG_PATH", "default_value_template": "{D}\\dev\\swift\\swiftpm"},
            {"name": "SWIFTPM_CACHE_DIR", "default_value_template": "{D}\\dev\\swift\\swiftpm\\cache"},
        ],
        "config_commands": [],
        "detect": "detect_swift",
        "current_path_fn": "swiftpm_path",
        "special": None,
        "clean_guide": "删除 %USERPROFILE%\\.swiftpm 下旧配置\n"
                       "注意：项目级 .build 目录无法迁移，按项目隔离",
        "download_url": "https://www.swift.org/install/"
    },

    # ===== Android =====
    {
        "id": "android_sdk",
        "name": "Android SDK/NDK",
        "category": "Android",
        "env_vars": [
            {"name": "ANDROID_HOME", "default_value_template": "{D}\\dev\\android\\sdk"},
            {"name": "ANDROID_SDK_ROOT", "default_value_template": "{D}\\dev\\android\\sdk"},
        ],
        "config_commands": [],
        "detect": "detect_android_sdk",
        "current_path_fn": "android_sdk_path",
        "special": None,
        "clean_guide": "⚠️ 配置后需手动把 C 盘的 Sdk 目录剪切到 D 盘\n"
                       "并在 Android Studio → SDK Manager 里更新路径\n"
                       "删除 %LOCALAPPDATA%\\Android\\Sdk 下旧文件（确认新路径生效后）",
        "download_url": "https://developer.android.com/studio"
    },

    # ===== Electron =====
    {
        "id": "electron_cache",
        "name": "Electron 二进制缓存",
        "category": "Electron",
        "env_vars": [
            {"name": "ELECTRON_CACHE", "default_value_template": "{D}\\dev\\electron\\cache"},
            {"name": "ELECTRON_BUILDER_CACHE", "default_value_template": "{D}\\dev\\electron\\builder\\cache"},
        ],
        "config_commands": [],
        "detect": "detect_electron",
        "current_path_fn": "electron_cache_path",
        "special": None,
        "clean_guide": "删除 %LOCALAPPDATA%\\electron\\Cache 和 %LOCALAPPDATA%\\electron-builder\\Cache",
        "download_url": "https://www.electronjs.org/docs/latest/tutorial/installation"
    },

    # ===== Conan (C++) =====
    {
        "id": "conan_home",
        "name": "CONAN_HOME (C++ 包)",
        "category": "C++",
        "env_vars": [
            {"name": "CONAN_HOME", "default_value_template": "{D}\\dev\\cpp\\conan2"},
        ],
        "config_commands": [],
        "detect": "detect_conan",
        "current_path_fn": "conan_home_path",
        "special": None,
        "clean_guide": "conan remove '*' -f  # 删除所有包\n"
                       "删除 %USERPROFILE%\\.conan2 下旧文件",
        "download_url": "https://conan.io/downloads.html"
    },

    # ===== Vcpkg (C++) =====
    {
        "id": "vcpkg_root",
        "name": "VCPKG_ROOT (C++ 包)",
        "category": "C++",
        "env_vars": [
            {"name": "VCPKG_ROOT", "default_value_template": "{D}\\dev\\cpp\\vcpkg"},
            {"name": "VCPKG_DEFAULT_BINARY_CACHE", "default_value_template": "{D}\\dev\\cpp\\vcpkg-cache"},
        ],
        "config_commands": [],
        "detect": "detect_vcpkg",
        "current_path_fn": "vcpkg_root_path",
        "special": None,
        "clean_guide": "vcpkg remove  # 卸载包\n"
                       "把 C 盘的 vcpkg 目录剪切到 D:\\dev\\vcpkg",
        "download_url": "https://vcpkg.io/en/getting-started.html"
    },

    # ===== Bazel =====
    {
        "id": "bazel_output",
        "name": "Bazel 输出目录",
        "category": "Bazel",
        "env_vars": [],
        "config_commands": [],  # 需改 .bazelrc，下面有专门处理
        "detect": "detect_bazel",
        "current_path_fn": "bazel_output_path",
        "special": None,
        "clean_guide": "⚠️ Bazel 需修改 ~/.bazelrc 文件：\n"
                       "  在文件中添加：startup --output_user_root=D:/dev/bazel/root\n"
                       "  或用 --output_user_root=D:/dev/bazel/root 启动参数\n"
                       "环境变量 BAZEL_OUTPUT_BASE 只能覆盖 output base",
        "download_url": "https://bazel.build/install/windows"
    },

    # ===== 特殊工具（环境变量解决不了，只给指引）=====
    {
        "id": "docker_data",
        "name": "Docker Desktop 数据",
        "category": "特殊工具",
        "env_vars": [],
        "config_commands": [],
        "detect": "detect_docker",
        "current_path_fn": "docker_data_path",
        "special": "docker",
        "clean_guide": "⚠️ Docker 数据在 ext4.vhdx 文件里，环境变量改不了。\n"
                       "迁移步骤：\n"
                       "  1. wsl --shutdown\n"
                       "  2. wsl --export docker-desktop-data D:\\docker-data.tar\n"
                       "  3. wsl --unregister docker-desktop-data\n"
                       "  4. wsl --import docker-desktop-data D:\\dev\\docker\\data D:\\docker-data.tar\n"
                       "  或：Docker Desktop 设置 → Resources → Disk image location 改为 D:\\dev\\docker",
        "download_url": "https://www.docker.com/get-started/"
    },
    {
        "id": "wsl_distros",
        "name": "WSL 发行版",
        "category": "特殊工具",
        "env_vars": [],
        "config_commands": [],
        "detect": "detect_wsl",
        "current_path_fn": "wsl_distros_path",
        "special": "wsl",
        "clean_guide": "⚠️ WSL 发行版环境变量改不了，需用 export/import 迁移：\n"
                       "  1. wsl --shutdown\n"
                       "  2. wsl --export <发行版名> D:\\wsl-backup.tar\n"
                       "  3. wsl --unregister <发行版名>\n"
                       "  4. wsl --import <发行版名> D:\\dev\\wsl\\<发行版名> D:\\wsl-backup.tar\n"
                       "  查看发行版名：wsl -l -v",
        "download_url": "https://learn.microsoft.com/zh-cn/windows/wsl/install"
    },
    {
        "id": "visual_studio",
        "name": "Visual Studio",
        "category": "特殊工具",
        "env_vars": [],
        "config_commands": [],
        "detect": "detect_vs",
        "current_path_fn": "vs_install_path",
        "special": "vs",
        "clean_guide": "⚠️ Visual Studio 需用 VS Installer 改安装位置：\n"
                       "  1. 打开 Visual Studio Installer\n"
                       "  2. 点击「修改」→「安装位置」标签\n"
                       "  3. 把安装路径改为 D:\\Program Files\\Microsoft Visual Studio\n"
                       "  注意：需先卸载再重装，无法直接移动",
        "download_url": "https://visualstudio.microsoft.com/zh-hans/downloads/"
    },
]


# ========== GitHub 仓库地址 ==========
# 用于下载菜单中额外提供"GitHub 仓库"选项，方便用户查看源码/提 issue
# 已逐一验证（2026-07-24）：以下地址均为官方/公认仓库主页
GITHUB_URLS = {
    # Node.js 生态
    "npm_global":       "https://github.com/nodejs/node",             # Node.js（含 npm）
    "npm_cache":        "https://github.com/nodejs/node",             # Node.js（含 npm）
    "yarn_global":      "https://github.com/yarnpkg/yarn",            # Yarn 包管理器（Classic）
    "pnpm_global":      "https://github.com/pnpm/pnpm",               # pnpm 包管理器
    # Python 生态
    "pip_install":      "https://github.com/python/cpython",         # CPython（含 pip）
    "pip_cache":        "https://github.com/python/cpython",         # CPython（含 pip）
    "conda":            "https://github.com/conda/conda",             # Conda
    # Rust 生态
    "cargo_home":       "https://github.com/rust-lang/cargo",         # Cargo
    "rustup_home":      "https://github.com/rust-lang/rustup",        # Rustup
    # Go 生态
    "gopath":           "https://github.com/golang/go",               # Go（GitHub 镜像，官方在 googlesource）
    "gocache":          "https://github.com/golang/go",               # Go
    "gomodcache":       "https://github.com/golang/go",               # Go
    # .NET 生态
    "dotnet_tools":     "https://github.com/dotnet/dotnet",           # .NET
    "nuget_cache":      "https://github.com/NuGet/NuGet.Client",      # NuGet
    # Java 生态
    "gradle_home":      "https://github.com/gradle/gradle",           # Gradle
    "maven_repo":       "https://github.com/apache/maven",            # Apache Maven
    # Ruby 生态
    "gem_home":         "https://github.com/rubygems/rubygems",       # RubyGems（含 gem 命令）
    # Julia
    "julia_depot":      "https://github.com/JuliaLang/julia",         # Julia
    # VS Code
    "vscode_ext":       "https://github.com/microsoft/vscode",        # VS Code
    # PHP
    "composer_cache":   "https://github.com/composer/composer",       # Composer
    # Dart
    "pub_cache":        "https://github.com/dart-lang/sdk",           # Dart SDK（pub 内置）
    # R
    "r_libs":           "https://github.com/wch/r-source",            # R 源码镜像（官方源码在 SVN）
    # Terraform
    "terraform_cache":  "https://github.com/hashicorp/terraform",     # Terraform
    # Haskell
    "stack_root":       "https://github.com/commercialhaskell/stack", # Haskell Stack
    # Scala
    "coursier_cache":   "https://github.com/coursier/coursier",       # Coursier
    # OCaml
    "opam_root":        "https://github.com/ocaml/opam",              # OCaml opam
    # Nim
    "nimble_dir":       "https://github.com/nim-lang/nimble",         # Nimble（Nim 包管理器）
    # Elixir
    "mix_home":         "https://github.com/elixir-lang/elixir",      # mix 是 Elixir 内置构建工具
    # Swift
    "swiftpm_config":   "https://github.com/swiftlang/swift",         # Swift（SwiftPM 内置）
    # Electron
    "electron_cache":   "https://github.com/electron/electron",       # Electron
    # Conan
    "conan_home":       "https://github.com/conan-io/conan",          # Conan C/C++ 包管理器
    # vcpkg
    "vcpkg_root":       "https://github.com/microsoft/vcpkg",         # vcpkg（微软）
    # Bazel
    "bazel_output":     "https://github.com/bazelbuild/bazel",        # Bazel 构建工具
    # WSL
    "wsl_distros":      "https://github.com/microsoft/WSL",           # WSL（微软官方）
    # Android（NDK 源码仓库，含 README、releases、issue 追踪，比组织主页更实用）
    "android_sdk":      "https://github.com/android/ndk",
    # Docker（Moby 是 Docker 引擎的开源上游项目，由 Docker 官方维护，活跃）
    "docker_data":      "https://github.com/moby/moby",
    # Visual Studio（IDE 本身闭源商业产品；MSBuild 是其核心构建组件，微软官方开源维护，活跃）
    "visual_studio":    "https://github.com/dotnet/msbuild",
}


# ========== 检测函数 ==========

def _run_cmd(cmd, timeout=2):
    """运行命令，返回输出（出错返回空字符串）
    Windows 下用 CREATE_NO_WINDOW 抑制黑框
    注意：npm/yarn/pnpm/gem/composer/dart/conda 等在 Windows 上是 .cmd/.bat 脚本，
    shell=False 会找不到文件，必须用 shell=True 调用。

    timeout 从 5s 降到 2s：慢工具（conda/gradle/julia）失败更快，
    path_fn 已加环境变量+默认路径 fallback，subprocess 失败不影响表格填充。
    """
    try:
        # 把 list 拼成字符串，统一用 shell=True
        # 这样既能调用 .cmd 脚本，又能用 creationflags 抑制黑框
        if isinstance(cmd, list):
            cmd = subprocess.list2cmdline(cmd)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           shell=True, encoding='utf-8', errors='ignore',
                           creationflags=_NO_WINDOW_FLAGS)
        if r.returncode == 0:
            out = r.stdout.strip()
            # 过滤无效输出（yarn/npm 等在未设置时可能返回 "undefined"/"null"）
            if out and out.lower() in ("undefined", "null", "none"):
                return ""
            return out
        return ""
    except Exception:
        return ""


def _which(name):
    """类似 which 命令，在 PATH 中查找可执行文件"""
    return shutil.which(name)


def detect_npm():
    return _which("npm") is not None


def detect_yarn():
    return _which("yarn") is not None


def detect_pnpm():
    return _which("pnpm") is not None


def detect_pip():
    return _which("pip") is not None or _which("pip3") is not None


def _find_conda_install_paths():
    """通用查找 conda 安装路径（不依赖硬编码盘符）
    顺序：注册表(含UninstallString提取) → 用户目录 → ProgramData → 遍历所有盘符根目录
    返回找到的路径列表（可能为空）
    """
    found = []
    seen = set()

    def _try_add(p):
        if not p:
            return
        p = os.path.normpath(p)
        key = p.lower()
        if key in seen:
            return
        # 判定条件：目录下有 Scripts\conda.exe 或 conda.exe
        if os.path.exists(os.path.join(p, "Scripts", "conda.exe")) or \
           os.path.exists(os.path.join(p, "conda.exe")):
            seen.add(key)
            found.append(p)

    # 1. 从注册表查 Anaconda/Miniconda 安装路径
    try:
        import winreg
        import re as _re
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for subkey in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                           r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    key = winreg.OpenKey(hive, subkey)
                except OSError:
                    continue
                try:
                    i = 0
                    while True:
                        try:
                            child_name = winreg.EnumKey(key, i)
                            i += 1
                        except OSError:
                            break
                        if "anaconda" not in child_name.lower() and \
                           "miniconda" not in child_name.lower():
                            continue
                        try:
                            child = winreg.OpenKey(key, child_name)
                            # 优先 InstallLocation，其次从 UninstallString/DisplayIcon 提取路径
                            for field in ("InstallLocation", "UninstallString", "DisplayIcon"):
                                try:
                                    val, _ = winreg.QueryValueEx(child, field)
                                    if field == "InstallLocation":
                                        _try_add(val)
                                    else:
                                        # 从 "D:\anaconda3\Uninstall-Anaconda3.exe" 提取目录
                                        # 去掉引号和可执行文件名
                                        cleaned = val.strip().strip('"').strip("'")
                                        # 取路径的目录部分
                                        dir_path = os.path.dirname(cleaned)
                                        if dir_path:
                                            _try_add(dir_path)
                                except OSError as e:
                                    _log.debug("忽略异常: %s", e)
                            winreg.CloseKey(child)
                        except OSError as e:
                            _log.debug("忽略异常: %s", e)
                finally:
                    winreg.CloseKey(key)
    except Exception as e:
        _log.debug("忽略异常: %s", e)

    # 2. 用户目录 / ProgramData / LOCALAPPDATA 下的默认安装路径
    _try_add(os.path.join(os.environ.get("USERPROFILE", ""), "anaconda3"))
    _try_add(os.path.join(os.environ.get("USERPROFILE", ""), "miniconda3"))
    _try_add(os.path.join(os.environ.get("LOCALAPPDATA", ""), "anaconda3"))
    _try_add(os.path.join(os.environ.get("LOCALAPPDATA", ""), "miniconda3"))
    _try_add(r"C:\ProgramData\anaconda3")
    _try_add(r"C:\ProgramData\miniconda3")

    # 3. 遍历所有盘符根目录（C-Z，不依赖 GetLogicalDriveStringsW）
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        drive = f"{letter}:\\"
        if not os.path.exists(drive):
            continue
        _try_add(os.path.join(drive, "anaconda3"))
        _try_add(os.path.join(drive, "miniconda3"))
        _try_add(os.path.join(drive, "Anaconda3"))
        _try_add(os.path.join(drive, "Miniconda3"))

    return found


@_cached
def detect_conda():
    """检测 conda：优先 PATH，其次通用查找安装路径（不依赖硬编码盘符）"""
    if _which("conda"):
        return True
    return len(_find_conda_install_paths()) > 0


def detect_cargo():
    return _which("cargo") is not None


def detect_rustup():
    return _which("rustup") is not None


def detect_go():
    return _which("go") is not None


def detect_dotnet():
    return _which("dotnet") is not None


def detect_gradle():
    return _which("gradle") is not None or os.path.exists(_home(".gradle"))


def detect_maven():
    return _which("mvn") is not None or os.path.exists(_home(".m2"))


def detect_gem():
    return _which("gem") is not None


def detect_julia():
    return _which("julia") is not None


def detect_vscode():
    # VS Code 扩展目录存在即认为装了
    ext_path = _home(".vscode", "extensions")
    return os.path.exists(ext_path) or _which("code") is not None


def detect_docker():
    return _which("docker") is not None


def detect_wsl():
    return _which("wsl") is not None


def detect_vs():
    """检测 Visual Studio（不是 VS Code）"""
    vs_paths = [
        r"C:\Program Files\Microsoft Visual Studio",
        r"C:\Program Files (x86)\Microsoft Visual Studio",
    ]
    return any(os.path.exists(p) for p in vs_paths)


def detect_composer():
    return _which("composer") is not None


def detect_dart():
    return _which("dart") is not None or _which("flutter") is not None


def detect_r():
    return _which("R") is not None or _which("Rscript") is not None


def detect_terraform():
    return _which("terraform") is not None


def detect_stack():
    return _which("stack") is not None


def detect_coursier():
    return _which("cs") is not None or _which("coursier") is not None


def detect_opam():
    return _which("opam") is not None


def detect_nimble():
    return _which("nimble") is not None


def detect_mix():
    return _which("mix") is not None


def detect_swift():
    return _which("swift") is not None


def detect_android_sdk():
    # 优先检查环境变量（值必须是有效路径，防止历史坏数据如 "0 MB" 误判为已安装）
    sdk = (os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "").strip()
    if _is_valid_path(sdk):
        return True
    # 默认路径
    default = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk")
    return os.path.exists(default)


def _is_valid_path(s):
    """判断字符串是否是有效的 Windows 路径
    用于过滤历史 bug 写入的坏环境变量值（如 "0 MB" "1434.5MB" 等）

    有效格式：
    1. X:\\ 开头的绝对路径（如 D:\\dev\\go）
    2. %XXX% 开头的环境变量引用（REG_EXPAND_SZ 类型，如 %USERPROFILE%\\go）
    """
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if len(s) < 3:
        return False
    # 排除明显的非路径文本（含单位/空格+字母组合）
    if any(kw in s for kw in (" MB", " GB", " KB", " TB")):
        return False
    # 格式1：%XXX% 环境变量引用（REG_EXPAND_SZ 合法值，Windows 会在使用时展开）
    if s.startswith("%") and "%" in s[1:]:
        return True
    # 格式2：X:\ 开头的绝对路径
    if s[1:2] != ":" or s[2:3] not in ("\\", "/"):
        return False
    # 盘符必须是字母
    if not s[0].isalpha():
        return False
    return True


def detect_electron():
    # electron 一般通过 electron/npm 装的，检查缓存目录
    cache = os.path.join(os.environ.get("LOCALAPPDATA", ""), "electron", "Cache")
    return os.path.exists(cache) or _which("electron") is not None


def detect_conan():
    return _which("conan") is not None


def detect_vcpkg():
    return _which("vcpkg") is not None or bool(os.environ.get("VCPKG_ROOT"))


def detect_bazel():
    return _which("bazel") is not None


# ========== 获取当前路径函数 ==========

@_cached
def npm_global_path():
    """npm 全局包路径：先环境变量+默认路径（O(1)），最后才 subprocess（1-2s）"""
    if not detect_npm():
        return ""
    # 1. NPM_CONFIG_PREFIX 环境变量（npm 读取的前缀配置）
    env_prefix = os.environ.get("NPM_CONFIG_PREFIX") or os.environ.get("npm_config_prefix")
    if env_prefix and os.path.exists(env_prefix):
        return env_prefix
    # 2. npm 默认全局包路径 %APPDATA%\npm
    default = os.path.join(os.environ.get("APPDATA", ""), "npm")
    if os.path.exists(default):
        return default
    # 3. node.exe 所在目录（旧版 npm 把全局包装在这里）
    node_exe = shutil.which("node")
    if node_exe:
        node_dir = os.path.dirname(node_exe)
        npm_dir = os.path.join(node_dir, "node_modules")
        if os.path.exists(npm_dir):
            return npm_dir
        return node_dir
    # 4. 最后兜底：subprocess（npm config get prefix，1-2s）
    out = _run_cmd(["npm", "config", "get", "prefix"])
    return out


@_cached
def npm_cache_path():
    """npm 缓存路径：先环境变量+默认路径，最后才 subprocess"""
    if not detect_npm():
        return ""
    # 1. NPM_CONFIG_CACHE 环境变量
    env_cache = os.environ.get("NPM_CONFIG_CACHE") or os.environ.get("npm_config_cache")
    if env_cache and os.path.exists(env_cache):
        return env_cache
    # 2. npm 默认缓存路径 %LOCALAPPDATA%\npm-cache
    default = os.path.join(os.environ.get("LOCALAPPDATA", ""), "npm-cache")
    if os.path.exists(default):
        return default
    # 3. 兜底：subprocess
    out = _run_cmd(["npm", "config", "get", "cache"])
    return out if out else default


@_cached
def yarn_global_path():
    """yarn 全局包路径：先环境变量+默认路径，最后才 subprocess"""
    if not detect_yarn():
        return ""
    # 1. YARN_GLOBAL_FOLDER 环境变量
    env_folder = os.environ.get("YARN_GLOBAL_FOLDER")
    if env_folder and os.path.exists(env_folder):
        return env_folder
    # 2. yarn 默认全局路径 %LOCALAPPDATA%\Yarn
    default = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Yarn")
    if os.path.exists(default):
        return default
    # 3. 兜底：subprocess（yarn config get prefix，1-2s）
    out = _run_cmd(["yarn", "config", "get", "prefix"])
    if out and out.lower() not in ("undefined", "null", "none", ""):
        return out
    return default


@_cached
def pnpm_global_path():
    """pnpm 全局目录：优先 PNPM_HOME + 默认路径，最后才 subprocess（2-5s）"""
    if not detect_pnpm():
        return ""
    # 1. PNPM_HOME 环境变量（最准）
    home = os.environ.get("PNPM_HOME")
    if home and os.path.exists(home):
        return home
    # 2. pnpm 7+ 默认 %LOCALAPPDATA%\pnpm
    default = os.path.join(os.environ.get("LOCALAPPDATA", ""), "pnpm")
    if os.path.exists(default):
        return default
    # 3. 兜底：subprocess（pnpm store path + config global-bin-dir，2-5s）
    out = _run_cmd(["pnpm", "store", "path"])
    if out:
        return out
    out = _run_cmd(["pnpm", "config", "get", "global-bin-dir"])
    return out


def pip_site_packages_path():
    """pip 默认安装路径 = Python 的 site-packages 目录

    优先用 Python 可执行文件所在目录推算（O(1)），避免 subprocess 启动 python。
    """
    if not detect_pip():
        return ""
    python = shutil.which("python") or shutil.which("python3")
    if not python:
        return ""
    # 1. 默认路径推算：Python 所在目录\Lib\site-packages
    python_dir = os.path.dirname(python)
    default_sp = os.path.join(python_dir, "Lib", "site-packages")
    if os.path.exists(default_sp):
        return default_sp
    # 2. 兜底：subprocess（python -c "import site..."，0.5-1s）
    out = _run_cmd([python, "-c",
                    "import site; print(site.getsitepackages()[0] if site.getsitepackages() else site.getusersitepackages())"])
    return out if out else ""


@_cached
def pip_cache_path():
    """pip 缓存路径：先环境变量+默认路径，最后才 subprocess"""
    if not detect_pip():
        return ""
    # 1. PIP_CACHE_DIR 环境变量
    env_cache = os.environ.get("PIP_CACHE_DIR")
    if env_cache and os.path.exists(env_cache):
        return env_cache
    # 2. pip 默认缓存路径 %LOCALAPPDATA%\pip\Cache
    default = os.path.join(os.environ.get("LOCALAPPDATA", ""), "pip", "Cache")
    if os.path.exists(default):
        return default
    # 3. 兜底：subprocess
    out = _run_cmd(["pip", "cache", "dir"])
    return out if out else os.environ.get("PIP_CACHE_DIR", "")


@_cached
def conda_path():
    """获取 conda 安装路径：先环境变量+which+注册表（无 subprocess），最后才 conda info（3-5s）"""
    if not detect_conda():
        return ""
    # 1. CONDA_PREFIX / CONDA_HOME 环境变量
    env_path = os.environ.get("CONDA_PREFIX") or os.environ.get("CONDA_HOME")
    if env_path and os.path.exists(env_path):
        return env_path
    # 2. conda 可执行文件所在目录（shutil.which 已找到 conda，推算安装路径）
    conda_exe = shutil.which("conda")
    if conda_exe:
        conda_dir = os.path.dirname(conda_exe)
        # conda.exe 通常在 Scripts\ 子目录，父目录才是 conda 安装根目录
        if os.path.basename(conda_dir).lower() == "scripts":
            parent = os.path.dirname(conda_dir)
            # 验证父目录是 conda 安装根（含 conda.exe 或 Scripts\conda.exe）
            if (os.path.exists(os.path.join(parent, "conda.exe")) or
                os.path.exists(os.path.join(parent, "Scripts", "conda.exe"))):
                return parent
        # 否则返回 conda 所在目录
        if os.path.exists(conda_dir):
            return conda_dir
    # 3. 通用查找（注册表 + 全盘符扫描，无 subprocess）
    paths = _find_conda_install_paths()
    if paths:
        return paths[0]
    # 4. 最后兜底：subprocess（conda info --base，3-5s，conda 启动极慢）
    out = _run_cmd(["conda", "info", "--base"])
    return out


def cargo_home_path():
    return os.environ.get("CARGO_HOME", _home(".cargo"))


def rustup_home_path():
    return os.environ.get("RUSTUP_HOME", _home(".rustup"))


@_cached
def gopath_path():
    """GOPATH：先环境变量+默认路径，最后才 subprocess（go env，0.5-1s）"""
    # 1. GOPATH 环境变量
    env_gp = os.environ.get("GOPATH")
    if env_gp and os.path.exists(env_gp):
        return env_gp
    # 2. 默认 %USERPROFILE%\go
    default = _home("go")
    if os.path.exists(default):
        return default
    # 3. 兜底：subprocess
    if detect_go():
        out = _run_cmd(["go", "env", "GOPATH"])
        if out:
            return out
    return default


@_cached
def gocache_path():
    """GOCACHE：先环境变量+默认路径，最后才 subprocess"""
    # 1. GOCACHE 环境变量
    env_gc = os.environ.get("GOCACHE")
    if env_gc and os.path.exists(env_gc):
        return env_gc
    # 2. 默认 %LOCALAPPDATA%\go-build
    default = os.path.join(os.environ.get("LOCALAPPDATA", ""), "go-build")
    if os.path.exists(default):
        return default
    # 3. 兜底：subprocess
    if detect_go():
        out = _run_cmd(["go", "env", "GOCACHE"])
        if out:
            return out
    return default


@_cached
def gomodcache_path():
    """GOMODCACHE：先环境变量+默认路径，最后才 subprocess"""
    # 1. GOMODCACHE 环境变量
    env_gmc = os.environ.get("GOMODCACHE")
    if env_gmc and os.path.exists(env_gmc):
        return env_gmc
    # 2. 默认 GOPATH\pkg\mod
    gp = gopath_path()
    default = os.path.join(gp, "pkg", "mod") if gp else ""
    if default and os.path.exists(default):
        return default
    # 3. 兜底：subprocess
    if detect_go():
        out = _run_cmd(["go", "env", "GOMODCACHE"])
        if out:
            return out
    return default or os.environ.get("GOMODCACHE", "")


@_cached
def dotnet_tools_path():
    """dotnet 全局工具路径：优先环境变量，其次默认路径（须存在才返回）"""
    env_path = os.environ.get("DOTNET_TOOLS_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    # 默认路径 C:\Users\xxx\.dotnet\tools（只在存在时返回，避免误报C盘）
    default = _home(".dotnet", "tools")
    if os.path.exists(default):
        return default
    # 尝试从 dotnet 命令查实际路径
    if _which("dotnet"):
        out = _run_cmd(["dotnet", "tool", "list", "--global"])
        # 输出含 "Tool Path: D:\xxx"
        for line in (out or "").splitlines():
            if "tool path" in line.lower():
                parts = line.split(":", 1)
                if len(parts) == 2:
                    p = parts[1].strip()
                    if p:
                        return p
    return ""


def nuget_path():
    """NuGet 包缓存路径：优先环境变量，其次默认路径（须存在才返回）"""
    env_path = os.environ.get("NUGET_PACKAGES")
    if env_path and os.path.exists(env_path):
        return env_path
    default = _home(".nuget", "packages")
    if os.path.exists(default):
        return default
    return ""


def gradle_home_path():
    return os.environ.get("GRADLE_USER_HOME", _home(".gradle"))


def maven_repo_path():
    # 检查 settings.xml 里的 localRepository，没有就用默认
    settings_path = _home(".m2", "settings.xml")
    if os.path.exists(settings_path):
        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                content = f.read()
            import re
            m = re.search(r'<localRepository>([^<]+)</localRepository>', content)
            if m:
                return m.group(1).strip()
        except Exception as e:
            _log.debug("忽略异常: %s", e)
    return _home(".m2", "repository")


@_cached
def gem_home_path():
    """GEM_HOME 路径：先环境变量+默认路径，最后才 subprocess（gem env，1-2s）

    未安装时也返回默认路径（%USERPROFILE%\.gem），与其他工具保持一致，
    让用户在表格中能看到默认会占用 C 盘的位置。
    """
    # 1. GEM_HOME 环境变量
    env_home = os.environ.get("GEM_HOME")
    if env_home and os.path.exists(env_home):
        return env_home
    # 2. 默认 %USERPROFILE%\.gem
    default = _home(".gem")
    if os.path.exists(default):
        return default
    # 3. 兜底：subprocess（gem env GEM_HOME，1-2s）
    if detect_gem():
        out = _run_cmd(["gem", "env", "GEM_HOME"])
        if out:
            return out
    # 4. 未安装或 gem env 查不到时，返回默认路径
    return default


def julia_depot_path():
    return os.environ.get("JULIA_DEPOT_PATH", _home(".julia"))


def vscode_ext_path():
    return os.environ.get("VSCODE_EXTENSIONS",
                          _home(".vscode", "extensions"))


def docker_data_path():
    """Docker Desktop WSL 数据位置
    实际数据在 ext4.vhdx 文件里，位于 %LOCALAPPDATA%\Docker\wsl\disk\
    （旧版本可能在 data\，都检查一下）
    也会从注册表读取 Docker Desktop 的安装路径作为兜底。
    """
    local = os.environ.get("LOCALAPPDATA", "")
    # 1. 新版 Docker Desktop: wsl\disk\ext4.vhdx（检查多个可能子目录）
    for sub in ["wsl\\disk", "wsl\\data", "WSL\\disk", "WSL\\data"]:
        p = os.path.join(local, "Docker", sub)
        if os.path.exists(p):
            return p
    # 2. 兜底：%LOCALAPPDATA%\Docker 目录本身
    docker_dir = os.path.join(local, "Docker")
    if os.path.exists(docker_dir):
        return docker_dir
    # 3. 注册表查 Docker Desktop 安装路径
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Docker Desktop") as key:
            install_loc, _ = winreg.QueryValueEx(key, "InstallLocation")
            if install_loc and os.path.exists(install_loc):
                return install_loc
    except Exception as e:
        _log.debug("忽略异常: %s", e)
    return ""


def wsl_distros_path():
    # WSL 发行版默认安装在 LocalAppData\Packages 下
    default = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages")
    return default if os.path.exists(default) else ""


def vs_install_path():
    for p in [r"C:\Program Files\Microsoft Visual Studio",
              r"C:\Program Files (x86)\Microsoft Visual Studio"]:
        if os.path.exists(p):
            return p
    return ""


# ===== 新增工具的当前路径函数 =====

@_cached
def composer_path():
    """Composer 主目录"""
    home = os.environ.get("COMPOSER_HOME")
    if home:
        return home
    # 默认 %APPDATA%\Composer
    return os.path.join(os.environ.get("APPDATA", ""), "Composer")


def pub_cache_path():
    """Dart/Flutter pub 缓存"""
    cache = os.environ.get("PUB_CACHE")
    if cache:
        return cache
    # 默认 %LOCALAPPDATA%\Pub\Cache
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Pub", "Cache")


def r_libs_path():
    """R 语言用户库路径

    未安装时也返回默认路径（~/Documents/R/win-library），与其他工具保持一致，
    让用户在表格中能看到默认会占用 C 盘的位置。
    """
    libs = os.environ.get("R_LIBS_USER")
    if libs:
        return libs
    # 默认 ~/Documents/R/win-library/<版本>
    base = _home("Documents", "R", "win-library")
    if os.path.exists(base):
        # 取版本号最大的子目录
        try:
            versions = sorted(os.listdir(base))
            if versions:
                return os.path.join(base, versions[-1])
        except Exception as e:
            _log.debug("忽略异常: %s", e)
        return base
    # 未安装时返回默认路径（不检查目录是否存在）
    return base


@_cached
def terraform_cache_path():
    """Terraform 插件缓存"""
    cache = os.environ.get("TF_PLUGIN_CACHE_DIR")
    if cache:
        return cache
    # 默认 ~/.terraform.d/plugin-cache（需用户手动启用）
    return _home(".terraform.d", "plugin-cache")


def stack_root_path():
    """Haskell Stack root"""
    root = os.environ.get("STACK_ROOT")
    if root:
        return root
    # 默认 %LOCALAPPDATA%\Programs\stack
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "stack")


def coursier_cache_path():
    """Coursier (Scala) 缓存"""
    cache = os.environ.get("COURSIER_CACHE")
    if cache:
        return cache
    # 默认 %LOCALAPPDATA%\Coursier\Cache\v1
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Coursier", "Cache", "v1")


def opam_root_path():
    """OCaml opam root"""
    root = os.environ.get("OPAMROOT")
    if root:
        return root
    return _home(".opam")


def nimble_dir_path():
    """Nim nimble 目录"""
    d = os.environ.get("NIMBLE_DIR")
    if d:
        return d
    return _home(".nimble")


def mix_home_path():
    """Elixir mix 主目录"""
    home = os.environ.get("MIX_HOME")
    if home:
        return home
    return _home(".mix")


def swiftpm_path():
    """SwiftPM 配置目录"""
    cfg = os.environ.get("SWIFTPM_CONFIG_PATH")
    if cfg:
        return cfg
    return _home(".swiftpm")


def android_sdk_path():
    """Android SDK 路径

    未安装时也返回官方默认路径（%LOCALAPPDATA%\\Android\\Sdk），与其他工具
    保持一致，让用户在表格中能看到默认会占用 C 盘的位置。
    环境变量值不是有效路径时（如历史 bug 写入的 "0 MB"）回退到默认路径。
    """
    sdk = (os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "").strip()
    if _is_valid_path(sdk):
        return sdk
    # 默认 %LOCALAPPDATA%\Android\Sdk（Android Studio 安装时默认位置）
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk")


def electron_cache_path():
    """Electron 缓存"""
    cache = os.environ.get("ELECTRON_CACHE")
    if cache:
        return cache
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "electron", "Cache")


def conan_home_path():
    """Conan v2 主目录"""
    home = os.environ.get("CONAN_HOME")
    if home:
        return home
    # v2 默认 ~/.conan2，v1 默认 ~/.conan
    for d in [".conan2", ".conan"]:
        p = _home(d)
        if os.path.exists(p):
            return p
    return _home(".conan2")


def vcpkg_root_path():
    """Vcpkg 根目录

    未安装时也返回官方推荐默认路径 C:\\vcpkg，与其他工具保持一致，
    让用户在表格中能看到默认会占用 C 盘的位置。
    """
    root = os.environ.get("VCPKG_ROOT")
    if root:
        return root
    # 看常见安装位置
    for p in [r"C:\vcpkg", r"C:\dev\vcpkg"]:
        if os.path.exists(p):
            return p
    # 未安装时返回官方文档推荐的默认路径
    return r"C:\vcpkg"


def bazel_output_path():
    """Bazel 输出根目录"""
    # 检查 .bazelrc 是否配置了 output_user_root
    bazelrc = _home(".bazelrc")
    if os.path.exists(bazelrc):
        try:
            with open(bazelrc, 'r', encoding='utf-8') as f:
                for line in f:
                    if "output_user_root" in line and line.strip().startswith("startup"):
                        # 提取路径
                        parts = line.split("output_user_root=")
                        if len(parts) > 1:
                            return parts[1].strip()
        except Exception as e:
            _log.debug("忽略异常: %s", e)
    # 默认 %LOCALAPPDATA%\bazel\_bazel_<用户名>
    username = os.environ.get("USERNAME", "user")
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "bazel",
                        f"_bazel_{username}")


# ========== 检测函数映射表 ==========

DETECT_FUNCS = {
    "detect_npm": detect_npm,
    "detect_yarn": detect_yarn,
    "detect_pnpm": detect_pnpm,
    "detect_pip": detect_pip,
    "detect_conda": detect_conda,
    "detect_cargo": detect_cargo,
    "detect_rustup": detect_rustup,
    "detect_go": detect_go,
    "detect_dotnet": detect_dotnet,
    "detect_gradle": detect_gradle,
    "detect_maven": detect_maven,
    "detect_gem": detect_gem,
    "detect_julia": detect_julia,
    "detect_vscode": detect_vscode,
    "detect_docker": detect_docker,
    "detect_wsl": detect_wsl,
    "detect_vs": detect_vs,
    "detect_composer": detect_composer,
    "detect_dart": detect_dart,
    "detect_r": detect_r,
    "detect_terraform": detect_terraform,
    "detect_stack": detect_stack,
    "detect_coursier": detect_coursier,
    "detect_opam": detect_opam,
    "detect_nimble": detect_nimble,
    "detect_mix": detect_mix,
    "detect_swift": detect_swift,
    "detect_android_sdk": detect_android_sdk,
    "detect_electron": detect_electron,
    "detect_conan": detect_conan,
    "detect_vcpkg": detect_vcpkg,
    "detect_bazel": detect_bazel,
}

CURRENT_PATH_FUNCS = {
    "npm_global_path": npm_global_path,
    "npm_cache_path": npm_cache_path,
    "yarn_global_path": yarn_global_path,
    "pnpm_global_path": pnpm_global_path,
    "pip_site_packages_path": pip_site_packages_path,
    "pip_cache_path": pip_cache_path,
    "conda_path": conda_path,
    "cargo_home_path": cargo_home_path,
    "rustup_home_path": rustup_home_path,
    "gopath_path": gopath_path,
    "gocache_path": gocache_path,
    "gomodcache_path": gomodcache_path,
    "dotnet_tools_path": dotnet_tools_path,
    "nuget_path": nuget_path,
    "gradle_home_path": gradle_home_path,
    "maven_repo_path": maven_repo_path,
    "gem_home_path": gem_home_path,
    "julia_depot_path": julia_depot_path,
    "vscode_ext_path": vscode_ext_path,
    "docker_data_path": docker_data_path,
    "wsl_distros_path": wsl_distros_path,
    "vs_install_path": vs_install_path,
    "composer_path": composer_path,
    "pub_cache_path": pub_cache_path,
    "r_libs_path": r_libs_path,
    "terraform_cache_path": terraform_cache_path,
    "stack_root_path": stack_root_path,
    "coursier_cache_path": coursier_cache_path,
    "opam_root_path": opam_root_path,
    "nimble_dir_path": nimble_dir_path,
    "mix_home_path": mix_home_path,
    "swiftpm_path": swiftpm_path,
    "android_sdk_path": android_sdk_path,
    "electron_cache_path": electron_cache_path,
    "conan_home_path": conan_home_path,
    "vcpkg_root_path": vcpkg_root_path,
    "bazel_output_path": bazel_output_path,
}


# 默认 C: 盘路径映射表（不读环境变量，用于在 env var 已指向 D: 时仍能定位 C: 原位置）
# 与上面各 *_path() 函数的 default 分支保持一致
def get_tool_default_c_path(tool):
    """获取工具的默认 C: 盘路径（不读工具自身的环境变量）

    用于在 env var 已指向 D: 时，仍能找到 C: 盘的原始位置（可能是符号链接）。
    返回空字符串表示该工具的默认 C: 路径无法静态确定（如 pip_install/wsl_distros）。
    """
    tool_id = tool.get("id", "") if isinstance(tool, dict) else ""
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    appdata = os.environ.get("APPDATA", "")
    userprofile = os.environ.get("USERPROFILE", "")
    home = _home() if userprofile else ""
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

    DEFAULT_C_PATHS = {
        "npm_global": os.path.join(appdata, "npm"),
        "npm_cache": os.path.join(local_appdata, "npm-cache"),
        "yarn_global": os.path.join(local_appdata, "Yarn"),
        "pnpm_global": os.path.join(local_appdata, "pnpm"),
        "pip_cache": os.path.join(local_appdata, "pip", "Cache"),
        "cargo_home": os.path.join(home, ".cargo") if home else "",
        "rustup_home": os.path.join(home, ".rustup") if home else "",
        "gopath": os.path.join(home, "go") if home else "",
        "gocache": os.path.join(local_appdata, "go-build"),
        "gomodcache": os.path.join(home, "go", "pkg", "mod") if home else "",
        "dotnet_tools": os.path.join(home, ".dotnet", "tools") if home else "",
        "nuget_cache": os.path.join(home, ".nuget", "packages") if home else "",
        "gradle_home": os.path.join(home, ".gradle") if home else "",
        "maven_repo": os.path.join(home, ".m2", "repository") if home else "",
        "julia_depot": os.path.join(home, ".julia") if home else "",
        "composer_cache": os.path.join(local_appdata, "Composer"),
        "pub_cache": os.path.join(local_appdata, "Pub", "Cache"),
        "terraform_cache": os.path.join(appdata, "terraform.d"),
        "conan_home": os.path.join(home, ".conan") if home else "",
        "bazel_output": os.path.join(local_appdata, "bazel"),
        "android_sdk": os.path.join(local_appdata, "Android", "Sdk"),
        "electron_cache": os.path.join(local_appdata, "electron", "Cache"),
        "vcpkg_root": os.path.join(home, "vcpkg") if home else "",
        "stack_root": os.path.join(home, ".stack") if home else "",
        "coursier_cache": os.path.join(local_appdata, "Coursier"),
        "opam_root": os.path.join(home, ".opam") if home else "",
        "nimble_dir": os.path.join(home, ".nimble") if home else "",
        "mix_home": os.path.join(home, ".mix") if home else "",
        # 修复：id 是 swiftpm_config（不是 swiftpm），之前 key 错配导致永远匹配不上
        "swiftpm_config": os.path.join(home, ".swiftpm") if home else "",
        "gem_home": os.path.join(home, ".gem") if home else "",
        "vscode_ext": os.path.join(home, ".vscode", "extensions") if home else "",
        # 新增：conda 默认安装位置（Anaconda/Miniconda 都用这个）
        # 注：conda 可能装在 C:\ProgramData\anaconda3（系统级）或 C:\Users\xxx\anaconda3（用户级）
        #     这里取用户级（最常见），系统级需用户手动确认
        "conda": os.path.join(home, "anaconda3") if home else "",
        # 新增：R 语言用户库默认位置（父目录，版本号子目录由 R 自动创建）
        "r_libs": os.path.join(home, "Documents", "R", "win-library") if home else "",
        # 新增：Docker Desktop WSL 数据位置（新版 ext4.vhdx 在 wsl\disk\ 下）
        "docker_data": os.path.join(local_appdata, "Docker", "wsl", "disk"),
        # 新增：Visual Studio 默认安装位置
        "visual_studio": os.path.join(program_files, "Microsoft Visual Studio"),
        # pip_install：路径含 Python 版本号（如 Python311\Lib\site-packages），无法静态确定
        # wsl_distros：Packages 目录不专属 WSL（包含所有 UWP 应用），无法定位到具体发行版
    }
    return DEFAULT_C_PATHS.get(tool_id, "")


# ========== 状态判断函数 ==========

def is_path_on_c(path):
    """判断路径是否在 C 盘"""
    if not path:
        return False
    # 去掉 \\?\ 前缀
    p = path.replace("\\\\?\\", "").replace("\\\\.", "")
    p = p.replace("/", "\\").lower()
    return p.startswith("c:") or p.startswith("c\\")


def is_already_configured(tool, target_drive):
    """判断工具是否已配置到目标盘
    识别符号链接：如果当前路径是符号链接指向目标盘，也算已配置
    """
    path_fn = CURRENT_PATH_FUNCS.get(tool["current_path_fn"])
    if not path_fn:
        return False
    current = path_fn()
    if not current:
        return False
    # 识别符号链接：如果是符号链接，用目标路径判断
    try:
        from utils import is_symlink, get_symlink_target
        if is_symlink(current):
            target = get_symlink_target(current)
            if target:
                current = target
    except Exception as e:
        _log.debug("忽略异常: %s", e)
    target = target_drive.lower()
    cur = current.replace("/", "\\").lower()
    return cur.startswith(target + ":") or cur.startswith(target + "\\")


def get_migrated_tool_path(tool, migrated_records):
    """通用：检查工具的 C 盘默认路径是否已被迁移，返回实际迁移目标路径

    用于：
    1. get_tool_status: 让"当前路径"列显示实际迁移目标，而非环境变量值
    2. _auto_config_dev_env_after_migrate: 自动配置环境变量时用实际路径

    匹配规则（路径全部规范化为小写+反斜杠比较）：
    - 工具 C 盘路径 == 迁移源路径 → 返回迁移目标路径
    - 工具 C 盘路径是迁移源路径的子目录 → 返回 迁移目标 + 相对子路径
      （如工具路径 C:\\...\\Android\\Sdk，迁移源 C:\\...\\Android，迁移目标 D:\\xxx\\appdata
       → 返回 D:\\xxx\\appdata\\Sdk）

    :param tool: TOOLS 中的 dict
    :param migrated_records: list of dict，每项含 src/dst（迁移记录）
    :return: 实际迁移目标路径（字符串），未匹配返回 ""
    """
    try:
        tool_c_path = get_tool_default_c_path(tool)
        if not tool_c_path:
            return ""
        norm_tool = tool_c_path.replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
        for m in migrated_records or []:
            m_src = (m.get("src") or "").replace("\\\\?\\", "").replace("/", "\\").lower().rstrip("\\")
            m_dst = (m.get("dst") or "").replace("\\\\?\\", "").replace("/", "\\").rstrip("\\")
            if not m_src or not m_dst:
                continue
            # 精确匹配：工具 C 盘路径 == 迁移源路径
            if norm_tool == m_src:
                return m_dst
            # 子目录匹配：工具 C 盘路径是迁移源路径的子目录
            # 如 tool=C:\...\Android\Sdk, src=C:\...\Android → dst=\Sdk
            if norm_tool.startswith(m_src + "\\"):
                rel = tool_c_path[len(m_src):].lstrip("\\").rstrip()
                # 用原始大小写的相对路径拼接（保留 Sdk 等大写）
                return os.path.join(m_dst, rel) if rel else m_dst
    except Exception as e:
        _log.error(f"get_migrated_tool_path 异常: {e}")
    return ""


# ========== 应用配置函数 ==========

def _read_user_env_orig(name):
    """读取用户环境变量原值（apply_tool 回滚用）；不存在/读取失败返回 None。

    读取失败按"原值不存在"处理（回滚时删除），并记日志便于追踪。
    """
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
        try:
            value, _ = winreg.QueryValueEx(key, name)
            return value
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        _log.warning(f"读取环境变量原值失败 {name}: {e}")
        return None


def set_user_env_var(name, value):
    """设置用户级环境变量（写入注册表 HKCU\\Environment）
    同步更新当前进程的 os.environ，使本程序内立即读到新值；
    同时广播 WM_SETTINGCHANGE 让其他程序感知到变化。
    返回 (成功?, 错误信息)

    安全检查：对路径类环境变量（_PATH_ENV_VARS）校验 value 是否为有效路径，
    防止历史 bug 重演（如把 "0 MB" 写进 ANDROID_HOME）。
    """
    # 安全校验：路径类环境变量必须是有效路径（X:\ 或 %XXX% 格式）
    if name in _PATH_ENV_VARS and not _is_valid_path(value):
        err = f"拒绝写入无效路径到 {name}: {value!r}（必须是 X:\\ 或 %XXX% 格式的路径）"
        _log.error(err)
        return False, err
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment",
                             0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, name, 0, winreg.REG_EXPAND_SZ, value)
        winreg.CloseKey(key)
        # 同步到当前进程，避免配置后刷新表格仍读到旧值（如 Android SDK 显示仍在 C 盘）
        os.environ[name] = value
        # 广播 WM_SETTINGCHANGE 让其他程序感知到环境变量变化
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", 0x2, 1000, None)
        return True, ""
    except Exception as e:
        return False, str(e)


def apply_tool(tool, target_drive, target_path_override=None):
    """应用单个工具的配置
    :param tool: TOOLS 中的某个 dict
    :param target_drive: 目标盘符（如 "D"）
    :param target_path_override: 实际目标路径（如用户在普通迁移区迁到了非默认目录）
           - 为 None 时用默认模板路径（如 D:\\dev\\android\\sdk）
           - 非 None 时用此路径覆盖所有环境变量和配置命令的路径参数
    :return: (成功?, 消息)
    """
    # H9: 盘符校验（防 shell 注入）：仅接受单字母 A-Z
    if not isinstance(target_drive, str) or len(target_drive) != 1 \
            or not ('A' <= target_drive.upper() <= 'Z'):
        return False, "非法目标盘符（仅支持单个字母，如 D）"
    # 清空 detect/path 缓存（配置变更后路径会变，下次刷新需重新检测）
    clear_detect_path_cache()
    # 特殊工具不支持自动配置
    if tool["special"] in ("pip", "docker", "wsl", "vs"):
        return False, "此工具无法自动配置，请查看清理指引手动处理"

    # 目标盘符带冒号（如 "D:"），拼成绝对路径 "D:\\dev\\xxx"
    target = target_drive.upper() + ":"
    msgs = []

    # 记录实际使用的路径（用于配置记录和创建目录）
    # target_path_override 非 None 时，所有环境变量都用这个路径
    # 但一个工具可能有多个环境变量（如 ANDROID_HOME 和 ANDROID_SDK_ROOT），
    # 它们通常指向同一目录，统一用 override 路径即可
    override = target_path_override.rstrip("\\/") if target_path_override else None

    # 1. 设置环境变量（任一失败回滚已设置的变量：原值存在→恢复原值，不存在→删除）
    applied = []  # [(name, 原值 or None)]
    for ev in tool["env_vars"]:
        if override:
            value = override
        else:
            value = ev["default_value_template"].replace("{D}", target)
        orig = _read_user_env_orig(ev["name"])
        ok, err = set_user_env_var(ev["name"], value)
        if ok:
            msgs.append(f"✓ 环境变量 {ev['name']} = {value}")
            applied.append((ev["name"], orig))
        else:
            msgs.append(f"✗ 设置 {ev['name']} 失败: {err}")
            # 回滚：逆序恢复已设置的变量，不留半配置状态
            for name, old in reversed(applied):
                if old is None:
                    r_ok, r_err = remove_user_env_var(name)
                else:
                    r_ok, r_err = set_user_env_var(name, old)
                if not r_ok:
                    _log.warning(f"回滚环境变量失败 {name}: {r_err}")
            return False, "\n".join(msgs)

    # 2. 执行配置命令（如 npm config set）
    for cmd_info in tool["config_commands"]:
        if override:
            # 有 override 时，命令中最后一个路径参数用 override 替换
            cmd = list(cmd_info["cmd_template"])
            if len(cmd) > 1:
                cmd[-1] = override
            else:
                cmd = [c.replace("{D}", target) for c in cmd]
        else:
            cmd = [c.replace("{D}", target) for c in cmd_info["cmd_template"]]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=10, shell=False,
                               encoding='utf-8', errors='ignore',
                               creationflags=_NO_WINDOW_FLAGS)
            if r.returncode == 0:
                msgs.append(f"✓ {cmd_info['desc']}: {' '.join(cmd)}")
            else:
                # 某些命令（如 conda config --add）重复执行会报错，但配置已生效
                stderr = (r.stderr or "").strip()
                if "already" in stderr.lower() or "exist" in stderr.lower():
                    msgs.append(f"✓ {cmd_info['desc']} (已配置过)")
                else:
                    msgs.append(f"✗ {cmd_info['desc']} 失败: {stderr}")
                    return False, "\n".join(msgs)
        except Exception as e:
            msgs.append(f"✗ {cmd_info['desc']} 异常: {e}")
            return False, "\n".join(msgs)

    # 3. Maven 特殊处理：改 settings.xml
    if tool["id"] == "maven_repo":
        maven_path = override if override else f"{target}\\dev\\java\\maven-repo"
        ok, msg = _configure_maven_settings(target, repo_path_override=maven_path if override else None)
        if ok:
            msgs.append(f"✓ Maven settings.xml 配置 localRepository = {maven_path}")
        else:
            msgs.append(f"✗ Maven settings.xml 配置失败: {msg}")
            return False, "\n".join(msgs)

    # 3.1 Bazel 特殊处理：改 .bazelrc 设置 output_user_root
    if tool["id"] == "bazel_output":
        bazel_path = override if override else f"{target}/dev/bazel/root"
        ok, msg = _configure_bazelrc(target, root_path_override=bazel_path if override else None)
        if ok:
            msgs.append(f"✓ .bazelrc 配置 output_user_root = {bazel_path}")
        else:
            msgs.append(f"✗ .bazelrc 配置失败: {msg}")
            return False, "\n".join(msgs)

    # 4. 创建目标目录
    target_dirs = set()
    if override:
        target_dirs.add(override)
    else:
        for ev in tool["env_vars"]:
            target_dirs.add(ev["default_value_template"].replace("{D}", target))
        for cmd_info in tool["config_commands"]:
            if len(cmd_info["cmd_template"]) > 1:
                # 最后一个参数是路径
                target_dirs.add(cmd_info["cmd_template"][-1].replace("{D}", target))
    for d in target_dirs:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            _log.debug("忽略异常: %s", e)

    # 5. 记录实际使用的目标路径到 dev_env_configured（供 unapply 和状态显示用）
    try:
        # 注意：调用方负责把 dev_env_configured 写入 config.json
        # 这里只返回消息，实际记录由调用方处理
        if override:
            msgs.append(f"ℹ️ 实际目标路径: {override}")
    except Exception as e:
        _log.debug("忽略异常: %s", e)

    return True, "\n".join(msgs) if msgs else "配置完成"


def unapply_tool(tool, target_drive):
    """撤销单个工具的配置（一键还原/回滚）
    顺序：1.删除环境变量 2.执行 unconfig_commands 3.还原 Maven/Bazel 配置文件
    :param tool: TOOLS 中的某个 dict
    :param target_drive: 当初配置时用的目标盘符（如 "D"）
    :return: (成功?, 消息)
    """
    # H9: 盘符校验（防 shell 注入）：仅接受单字母 A-Z
    if not isinstance(target_drive, str) or len(target_drive) != 1 \
            or not ('A' <= target_drive.upper() <= 'Z'):
        return False, "非法目标盘符（仅支持单个字母，如 D）"
    # 清空 detect/path 缓存（撤销后路径会变回 C 盘，下次刷新需重新检测）
    clear_detect_path_cache()
    target = target_drive.upper() + ":"
    msgs = []

    # 1. 删除环境变量
    for ev in tool["env_vars"]:
        ok, err = remove_user_env_var(ev["name"])
        if ok:
            msgs.append(f"✓ 已删除环境变量 {ev['name']}")
        else:
            msgs.append(f"✗ 删除 {ev['name']} 失败: {err}")

    # 2. 执行撤销配置命令（如 npm config delete）
    for cmd_info in tool.get("unconfig_commands", []):
        cmd = [c.replace("{D}", target) for c in cmd_info["cmd_template"]]
        try:
            # 用 _run_cmd 统一调用（已处理 .cmd 脚本和黑框抑制）
            _run_cmd(cmd, timeout=10)
            # 撤销命令一般返回 0 即使没配置过，不严格判断成功
            msgs.append(f"✓ {cmd_info['desc']}")
        except Exception as e:
            msgs.append(f"✗ {cmd_info['desc']} 异常: {e}")

    # 3. Maven 特殊处理：从 settings.xml 移除 localRepository
    if tool["id"] == "maven_repo":
        ok, msg = _unconfigure_maven_settings()
        if ok:
            msgs.append(f"✓ Maven settings.xml 已移除 localRepository（恢复默认 ~/.m2/repository）")
        else:
            msgs.append(f"✗ Maven settings.xml 还原失败: {msg}")

    # 3.1 Bazel 特殊处理：从 .bazelrc 移除 output_user_root
    if tool["id"] == "bazel_output":
        ok, msg = _unconfigure_bazelrc()
        if ok:
            msgs.append(f"✓ .bazelrc 已移除 output_user_root（恢复默认）")
        else:
            msgs.append(f"✗ .bazelrc 还原失败: {msg}")

    # 不删除 D 盘数据目录（用户数据无价，由用户手动删）
    msgs.append("ℹ️ D 盘的数据目录未删除（保留用户数据），如需清理请右键『删除 D 盘目录』")

    return True, "\n".join(msgs) if msgs else "已还原"


def _unconfigure_maven_settings():
    """从 Maven settings.xml 移除 localRepository 标签（恢复默认 ~/.m2/repository）"""
    settings_path = _home(".m2", "settings.xml")
    if not os.path.exists(settings_path):
        return True, "settings.xml 不存在，无需还原"
    try:
        import re
        with open(settings_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # 移除 localRepository 标签（含前后空白）
        new_content = re.sub(r'\s*<localRepository>[^<]*</localRepository>\s*', '\n  ', content)
        if new_content != content:
            with open(settings_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            return True, "已移除 localRepository"
        return True, "未找到 localRepository，无需还原"
    except Exception as e:
        return False, str(e)


def _unconfigure_bazelrc():
    """从 .bazelrc 移除 output_user_root 配置行（恢复默认）"""
    bazelrc_path = _home(".bazelrc")
    if not os.path.exists(bazelrc_path):
        return True, ".bazelrc 不存在，无需还原"
    try:
        with open(bazelrc_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        new_lines = [line for line in lines
                     if not ("output_user_root" in line and line.strip().startswith("startup"))]
        if len(new_lines) != len(lines):
            with open(bazelrc_path, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            return True, "已移除 output_user_root"
        return True, "未找到 output_user_root，无需还原"
    except Exception as e:
        return False, str(e)


def _configure_maven_settings(target, repo_path_override=None):
    """配置 Maven 的 settings.xml 设置 localRepository

    :param target: 盘符带冒号（如 "D:"），用于生成默认路径
    :param repo_path_override: 自定义仓库路径，None 时用默认 {target}\\dev\\java\\maven-repo
    """
    m2_dir = _home(".m2")
    os.makedirs(m2_dir, exist_ok=True)
    settings_path = os.path.join(m2_dir, "settings.xml")
    repo_path = repo_path_override.rstrip("\\/") if repo_path_override else f"{target}\\dev\\java\\maven-repo"

    # 如果 settings.xml 已存在，尝试修改 localRepository
    if os.path.exists(settings_path):
        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                content = f.read()
            import re
            # 已有 localRepository 标签 → 替换
            if '<localRepository>' in content:
                content = re.sub(
                    r'<localRepository>[^<]*</localRepository>',
                    f'<localRepository>{repo_path}</localRepository>',
                    content)
                with open(settings_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                return True, "已更新 localRepository"
            # 没 localRepository 但有 settings 标签 → 插入
            if '<settings' in content:
                content = re.sub(
                    r'(<settings[^>]*>)',
                    rf'\1\n  <localRepository>{repo_path}</localRepository>',
                    content, count=1)
                with open(settings_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                return True, "已插入 localRepository"
        except Exception as e:
            return False, str(e)

    # 不存在 → 创建最小化的 settings.xml
    try:
        with open(settings_path, 'w', encoding='utf-8') as f:
            f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0
          http://maven.apache.org/xsd/settings-1.0.0.xsd">
  <localRepository>{repo_path}</localRepository>
</settings>
""")
        return True, "已创建 settings.xml"
    except Exception as e:
        return False, str(e)


def _configure_bazelrc(target, root_path_override=None):
    """配置 Bazel 的 .bazelrc 设置 output_user_root

    :param target: 盘符带冒号（如 "D:"），用于生成默认路径
    :param root_path_override: 自定义 root 路径，None 时用默认 {target}/dev/bazel/root
    """
    bazelrc_path = _home(".bazelrc")
    # 用正斜杠避免转义问题
    root_path = root_path_override.replace("\\", "/").rstrip("/") if root_path_override else f"{target}/dev/bazel/root"
    new_line = f"startup --output_user_root={root_path}"

    try:
        # 如果 .bazelrc 已存在，检查是否已配置过
        if os.path.exists(bazelrc_path):
            with open(bazelrc_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            # 已有 output_user_root 配置 → 替换
            found = False
            for i, line in enumerate(lines):
                if "output_user_root" in line and line.strip().startswith("startup"):
                    lines[i] = new_line + "\n"
                    found = True
                    break
            if not found:
                # 追加到文件末尾
                if lines and not lines[-1].endswith("\n"):
                    lines.append("\n")
                lines.append(new_line + "\n")
            with open(bazelrc_path, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            return True, "已更新 .bazelrc"
        else:
            # 创建新文件
            with open(bazelrc_path, 'w', encoding='utf-8') as f:
                f.write(new_line + "\n")
            return True, "已创建 .bazelrc"
    except Exception as e:
        return False, str(e)


def get_tool_status(tool, target_drive, migrated_records=None):
    """获取工具状态信息
    :param migrated_records: 迁移记录列表（config.json 的 migrated 字段）
           如果传入，会检查工具的 C 盘默认路径是否已被迁移，
           若是则 current_path 显示实际迁移目标路径（而非环境变量值）
    :return: dict {
        installed: 是否安装,
        current_path: 当前路径（符号链接已解析为目标，或迁移目标路径）,
        original_path: 原始路径（符号链接解析前，可能是 C 盘）,
        is_symlink: 当前路径是否是符号链接,
        symlink_target: 符号链接目标,
        on_c: 是否在 C 盘,
        configured: 是否已配置到目标盘（识别符号链接）,
        migrated_target: 实际迁移目标路径（未迁移则为空）,
    }
    """
    detect_fn = DETECT_FUNCS.get(tool["detect"])
    path_fn = CURRENT_PATH_FUNCS.get(tool["current_path_fn"])
    installed = detect_fn() if detect_fn else False
    # 性能优化：未安装的工具不调 path_fn（path_fn 内部会再调 detect_fn，重复且无意义）
    # path_fn 只在工具已安装时才调用，避免 26+ 个 subprocess 串行启动
    if installed and path_fn:
        current_path = path_fn()
    else:
        current_path = ""
    # fallback：如果已安装但路径为空，用 shutil.which() 找可执行文件所在目录
    if installed and not current_path:
        current_path = _fallback_path_by_tool_id(tool.get("id", ""))
    # 统一路径分隔符为反斜杠（Windows 习惯），去掉 \\?\ 前缀
    if current_path:
        current_path = current_path.replace("\\\\?\\", "").replace("/", "\\")

    # 通用：检查工具的 C 盘默认路径是否已被普通迁移区迁移
    # 若是，current_path 用实际迁移目标路径（而非环境变量值或默认 C 盘路径）
    # 这样表格"当前路径"列能反映数据的真实位置
    migrated_target = ""
    if migrated_records is not None:
        try:
            migrated_target = get_migrated_tool_path(tool, migrated_records)
            if migrated_target:
                # 优先用迁移目标路径，但保留 path_fn 返回的路径用于符号链接检测
                # 注意：迁移目标路径本身不是符号链接，是真实数据目录
                current_path = migrated_target.replace("\\\\?\\", "").replace("/", "\\")
        except Exception as e:
            _log.error(f"get_tool_status 检查迁移记录异常: {e}")

    # 识别符号链接：如果是符号链接，记录原始路径和目标，current_path 用目标
    is_sym = False
    symlink_target = ""
    original_path = current_path
    if current_path:
        try:
            from utils import is_symlink, get_symlink_target
            if is_symlink(current_path):
                is_sym = True
                symlink_target = get_symlink_target(current_path)
                if symlink_target:
                    current_path = symlink_target.replace("/", "\\")
        except Exception as e:
            _log.debug("忽略异常: %s", e)
    return {
        "installed": installed,
        "current_path": current_path,
        "original_path": original_path,
        "is_symlink": is_sym,
        "symlink_target": symlink_target,
        "on_c": is_path_on_c(current_path),
        "configured": is_already_configured(tool, target_drive),
        "migrated_target": migrated_target,
        "size_mb": 0,  # 默认 0，实际大小由 DevEnvSizeWorker 异步计算后填入表格
    }


# 工具 id → 可执行文件名映射（用于 path 函数返回空时的 fallback）
_TOOL_EXE_MAP = {
    "npm_global": "npm",
    "npm_cache": "npm",
    "yarn_global": "yarn",
    "pnpm_global": "pnpm",
    "pip_install": "pip",
    "pip_cache": "pip",
    "conda": "conda",
    "cargo_home": "cargo",
    "rustup_home": "rustup",
    "gopath": "go",
    "gocache": "go",
    "gomodcache": "go",
    "dotnet_tools": "dotnet",
    "nuget_cache": "dotnet",
    "gradle_home": "gradle",
    "maven_repo": "mvn",
    "gem_home": "gem",
    "julia_depot": "julia",
    "composer_cache": "composer",
    "pub_cache": "dart",
    "r_libs": "R",
    "terraform_cache": "terraform",
    "conan_home": "conan",
    "vcpkg_root": "vcpkg",
    "bazel_output": "bazel",
    "stack_root": "stack",
    "coursier_cache": "cs",
    "opam_root": "opam",
    "nimble_dir": "nimble",
    "mix_home": "elixir",
    "swiftpm_config": "swift",
    "electron_cache": "electron",
}


def _fallback_path_by_tool_id(tool_id):
    """当路径函数返回空但工具已安装时，用 shutil.which() 找可执行文件所在目录"""
    exe_name = _TOOL_EXE_MAP.get(tool_id)
    if not exe_name:
        return ""
    exe_path = shutil.which(exe_name)
    if not exe_path:
        # 试一些变体
        for variant in [exe_name + ".exe", exe_name + ".cmd", exe_name + ".bat"]:
            exe_path = shutil.which(variant)
            if exe_path:
                break
    if not exe_path:
        return ""
    # 返回可执行文件所在目录
    return os.path.dirname(exe_path)


def get_suggest_path(tool, target_drive):
    """获取工具的建议新路径（用于显示和打开目录）"""
    # 带冒号拼成绝对路径
    target = target_drive.upper() + ":"
    if tool["env_vars"]:
        return tool["env_vars"][0]["default_value_template"].replace("{D}", target)
    if tool["config_commands"]:
        return tool["config_commands"][0]["cmd_template"][-1].replace("{D}", target)
    # 特殊工具的固定路径（保持与 TOOLS 中路径模板一致，按类别分子目录）
    special_paths = {
        "maven_repo": f"{target}\\dev\\java\\maven-repo",
        "npm_global": f"{target}\\dev\\nodejs\\npm-global",
        "npm_cache": f"{target}\\dev\\nodejs\\npm-cache",
        "yarn_global": f"{target}\\dev\\nodejs\\yarn-global",
        "docker_data": f"{target}\\dev\\docker",
        "wsl_distros": f"{target}\\dev\\wsl",
        # pip 装到 Python 的 site-packages，建议路径指向 Python 安装目录
        "pip_install": f"{target}\\dev\\python\\python",
        # Bazel 输出目录
        "bazel_output": f"{target}\\dev\\bazel\\output",
        # Visual Studio 安装目录（需 VS Installer，建议路径仅作参考）
        "visual_studio": f"{target}\\dev\\visualstudio",
    }
    # 此时 target 已带冒号，无需再处理
    return special_paths.get(tool["id"], "")


def _registry_env_exists(name):
    """环境变量是否存在于用户或系统注册表（HKCU 优先，HKLM 兜底）

    :return: True=注册表有该变量（真实配置）；False=注册表无（可能是残留）
    """
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(root, "Environment")
            try:
                winreg.QueryValueEx(key, name)
                return True
            except FileNotFoundError:
                pass
            finally:
                winreg.CloseKey(key)
        except FileNotFoundError:
            continue  # HKLM\Environment 不存在（部分系统无此键）
        except Exception:
            return True  # 注册表读取失败：保守返回 True（不清除，避免误删系统配置）
    return False


def clean_env_var_residues():
    """启动自愈：清除软件管理的工具环境变量残留

    实测事故（2026-08-13）：配置时 set_user_env_var 把值同步写入了当时进程的
    os.environ（dev_env_migrate.py set_user_env_var），注册表随后被外部清理，
    从旧进程链启动的软件继承残留，导致检测显示旧路径（如 H:\\ceshi软件）。

    判断：进程 os.environ 有值，但注册表（HKCU + HKLM）均无该变量
    → 视为残留，从当前进程环境清除，让检测回落到默认路径。

    安全边界：
    - 只处理本软件管理的工具变量名（TOOLS 的 env_vars，34 个），不碰其他变量
    - 注册表有值则不动（用户/软件的真实配置）
    - 只改当前进程 os.environ（内存），不改注册表、不影响其他进程
    :return: 被清除的变量名列表
    """
    managed = set()
    for tool in TOOLS:
        for ev in tool.get("env_vars", []):
            managed.add(ev["name"])
    cleaned = []
    for name in managed:
        if name not in os.environ:
            continue
        if _registry_env_exists(name):
            continue  # 注册表有值：真实配置，不碰
        # 注册表无值但进程有 → 残留，清除
        os.environ.pop(name, None)
        cleaned.append(name)
    if cleaned:
        _log.info(f"启动自愈：清除 {len(cleaned)} 个环境变量残留: {cleaned}")
    return cleaned


def remove_user_env_var(name):
    """删除用户级环境变量
    :return: (成功?, 错误信息)
    """
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment",
                             0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass  # 不存在也算成功
        winreg.CloseKey(key)
        # 同步从当前进程移除，避免撤销后刷新表格仍读到旧值
        os.environ.pop(name, None)
        # 广播变化
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", 0x2, 1000, None)
        return True, ""
    except Exception as e:
        return False, str(e)


def unconfigure_tool(tool):
    """取消工具配置（删除环境变量，不动文件）
    用于卸载开发工具时清理环境变量
    :return: (成功?, 消息)
    """
    # 清空 detect/path 缓存（环境变量删除后路径会变，下次刷新需重新检测）
    clear_detect_path_cache()
    msgs = []
    for ev in tool["env_vars"]:
        ok, err = remove_user_env_var(ev["name"])
        if ok:
            msgs.append(f"✓ 已删除环境变量 {ev['name']}")
        else:
            msgs.append(f"✗ 删除 {ev['name']} 失败: {err}")
    return True, "\n".join(msgs) if msgs else "无环境变量需要删除"


def collect_original_dir_structure():
    """收集所有开发工具在 C 盘的原始目录结构（迁移成符号链接前的状态）

    用于快照保存：记录每个工具的原始路径、是否存在、是否已变成符号链接、大小等。
    恢复快照时可以对比当前状态，看出哪些目录被迁移了。

    :return: list of dict，每项格式：
        {
            "tool_id": "npm_cache",
            "name": "npm 缓存",
            "category": "Node.js",
            "original_path": "C:\\Users\\<用户名>\\AppData\\Local\\npm-cache",
            "exists": True,
            "is_symlink": False,
            "symlink_target": "",
            "size_mb": 0,  # 启动时不计算，避免卡死
            "on_c": True
        }
    """
    from utils import is_symlink, get_symlink_target
    results = []
    for tool in TOOLS:
        path_fn = CURRENT_PATH_FUNCS.get(tool["current_path_fn"])
        if not path_fn:
            continue
        try:
            current_path = path_fn()
        except Exception:
            current_path = ""
        # fallback：路径为空但工具已安装时，用 shutil.which 找
        if not current_path:
            try:
                detect_fn = DETECT_FUNCS.get(tool["detect"])
                installed = detect_fn() if detect_fn else False
            except Exception:
                installed = False
            if installed:
                current_path = _fallback_path_by_tool_id(tool.get("id", ""))
        # 路径规范化（去掉 \\?\ 前缀）
        if current_path and current_path.startswith("\\\\?\\"):
            current_path = current_path[4:]
        # 收集状态
        exists = bool(current_path) and os.path.exists(current_path)
        is_link = is_symlink(current_path) if exists else False
        target = get_symlink_target(current_path) if is_link else ""
        results.append({
            "tool_id": tool.get("id", ""),
            "name": tool.get("name", ""),
            "category": tool.get("category", ""),
            "original_path": current_path or "",
            "exists": exists,
            "is_symlink": is_link,
            "symlink_target": target,
            "size_mb": 0,  # 启动时不计算，避免遍历大目录卡死
            "on_c": is_path_on_c(current_path) if current_path else False,
        })
    return results


# ========== 数据迁移功能（开发环境配置时同时迁移 C 盘数据到 D 盘）==========

def get_tool_data_info(tool):
    """获取工具当前数据路径的信息（用于弹窗显示"检测到 X MB 数据"）

    通用：无论数据在 C 盘还是非 C 盘，都返回真实信息，让 UI 决定是否迁移。
    （支持 D → D 迁移场景：用户之前迁到了非默认目录，现在想搬到另一个目录）

    :return: dict {
        has_data: bool,          # 是否有数据可迁移（数据存在且不是符号链接）
        is_symlink: bool,        # 当前路径是否已是符号链接（已被待迁移区搬过）
        symlink_target: str,     # 如果是符号链接，指向哪里
        size_mb: float,          # 数据大小（MB），仅在 has_data=True 时计算
        source_path: str,        # 当前数据路径（可能是 C 盘或非 C 盘）
        on_c: bool,              # 数据是否在 C 盘
        message: str,            # 提示消息
    }
    """
    from utils import is_symlink, get_symlink_target, get_dir_size_fast

    path_fn = CURRENT_PATH_FUNCS.get(tool["current_path_fn"])
    try:
        source_path = path_fn() if path_fn else ""
    except Exception:
        source_path = ""
    if not source_path:
        # fallback
        source_path = _fallback_path_by_tool_id(tool.get("id", ""))

    # 规范化路径
    if source_path and source_path.startswith("\\\\?\\"):
        source_path = source_path[4:]

    if not source_path:
        return {"has_data": False, "is_symlink": False, "symlink_target": "",
                "size_mb": 0, "source_path": "", "on_c": False,
                "message": "无法确定源路径"}

    # 检查是否已是符号链接（待迁移区已搬过）
    if is_symlink(source_path):
        target = get_symlink_target(source_path)
        return {"has_data": False, "is_symlink": True, "symlink_target": target,
                "size_mb": 0, "source_path": source_path, "on_c": is_path_on_c(source_path),
                "message": f"数据已通过待迁移区迁移到 {target}"}

    # 检查路径是否存在
    if not os.path.exists(source_path):
        return {"has_data": False, "is_symlink": False, "symlink_target": "",
                "size_mb": 0, "source_path": source_path, "on_c": is_path_on_c(source_path),
                "message": "无现有数据，无需迁移"}

    # 计算大小（无论在哪个盘都算，让 UI 能显示数据量）
    try:
        size_mb = get_dir_size_fast(source_path)
    except Exception:
        size_mb = 0

    on_c = is_path_on_c(source_path)
    if on_c:
        msg = f"检测到 C 盘有 {size_mb:.1f} MB 数据"
    else:
        msg = f"检测到 {size_mb:.1f} MB 数据（当前在 {source_path}）"

    return {"has_data": True, "is_symlink": False, "symlink_target": "",
            "size_mb": size_mb, "source_path": source_path, "on_c": on_c,
            "message": msg}


def migrate_tool_data(tool, target_drive, config=None, source_path_override=None,
                       target_path_override=None, log_callback=None,
                       force_overwrite=False, merge=False):
    """迁移工具数据到目标路径（通用：支持 C→D / D→D / D→E / E→F 等任意盘间迁移）

    通用迁移逻辑：
    1. 确定源路径（优先 source_path_override，否则读 path_fn）
    2. 确定目标路径（优先 target_path_override，否则用 get_suggest_path）
    3. 如果源是符号链接 → 解析出真实数据路径作为源
    4. 如果源和目标相同 → 跳过
    5. robocopy 复制数据 + 删除源目录 + 创建符号链接 + 记录 migrated

    在 apply_tool 配置完环境变量后调用此函数，把现有数据搬到目标路径，
    然后在源位置建符号链接指向目标。

    :param tool: TOOLS 中的 dict
    :param target_drive: 目标盘符（如 "D"），仅用于 fallback 计算目标路径
    :param config: state.json 配置字典（用于记录 migrated），可为 None
    :param source_path_override: 外部传入的源路径（在 apply_tool 改环境变量前捕获）
    :param target_path_override: 外部传入的目标路径（用户在表格中修改过的路径）
    :param log_callback: 日志回调
    :param force_overwrite: True=跳过目标非空检测（配合 merge 使用，开发环境区覆盖确认后）
    :param merge: True=合并复制（目标中源没有的文件保留，不 purge；同名覆盖）
    :return: (成功?, 消息, 迁移记录 dict or None)
    """
    # 清空 detect/path 缓存（迁移后源位置变符号链接，下次刷新需重新检测）
    clear_detect_path_cache()
    # 特殊工具不支持数据迁移
    if tool.get("special") in ("pip", "docker", "wsl", "vs"):
        return False, "此工具不支持数据迁移（特殊工具）", None

    from utils import is_symlink as _is_symlink, get_symlink_target as _get_target

    # ===== 1. 确定源路径 =====
    if source_path_override:
        source_path = source_path_override.replace("\\\\?\\", "")
    else:
        info = get_tool_data_info(tool)
        source_path = info.get("source_path", "").replace("\\\\?\\", "")

    if not source_path:
        return False, "无法确定源路径", None

    # 如果源是符号链接，解析出真实数据路径
    # （用户之前可能用待迁移区迁移过，源位置已经是符号链接）
    try:
        if _is_symlink(source_path):
            real_target = _get_target(source_path)
            if real_target:
                # 真实数据在 real_target，迁移时要把 real_target 的数据搬到新目标
                # 然后把符号链接重新指向新目标
                # 但这里更简单的做法：直接把 real_target 作为源路径
                source_path = real_target.replace("\\\\?\\", "")
    except Exception as e:
        _log.debug("忽略异常: %s", e)

    # ===== 2. 确定目标路径（提前到源==目标比较之前）=====
    if target_path_override:
        target_path = target_path_override.replace("\\\\?\\", "")
    else:
        target_path = get_suggest_path(tool, target_drive)
    if not target_path:
        return False, "无法确定目标路径", None

    # 规范化路径
    if source_path.startswith("\\\\?\\"):
        source_path = source_path[4:]
    if target_path.startswith("\\\\?\\"):
        target_path = target_path[4:]

    # ===== 2.5 源==目标检查（先于源存在检查，防"无数据可迁移"假成功）=====
    # 源==目标说明环境变量已指向目标路径（可能是残留或重复配置），
    # 提示另选新路径，而非静默"无需迁移"成功
    # （避免数据未迁移的假成功：环境变量指向目标但 C 盘真实数据没搬过去）
    if os.path.normpath(source_path).lower() == os.path.normpath(target_path).lower():
        return False, ("当前路径已是目标路径，请另外选择一个新的目标路径：\n"
                       f"  {source_path}\n"
                       f"（环境变量可能已指向该路径；若数据确已在目标位置，"
                       f"请先撤销配置再重新配置，或直接使用该路径无需迁移）"), None

    # 检查源路径是否存在
    if not os.path.exists(source_path):
        return True, f"源路径不存在（{source_path}），无数据可迁移", None

    # ===== 4. 复用 Migrator.migrate() =====
    # Migrator.migrate 内部：robocopy /MIR 复制 + 删源 + mklink + 记录 migrated
    # 注意：Migrator.migrate 会在源位置创建符号链接指向目标
    # 如果源在非 C 盘（如 D→E 迁移），同样在源位置创建符号链接
    if config is not None:
        from migrator import Migrator
        migrator = Migrator(config)
        # 接收外部 log_callback（开发工具迁移区专用，普通调用为 None）
        if log_callback is not None:
            migrator.log_callback = log_callback
        ok, msg = migrator.migrate(source_path, target_path,
                                   force_overwrite=force_overwrite, merge=merge)
        # 从 config["migrated"] 中查找刚写入的记录
        record = None
        if ok:
            for m in config.get("migrated", []):
                if m.get("src") == source_path:
                    record = m
                    break
        return ok, msg, record

    # config 为 None 时无法调用 Migrator（需要 config 记录 migrated）
    return False, "config 为空，无法迁移数据", None

