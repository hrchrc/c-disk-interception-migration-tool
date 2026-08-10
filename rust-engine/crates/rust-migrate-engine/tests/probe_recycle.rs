//! 回收站软删除兼容路径验证:SHFileOperationW 不依赖 IFileOperation COM 类注册。
//! 本机 IFileOperation CLSID 未注册(REGDB_E_CLASSNOTREG)时,RecycleBin::new()
//! 必然失败,recycle_via_shfileop 必须仍能把文件删进回收站。

mod common;

#[test]
fn shfileop_recycles_files_and_dirs() {
    let root = common::temp_dir("shfileop");
    let f = root.join("t.txt");
    let d = root.join("sub");
    std::fs::create_dir_all(&d).unwrap();
    common::write_file(&f, b"recycle me");
    common::write_file(&d.join("inner.txt"), b"inner");

    let rc = rust_migrate_engine::recycle::recycle_via_shfileop(
        &[f.clone()],
        &[d.clone()],
    );
    println!("[probe] recycle_via_shfileop rc={:?}", rc);
    assert!(rc.is_ok(), "SHFileOperationW 软删除必须成功,实际 {:?}", rc);
    assert!(!f.exists(), "文件应已进入回收站");
    assert!(!d.exists(), "目录树应已进入回收站");
}
