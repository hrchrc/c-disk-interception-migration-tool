#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QThread Worker 类（从 main.py 抽出）

包含：
- DevEnvRefreshWorker：开发环境工具状态检测（并行+流式）
- DevEnvSizeWorker：已废弃（大小计算已合并到 DevEnvRefreshWorker）
- DevToolDownloadWorker：开发工具安装包下载
- DevEnvApplyWorker：批量应用开发工具配置
- _DEV_TOOL_DOWNLOAD_APIS：下载 API 配置字典
- _get_arch_suffix：系统架构标识
"""
import os
import logging
from PySide6.QtCore import QThread, Signal

log = logging.getLogger('CDriveRelocator')

from dev_env_migrate import (
    get_tool_status as dev_get_tool_status,
    get_tool_data_info as dev_get_tool_data_info,
    apply_tool as dev_apply_tool,
    migrate_tool_data as dev_migrate_tool_data,
)


# ========== 开发环境迁移：后台 Worker ==========
# 用 QThread + Signal 实现跨线程通信，避免子线程操作 Qt 控件
class DevEnvRefreshWorker(QThread):
    """后台检测所有开发工具状态（并行检测 + 流式输出，大幅提速）

    流式刷新：每检测完一个工具就 emit row_ready_signal，主线程立即更新对应行，
    用户能看到逐行更新的进度，不用等所有工具检测完才看到表格变化。
    """
    finished_signal = Signal(list, str)  # (rows_data, target_drive) 全部完成
    row_ready_signal = Signal(object, object, str)  # (tool, status, target_drive) 单个工具就绪
    error_signal = Signal(str)  # 错误信息

    def __init__(self, tools, target_drive, config=None):
        super().__init__()
        self.tools = tools
        self.target_drive = target_drive
        self.config = config  # state.json 配置（用于读 dev_env_configured.source_path）
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        try:
            results = {}  # {tool_id: (tool, status)} 保持原顺序
            # 并行检测所有工具（subprocess 调用是 IO 密集型，线程池可大幅提速）
            # max_workers=20：26+ 个工具分两批跑完，避免串行等待 subprocess 超时
            with ThreadPoolExecutor(max_workers=20) as executor:
                future_to_tool = {}
                for tool in self.tools:
                    if self._cancelled:
                        break
                    future = executor.submit(self._detect_one, tool, self.target_drive, self.config)
                    future_to_tool[future] = tool
                for future in as_completed(future_to_tool):
                    if self._cancelled:
                        break
                    tool = future_to_tool[future]
                    try:
                        status = future.result()
                        results[tool["id"]] = (tool, status)
                        # 流式输出：单个工具就绪立即通知主线程更新该行
                        self.row_ready_signal.emit(tool, status, self.target_drive)
                    except Exception as e:
                        log.error(f"检测工具 {tool.get('id')} 失败: {e}")
                        # 失败的工具给一个默认状态，保证表格行数完整
                        default_status = {
                            "installed": False, "current_path": "",
                            "on_c": False, "configured": False, "size_mb": 0,
                        }
                        results[tool["id"]] = (tool, default_status)
                        # 失败也 emit，让主线程把"加载中..."更新为"未检测到"
                        self.row_ready_signal.emit(tool, default_status, self.target_drive)
            # 按原始 tools 顺序输出
            rows_data = []
            for tool in self.tools:
                pair = results.get(tool["id"])
                if pair:
                    rows_data.append(pair)
            self.finished_signal.emit(rows_data, self.target_drive)
        except Exception as e:
            self.error_signal.emit(str(e))

    @staticmethod
    def _detect_one(tool, target_drive, config=None):
        """检测单个工具（在工作线程中执行，含大小计算）

        大小计算直接在这里做（ThreadPoolExecutor 工作线程），MFT 模式下 O(1) 查缓存
        < 0.1 秒，os.walk 兜底也在工作线程里不卡 UI。不再需要 DevEnvSizeWorker。

        size_mb 缓存优化：路径不变时复用 dev_env_migrate._size_cache 中的缓存值
        （TTL 1 小时），避免 D 盘大目录（如 Android SDK 1.5GB / 28050 文件）反复
        os.walk 拖慢刷新。迁移/还原后会主动 clear_size_cache 清空。

        size_mb 值约定：
          >0  = 正常大小（MB）
           0  = 空目录或路径不存在
          -1  = 符号链接（已迁移）
          -2  = 路径不存在（工具已装但目录未生成）
          -3  = 计算失败

        路径选择优先级（避免 apply_tool 改环境变量后读到 D 盘空目录）：
        1. dev_env_configured.source_path（apply 前捕获的 C 盘真实路径，最准）
        2. get_tool_default_c_path（静态默认 C 盘路径表）
        3. status.original_path / current_path（path_fn 实时返回，可能已被改环境变量）
        """
        status = dev_get_tool_status(tool, target_drive,
                                      migrated_records=(config or {}).get("migrated", []))

        # 算 size 用的路径：优先用配置前捕获的 C 盘源路径
        # （apply_tool 改环境变量后 path_fn 会返回 D 盘路径，但 C 盘数据还在原地，
        #  应该读 C 盘路径算 size，否则 D 盘空目录会显示 0 MB）
        size_path = ""
        try:
            from dev_env_migrate import get_tool_default_c_path as _get_default_c
            # 1. dev_env_configured.source_path（apply 前捕获的 C 盘路径）
            dev_env_cfg = (config or {}).get("dev_env_configured") or {}
            cfg_info = dev_env_cfg.get(tool.get("id", "")) or {}
            sp = (cfg_info.get("source_path") or "").replace("\\\\?\\", "")
            if sp and sp[1:2] == ":" and sp[0].upper() == "C":
                size_path = sp
            # 2. 静态默认 C 盘路径（仅当真实存在时才用，否则跳到 3）
            #    避免 VS 默认路径表返回 64 位路径但用户实际装在 32 位目录时
            #    拿到不存在的路径导致显示"未生成"
            if not size_path:
                default_c = _get_default_c(tool)
                if default_c:
                    default_c = default_c.replace("\\\\?\\", "")
                    if os.path.exists(default_c):
                        size_path = default_c
        except Exception:
            pass
        # 3. 兜底：用 status 的 original_path / current_path（仅当在 C 盘时）
        #    VS 等工具的 current_path 能反映真实安装位置（32/64 位目录）
        if not size_path:
            fallback = (status.get("original_path") or status.get("current_path") or "") \
                        .replace("\\\\?\\", "")
            if fallback and fallback[1:2] == ":" and fallback[0].upper() == "C":
                size_path = fallback

        cur = size_path
        if not status.get("installed"):
            status["size_mb"] = 0  # 未安装
        elif not cur:
            # 已安装但无 C 盘路径（如 pnpm 数据已配到 D 盘，C 盘从未生成目录）
            status["size_mb"] = -2  # → 显示"未生成"
        elif status.get("is_symlink"):
            status["size_mb"] = -1  # 符号链接 → 显示"已迁移"
        elif not os.path.exists(cur):
            status["size_mb"] = -2  # 路径不存在 → "未生成"
        else:
            # 1. 先查 size 缓存（路径不变时复用，避免 D 盘大目录 os.walk 慢）
            try:
                from dev_env_migrate import get_cached_size, set_cached_size
                cached = get_cached_size(cur)
                if cached is not None:
                    status["size_mb"] = cached
                    return status
            except Exception:
                pass
            # 2. 缓存未命中：实际计算大小
            try:
                from utils import get_dir_size_fast
                size = get_dir_size_fast(cur)
                status["size_mb"] = size
                # 写入缓存（下次刷新同路径直接复用）
                try:
                    from dev_env_migrate import set_cached_size as _scs
                    _scs(cur, size)
                except Exception:
                    pass
            except Exception:
                status["size_mb"] = -3  # 计算失败
        return status


class DevEnvSizeWorker(QThread):
    """[已废弃] 大小计算已合并到 DevEnvRefreshWorker._detect_one
    保留类定义避免外部引用报错，但不再实例化。
    """
    pass


# ========== 开发工具自动下载 API 配置 ==========
# 仅配置有稳定 JSON API 的工具，其余工具走官网下载页
# 每个工具提供两个版本：latest（最新稳定版）和 popular（LTS/推荐版）
# resolver 是一个字符串，指向 _resolve_<name> 函数名，由 DevToolDownloadWorker 调用
_DEV_TOOL_DOWNLOAD_APIS = {
    "npm_global": {
        "api_url": "https://nodejs.org/dist/index.json",
        "resolver": "nodejs",
    },
    "npm_cache": {
        "api_url": "https://nodejs.org/dist/index.json",
        "resolver": "nodejs",
    },
    "pnpm_global": {
        # pnpm 通过 npm 安装，直接给独立安装包
        "api_url": "https://api.github.com/repos/pnpm/pnpm/releases/latest",
        "resolver": "pnpm",
    },
    "pip_install": {
        "api_url": "https://www.python.org/downloads/windows/",
        "resolver": "python",
    },
    "pip_cache": {
        "api_url": "https://www.python.org/downloads/windows/",
        "resolver": "python",
    },
    "conda": {
        "api_url": "https://repo.anaconda.com/miniconda/",
        "resolver": "conda",
    },
    "cargo_home": {
        "api_url": "https://rustup.rs/",
        "resolver": "rustup",
    },
    "rustup_home": {
        "api_url": "https://rustup.rs/",
        "resolver": "rustup",
    },
    "gopath": {
        "api_url": "https://go.dev/dl/?mode=json",
        "resolver": "go",
    },
    "gocache": {
        "api_url": "https://go.dev/dl/?mode=json",
        "resolver": "go",
    },
    "gomodcache": {
        "api_url": "https://go.dev/dl/?mode=json",
        "resolver": "go",
    },
    "dotnet_tools": {
        "api_url": "https://dotnetcli.azureedge.net/dotnet/Sdk/Current/latest.version",
        "resolver": "dotnet",
    },
    "nuget_cache": {
        "api_url": "https://dotnetcli.azureedge.net/dotnet/Sdk/Current/latest.version",
        "resolver": "dotnet",
    },
    "vscode_ext": {
        "api_url": "https://update.code.visualstudio.com/api/releases/stable",
        "resolver": "vscode",
    },
    "gradle_home": {
        "api_url": "https://services.gradle.org/versions/current",
        "resolver": "gradle",
    },
    "docker_data": {
        "api_url": "https://api.github.com/repos/docker/docker-ce/releases/latest",
        "resolver": "docker",
    },
}


def _get_arch_suffix():
    """获取当前系统架构对应的标识（用于选择正确的安装包）"""
    import platform
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "x64"
    elif machine in ("arm64", "aarch64"):
        return "arm64"
    elif machine in ("x86", "i386", "i486", "i586", "i686"):
        return "x86"
    return "x64"  # 默认


class DevToolDownloadWorker(QThread):
    """后台下载开发工具安装包（不自动安装）
    1. 根据 tool_id 和 version_type 调用对应 API 获取下载链接
    2. 下载到用户指定的本地路径
    3. 通过信号报告进度和结果
    """
    progress_signal = Signal(str, int, str)   # (tool_id, percent, msg)
    finished_signal = Signal(str, str)        # (tool_id, save_path)
    error_signal = Signal(str, str)           # (tool_id, error_msg)

    def __init__(self, tool_id, version_type, save_path, api_info):
        super().__init__()
        self.tool_id = tool_id
        self.version_type = version_type  # "latest" 或 "popular"
        self.save_path = save_path
        self.api_info = api_info
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            # 第1步：获取下载链接
            self.progress_signal.emit(self.tool_id, 0, "正在获取下载链接...")
            download_url = self._resolve_download_url()
            if not download_url:
                self.error_signal.emit(self.tool_id, "未能解析出下载链接")
                return
            self.progress_signal.emit(self.tool_id, 5, f"已获取链接: {download_url[:80]}...")
            # 第2步：下载文件
            self._download_file(download_url)
        except Exception as e:
            self.error_signal.emit(self.tool_id, str(e))

    def _resolve_download_url(self):
        """根据 resolver 类型调用对应的解析函数"""
        resolver = self.api_info.get("resolver", "")
        api_url = self.api_info.get("api_url", "")
        arch = _get_arch_suffix()
        import urllib.request
        import json as _json

        if resolver == "nodejs":
            # Node.js: https://nodejs.org/dist/index.json
            # LTS 版本标记为 lts（非 null），最新版取第一个
            with urllib.request.urlopen(api_url, timeout=10) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            if self.version_type == "popular":
                # 找第一个有 lts 标记的版本
                for v in data:
                    if v.get("lts"):
                        version = v["version"]
                        files = v.get("files", [])
                        break
                else:
                    version = data[0]["version"]
                    files = data[0].get("files", [])
            else:
                # latest: 第一个版本
                version = data[0]["version"]
                files = data[0].get("files", [])
            # 找 Windows x64 的 msi 安装包
            # files 是字符串列表，如 ["win-x64", "win-x86", ...]
            # 实际下载链接: https://nodejs.org/dist/{version}/node-{version}-x64.msi
            if arch == "arm64":
                return f"https://nodejs.org/dist/{version}/node-{version}-arm64.msi"
            return f"https://nodejs.org/dist/{version}/node-{version}-x64.msi"

        elif resolver == "go":
            # Go: https://go.dev/dl/?mode=json
            with urllib.request.urlopen(api_url, timeout=10) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            version_data = data[0]  # 最新版
            version = version_data["version"]
            # 找 Windows 安装包（.msi）
            for f in version_data.get("files", []):
                if f.get("os") == "windows" and f.get("arch") == arch and f.get("kind") == "installer":
                    return f"https://dl.google.com/go/{f['filename']}"
            # 兜底：用 zip
            for f in version_data.get("files", []):
                if f.get("os") == "windows" and f.get("arch") == arch:
                    return f"https://dl.google.com/go/{f['filename']}"
            return None

        elif resolver == "dotnet":
            # .NET: 先获取最新版本号，再拼下载链接
            with urllib.request.urlopen(api_url, timeout=10) as resp:
                version = resp.read().decode("utf-8").strip()
            # SDK 安装包: https://download.visualstudio.microsoft.com/download/pr/.../dotnet-sdk-{version}-win-x64.exe
            # 实际链接需要从 release.json 获取，这里用稳定模式
            if arch == "arm64":
                return f"https://download.visualstudio.microsoft.com/download/pr/dotnet-sdk-{version}-win-arm64.exe"
            return f"https://download.visualstudio.microsoft.com/download/pr/dotnet-sdk-{version}-win-x64.exe"

        elif resolver == "vscode":
            # VS Code: https://update.code.visualstudio.com/api/releases/stable
            with urllib.request.urlopen(api_url, timeout=10) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            version = data[0] if isinstance(data, list) else data
            # 下载链接: https://update.code.visualstudio.com/{version}/win32-x64-user/stable
            if arch == "arm64":
                return f"https://update.code.visualstudio.com/{version}/win32-arm64-user/stable"
            return f"https://update.code.visualstudio.com/{version}/win32-x64-user/stable"

        elif resolver == "python":
            # Python: 从 python.org 下载页获取最新稳定版
            # 直接用稳定链接模式: https://www.python.org/ftp/python/{version}/python-{version}-amd64.exe
            # 需先获取最新版本号
            with urllib.request.urlopen("https://www.python.org/downloads/windows/", timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            import re as _re
            # 找最新 Python 3 版本
            m = _re.search(r'Python (\d+\.\d+\.\d+)', html)
            if not m:
                return None
            version = m.group(1)
            if arch == "arm64":
                return f"https://www.python.org/ftp/python/{version}/python-{version}-arm64.exe"
            return f"https://www.python.org/ftp/python/{version}/python-{version}-amd64.exe"

        elif resolver == "conda":
            # Miniconda: https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe
            if arch == "arm64":
                return "https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-arm64.exe"
            return "https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe"

        elif resolver == "rustup":
            # Rust: rustup-init.exe（统一安装器，不区分版本）
            return "https://win.rustup.rs/x86_64" if arch == "x64" else "https://win.rustup.rs/i686"

        elif resolver == "gradle":
            # Gradle: https://services.gradle.org/versions/current 返回 JSON
            with urllib.request.urlopen(api_url, timeout=10) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            version = data.get("version", "")
            if not version:
                return None
            # 下载链接: https://services.gradle.org/distributions/gradle-{version}-bin.zip
            return f"https://services.gradle.org/distributions/gradle-{version}-bin.zip"

        elif resolver == "pnpm":
            # pnpm: GitHub Releases
            with urllib.request.urlopen(api_url, timeout=10) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            # 找 Windows x64 的独立安装包
            for asset in data.get("assets", []):
                name = asset.get("name", "").lower()
                if "win" in name and ("x64" in name or "win32" in name):
                    return asset.get("browser_download_url")
            return None

        elif resolver == "docker":
            # Docker Desktop: GitHub Releases
            with urllib.request.urlopen(api_url, timeout=10) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            for asset in data.get("assets", []):
                name = asset.get("name", "").lower()
                if "docker" in name and "desktop" in name and name.endswith(".exe"):
                    return asset.get("browser_download_url")
            return None

        return None

    def _download_file(self, url):
        """下载文件到本地，报告进度"""
        import urllib.request
        import shutil
        # 创建临时文件
        tmp_path = self.save_path + ".tmp"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 64 * 1024  # 64KB
                with open(tmp_path, "wb") as f:
                    while True:
                        if self._cancelled:
                            f.close()
                            try:
                                os.remove(tmp_path)
                            except OSError:
                                pass
                            return
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            percent = int(5 + (downloaded / total) * 90)  # 5%-95%
                            self.progress_signal.emit(
                                self.tool_id, percent,
                                f"{downloaded // 1024}KB / {total // 1024}KB")
                        else:
                            self.progress_signal.emit(
                                self.tool_id, 50,
                                f"已下载 {downloaded // 1024}KB")
            # 下载完成，重命名
            self.progress_signal.emit(self.tool_id, 98, "正在保存文件...")
            shutil.move(tmp_path, self.save_path)
            self.progress_signal.emit(self.tool_id, 100, "完成")
            self.finished_signal.emit(self.tool_id, self.save_path)
        except Exception as e:
            # 清理临时文件
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise


class DevEnvApplyWorker(QThread):
    """后台批量应用开发工具配置"""
    finished_signal = Signal(list, str)  # (results, target_drive)
    error_signal = Signal(str)
    progress_signal = Signal(int, int, str)  # (current, total, msg) - 进度反馈
    verbose_log_sig = Signal(str, str)  # migrator 阶段日志（开发工具区专用）

    def __init__(self, tools, target_drive, migrate_data=False, config=None,
                 target_path_override=None):
        """通用：支持任意盘间迁移

        :param target_path_override: 用户指定的目标路径（覆盖默认模板路径）
               支持 D→D / D→E / E→F 等任意盘间迁移
        """
        super().__init__()
        self.tools = tools
        self.target_drive = target_drive
        self.migrate_data = migrate_data  # 是否同时迁移数据
        self.config = config  # state.json 配置（用于记录 migrated）
        self.target_path_override = target_path_override  # 用户指定的目标路径

    def run(self):
        try:
            results = []
            total = len(self.tools)
            for i, tool in enumerate(self.tools):
                source_path = ""
                try:
                    # 进度反馈：开始处理第 i 个工具
                    self.progress_signal.emit(i, total,
                        f"[{i+1}/{total}] 正在配置 {tool['name']}...")
                    # 0. 配置前捕获源路径（用于数据迁移和待迁移区橙色提示）
                    # 必须在 apply_tool 之前调用，因为 apply_tool 会改环境变量导致
                    # current_path_fn 返回新路径而非原始路径
                    try:
                        info = dev_get_tool_data_info(tool)
                        source_path = info.get("source_path", "")
                    except Exception:
                        pass
                    # 1. 应用配置（环境变量+配置命令）
                    # 通用：传 target_path_override 让环境变量指向用户指定的路径
                    if self.migrate_data:
                        self.progress_signal.emit(i, total,
                            f"[{i+1}/{total}] {tool['name']}: 配置环境变量+迁移数据中...")
                    ok, msg = dev_apply_tool(tool, self.target_drive,
                                              target_path_override=self.target_path_override)
                    # 2. 如果配置成功且用户要求迁移数据，执行数据迁移
                    if ok and self.migrate_data:
                        try:
                            # 通用：传入 source_path_override 和 target_path_override
                            # 支持任意盘间迁移（C→D / D→D / D→E / E→F）
                            mok, mmsg, record = dev_migrate_tool_data(
                                tool, self.target_drive, self.config,
                                source_path_override=source_path or None,
                                target_path_override=self.target_path_override,
                                log_callback=lambda et, m: self.verbose_log_sig.emit(et, m))
                            msg = msg + "\n  [数据迁移] " + mmsg
                            if not mok:
                                # 数据迁移失败：环境变量配置成功了，但数据没迁移
                                # 整体算失败，让用户知道数据迁移出了问题
                                ok = False
                        except Exception as e:
                            msg = msg + f"\n  [数据迁移] 异常: {e}"
                            ok = False
                    results.append((tool, ok, msg, source_path))
                except Exception as e:
                    results.append((tool, False, f"异常: {e}", source_path))
            # 进度反馈：全部完成
            self.progress_signal.emit(total, total, f"全部 {total} 个工具处理完成")
            self.finished_signal.emit(results, self.target_drive)
        except Exception as e:
            self.error_signal.emit(str(e))
