//! JSONL 事件序列化。对应执行文档 §2.3.2 事件类型表。
//! 每行一个 JSON 对象,均带 ts(unix 秒,P7 改 RFC3339)与 event 字段。

use serde::Serialize;
use std::io::{self, Write};
use std::time::{SystemTime, UNIX_EPOCH};

/// 所有可能的事件。用 owned String 避免调用方生命周期约束。
#[derive(Debug, Serialize)]
#[serde(tag = "event")]
#[serde(rename_all = "snake_case")]
pub enum Event {
    JobStart {
        source: String,
        target: String,
        mode: String,
    },
    FileStart {
        path: String,
        size: u64,
    },
    FileDone {
        path: String,
        bytes_written: u64,
        duration_ms: u64,
    },
    Progress {
        files_done: u64,
        bytes_done: u64,
        rate_fps: f64,
    },
    FileError {
        path: String,
        code: u32,
        stage: String,
    },
    /// 重试(v5 §4.4):可重试错误命中退避重试,Python 侧可显示"重试中"。
    /// attempt=1 是首次尝试,attempt=2..=max_attempts 是重试。
    Retry {
        path: String,
        code: u32,
        attempt: u32,
    },
    JobDone {
        files_total: u64,
        bytes_total: u64,
        duration_ms: u64,
        rc: i32,
    },
    Cancelled {
        files_done: u64,
        bytes_done: u64,
    },
    /// 通用信息(续传提示等)。key 为类别,value 为人类可读描述。
    Info {
        key: String,
        value: String,
    },
    /// verify=hash 校验阶段:源/目标文件内容不一致(BLAKE3 哈希不同)。
    /// Python 侧可提示"校验失败"并列出路径。
    VerifyMismatch {
        path: String,
    },
    /// purge 阶段:目标多余文件被处理(v5 §4.3)。
    /// soft_deleted=true 表示到回收站,false 表示硬删除,dry_run=true 时仅清单不真删。
    Purge {
        path: String,
        soft_deleted: bool,
        dry_run: bool,
    },
}

impl Event {
    /// 序列化为单行 JSON 并写入 stdout,末尾换行。
    /// 用全局 LineWriter 包裹 stdout:遇 \n 自动 flush,
    /// 避免每事件手动 flush 的 syscall 开销(每文件 2 事件 × 42万文件 = 84万次 flush)。
    /// LineWriter 保留行级实时性(Python 侧按行读取不会阻塞)。
    pub fn emit(&self) {
        use std::sync::{Mutex, OnceLock};
        static OUT: OnceLock<Mutex<std::io::LineWriter<std::io::Stdout>>> = OnceLock::new();
        let out = OUT.get_or_init(|| Mutex::new(std::io::LineWriter::new(io::stdout())));
        if let Ok(mut guard) = out.lock() {
            let v = serde_json::to_value(self).unwrap_or(serde_json::Value::Null);
            let mut v = v;
            if let serde_json::Value::Object(ref mut map) = v {
                map.insert("ts".to_string(), serde_json::Value::from(now_ts()));
            }
            let _ = writeln!(guard, "{}", v);
            // LineWriter 遇 \n 自动 flush,无需手动 flush
        }
    }
}

/// 取当前时间(unix 秒)。P7 阶段替换为 RFC3339 字符串。
fn now_ts() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}
