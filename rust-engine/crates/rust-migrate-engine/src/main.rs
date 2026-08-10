//! CLI 入口:解析 --job/--log-format,加载 job.json 并驱动引擎。
//! 对应执行文档 §2.3 调用方式。
//!
//! 崩溃兜底:安装 panic hook + catch_unwind 双保险,
//! 保证任何 panic 都会写崩溃日志 + 发 JobDone 事件,Python 侧可感知。

use rust_migrate_engine::{engine, job, mft_index};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};

/// 标记 JobStart 是否已发(用于 panic hook 判断是否需要补发 JobDone)。
/// panic 可能发生在 JobStart 之前(此时补发 JobDone 会让 Python 侧困惑),
/// 也可能发生在 JobStart 之后(此时必须补发,否则 Python 侧会一直等)。
static JOB_STARTED: AtomicBool = AtomicBool::new(false);

/// 崩溃日志路径:%TEMP%\rust_engine_crash.log(追加模式,保留历史)。
/// 文件名带 rust 前缀,与 Python 适配层诊断日志区分(ADR-011 日志前缀规范)。
/// 写入时机:panic hook 触发时;读取时机:Python 侧在引擎异常退出后查日志。
fn crash_log_path() -> PathBuf {
    let mut p = std::env::temp_dir();
    p.push("rust_engine_crash.log");
    p
}

/// 安装 panic hook:写崩溃日志 + stderr + 尝试发 JobDone。
/// **不调用默认 hook** —— 默认 hook 的 stderr 输出 `thread 'main' panicked at ...`
/// 不带 `[rust-engine]` 前缀,会让 Python 侧日志区分困难(ADR-014)。
/// 本 hook 自行格式化所有输出,统一加 `[rust-engine]` 前缀。
fn install_panic_hook() {
    std::panic::set_hook(Box::new(move |info| {
        // 1. 构造 panic 信息(payload + location + backtrace)
        let location = info.location().map(|l| l.to_string()).unwrap_or_else(|| "<unknown>".into());
        let payload = info.payload();
        let payload_str = if let Some(s) = payload.downcast_ref::<&'static str>() {
            (*s).to_string()
        } else if let Some(s) = payload.downcast_ref::<String>() {
            s.clone()
        } else {
            format!("{:?}", payload)
        };
        let backtrace = std::backtrace::Backtrace::force_capture();
        let thread_name = std::thread::current()
            .name()
            .unwrap_or("<unnamed>")
            .to_string();

        // 2. 写 stderr(统一 [rust-engine] 前缀,ADR-014)
        //    格式参考默认 hook,但加前缀让 Python 侧日志可区分来源
        let _ = eprintln!(
            "[rust-engine] thread '{}' panicked at {}:\n{}\nstack backtrace:\n{:?}",
            thread_name, location, payload_str, backtrace
        );

        // 3. 写崩溃日志文件(追加模式,保留历史;超 1MB 时重置避免无限增长)
        let msg = format!(
            "[{}] [rust-engine] panic\n  thread: {}\n  location: {}\n  payload: {}\n  backtrace: {:?}\n\n",
            chrono_like_ts(),
            thread_name,
            location,
            payload_str,
            backtrace,
        );
        let log_path = crash_log_path();
        // 检查文件大小,超 1MB 时重置
        if let Ok(meta) = std::fs::metadata(&log_path) {
            if meta.len() > 1_000_000 {
                let _ = std::fs::remove_file(&log_path);
            }
        }
        use std::io::Write;
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
        {
            let _ = f.write_all(msg.as_bytes());
        }

        // 4. 若 JobStart 已发,补发 JobDone rc=16,让 Python 侧感知结束
        //    (stdout 可能已损坏,writeln 失败时用 eprintln 兜底)
        if JOB_STARTED.load(Ordering::SeqCst) {
            let done = serde_json::json!({
                "event": "job_done",
                "files_total": 0,
                "bytes_total": 0,
                "duration_ms": 0,
                "rc": 16,
                "ts": std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs())
                    .unwrap_or(0),
            });
            let _ = writeln!(std::io::stdout(), "{}", done);
            let _ = std::io::stdout().flush();
            let _ = eprintln!("[rust-engine] [panic-hook] 补发 job_done rc=16");
        }
    }));
}

/// 简易时间戳(无 chrono 依赖,格式 YYYY-MM-DD HH:MM:SS)。
fn chrono_like_ts() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    // 简化实现:用 unix 秒 + 8h(Asia/Shanghai)转字符串
    let secs = secs + 8 * 3600;
    let days = secs / 86400;
    let remainder = secs % 86400;
    let h = remainder / 3600;
    let m = (remainder % 3600) / 60;
    let s = remainder % 60;
    // 1970-01-01 + days 天(粗略,不处理闰年,够用于日志)
    let year = 1970 + days / 365;
    let day_of_year = days % 365;
    let month = (day_of_year / 30).min(11) + 1;
    let day = (day_of_year % 30) + 1;
    format!("{:04}-{:02}-{:02} {:02}:{:02}:{:02}", year, month, day, h, m, s)
}

