"""迁移引擎适配层:构造 job.json → 启动 Rust 引擎子进程 → 解析 JSONL → 分发回调。
对应执行文档 §2.1 引擎适配层与 §4 调用点迁移方案。

职责边界:本模块只负责跨语言通信(构造 job、启动子进程、解析 JSONL、回调)。
业务逻辑(事务/校验/取消锁纪律/进度限频)在 migrator 层。
错误码翻译(ADR-005)由本模块提供工具函数 translate_error_code,
migrator 层调用时直接拿中文 reason/suggestion,无需自己维护映射表。

日志规范(ADR-011):所有 Rust 引擎相关日志加 [rust-engine] 前缀,
与 Python 业务层日志区分,方便在 app.log 中定位引擎行为。
关键事件(JobStart/JobDone/Cancelled/FileError/Retry)写入 app.log,
让软件内可见引擎在做什么(P4 集成后由 migrator 层进一步处理展示)。
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# ========== 长路径安全化 ==========
# Windows API 对 \\?\ 前缀向下兼容：深层嵌套目录（node_modules 等）路径超过
# 260 字符时，无前缀会在 CreateFileW 层直接失败（ERROR_PATH_NOT_FOUND），
# 除非系统手动开启了 Win32 长路径组策略（默认关闭）。
# 给引擎 job 的 source/target 统一加前缀后，任意深度路径都可正常工作。
# 注意：\\?\ 是 Windows API 强制语法（无法用正斜杠替代），此处作为常量；
# 其余新代码路径处理一律用 pathlib / 正斜杠（防斜杠规范）。
_LONG_PATH_PREFIX = "\\\\?\\"


def _with_long_path_prefix(path):
    """长路径安全化：给本地盘符绝对路径加 \\?\\ 前缀（幂等，短路径也正常）

    - 已带前缀 / UNC（\\\\server\\share 或 //server/share）/ 相对路径 → 原样返回
    - 本地绝对路径（X:\\... 或 X:/...）→ 加前缀，并统一为反斜杠分隔符
    """
    try:
        p = os.fspath(path) if not isinstance(path, str) else path
        if not p or p.startswith(_LONG_PATH_PREFIX):
            return path
        pp = Path(p)
        if not pp.is_absolute():
            return path  # 相对路径不加（引擎内部可能基于 cwd 解析）
        drive = pp.drive
        if not drive:
            return path  # 无盘符（如 / 根）不加
        # UNC 网络路径不加（引擎有 network_path 独立处理）；正斜杠/反斜杠形式都判
        if drive.startswith("//") or drive.startswith("\\\\"):
            return path
        # 统一反斜杠分隔符（str(Path) 在 Windows 上转反斜杠），
        # 避免 \\?\C:/x 混合分隔符
        return _LONG_PATH_PREFIX + str(pp)
    except Exception:
        return path

# 模块级 logger(与 migrator/config 共用 CDriveRelocator,写入 app.log)
log = logging.getLogger('CDriveRelocator')


class MigrateEngineError(Exception):
    """引擎不可用或调用失败。"""


def translate_error_code(code):
    """Win32 错误码 → (中文原因, 建议)。

    ADR-005:错误码翻译职责在 Python 侧单一表 migrator._WIN32_ERR_MAP。
    本函数从 migrator 导入复用,避免双份映射表维护漂移。

    :param code: Win32 错误码(Rust 引擎 file_error.code 或 retry.code)
    :return: (reason, suggestion) 中文元组;未知码返回通用提示
    """
    # 延迟导入避免循环依赖(migrator 导入本模块的 MigrateEngine)
    # 相对导入适用于包内调用(src.core.migrate_engine);
    # 脚本直跑(sys.path 含 src/core,migrate_engine 为顶层模块)时相对导入
    # 报 ImportError,回退绝对导入(BUG 修复:file_error 事件曾因此中断事件流,
    # 导致所有含失败文件的迁移被误判"引擎被外部强杀")。
    try:
        from .migrator import _WIN32_ERR_MAP
    except ImportError:
        from migrator import _WIN32_ERR_MAP
    return _WIN32_ERR_MAP.get(
        code, ("未知错误(码 %d)" % code, "请查看日志或重试")
    )


def enrich_file_error(evt):
    """给 file_error 事件补充中文 reason/suggestion 字段(原地修改)。

    :param evt: 引擎 JSONL 事件 dict,需含 code 字段
    :return: 同一 dict(reason/suggestion 字段已填)
    """
    if "code" in evt:
        reason, suggestion = translate_error_code(evt["code"])
        evt["reason"] = reason
        evt["suggestion"] = suggestion
    return evt


# 关键事件级别配置(ADR-011):决定哪些事件写 app.log 及级别
# Progress/FileStart/FileDone 高频,不写 app.log(避免日志爆炸);
# JobStart/JobDone/Cancelled/FileError/Retry/Info/VerifyMismatch 低频且重要,写入
_EVENT_LOG_LEVEL = {
    "job_start": logging.INFO,
    "job_done": logging.INFO,
    "cancelled": logging.INFO,
    "file_error": logging.WARNING,
    "retry": logging.INFO,
    "info": logging.INFO,
    "purge": logging.INFO,
    "verify_mismatch": logging.WARNING,
}


def _log_engine_event(evt):
    """将引擎关键事件写入 app.log(加 [rust-engine] 前缀,ADR-011)。

    高频事件(Progress/FileStart/FileDone)不写,避免日志爆炸;
    低频关键事件(JobStart/JobDone/Cancelled/FileError/Retry/Info)写入,
    让软件内可见引擎在做什么。

    :param evt: 引擎 JSONL 事件 dict
    """
    event = evt.get("event")
    level = _EVENT_LOG_LEVEL.get(event)
    if level is None:
        return  # 高频事件不记录
    try:
        if event == "job_start":
            msg = "[rust-engine] 任务开始 source=%s target=%s mode=%s" % (
                evt.get("source", "?"), evt.get("target", "?"), evt.get("mode", "?"))
        elif event == "job_done":
            msg = "[rust-engine] 任务完成 rc=%s files=%s bytes=%s duration=%sms" % (
                evt.get("rc"), evt.get("files_total"), evt.get("bytes_total"),
                evt.get("duration_ms"))
        elif event == "cancelled":
            msg = "[rust-engine] 任务被取消 files=%s bytes=%s" % (
                evt.get("files_done"), evt.get("bytes_done"))
        elif event == "file_error":
            # 引擎内部码(0xE0000000 段)显示 hex,便于与 Win32 码区分
            code = evt.get("code")
            if isinstance(code, int) and code >= 0xE0000000:
                code = "0x%08X" % code
            msg = "[rust-engine] 文件错误 path=%s code=%s reason=%s" % (
                evt.get("path", "?"), code, evt.get("reason", "?"))
        elif event == "retry":
            msg = "[rust-engine] 重试 path=%s code=%s attempt=%s/%s" % (
                evt.get("path", "?"), evt.get("code"),
                evt.get("attempt", "?"), "?")
        elif event == "info":
            msg = "[rust-engine] 信息 %s=%s" % (
                evt.get("key", "?"), evt.get("value", "?"))
        elif event == "verify_mismatch":
            msg = "[rust-engine] 校验不一致 path=%s (内容与源不同,建议重新迁移该文件)" % (
                evt.get("path", "?"))
        elif event == "purge":
            msg = "[rust-engine] Purge path=%s soft=%s dry=%s" % (
                evt.get("path", "?"), evt.get("soft_deleted"),
                evt.get("dry_run"))
        else:
            msg = "[rust-engine] %s" % evt
        log.log(level, msg)
    except Exception:
        pass  # 日志写失败不影响引擎事件流


class MigrateEngine:
    """Rust 迁移引擎的薄封装。"""

    def __init__(self):
        self._exe_path = self._locate_engine()
        self._proc = None
        self._cancel_token = None
        self._lock = threading.Lock()

    @staticmethod
    def _locate_engine():
        # #32 修复:打包模式(PyInstaller)下 __file__ 指向解包临时目录,
        # 不能用源码路径推算;引擎 exe 随包分发,优先解包目录/ exe 同目录的 bin/
        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            exe_dir = os.path.dirname(sys.executable)
            candidates = []
            if meipass:
                candidates.append(os.path.join(meipass, "bin", "rust-migrate-engine.exe"))
            candidates.append(os.path.join(exe_dir, "bin", "rust-migrate-engine.exe"))
            candidates.append(os.path.join(exe_dir, "rust-migrate-engine.exe"))
            for cand in candidates:
                if os.path.isfile(cand):
                    return cand
            return candidates[0]  # 都不存在时返回首选(engine_available 会检测并禁用)
        # 源码模式: src/core/migrate_engine.py → 上两级(src/) → 再上到项目根 → bin/rust-migrate-engine.exe
        here = Path(__file__).resolve().parent  # src/core
        return str(here.parent.parent / "bin" / "rust-migrate-engine.exe")

    def engine_available(self):
        """引擎 exe 是否存在(启动时检测,缺失则禁用迁移/还原)。"""
        return os.path.isfile(self._exe_path)

    def exe_path(self):
        return self._exe_path

    def run_job(self, source, target, mode="copy", verify="none",
                retry_max=5, retry_backoff_ms=500, network_path=False,
                flush_checkpoint_mb=64, purge_enabled=False, purge_soft_delete=True,
                purge_dry_run=False,
                background_mode=False, process_background=False, write_through=False,
                large_file_threshold_mb=64, block_size_mb=64, on_event=None,
                timeout_sec=1800):
        """启动引擎执行任务,逐行解析 JSONL 并回调。返回引擎退出码。

        mode: copy=/E 等价;mirror=/MIR 等价(含 purge);verify=只校验。
        on_event: 回调函数,接收解析后的事件 dict;为 None 则不回调。
        block_size_mb: P4.5 任务#8 大文件块大小(MB),可选 1/4/16/64,默认 64。
        background_mode: P6 后台低优先级——句柄级 FILE_IO_PRIORITY_HINT_INFO(VeryLow),
            不影响缓存,温和让路(实测性能损失小)。
        process_background: P6 极致让路——进程级 PROCESS_MODE_BACKGROUND_BEGIN,
            限制工作集/缓存驻留,实测复制吞吐降 ~19 倍,按需选择(默认关闭)。
        timeout_sec: N1 修复——引擎作业超时保护(默认 1800 秒=30 分钟,None/<=0 关闭)。
            引擎卡在慢 I/O/网络挂载时,stdout 循环永久阻塞会让 UI 显示
            "迁移中..."永不结束;超时后 force_kill 强制终止并按超时异常上报
            (数据安全:复制阶段源未删,pending 事务保留可续传)。
            退出码语义见执行文档 §2.3.3(0/1 成功,2 部分成功,8/16 失败,-1 取消)。
        """
        if not self.engine_available():
            raise MigrateEngineError("迁移引擎缺失: %s" % self._exe_path)

        # 取消标志文件:用 PID 拼唯一路径,文件本身不存在;取消时 touch 触发停止
        # 不用 mkstemp+remove 是为了避免 TOCTOU(创建-删除窗口可能被其他进程占位)
        cancel_token = os.path.join(
            tempfile.gettempdir(),
            "cdrive_cancel_%d_%d.flag" % (os.getpid(), id(source) ^ id(target))
        )
        # 确保起始状态干净(上次异常残留)
        try:
            os.remove(cancel_token)
        except OSError as e:
            log.debug("忽略异常: %s", e)

        job = {
            # 长路径安全化：统一加 \\?\ 前缀，
            # 深层目录超 260 字符不再在 CreateFileW 层失败
            "source": _with_long_path_prefix(str(source)),
            "target": _with_long_path_prefix(str(target)),
            "mode": mode,
            "verify": verify,
            "retry": {
                "max_attempts": retry_max,
                "backoff_base_ms": retry_backoff_ms,
                "network_path": network_path,
            },
            "flush_checkpoint_mb": flush_checkpoint_mb,
            "purge": {"enabled": purge_enabled, "soft_delete": purge_soft_delete,
                      "dry_run": purge_dry_run},
            "background_mode": background_mode,
            "process_background": process_background,
            "write_through": write_through,
            "large_file_threshold_mb": large_file_threshold_mb,
            "block_size_mb": block_size_mb,
            "cancel_token": cancel_token,
        }

        fd, job_path = tempfile.mkstemp(suffix=".json", prefix="cdrive_job_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(job, f, ensure_ascii=False)
            with self._lock:
                self._cancel_token = cancel_token
            return self._spawn_and_drain(job_path, on_event, timeout_sec=timeout_sec)
        finally:
            with self._lock:
                self._cancel_token = None
            for p in (job_path, cancel_token):
                try:
                    os.remove(p)
                except OSError as e:
                    log.debug("忽略异常: %s", e)

    def run_job_sync(self, source, target, mode="copy", **kwargs):
        """同步便捷封装(无事件回调),等待完成返回退出码。
        用于 rebuild_all_links / fix_broken_link 等低频小数据量场景。
        """
        kwargs.pop("on_event", None)
        return self.run_job(source, target, mode=mode, on_event=None, **kwargs)

    def request_cancel(self):
        """请求取消:写 cancel_token 触发引擎优雅退出(下个块边界停止 + 保存 ckpt)。
        不阻塞等待引擎退出 —— _spawn_and_drain 的 stdout 循环会感知引擎退出并收尾。
        terminate 兜底交给调用方在确认超时后另行调用 force_kill()。
        """
        with self._lock:
            token = self._cancel_token
        if token:
            try:
                Path(token).touch()
            except OSError as e:
                log.debug("忽略异常: %s", e)
        # 不在此处 wait/terminate:与 _spawn_and_drain 的 stdout 读循环并发会竞争
        # (subprocess.wait 非线程安全,两个线程同时 wait 同一进程可能 OSError)。
        # 引擎收到 cancel_token 后会在下个块边界(<4MB,通常 <1 秒)发 Cancelled 事件并退出,
        # _spawn_and_drain 的 for line 循环读到 EOF 自然收尾。

    def force_kill(self):
        """强制杀引擎进程(调用方在 request_cancel 超时后兜底)。
        场景:引擎卡在慢 I/O 或网络挂载上,cancel_token 轮询迟迟不到。
        """
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except OSError as e:
                log.debug("忽略异常: %s", e)

    def wait_exit(self, timeout):
        """等待引擎进程退出(优雅取消后引擎需时间 save ckpt 并退出)。

        N2 修复:引擎块大小默认 64MB(≥4GB 内存自动分级),取消后需等
        当前块写完 + ckpt 保存 + 退出事件输出,固定 sleep(500ms) 不足;
        轮询 returncode 直到超时。返回 True=超时内已退出/已收尾,False=仍在运行。

        :param timeout: 最大等待秒数
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                proc = self._proc
            if proc is None:
                return True  # _spawn_and_drain 已收尾(进程退出且清理完成)
            if proc.poll() is not None:
                return True
            time.sleep(0.05)
        return False

    def _spawn_and_drain(self, job_path, on_event, timeout_sec=None):
        # Popen 启动失败捕获:exe 被杀软删除/权限不足/路径错误
        try:
            with self._lock:
                # CREATE_NO_WINDOW：引擎是 console 程序，不加会在桌面弹出黑框
                self._proc = subprocess.Popen(
                    [self._exe_path, "--job", job_path, "--log-format", "jsonl"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=1,
                    text=True,
                    encoding="utf-8",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
        except OSError as e:
            raise MigrateEngineError("启动引擎失败: %s (%s)" % (self._exe_path, e))

        # N1 修复:引擎超时 watchdog——stdout 阻塞读无超时,引擎卡在慢 I/O 时
        # Python 侧永久阻塞(UI 永远"迁移中");Timer 到点 force_kill 强制终止,
        # stdout 循环读到 EOF 自然收尾,按超时异常上报(数据安全:源未删)。
        timeout_flag = threading.Event()
        timeout_timer = None
        if timeout_sec and timeout_sec > 0:
            def _on_timeout():
                timeout_flag.set()
                log.warning("[rust-engine] 引擎作业超时(%s 秒),强制终止进程", timeout_sec)
                self.force_kill()
            timeout_timer = threading.Timer(timeout_sec, _on_timeout)
            timeout_timer.daemon = True
            timeout_timer.start()

        # 异步排空 stderr,避免管道写满导致子进程阻塞(引擎正常不写 stderr)
        stderr_lines = []
        # JSON 解析失败行(用于诊断引擎输出异常)
        parse_errors = []

        def _drain_stderr():
            if self._proc and self._proc.stderr:
                try:
                    for line in self._proc.stderr:
                        stderr_lines.append(line)
                except Exception:
                    pass  # stderr 读异常(如进程被强杀)不致命

        t = threading.Thread(target=_drain_stderr, daemon=True)
        t.start()

        stdout_closed_prematurely = False
        has_job_done = False  # 是否收到 job_done 事件(强杀场景关键判据)
        try:
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError as e:
                    # 记录解析失败行(截断防止超长行污染诊断)
                    parse_errors.append("%s: %s" % (e, line[:200]))
                    continue
                # 显示层剥离 \\?\ 前缀：引擎 job 的 source/target 加了前缀
                # （4.1 长路径），事件输出会带，UI 日志/诊断无需显示该前缀
                if _LONG_PATH_PREFIX in line:
                    evt = {k: (v.replace(_LONG_PATH_PREFIX, "") if isinstance(v, str) else v)
                           for k, v in evt.items()}
                # 检测 job_done 事件(用于判断进程是否正常完成)
                if evt.get("event") == "job_done":
                    has_job_done = True
                # file_error / retry 事件自动补充中文翻译(ADR-005)
                # 事件处理异常(翻译/日志)不得中断 stdout 读取循环:
                # 否则 job_done 读不到,正常失败(rc=2/8)会被误判"引擎被外部强杀"
                # (BUG 修复:translate_error_code 的 ImportError 曾直接中断循环)
                try:
                    if evt.get("event") in ("file_error", "retry"):
                        enrich_file_error(evt)
                    # 关键事件写入 app.log(ADR-011:加 [rust-engine] 前缀,软件内可见)
                    _log_engine_event(evt)
                except Exception:
                    pass  # 事件处理异常不影响引擎事件流
                if on_event:
                    try:
                        on_event(evt)
                    except Exception:
                        pass  # 回调异常不应影响引擎事件流
        except Exception as _e:
            # stdout 读异常(如进程被强杀导致管道破裂)
            stdout_closed_prematurely = True
            log.warning("引擎 stdout 读取中断: %s: %s", type(_e).__name__, _e)
        finally:
            if timeout_timer:
                timeout_timer.cancel()
            try:
                self._proc.wait()
            except Exception as e:
                log.debug("忽略异常: %s", e)
            t.join(timeout=5)
            # 线程仍活着时保留已收集的 stderr(用于诊断),不丢弃
            rc = self._proc.returncode if self._proc.returncode is not None else -1
            with self._lock:
                self._proc = None

        # 退出码语义(执行文档 §2.3.3):
        #   0/1/2 = 成功(无文件/有文件/部分成功),255 = 取消(进程级 -1)
        #   8/16 = 引擎自身报告失败
        #   其他(如 101=panic, -1=wait 失败)= 异常,需诊断
        # 正常集合:{0, 1, 2, 8, 16, 255};其他值视为异常崩溃
        # 例外:rc 在正常集合内但未收到 job_done 事件 = 进程被外部强杀
        #   (taskkill /F 返回 1,与"成功有文件"无法区分;靠 has_job_done 判定)
        is_normal_rc = rc in (0, 1, 2, 8, 16, 255)
        # 强杀场景:rc 可能是 1(正常值),但无 job_done 事件 → 视为异常
        # 注意:cancel 路径有 Cancelled + JobDone 事件,不会被误判
        is_crash_no_done = is_normal_rc and not has_job_done and rc != 255
        stderr_text = "".join(stderr_lines)[:2000]
        parse_text = "; ".join(parse_errors[:5])[:1000] if parse_errors else ""

        # 持久化崩溃诊断到日志(无论是否抛异常,都写一份方便排查)
        if not is_normal_rc or stdout_closed_prematurely or parse_errors or is_crash_no_done:
            self._dump_crash_diag(rc, stderr_text, parse_text,
                                  stdout_closed_prematurely, job_path,
                                  has_job_done)

        # 超时场景:Timer 已 force_kill 且标志置位,按超时异常上报
        # (不落 is_crash_no_done 的"外部强杀"误报分支)
        if timeout_flag.is_set():
            raise MigrateEngineError(
                "引擎作业超时(%s 秒),已强制终止。可能卡在慢 I/O 或网络挂载上,"
                "请检查源/目标盘连接状态。已复制数据保留,下次启动自动续传。"
                % timeout_sec
            )

        # 异常场景判断:
        #   - rc 不在正常集合:引擎崩溃(panic/强杀)
        #   - rc 正常但无 job_done:进程被外部强杀(taskkill /F)
        #   - stdout 提前关闭 + 无 JobDone 事件:进程级闪退
        #   - 解析失败:引擎输出非 JSON(可能 panic 信息混入 stdout)
        if not is_normal_rc:
            raise MigrateEngineError(
                "引擎异常退出 code=%s,非正常退出码(panic/强杀)。"
                "stderr=%s; 诊断见 rust_engine_crash.log" % (rc, stderr_text)
            )
        if is_crash_no_done:
            raise MigrateEngineError(
                "引擎异常退出 code=%s,未收到 job_done 事件(进程被外部强杀)。"
                "stderr=%s; 诊断见 rust_engine_crash.log" % (rc, stderr_text)
            )
        if rc >= 8 and stderr_text:
            # 8/16 且 stderr 非空:引擎报告失败,附带诊断
            raise MigrateEngineError(
                "引擎异常退出 code=%s, stderr=%s" % (rc, stderr_text)
            )
        if parse_errors:
            # rc 正常但有解析失败:警告但不抛(可能个别事件损坏,主流程完成)
            log.warning("[rust-engine] 事件解析失败 %d 行: %s" % (len(parse_errors), parse_text))
        return rc

    def _dump_crash_diag(self, rc, stderr_text, parse_text,
                         stdout_premature, job_path, has_job_done=True):
        """持久化崩溃诊断到日志文件,便于事后排查。

        日志文件名带 rust 前缀(ADR-011),与 Rust 引擎 panic hook 写入同一文件,
        两端都用追加模式,避免互相覆盖。超 1MB 自动重置防止无限增长。
        """
        try:
            import tempfile
            import time
            # 与 Rust 侧 panic hook 共用同一文件(都是 rust_engine_crash.log),
            # 统一追加模式;Rust 侧写 panic backtrace,Python 侧写适配层诊断
            log_path = os.path.join(tempfile.gettempdir(),
                                    "rust_engine_crash.log")
            # 追加式写入(保留历史崩溃记录,最多 1MB 自动截断)
            content = (
                "=== %s [python-adapter] ===\n"
                "rc=%s, stdout_premature=%s, has_job_done=%s\n"
                "job=%s\n"
                "stderr:\n%s\n"
                "parse_errors:\n%s\n\n"
            ) % (time.strftime("%Y-%m-%d %H:%M:%S"), rc, stdout_premature,
                 has_job_done, job_path, stderr_text, parse_text)
            # 检查文件大小,超过 1MB 时重置(避免无限增长)
            try:
                size = os.path.getsize(log_path)
                if size > 1_000_000:
                    os.remove(log_path)
            except OSError as e:
                log.debug("忽略异常: %s", e)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(content)
            # 同时写入 app.log(让软件内可见),前缀 [rust-engine] 标识来源
            log.error(
                "[rust-engine] 引擎崩溃诊断已写入 %s | rc=%s has_job_done=%s",
                log_path, rc, has_job_done
            )
        except Exception:
            pass  # 诊断日志写失败不能影响主流程
