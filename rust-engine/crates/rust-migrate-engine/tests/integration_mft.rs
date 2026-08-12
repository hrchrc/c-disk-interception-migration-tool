//! MFT 索引合成数据集成测试（方案 8.8 §4.3）。
//!
//! 关键架构决策：把"卷读取"与"解析/构建"分离——卷读取无法在测试中模拟，
//! 但解析/构建可用合成 MFT 记录字节驱动。本测试：
//!   1. 合成一批 MFT 记录流（正常/中文/多命名空间/畸形/未使用/无签名）
//!   2. parse_records_bulk → build_index → precompute_dir_sizes → write_index_file
//!   3. 输出两份文件（当前目录）：
//!      - mft_mock_raw.bin  — 合成记录原始字节（Python 侧用同一份跑 Cython 路径）
//!      - mft_mock_out.idx  — Rust 二进制索引（Python 侧逐字段对照）
//!
//! Python 对照：bin/test_mft_rust_mock.py（需 src/mft 下已编译的 mft_fast.pyd）。

use rust_migrate_engine::mft_index::{self, MftRecord};
use std::path::Path;

fn put_u16(rec: &mut [u8], off: usize, v: u16) {
    rec[off..off + 2].copy_from_slice(&v.to_le_bytes());
}

fn put_u32(rec: &mut [u8], off: usize, v: u32) {
    rec[off..off + 4].copy_from_slice(&v.to_le_bytes());
}

fn put_u64(rec: &mut [u8], off: usize, v: u64) {
    rec[off..off + 8].copy_from_slice(&v.to_le_bytes());
}

/// 单条合成 MFT 记录的规格。
struct MockSpec<'a> {
    name: &'a str,
    ns: u8,
    /// 第二 $FILE_NAME（命名空间选择测试）
    extra_fn: Option<(&'a str, u8)>,
    parent: u32,
    is_dir: bool,
    size: u64,
    is_reparse: bool,
    in_use: bool,
}

