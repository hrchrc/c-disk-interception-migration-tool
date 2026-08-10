//! CRC32 区间校验(v5 §4.2 末段)。
//!
//! 用途:checkpoint sidecar 记录 [ckpt_base, written) 区间的 CRC32,
//! 续传时重算该区间验证数据完整性。断电/崩溃导致缓存丢失时,
//! 目标文件最后一块可能只写了一半,文件大小却反映目标长度。
//! CRC32 检测出这种损坏 → 判定 ckpt 不可信 → 整文件重传。
//!
//! 算法:IEEE 802.3 CRC32(与 zip/png/zlib 同),多项式 0xEDB88320(反转)。
//! 无外部依赖,自实现约 40 行。性能:~4 GB/s(SIMD 优化后,本实现 ~1 GB/s 足够)。

// IEEE 802.3 反转多项式
const POLY: u32 = 0xEDB88320;

/// 预计算表(懒加载,首次调用时初始化)。
fn table() -> &'static [u32; 256] {
    use std::sync::OnceLock;
    static TABLE: OnceLock<[u32; 256]> = OnceLock::new();
    TABLE.get_or_init(|| {
        let mut t = [0u32; 256];
        for i in 0..256u32 {
            let mut c = i;
            for _ in 0..8 {
                if c & 1 != 0 {
                    c = POLY ^ (c >> 1);
                } else {
                    c >>= 1;
                }
            }
            t[i as usize] = c;
        }
        t
    })
}

/// 更新 CRC32 状态:在当前 crc 基础上追加 data 的 CRC。
/// 初始 crc 应为 0(内部会 XOR 0xFFFFFFFF)。
pub fn update(crc: u32, data: &[u8]) -> u32 {
    let tbl = table();
    let mut c = crc ^ 0xFFFF_FFFF;
    for &b in data {
        let idx = ((c ^ b as u32) & 0xFF) as usize;
        c = tbl[idx] ^ (c >> 8);
    }
    c ^ 0xFFFF_FFFF
}

/// 计算整个 data 切片的 CRC32(便捷封装,等价于 update(0, data))。
#[allow(dead_code)]
pub fn compute(data: &[u8]) -> u32 {
    update(0, data)
}

// ============================================================
// 单元测试
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;

    /// 空数据 CRC32 = 0(标准校验值)
    #[test]
    fn test_empty() {
        assert_eq!(compute(b""), 0);
    }

    /// "123456789" 的 CRC32 = 0xCBF43926(标准校验值,IEEE 802.3)
    #[test]
    fn test_standard_check_value() {
        assert_eq!(compute(b"123456789"), 0xCBF4_3926);
    }

    /// 增量更新等价于一次性计算
    #[test]
    fn test_incremental_equivalence() {
        let data = b"hello world, this is a test of crc32 incremental update";
        let whole = compute(data);
        let mut crc = 0;
        for chunk in data.chunks(7) {
            crc = update(crc, chunk);
        }
        assert_eq!(crc, whole);
    }

    /// 不同数据产生不同 CRC32
    #[test]
    fn test_different_data() {
        assert_ne!(compute(b"foo"), compute(b"bar"));
    }

    /// CRC32 检测单字节翻转
    #[test]
    fn test_detect_corruption() {
        let original = b"the quick brown fox jumps over the lazy dog";
        let corrupted = b"the quick brown fox jumps over the lazy hog"; // d→h
        assert_ne!(compute(original), compute(corrupted));
    }

    /// 大数据量一致性(4MB 块,模拟 LARGE_BLOCK)
    #[test]
    fn test_large_block() {
        let data: Vec<u8> = (0..4 * 1024 * 1024).map(|i| (i & 0xFF) as u8).collect();
        let crc1 = compute(&data);
        // 增量计算
        let mut crc2 = 0;
        for chunk in data.chunks(1024) {
            crc2 = update(crc2, chunk);
        }
        assert_eq!(crc1, crc2);
        // 修改 1 字节 → CRC32 必变
        let mut modified = data.clone();
        modified[0] ^= 1;
        assert_ne!(compute(&modified), crc1);
    }
}
