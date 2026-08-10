//! 集成测试共享辅助函数。
//! 用法:在 tests/ 下的测试文件中 `mod common;` 引入。

use rust_migrate_engine::job::Job;
use std::path::{Path, PathBuf};

/// 创建临时测试目录,返回路径。prefix 用于区分不同测试。
pub fn temp_dir(prefix: &str) -> PathBuf {
    let mut p = std::env::temp_dir();
    p.push(format!(
        "cdrive_test_{}_{}_{}",
        prefix,
        std::process::id(),
        rand_u32()
    ));
    std::fs::create_dir_all(&p).unwrap();
    p
}

/// 简单随机数(不引入 rand crate)。
fn rand_u32() -> u32 {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    nanos.wrapping_mul(2654435761)
}

/// 从 JSON 字符串构造 Job(serde 自动填默认值)。
pub fn job_from_json(json: &str) -> Job {
    serde_json::from_str(json).unwrap_or_else(|e| panic!("job.json 解析失败: {}", e))
}

/// 构造 copy 模式 Job。
pub fn copy_job(src: &Path, dst: &Path) -> Job {
    job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "copy",
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}},
            "large_file_threshold_mb": 1,
            "fast_move_same_volume": false
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\")
    ))
}

/// 构造 mirror 模式 Job。
pub fn mirror_job(src: &Path, dst: &Path, purge_enabled: bool, dry_run: bool) -> Job {
    job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "mirror",
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}},
            "large_file_threshold_mb": 1,
            "fast_move_same_volume": false,
            "purge": {{"enabled": {}, "soft_delete": true, "dry_run": {}}},
            "write_through": true
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\"),
        purge_enabled,
        dry_run
    ))
}

/// 构造带 cancel_token 的 Job。
pub fn copy_job_with_cancel(src: &Path, dst: &Path, cancel_token: &Path) -> Job {
    job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "copy",
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}},
            "large_file_threshold_mb": 1,
            "fast_move_same_volume": false,
            "write_through": true,
            "cancel_token": "{}"
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\"),
        cancel_token.display().to_string().replace('\\', "\\\\")
    ))
}

/// 写入测试文件(指定内容)。
pub fn write_file(path: &Path, content: &[u8]) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    std::fs::write(path, content).unwrap();
}

/// 写入大文件(指定大小,固定模式数据)。
pub fn write_large_file(path: &Path, size: usize) {
    use std::io::Write;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    let mut f = std::fs::File::create(path).unwrap();
    let pattern = bytes_pattern();
    let mut written = 0;
    while written < size {
        let n = std::cmp::min(pattern.len(), size - written);
        f.write_all(&pattern[..n]).unwrap();
        written += n;
    }
}

/// 4KB 固定模式数据(非全 0,防止 0 填充通过校验)。
fn bytes_pattern() -> Vec<u8> {
    (0..4096).map(|i| (i * 7 + 13) as u8).collect()
}

/// 计算文件 MD5(用于内容比对)。
pub fn file_md5(path: &Path) -> String {
    use std::io::Read;
    let mut h = md5_compat();
    let mut f = std::fs::File::open(path).unwrap();
    let mut buf = [0u8; 64 * 1024];
    loop {
        let n = f.read(&mut buf).unwrap();
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    h.finalize()
}

/// 简易 MD5(避免引入 md5 crate,用 Rust 标准库手写)。
/// 如果标准库没有,用 CRC32 替代(足以检测内容差异)。
fn md5_compat() -> Md5Compat {
    Md5Compat { crc: 0 }
}

struct Md5Compat {
    crc: u32,
}

impl Md5Compat {
    fn update(&mut self, data: &[u8]) {
        self.crc = rust_migrate_engine::crc32::update(self.crc, data);
    }
    fn finalize(self) -> String {
        format!("crc32:{:08x}", self.crc)
    }
}

/// 递归计算目录树所有文件的 {相对路径: hash} 字典。
pub fn dir_tree_hash(root: &Path) -> std::collections::HashMap<String, String> {
    let mut result = std::collections::HashMap::new();
    for entry in walk_recursive(root) {
        if entry.is_file() {
            let rel = entry.strip_prefix(root).unwrap().to_string_lossy().replace('\\', "/");
            result.insert(rel, file_md5(&entry));
        }
    }
    result
}

/// 递归列出所有文件路径。
fn walk_recursive(root: &Path) -> Vec<PathBuf> {
    let mut result = Vec::new();
    if !root.is_dir() {
        return result;
    }
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        if let Ok(entries) = std::fs::read_dir(&dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.is_dir() {
                    stack.push(path);
                } else {
                    result.push(path);
                }
            }
        }
    }
    result
}

/// 清理临时目录。
pub fn cleanup(path: &Path) {
    let _ = std::fs::remove_dir_all(path);
}