/// 构造一条 1024B 的合成 MFT 记录（合法 USA fixup + $FILE_NAME + $DATA/$REPARSE）。
fn make_record(spec: &MockSpec) -> Vec<u8> {
    let mut rec = vec![0u8; 1024];
    rec[0..4].copy_from_slice(b"FILE");
    // USA：usa_offset=0x30, usa_count=3（check + 2 个扇区替换值）
    put_u16(&mut rec, 0x04, 0x30);
    put_u16(&mut rec, 0x06, 3);
    put_u16(&mut rec, 0x30, 0xABCD); // check value
    put_u16(&mut rec, 0x32, 0x1111);
    put_u16(&mut rec, 0x34, 0x2222);
    // 每个扇区尾 2 字节写 check value（fixup 时会被 usa 值替换）
    put_u16(&mut rec, 0x1FE, 0xABCD);
    put_u16(&mut rec, 0x3FE, 0xABCD);
    // 记录头
    put_u16(&mut rec, 0x14, 0x38); // first_attr_offset
    let flags = (if spec.in_use { 1u16 } else { 0 }) | (if spec.is_dir { 2u16 } else { 0 });
    put_u16(&mut rec, 0x16, flags);

    let mut pos = 0x38usize;
    // $FILE_NAME 属性（1-2 个，均驻留）
    let mut fns: Vec<(&str, u8)> = vec![(spec.name, spec.ns)];
    if let Some((n, ns)) = spec.extra_fn {
        fns.push((n, ns));
    }
    for (name, ns) in fns {
        let name_u16: Vec<u16> = name.encode_utf16().collect();
        let content_len = 0x42 + name_u16.len() * 2;
        let attr_len = 0x18 + content_len;
        put_u32(&mut rec, pos, 0x30); // ATTR_FILE_NAME
        put_u32(&mut rec, pos + 4, attr_len as u32);
        rec[pos + 8] = 0; // 驻留
        put_u32(&mut rec, pos + 0x10, content_len as u32);
        put_u16(&mut rec, pos + 0x14, 0x18); // content offset
        let c = pos + 0x18;
        put_u64(&mut rec, c, spec.parent as u64); // parent ref
        rec[c + 0x40] = name_u16.len() as u8;     // name len (chars)
        rec[c + 0x41] = ns;                        // namespace
        for (i, u) in name_u16.iter().enumerate() {
            put_u16(&mut rec, c + 0x42 + i * 2, *u);
        }
        pos += attr_len;
    }
    // $DATA：>4GB 用非驻留（real_size u64，驻留 content_length 是 u32 存不下）
    if !spec.is_dir {
        if spec.size > u32::MAX as u64 {
            let attr_len = 0x40 + 1; // 非驻留头 0x40 + 空 run list 终止符
            put_u32(&mut rec, pos, 0x80);
            put_u32(&mut rec, pos + 4, attr_len as u32);
            rec[pos + 8] = 1; // 非驻留
            put_u16(&mut rec, pos + 0x20, 0x40); // run list offset
            // 非驻留 $DATA 大小字段（与 mft_index.rs 读取逻辑一致）：
            // 0x28 = AllocatedSize（实际占用，mft_index 读取它统计真实占用）
            // 0x30 = DataLength（文件逻辑大小，测试原始字节供 Python 侧对照）
            // 普通非稀疏文件两者相等
            put_u64(&mut rec, pos + 0x28, spec.size); // allocated size
            put_u64(&mut rec, pos + 0x30, spec.size); // data length
            pos += attr_len;
        } else {
            let data_len = 0x18 + 0x18;
            put_u32(&mut rec, pos, 0x80);
            put_u32(&mut rec, pos + 4, data_len as u32);
            rec[pos + 8] = 0;
            put_u32(&mut rec, pos + 0x10, spec.size as u32);
            put_u16(&mut rec, pos + 0x14, 0x18);
            pos += data_len;
        }
    }
    // $REPARSE_POINT
    if spec.is_reparse {
        let rl = 0x18 + 0x10;
        put_u32(&mut rec, pos, 0xC0);
        put_u32(&mut rec, pos + 4, rl as u32);
        rec[pos + 8] = 0;
        pos += rl;
    }
    put_u32(&mut rec, pos, 0xFFFF_FFFF); // ATTR_END
    rec
}

/// 畸形记录：$FILE_NAME 的 attr_len=0x10（<0x18 下界，M10 守卫应 break 跳过）。
fn make_bad_attr_len() -> Vec<u8> {
    let mut rec = vec![0u8; 1024];
    rec[0..4].copy_from_slice(b"FILE");
    put_u16(&mut rec, 0x04, 0x30);
    put_u16(&mut rec, 0x06, 3);
    put_u16(&mut rec, 0x30, 0xABCD);
    put_u16(&mut rec, 0x32, 0x1111);
    put_u16(&mut rec, 0x34, 0x2222);
    put_u16(&mut rec, 0x1FE, 0xABCD);
    put_u16(&mut rec, 0x3FE, 0xABCD);
    put_u16(&mut rec, 0x14, 0x38);
    put_u16(&mut rec, 0x16, 0x01); // in use
    put_u32(&mut rec, 0x38, 0x30); // FILE_NAME
    put_u32(&mut rec, 0x38 + 4, 0x10); // attr_len < 0x18 → 守卫 break
    rec
}

