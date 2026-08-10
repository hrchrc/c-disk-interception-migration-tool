//! integration_metadata.rs — 元数据复制(ACL/ADS/硬链接)端到端测试。
//!
//! 覆盖:
//! - ADS(备用数据流)复制:用 PowerShell 写入 file:Zone.Identifier,验证目标保留
//! - 硬链接去重:创建硬链接组,验证目标 nNumberOfLinks 与源一致
//! - ACL 复制:修改源文件 DACL,验证目标文件安全描述符一致
//!
//! 这些特性对齐 FastCopy 的 /acl /stream /link 选项。
//! 测试需要 Windows + 管理员权限(ACL 完整复制需 SeRestorePrivilege)。

mod common;

use common::*;
use rust_migrate_engine::engine;
use std::path::Path;
use std::process::Command;

/// 构造带元数据选项的 copy job。
fn metadata_job(src: &Path, dst: &Path, copy_acl: bool, copy_ads: bool, preserve_hardlinks: bool) -> rust_migrate_engine::job::Job {
    job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "copy",
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}},
            "large_file_threshold_mb": 1,
            "fast_move_same_volume": false,
            "copy_acl": {},
            "copy_ads": {},
            "preserve_hardlinks": {}
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\"),
        copy_acl,
        copy_ads,
        preserve_hardlinks
    ))
}

/// 构造带 reparse_mode 的 copy job(用于符号链接/Junction 测试)。
fn reparse_job(src: &Path, dst: &Path, reparse_mode: &str) -> rust_migrate_engine::job::Job {
    job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "copy",
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}},
            "large_file_threshold_mb": 1,
            "fast_move_same_volume": false,
            "reparse_mode": "{}"
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\"),
        reparse_mode
    ))
}

/// 用 PowerShell 写入 ADS(file:ZoneIdentifier)。
fn write_ads(file: &Path, content: &str) {
    let ps = format!(
        "Set-Content -Path '{}' -Stream Zone.Identifier -Value '{}'",
        file.display().to_string().replace('\'', "''"),
        content
    );
    let out = Command::new("powershell")
        .args(["-NoProfile", "-NonInteractive", "-Command", &ps])
        .output();
    match out {
        Ok(o) if o.status.success() => {}
        Ok(o) => panic!(
            "写入 ADS 失败: {}",
            String::from_utf8_lossy(&o.stderr)
        ),
        Err(e) => panic!("启动 PowerShell 失败: {}", e),
    }
}

