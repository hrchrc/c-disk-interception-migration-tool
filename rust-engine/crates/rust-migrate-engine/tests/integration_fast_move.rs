//! 回归:P9 同卷快速移动(原子重命名,零复制)。
//!
//! - 同卷 + 目标不存在:整目录 rename,源消失/目标出现/零复制(rc=1)
//! - 跨卷或目标已存在:回退复制路径(不影响功能)
//! - fast_move_same_volume=false:强制走复制路径

mod common;

use rust_migrate_engine::engine;
use rust_migrate_engine::job::Job;
use std::path::Path;

fn copy_job_json(src: &Path, dst: &Path, fast_move: bool) -> Job {
    common::job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "copy",
            "fast_move_same_volume": {},
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}},
            "large_file_threshold_mb": 1,
            "verify": "hash"
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\"),
        fast_move,
    ))
}

#[test]
fn same_volume_fast_move_moves_dir_atomically() {
    let root = common::temp_dir("fastmove");
    let src = root.join("src");
    let dst = root.join("dst");
    std::fs::create_dir_all(&src).unwrap();
    common::write_file(&src.join("a.txt"), b"alpha");
    common::write_file(&src.join("b.txt"), b"beta");
    std::fs::create_dir_all(&src.join("sub")).unwrap();
    common::write_file(&src.join("sub/c.txt"), b"gamma");

    // 同卷(C: 临时目录) + 目标不存在 → 整目录 rename
    let job = copy_job_json(&src, &dst, true);
    let rc = engine::run(&job);
    assert_eq!(rc, 1, "同卷快速移动必须成功,实际 rc={}", rc);
    assert!(!src.exists(), "源目录应已被原子移动(不再存在)");
    assert!(dst.is_dir(), "目标目录应存在");
    assert!(
        dst.join("a.txt").exists() && dst.join("b.txt").exists() && dst.join("sub/c.txt").exists(),
        "目标应包含全部文件"
    );
    common::cleanup(&root);
}

#[test]
fn fast_move_disabled_falls_back_to_copy() {
    let root = common::temp_dir("fastmove_off");
    let src = root.join("src");
    let dst = root.join("dst");
    std::fs::create_dir_all(&src).unwrap();
    common::write_file(&src.join("a.txt"), b"alpha");

    // 关闭 fast move → 复制路径:源保留、目标出现
    let job = copy_job_json(&src, &dst, false);
    let rc = engine::run(&job);
    assert_eq!(rc, 1, "复制路径必须成功,实际 rc={}", rc);
    assert!(src.join("a.txt").exists(), "复制路径不移动源");
    assert!(dst.join("a.txt").exists(), "目标应有复制内容");
    common::cleanup(&root);
}

#[test]
fn verify_mode_never_triggers_fast_move() {
    // 纯校验任务:同卷 + 目标不存在也不能移动源(校验≠迁移!)
    let root = common::temp_dir("fastmove_verify");
    let src = root.join("src");
    let dst = root.join("dst");
    std::fs::create_dir_all(&src).unwrap();
    common::write_file(&src.join("a.txt"), b"alpha");

    let job = common::job_from_json(&format!(
        r#"{{
            "source": "{}",
            "target": "{}",
            "mode": "verify",
            "fast_move_same_volume": true,
            "retry": {{"max_attempts": 1, "backoff_base_ms": 50, "network_path": false}}
        }}"#,
        src.display().to_string().replace('\\', "\\\\"),
        dst.display().to_string().replace('\\', "\\\\"),
    ));
    let _ = engine::run(&job);
    assert!(src.join("a.txt").exists(), "Verify 模式绝不能移动源");
    assert!(!dst.exists(), "Verify 模式不应创建目标");
    common::cleanup(&root);
}

#[test]
fn symlink_source_never_triggers_fast_move() {
    // 源是符号链接时不能 rename(会移动链接本身而非数据)
    let root = common::temp_dir("fastmove_link");
    let real = root.join("real");
    let src = root.join("src"); // 将作为指向 real 的符号链接
    let dst = root.join("dst");
    std::fs::create_dir_all(&real).unwrap();
    common::write_file(&real.join("a.txt"), b"alpha");
    std::os::windows::fs::symlink_dir(&real, &src).unwrap();

    let job = copy_job_json(&src, &dst, true);
    let rc = engine::run(&job);
    // 源是链接:fast move 被跳过 → 复制路径(reparse skip 模式对链接发 1742 错误)
    // 但绝不能发生"链接被 rename 到 dst"的行为
    assert!(
        !dst.exists() || !dst.join("a.txt").exists() || !dst.is_symlink(),
        "符号链接源不得被整体移动"
    );
    assert!(std::fs::symlink_metadata(&src).is_ok(), "源链接应保留");
    assert!(real.join("a.txt").exists(), "真实数据不得被移动");
    let _ = rc;
    common::cleanup(&root);
}