/// 畸形记录：USA 越界（usa_count 超大 → fixup 跳过，属性仍应正常解析）。
fn make_bad_usa() -> Vec<u8> {
    let mut rec = vec![0u8; 1024];
    rec[0..4].copy_from_slice(b"FILE");
    put_u16(&mut rec, 0x04, 0x30);
    put_u16(&mut rec, 0x06, 0xFFFF); // usa_count 越界
    put_u16(&mut rec, 0x14, 0x38);
    put_u16(&mut rec, 0x16, 0x01); // in use
    // 正常 $FILE_NAME（复用 make_record 的属性布局）
    let spec = MockSpec {
        name: "after_bad_usa.txt",
        ns: 1,
        extra_fn: None,
        parent: 24, // 与 root 自引用一致（root 的直接子项）
        is_dir: false,
        size: 42,
        is_reparse: false,
        in_use: true,
    };
    // 复制属性部分（0x38 起）到本记录
    let good = make_record(&spec);
    rec[0x38..].copy_from_slice(&good[0x38..]);
    rec
}

#[test]
fn mock_index_roundtrip() {
    // 合成记录流（record_num = start(24) + 下标；root 自引用 parent=24 模拟
    // 真实盘 root(parent_ref=5) 的自引用语义；含目录层级/命名空间选择/畸形）
    let specs = vec![
        MockSpec { name: "root", ns: 1, extra_fn: None, parent: 24, is_dir: true, size: 0, is_reparse: false, in_use: true },                    // 0 → 24: root（自引用）
        MockSpec { name: "file1.txt", ns: 1, extra_fn: None, parent: 24, is_dir: false, size: 12345, is_reparse: false, in_use: true },          // 1 → 25
        MockSpec { name: "中文文件.txt", ns: 1, extra_fn: None, parent: 24, is_dir: false, size: 999, is_reparse: false, in_use: true },         // 2 → 26
        MockSpec { name: "subdir", ns: 1, extra_fn: None, parent: 24, is_dir: true, size: 0, is_reparse: false, in_use: true },                  // 3 → 27
        MockSpec { name: "file_in_subdir.bin", ns: 1, extra_fn: None, parent: 27, is_dir: false, size: 777, is_reparse: false, in_use: true }, // 4 → 28
        MockSpec { name: "SHORT~1.TXT", ns: 2, extra_fn: Some(("Long Name.txt", 1)), parent: 27, is_dir: false, size: 100, is_reparse: false, in_use: true },  // 5 → 29: DOS + Win32 → 选 Win32
        MockSpec { name: "posix_name", ns: 0, extra_fn: Some(("win_name.txt", 1)), parent: 27, is_dir: false, size: 200, is_reparse: false, in_use: true },    // 6 → 30: POSIX + Win32 → 选 Win32
        MockSpec { name: "win_first.txt", ns: 1, extra_fn: Some(("posix_second", 0)), parent: 27, is_dir: false, size: 300, is_reparse: false, in_use: true }, // 7 → 31: Win32 先到 → 保留
        MockSpec { name: "link_dir", ns: 1, extra_fn: None, parent: 24, is_dir: true, size: 0, is_reparse: true, in_use: true },                 // 8 → 32: reparse 目录
        MockSpec { name: "unused", ns: 1, extra_fn: None, parent: 24, is_dir: false, size: 1, is_reparse: false, in_use: false },                // 9 → 33: 未使用 → 跳过
        MockSpec { name: "deep_dir", ns: 1, extra_fn: None, parent: 27, is_dir: true, size: 0, is_reparse: false, in_use: true },               // 10 → 34: subdir 的子目录
        MockSpec { name: "deep_file.txt", ns: 1, extra_fn: None, parent: 34, is_dir: false, size: 555, is_reparse: false, in_use: true },       // 11 → 35
        MockSpec { name: "bigfile.bin", ns: 1, extra_fn: None, parent: 24, is_dir: false, size: 5_000_000_000, is_reparse: false, in_use: true }, // 12 → 36: >4GB 溢出表用例
    ];

    // 畸形记录拼在后面（record_num 37/38/39）
    let mut raw: Vec<u8> = Vec::new();
    for spec in &specs {
        raw.extend_from_slice(&make_record(spec));
    }
    raw.extend_from_slice(&make_bad_attr_len()); // 13 → 37: attr_len 下界 → 跳过
    raw.extend_from_slice(&make_bad_usa());      // 14 → 38: USA 越界 → 仍解析
    let mut bad_sig = make_record(&specs[1]);
    bad_sig[0..4].copy_from_slice(b"BAAD");      // 15 → 39: 无签名 → 跳过
    raw.extend_from_slice(&bad_sig);

    let count = raw.len() / 1024;
    assert_eq!(count, 16, "mock 记录数应为 16");

    // 解析（start_record_num=24：>=24 的文件会计入 total_size，与 fast_scan 一致）
    let mut bulk = raw.clone();
    let recs = mft_index::parse_records_bulk(&mut bulk, 1024, 512, 24)
        .expect("解析合成记录应成功");
    // 期望：16 条中跳过 unused(33)/bad_attr(37)/bad_sig(39) → 13 条
    assert_eq!(recs.len(), 13, "应解析出 13 条（跳过 3 条）");
    // 命名空间选择验证
    let by_num = |n: u32| -> &MftRecord { recs.iter().find(|r| r.record_num == n).unwrap() };
    assert_eq!(by_num(29).name, "Long Name.txt", "DOS+Win32 应选 Win32");
    assert_eq!(by_num(30).name, "win_name.txt", "POSIX+Win32 应选 Win32");
    assert_eq!(by_num(31).name, "win_first.txt", "Win32 先到应保留");
    assert_eq!(by_num(38).name, "after_bad_usa.txt", "USA 越界仍应解析");
    // reparse / 大小 / 目录标志
    assert!(by_num(32).is_reparse && by_num(32).is_dir);
    assert_eq!(by_num(25).size, 12345);
    assert_eq!(by_num(36).size, 5_000_000_000, "bigfile 完整大小应保留");

    // 构建索引 + 预计算 + 写出
    let mut index = mft_index::build_index(&recs);
    mft_index::precompute_dir_sizes(&mut index);
    let out_idx = Path::new(env!("CARGO_MANIFEST_DIR")).join("mft_mock_out.idx");
    mft_index::write_index_file(&out_idx, &index).expect("写出索引应成功");

    // 原始字节输出（Python 侧跑同一份数据）
    let out_raw = Path::new(env!("CARGO_MANIFEST_DIR")).join("mft_mock_raw.bin");
    std::fs::write(&out_raw, &raw).expect("写出 raw 应成功");

    // 统计验证
    assert_eq!(index.file_count, 9); // 13 条 - 4 目录(root/subdir/link_dir/deep_dir)
    assert_eq!(index.dir_count, 4);
    // 全部记录号 >= 24 → total_size 全部计入（含 bigfile 5GB）：
    // 12345+999+777+100+200+300+555+42+5_000_000_000
    assert_eq!(index.total_size, 5_000_015_318);
    // root_index：记录号 5 不存在（root 是记录 24），u32::MAX 表示未找到
    assert_eq!(index.root_index, u32::MAX);
    // v2 溢出表：bigfile（解析后 index 11）
    assert_eq!(index.size_ovf_idx, vec![11]);
    assert_eq!(index.size_ovf_val, vec![5_000_000_000]);
    // 拓扑预计算：root(24) 自引用——pending 含 root 自己，普通子目录减完
    // 后仍欠 1（root 自身），但 reparse 子目录 link_dir(32) 处理时也会通知
    // 父 → pending 减到 0 → root 入队被处理（与 fast_scan 对真实盘的语义一致）。
    // 解析后 13 条的 index：root=0, file1=1, 中文=2, subdir=3, file_in=4,
    // Long=5, win_name=6, win_first=7, link_dir=8, deep_dir=9, deep_file=10,
    // bigfile=11, after=12
    assert_eq!(index.dir_size_idx, vec![0, 3, 8, 9]);
    assert_eq!(index.dir_size_val, vec![5_000_015_318, 1932, 0, 555]);
}
