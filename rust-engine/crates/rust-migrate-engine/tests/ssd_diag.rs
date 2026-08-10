//! P8 盘型检测诊断:打印各盘 is_ssd 判定(cargo test --test ssd_diag -- --nocapture)
//! 只打印不断言 —— 各机器盘型不同,硬编码断言会让别的机器跑挂。

#[test]
fn diag_ssd() {
    for d in ['C', 'D', 'E', 'F', 'G', 'H', 'J'] {
        println!("is_ssd('{}') = {}", d, rust_migrate_engine::win_io::is_ssd(d));
    }
}