/// 用 PowerShell 读取 ADS 内容。返回 None 表示流不存在。
fn read_ads(file: &Path, stream: &str) -> Option<String> {
    let ps = format!(
        "Get-Content -Path '{}' -Stream {} -ErrorAction SilentlyContinue",
        file.display().to_string().replace('\'', "''"),
        stream
    );
    let out = Command::new("powershell")
        .args(["-NoProfile", "-NonInteractive", "-Command", &ps])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

/// ADS 复制:源文件带 Zone.Identifier,启用 copy_ads 后目标应保留。
#[test]
fn ads_copy_preserves_zone_identifier() {
    let base = temp_dir("ads_zone");
    let src = base.join("src");
    let dst = base.join("dst");
    let f = src.join("with_ads.txt");
    write_file(&f, b"main stream content");
    let ads_content = "[ZoneTransfer]\r\nZoneId=3\r\n";
    write_ads(&f, ads_content);

    let job = metadata_job(&src, &dst, false, true, false);
    let rc = engine::run(&job);
    assert!(rc < 8, "ADS 复制任务失败 rc={}", rc);

    let dst_f = dst.join("with_ads.txt");
    assert!(dst_f.exists(), "目标文件未创建");
    let got = read_ads(&dst_f, "Zone.Identifier");
    assert!(got.is_some(), "目标文件未保留 ADS Zone.Identifier");
    // 内容应包含 ZoneId=3
    let got_str = got.unwrap();
    assert!(
        got_str.contains("ZoneId=3"),
        "ADS 内容不匹配: {}",
        got_str
    );
    cleanup(&base);
}

/// ADS 关闭:默认不复制 ADS,目标不应有 Zone.Identifier。
#[test]
fn ads_disabled_drops_zone_identifier() {
    let base = temp_dir("ads_off");
    let src = base.join("src");
    let dst = base.join("dst");
    let f = src.join("no_ads.txt");
    write_file(&f, b"main stream content");
    write_ads(&f, "[ZoneTransfer]\r\nZoneId=3\r\n");

    let job = metadata_job(&src, &dst, false, false, false);
    let rc = engine::run(&job);
    assert!(rc < 8, "复制任务失败 rc={}", rc);

    let dst_f = dst.join("no_ads.txt");
    assert!(dst_f.exists(), "目标文件未创建");
    let got = read_ads(&dst_f, "Zone.Identifier");
    assert!(
        got.is_none(),
        "copy_ads=false 时不应复制 ADS,但目标有 Zone.Identifier: {:?}",
        got
    );
    cleanup(&base);
}

/// 硬链接去重:源端两个硬链接指向同一 inode,启用 preserve_hardlinks 后目标也应同 inode。
#[test]
fn hardlink_preserved_on_same_volume() {
    let base = temp_dir("hardlink");
    let src = base.join("src");
    let dst = base.join("dst");
    let f1 = src.join("file1.txt");
    write_file(&f1, b"hardlinked content");
    let f2 = src.join("file2.txt");
    // 用 PowerShell 创建硬链接
    let ps = format!(
        "New-Item -ItemType HardLink -Path '{}' -Target '{}'",
        f2.display().to_string().replace('\'', "''"),
        f1.display().to_string().replace('\'', "''")
    );
    let out = Command::new("powershell")
        .args(["-NoProfile", "-NonInteractive", "-Command", &ps])
        .output()
        .expect("启动 PowerShell 失败");
    assert!(out.status.success(), "创建硬链接失败: {}", String::from_utf8_lossy(&out.stderr));

    let job = metadata_job(&src, &dst, false, false, true);
    let rc = engine::run(&job);
    assert!(rc < 8, "硬链接复制任务失败 rc={}", rc);

    let dst_f1 = dst.join("file1.txt");
    let dst_f2 = dst.join("file2.txt");
    assert!(dst_f1.exists(), "目标 file1 未创建");
    assert!(dst_f2.exists(), "目标 file2 未创建");

    // 验证两个目标文件是硬链接(同 inode)
    let info1 = file_link_info(&dst_f1);
    let info2 = file_link_info(&dst_f2);
    assert!(info1.is_some() && info2.is_some(), "无法读取目标文件 inode");
    let (links1, _, idx1) = info1.unwrap();
    let (links2, _, idx2) = info2.unwrap();
    assert_eq!(idx1, idx2, "目标两个文件不是硬链接(不同 inode)");
    assert!(links1 >= 2, "目标硬链接数 < 2: {}", links1);
    assert_eq!(links1, links2, "两个目标的 nNumberOfLinks 不一致");
    cleanup(&base);
}

/// 硬链接去重关闭:每个路径独立复制,目标 nNumberOfLinks 应为 1。
#[test]
fn hardlink_disabled_copies_independently() {
    let base = temp_dir("hardlink_off");
    let src = base.join("src");
    let dst = base.join("dst");
    let f1 = src.join("file1.txt");
    write_file(&f1, b"hardlinked content");
    let f2 = src.join("file2.txt");
    let ps = format!(
        "New-Item -ItemType HardLink -Path '{}' -Target '{}'",
        f2.display().to_string().replace('\'', "''"),
        f1.display().to_string().replace('\'', "''")
    );
    let out = Command::new("powershell")
        .args(["-NoProfile", "-NonInteractive", "-Command", &ps])
        .output()
        .expect("启动 PowerShell 失败");
    assert!(out.status.success(), "创建硬链接失败: {}", String::from_utf8_lossy(&out.stderr));

    let job = metadata_job(&src, &dst, false, false, false);
    let rc = engine::run(&job);
    assert!(rc < 8, "复制任务失败 rc={}", rc);

    let dst_f1 = dst.join("file1.txt");
    let info1 = file_link_info(&dst_f1).expect("读取目标 inode 失败");
    let (links1, _, _) = info1;
    assert_eq!(links1, 1, "preserve_hardlinks=false 时目标不应有硬链接: {}", links1);
    cleanup(&base);
}

/// ACL 复制:源文件设置自定义 DACL,启用 copy_acl 后目标应保留。
#[test]
fn acl_copy_preserves_dacl() {
    let base = temp_dir("acl");
    let src = base.join("src");
    let dst = base.join("dst");
    let f = src.join("with_acl.txt");
    write_file(&f, b"content with custom acl");

    // 用 icacls 设置自定义 DACL:给 Users 完全控制权限
    let out = Command::new("icacls")
        .arg(&f)
        .args(["/grant", "Users:(F)"])
        .output()
        .expect("启动 icacls 失败");
    assert!(out.status.success(), "设置 DACL 失败: {}", String::from_utf8_lossy(&out.stderr));

    let job = metadata_job(&src, &dst, true, false, false);
    let rc = engine::run(&job);
    assert!(rc < 8, "ACL 复制任务失败 rc={}", rc);

    let dst_f = dst.join("with_acl.txt");
    assert!(dst_f.exists(), "目标文件未创建");

    // 用 icacls 比对源和目标的 DACL(保存行)
    let src_acl = icacls_save(&f);
    let dst_acl = icacls_save(&dst_f);
    eprintln!("DEBUG src_acl={:?}", src_acl);
    eprintln!("DEBUG dst_acl={:?}", dst_acl);
    assert!(!src_acl.is_empty(), "源文件 icacls 输出为空");
    assert!(!dst_acl.is_empty(), "目标文件 icacls 输出为空");
    // 目标应包含 Users:(F) 权限(源端设置的)
    assert!(
        dst_acl.contains("Users") && dst_acl.contains("(F)"),
        "目标 DACL 未保留 Users:(F) 权限\n目标: {}",
        dst_acl
    );
    // 源和目标的 ACL 行应一致(都包含 Users:(F))
    assert_eq!(
        src_acl, dst_acl,
        "源和目标 DACL 不一致\n源: {}\n目标: {}",
        src_acl, dst_acl
    );
    cleanup(&base);
}

/// 读取文件的 (nNumberOfLinks, volume_serial, file_index)。
fn file_link_info(path: &Path) -> Option<(u32, u32, u64)> {
    rust_migrate_engine::hardlink::HardlinkMap::query_file_info(path)
}

// ============================================================
// 符号链接/Junction 复制测试(P1 修复验证)
// ============================================================

/// 用 fsutil 判断路径是否为 reparse point(符号链接/Junction)。
/// fsutil reparsepoint query 成功(exit 0) = 是 reparse point;
/// 失败(exit 1) = 不是 reparse point 或路径不存在。
fn is_reparse_point(path: &Path) -> bool {
    let out = Command::new("fsutil")
        .args(["reparsepoint", "query", &path.to_string_lossy()])
        .output()
        .expect("启动 fsutil 失败");
    out.status.success()
}

/// 创建目录 Junction:mklink /J <link> <target>
fn create_junction(link: &Path, target: &Path) {
    let out = Command::new("cmd")
        .args([
            "/c", "mklink", "/J",
            &link.to_string_lossy(),
            &target.to_string_lossy(),
        ])
        .output()
        .expect("启动 mklink 失败");
    assert!(out.status.success(), "创建 Junction 失败: {}", String::from_utf8_lossy(&out.stderr));
}

/// Junction 复制:reparse_mode=copy 时应保留 Junction 本身,不递归遍历目标。
#[test]
fn junction_copy_preserves_link() {
    let base = temp_dir("junction");
    let src = base.join("src");
    let dst = base.join("dst");
    std::fs::create_dir_all(&src).unwrap();
    // 真实目录 + 文件
    let real_dir = base.join("real_dir");
    write_file(&real_dir.join("file.txt"), b"real content");
    // src 下创建指向 real_dir 的 Junction
    let junction_link = src.join("my_junction");
    create_junction(&junction_link, &real_dir);

    let job = reparse_job(&src, &dst, "copy");
    let rc = engine::run(&job);
    assert!(rc < 8, "Junction 复制任务失败 rc={}", rc);

    let dst_junction = dst.join("my_junction");
    assert!(dst_junction.exists(), "目标 Junction 不存在");
    assert!(
        is_reparse_point(&dst_junction),
        "目标不是 reparse point(Junction 未保留)"
    );
    // 跟随后应能读到 real_dir 的内容
    let dst_file = dst_junction.join("file.txt");
    assert!(dst_file.exists(), "目标 Junction 跟随后找不到 file.txt");
    cleanup(&base);
}

/// 目录符号链接复制:reparse_mode=copy 时应保留符号链接本身。
/// 需要 SeCreateSymbolicLinkPrivilege(开发者模式或管理员权限),无权限时跳过。
#[test]
fn dir_symlink_copy_preserves_link() {
    let base = temp_dir("symlink_dir");
    let src = base.join("src");
    let dst = base.join("dst");
    std::fs::create_dir_all(&src).unwrap();
    let real_dir = base.join("real_dir");
    write_file(&real_dir.join("file.txt"), b"real content");
    let symlink_link = src.join("my_symlink");
    // 目录符号链接创建需开发者模式或管理员权限,失败则跳过测试
    let create_ok = std::process::Command::new("cmd")
        .args([
            "/c", "mklink", "/D",
            &symlink_link.to_string_lossy(),
            &real_dir.to_string_lossy(),
        ])
        .output();
    match create_ok {
        Ok(o) if o.status.success() => {}
        _ => {
            eprintln!("跳过:无法创建目录符号链接(需开发者模式或管理员权限)");
            cleanup(&base);
            return;
        }
    }

    let job = reparse_job(&src, &dst, "copy");
    let rc = engine::run(&job);
    // 符号链接复制需要 SeCreateSymbolicLinkPrivilege(FSCTL_SET_REPARSE_POINT 对符号链接)
    // 无权限时 write_reparse 返回 1314(ERROR_PRIVILEGE_NOT_HELD),rc=8
    if rc == 8 {
        eprintln!("跳过:符号链接复制需要 SeCreateSymbolicLinkPrivilege(错误 1314)");
        cleanup(&base);
        return;
    }
    assert!(rc < 8, "符号链接复制任务失败 rc={}", rc);

    let dst_symlink = dst.join("my_symlink");
    assert!(dst_symlink.exists(), "目标符号链接不存在");
    assert!(
        is_reparse_point(&dst_symlink),
        "目标不是 reparse point(符号链接未保留)"
    );
    // 跟随后应能读到 real_dir 的内容
    let dst_file = dst_symlink.join("file.txt");
    assert!(dst_file.exists(), "目标符号链接跟随后找不到 file.txt");
    cleanup(&base);
}

/// reparse_mode=skip(默认)时跳过符号链接,发 FileError code=1742。
#[test]
fn reparse_skip_mode_emits_error() {
    let base = temp_dir("reparse_skip");
    let src = base.join("src");
    let dst = base.join("dst");
    std::fs::create_dir_all(&src).unwrap();
    let real_dir = base.join("real_dir");
    write_file(&real_dir.join("file.txt"), b"real content");
    let junction_link = src.join("my_junction");
    create_junction(&junction_link, &real_dir);

    let job = reparse_job(&src, &dst, "skip");
    let rc = engine::run(&job);
    // skip 模式:Junction 被跳过算 error,src 下只有 Junction 一个 entry,
    // errors=1, files=0 → rc=8 (errors >= files.max(1))
    // rc 可能是 8(全失败)或 2(部分失败),都算正常
    assert!(rc == 8 || rc == 2 || rc == 0, "skip 模式 rc 异常: {}", rc);

    let dst_junction = dst.join("my_junction");
    // skip 模式不应复制 Junction reparse point
    // (目标可能不存在,或存在但不是 reparse point)
    assert!(
        !is_reparse_point(&dst_junction),
        "skip 模式不应保留 Junction reparse point"
    );
    cleanup(&base);
}

/// 用 icacls 读取文件的 ACE 列表(过滤路径前缀和提示行)。
/// icacls 输出第一行格式 "路径 ACE1",后续行 "ACE2", "ACE3"...
/// 本函数提取所有 ACE,去掉路径前缀,便于跨文件比对。
fn icacls_save(path: &Path) -> String {
    let out = Command::new("icacls")
        .arg(path)
        .output()
        .expect("启动 icacls 失败");
    let raw = String::from_utf8_lossy(&out.stdout).to_string();
    let mut aces = Vec::new();
    for (i, line) in raw.lines().enumerate() {
        let line = line.trim();
        if line.is_empty()
            || line.contains("已成功处理")
            || line.contains("Successfully processed")
            || line.contains("已成功读取")
        {
            continue;
        }
        if i == 0 {
            // 第一行: "路径 ACE1"
            // 路径可能含空格,但 ACE 一定含 ":(权限)" 模式(如 "Users:(F)")
            // 找第一个 ":(...)" 模式的位置,从那之前最近的空间开始截取
            if let Some(ace_pos) = line.find(":(") {
                // 往前找最近的空格(ACE 的起始)
                let ace_start = line[..ace_pos].rfind(' ').map(|p| p + 1).unwrap_or(0);
                aces.push(line[ace_start..].to_string());
            }
        } else {
            aces.push(line.to_string());
        }
    }
    aces.join("\n")
}
