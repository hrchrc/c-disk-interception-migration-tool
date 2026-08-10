//! integration_crash.rs — 崩溃捕获测试(P3 补丁,对应 ADR-014)。
//!
//! 验证 catch_unwind 兜底机制 + engine 对异常输入的防御性:
//! - catch_unwind 捕获 panic,不导致进程崩溃
//! - engine::run 对异常输入(路径不存在/嵌套)返回错误码而非 panic
//!
//! 注意:panic hook 安装在 main.rs(二进制入口),库测试不经 main()。
//! 真实的 panic hook 写崩溃日志 + 补发 JobDone 行为由进程级测试
//! (Python 侧 test_crash.py)覆盖,不在 Rust 集成测试范围。

mod common;

use rust_migrate_engine::engine;
use std::panic::{catch_unwind, AssertUnwindSafe};

/// catch_unwind 捕获 panic:模拟 engine::run 内部 panic,验证不导致进程崩溃。
///
/// 场景:engine::run 本身对异常输入(路径不存在/字段非法)返回错误码而非 panic,
/// 但若内部逻辑 bug 导致 panic,catch_unwind(main.rs 层)应捕获并返回 16。
/// 这里直接用 catch_unwind 包裹一个会 panic 的闭包,验证捕获机制本身工作。
#[test]
fn catch_unwind_catches_panic() {
    let result = catch_unwind(AssertUnwindSafe(|| {
        // 模拟 engine::run 内部 panic(如数组越界、unwrap 失败)
        let v: Vec<u8> = vec![];
        v[0] // 越界 panic
    }));
    assert!(result.is_err(), "catch_unwind 未捕获 panic");
}

/// engine::run 对异常输入不 panic:路径不存在返回错误码,不崩溃。
///
/// 验证 engine 层的防御性编程:异常输入走错误码路径,不触发 panic。
/// 若 engine::run 对异常输入 panic,则 catch_unwind 兜底(但这是 bug,应修)。
#[test]
fn engine_run_handles_bad_input_without_panic() {
    let base = common::temp_dir("crash_bad_input");
    let src = base.join("nonexistent_src");
    let dst = base.join("dst");

    let job = common::copy_job(&src, &dst);
    // 用 catch_unwind 包裹,验证不 panic(应返回错误码 16)
    let result = catch_unwind(AssertUnwindSafe(|| engine::run(&job)));
    assert!(result.is_ok(), "engine::run 对不存在的源路径 panic(应返回错误码)");
    let rc = result.unwrap();
    assert_eq!(rc, 16, "源不存在应返回 rc=16,实际 rc={}", rc);

    common::cleanup(&base);
}

/// engine::run 对嵌套路径不 panic:validate() 拦截并返回错误码。
///
/// 对应 ADR-012 的栈溢出修复:嵌套路径应在 run() 开头被 validate() 拦截,
/// 而非进入 walk 递归导致栈溢出(栈溢出是 SIGSEGV,catch_unwind 无法捕获)。
#[test]
fn engine_run_handles_nested_paths_without_stack_overflow() {
    let base = common::temp_dir("crash_nested");
    let src = base.join("parent");
    let dst = base.join("parent").join("child"); // dst 在 src 内部

    common::write_file(&src.join("a.txt"), b"content");

    let job = common::copy_job(&src, &dst);
    let result = catch_unwind(AssertUnwindSafe(|| engine::run(&job)));
    assert!(result.is_ok(), "engine::run 对嵌套路径 panic(应被 validate 拦截)");
    let rc = result.unwrap();
    assert_eq!(rc, 16, "嵌套路径应返回 rc=16,实际 rc={}", rc);

    common::cleanup(&base);
}