fn main() -> ExitCode {
    install_panic_hook();

    let args: Vec<String> = std::env::args().collect();
    let mut job_path: Option<PathBuf> = None;
    // MFT 索引子命令（方案 8.8）:--mft-index --volume C --out <path>
    let mut mft_mode = false;
    let mut mft_volume: Option<char> = None;
    let mut mft_out: Option<PathBuf> = None;
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--job" => {
                i += 1;
                if i < args.len() {
                    job_path = Some(PathBuf::from(&args[i]));
                }
            }
            "--log-format" => {
                // 仅支持 jsonl(默认),接受参数但不切换;缺参数时报错
                if i + 1 >= args.len() {
                    eprintln!("[rust-engine] --log-format 缺少参数");
                    return ExitCode::from(16);
                }
                i += 1;
            }
            "--mft-index" => {
                mft_mode = true;
            }
            "--volume" => {
                i += 1;
                if i < args.len() {
                    // 仅接受单字符盘符（"CC" 之类按非法处理，不静默取首字符）
                    let v = &args[i];
                    if v.len() == 1 && v.chars().next().unwrap().is_ascii_alphabetic() {
                        mft_volume = v.chars().next();
                    }
                }
            }
            "--out" => {
                i += 1;
                if i < args.len() {
                    mft_out = Some(PathBuf::from(&args[i]));
                }
            }
            _ => {
                eprintln!("[rust-engine] 未知参数: {}", args[i]);
                return ExitCode::from(16);
            }
        }
        i += 1;
    }

    // --mft-index 子命令:独立分支,不经过 job 解析
    if mft_mode {
        let volume = match mft_volume {
            Some(c) if c.is_ascii_alphabetic() => c.to_ascii_uppercase(),
            _ => {
                eprintln!("[rust-engine] --mft-index 缺少有效 --volume 参数(如 --volume C)");
                return ExitCode::from(16);
            }
        };
        let out = match mft_out {
            Some(p) => p,
            None => {
                eprintln!("[rust-engine] --mft-index 缺少 --out 参数(索引输出路径)");
                return ExitCode::from(16);
            }
        };
        // 标记 JobStart 即将发出(panic hook 据此补发 JobDone)
        JOB_STARTED.store(true, Ordering::SeqCst);
        // catch_unwind 兜底:panic 已被 hook 处理(写日志),这里只返回退出码
        let rc = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            mft_index::run(volume, &out)
        }))
        .map(|rc| rc as u8)
        .unwrap_or_else(|_| {
            eprintln!("[rust-engine] [main] mft-index panic 已被捕获,返回 16");
            16
        });
        return ExitCode::from(rc);
    }

    let job_path = match job_path {
        Some(p) => p,
        None => {
            eprintln!("[rust-engine] 用法: rust-migrate-engine --job <job.json> [--log-format jsonl]");
            return ExitCode::from(16);
        }
    };

    let content = match std::fs::read_to_string(&job_path) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("[rust-engine] 读取 job 失败 ({}): {}", job_path.display(), e);
            return ExitCode::from(16);
        }
    };

    let job: job::Job = match serde_json::from_str(&content) {
        Ok(j) => j,
        Err(e) => {
            eprintln!("[rust-engine] 解析 job.json 失败: {}", e);
            return ExitCode::from(16);
        }
    };

    if let Err(e) = job.validate() {
        eprintln!("[rust-engine] job 校验失败: {}", e);
        return ExitCode::from(16);
    }

    // 标记 JobStart 即将发出(panic hook 据此判断是否补发 JobDone)
    JOB_STARTED.store(true, Ordering::SeqCst);

    // 线程预算/流水线由 engine::run 内部决策(冷热探测 + RAYON_NUM_THREADS 覆盖),
    // main 只负责调用与 panic 兜底。
    // catch_unwind 兜底:即使 panic hook 未生效(如 SIGSEGV),catch_unwind 也能捕获 unwind 的 panic
    let rc = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| engine::run(&job)))
        .map(|rc| {
            // -1 用 255 表示(进程退出码无符号)
            if rc < 0 { 255 } else { rc as u8 }
        })
        .unwrap_or_else(|_| {
            // panic 已被 hook 处理(写日志 + 补发 JobDone),这里只返回退出码
            eprintln!("[rust-engine] [main] engine panic 已被捕获,返回 16");
            16
        });
    ExitCode::from(rc)
}

