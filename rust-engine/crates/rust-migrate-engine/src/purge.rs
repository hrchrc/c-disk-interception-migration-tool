//! purge 阶段:遍历目标,删除源中不存在的多余文件(v5 §3.2 + §4.3)。
//!
//! 流程:先复制后 purge,两阶段严格分开(v5 §3.2)。
//!   1. 递归遍历目标目录树
//!   2. 对每个目标路径,检查对应源路径是否存在;不存在 → 标记删除
//!   3. dry_run=true:只发 Purge 事件(清单),不真删
//!   4. soft_delete=true:RecycleBin 批量到回收站(可还原)
//!   5. soft_delete=false:硬删除(文件 remove_file,目录自底向上清理)
//!   6. 先删文件后删空目录(避免 remove_dir_all 误删非空目录里残留的源文件)
//!
//! 安全网:source/target 嵌套校验在 engine::run 做(防止 purge 误删源自身)。

use crate::event::Event;
use crate::job::{Job, Purge};
use crate::recycle::RecycleBin;
use std::path::{Path, PathBuf};

/// purge 结果统计。
#[derive(Default)]
pub struct PurgeStats {
    pub purged_files: u64,
    pub purged_dirs: u64,
    pub bytes_freed: u64,
    pub errors: u32,
}

/// 执行 purge。返回是否被取消(Err(1223)=取消)。
pub fn run(job: &Job, stats: &mut PurgeStats) -> Result<(), u32> {
    let purge_cfg = &job.purge;
    if !purge_cfg.enabled {
        return Ok(());
    }

    // dry-run 模式:只遍历发清单,不删
    if job.mode != crate::job::Mode::Mirror {
        // 只在 mirror 模式 purge(copy 模式不 purge)
        return Ok(());
    }

    let dry_run = purge_cfg.dry_run;
    collect_and_purge(&job.source, &job.target, purge_cfg, dry_run, job, stats)
}

/// 递归收集需 purge 的文件,然后批量处理。
fn collect_and_purge(
    src: &Path,
    dst: &Path,
    purge_cfg: &Purge,
    dry_run: bool,
    job: &Job,
    stats: &mut PurgeStats,
) -> Result<(), u32> {
    // 第一阶段:收集需删除的文件(先文件后目录,目录单独收集待自底向上删)
    let mut files_to_purge: Vec<PathBuf> = Vec::new();
    let mut dirs_to_purge: Vec<PathBuf> = Vec::new();
    let mut collect_errors: u32 = 0;

    collect_purge_list(src, dst, &mut files_to_purge, &mut dirs_to_purge, job, &mut collect_errors)?;
    stats.errors += collect_errors;

    // 第二阶段:执行删除
    if dry_run {
        // dry-run:只发清单事件,不真删
        for f in &files_to_purge {
            Event::Purge {
                path: f.to_string_lossy().into_owned(),
                soft_deleted: false,
                dry_run: true,
            }
            .emit();
            stats.purged_files += 1;
        }
        for d in &dirs_to_purge {
            Event::Purge {
                path: d.to_string_lossy().into_owned(),
                soft_deleted: false,
                dry_run: true,
            }
            .emit();
            stats.purged_dirs += 1;
        }
        return Ok(());
    }

    // 软删除:RecycleBin 批量到回收站
    if purge_cfg.soft_delete {
        let bin = match RecycleBin::new() {
            Ok(b) => b,
            Err(init_code) => {
                // IFileOperation 不可用(常见:系统 CLSID 未注册 REGDB_E_CLASSNOTREG)
                // → 尝试 SHFileOperationW 兼容路径(shell32 导出,不依赖 COM 类注册)
                Event::Info {
                    key: "purge_recycle_compat".to_string(),
                    value: format!(
                        "IFileOperation 初始化失败 code={}, 尝试 SHFileOperationW 软删除",
                        init_code
                    ),
                }
                .emit();
                match crate::recycle::recycle_via_shfileop(&files_to_purge, &dirs_to_purge) {
                    Ok(()) => {
                        // SHFileOperationW 已执行:存在性检查精确统计(部分失败时
                        // 返回值仍为 0,必须逐项确认哪些真的进了回收站)
                        let mut file_sizes: Vec<(PathBuf, u64)> =
                            Vec::with_capacity(files_to_purge.len());
                        for f in &files_to_purge {
                            file_sizes.push((
                                f.clone(),
                                std::fs::metadata(f).map(|m| m.len()).unwrap_or(0),
                            ));
                        }
                        count_purged_soft(stats, &file_sizes, &dirs_to_purge);
                        return Ok(());
                    }
                    Err(sf_code) => {
                        // SHFileOperationW 也失败(如回收站不可用):回退硬删除
                        Event::Info {
                            key: "purge_fallback_hard".to_string(),
                            value: format!(
                                "RecycleBin 初始化失败 code={}, SHFileOperationW 失败 code={}, 回退硬删除",
                                init_code, sf_code
                            ),
                        }
                        .emit();
                        hard_delete_files(&files_to_purge, stats, job)?;
                        hard_delete_dirs(&dirs_to_purge, stats, job)?;
                        return Ok(());
                    }
                }
            }
        };
        // 先记录每个文件大小(queue 前文件还在,大小可读),再入队
        let mut file_sizes: Vec<(PathBuf, u64)> = Vec::with_capacity(files_to_purge.len());
        for f in &files_to_purge {
            let size = std::fs::metadata(f).map(|m| m.len()).unwrap_or(0);
            if let Err(code) = bin.queue_delete(f) {
                Event::FileError {
                    path: f.to_string_lossy().into_owned(),
                    code,
                    stage: "purge_queue".to_string(),
                }
                .emit();
                stats.errors += 1;
                continue;
            }
            file_sizes.push((f.clone(), size));
        }
        // 目录也加入队列(回收站支持删目录)
        for d in &dirs_to_purge {
            if let Err(code) = bin.queue_delete(d) {
                Event::FileError {
                    path: d.to_string_lossy().into_owned(),
                    code,
                    stage: "purge_queue".to_string(),
                }
                .emit();
                stats.errors += 1;
            }
        }
        if let Err(code) = bin.commit() {
            Event::Info {
                key: "purge_commit_err".to_string(),
                value: format!("RecycleBin commit 部分失败 code={}(检查文件存在性确认)", code),
            }
            .emit();
            // commit 失败不代表全部失败(FOF_NOERRORUI 跳过错误项继续删),不在此累加 errors
            // 下方逐项检查存在性来精确统计
        }
        // commit 后重新检查:只对确实被删除的文件发 Purge 事件 + 计数
        // 避免误报"已删除"但实际因权限/占用未删除(v5 §4.3 数据完整性要求)
        count_purged_soft(stats, &file_sizes, &dirs_to_purge);
        return Ok(());
    }

    // 硬删除
    hard_delete_files(&files_to_purge, stats, job)?;
    hard_delete_dirs(&dirs_to_purge, stats, job)?;
    Ok(())
}

/// 递归遍历目标,收集源中不存在的文件/目录。
fn collect_purge_list(
    src: &Path,
    dst: &Path,
    files: &mut Vec<PathBuf>,
    dirs: &mut Vec<PathBuf>,
    job: &Job,
    errors: &mut u32,
) -> Result<(), u32> {
    if job.cancel_requested() {
        return Err(1223);
    }
    let entries = match std::fs::read_dir(dst) {
        Ok(e) => e,
        Err(_) => return Ok(()), // 目标目录不存在,无东西可 purge
    };
    for entry in entries {
        if job.cancel_requested() {
            return Err(1223);
        }
        let entry = match entry {
            Ok(e) => e,
            Err(_) => {
                *errors += 1;
                continue;
            }
        };
        let ft = match entry.file_type() {
            Ok(t) => t,
            Err(_) => {
                *errors += 1;
                continue;
            }
        };
        let dst_path = entry.path();
        let name = entry.file_name();
        let src_path = src.join(name);

        if ft.is_dir() {
            if !src_path.exists() {
                // 源目录不存在 → 整个目录需 purge(软删除时交给回收站,硬删除时自底向上)
                dirs.push(dst_path);
            } else if src_path.is_dir() {
                // 源目录存在 → 递归进去找更深的差异
                collect_purge_list(&src_path, &dst_path, files, dirs, job, errors)?;
            } else {
                // BUG 修复:源同名路径是文件(目录→文件变更) → 目标目录整体多余 → purge
                // (原实现递归进文件 read_dir 失败 → 静默残留,镜像不干净)
                dirs.push(dst_path);
            }
        } else if ft.is_file() {
            if !src_path.exists() {
                files.push(dst_path);
            }
        }
        // 重解析点:P4 处理,P2 暂不 purge(避免误删符号链接指向的数据)
    }
    Ok(())
}

/// 硬删除文件列表。
/// 软删除后按存在性精确统计(RecycleBin 与 SHFileOperationW 两条路径共用)。
/// 只对确实被删除的项发 Purge 事件 + 计数,避免误报"已删除"但实际因
/// 权限/占用未删除(v5 §4.3 数据完整性要求;SHFileOperationW 部分失败时
/// 返回值仍为 0,必须靠本检查兜底)。
fn count_purged_soft(
    stats: &mut PurgeStats,
    file_sizes: &[(PathBuf, u64)],
    dirs_to_purge: &[PathBuf],
) {
    for (path, size) in file_sizes {
        if !path.exists() {
            Event::Purge {
                path: path.to_string_lossy().into_owned(),
                soft_deleted: true,
                dry_run: false,
            }
            .emit();
            stats.purged_files += 1;
            stats.bytes_freed += size;
        } else {
            // 仍在原位 = 删除失败
            stats.errors += 1;
        }
    }
    for d in dirs_to_purge {
        if !d.exists() {
            Event::Purge {
                path: d.to_string_lossy().into_owned(),
                soft_deleted: true,
                dry_run: false,
            }
            .emit();
            stats.purged_dirs += 1;
        } else {
            stats.errors += 1;
        }
    }
}

fn hard_delete_files(files: &[PathBuf], stats: &mut PurgeStats, job: &Job) -> Result<(), u32> {
    for f in files {
        if job.cancel_requested() {
            return Err(1223);
        }
        let size = std::fs::metadata(f).map(|m| m.len()).unwrap_or(0);
        match std::fs::remove_file(f) {
            Ok(()) => {
                Event::Purge {
                    path: f.to_string_lossy().into_owned(),
                    soft_deleted: false,
                    dry_run: false,
                }
                .emit();
                stats.purged_files += 1;
                stats.bytes_freed += size;
            }
            Err(e) => {
                let code = e.raw_os_error().map(|c| c as u32).unwrap_or(crate::ERR_NO_OS_ERROR);
                Event::FileError {
                    path: f.to_string_lossy().into_owned(),
                    code,
                    stage: "purge_delete".to_string(),
                }
                .emit();
                stats.errors += 1;
            }
        }
    }
    Ok(())
}

/// 硬删除目录列表(自底向上:先删子内容,再删目录本身)。
fn hard_delete_dirs(dirs: &[PathBuf], stats: &mut PurgeStats, job: &Job) -> Result<(), u32> {
    for d in dirs {
        if job.cancel_requested() {
            return Err(1223);
        }
        match std::fs::remove_dir_all(d) {
            Ok(()) => {
                Event::Purge {
                    path: d.to_string_lossy().into_owned(),
                    soft_deleted: false,
                    dry_run: false,
                }
                .emit();
                stats.purged_dirs += 1;
            }
            Err(e) => {
                let code = e.raw_os_error().map(|c| c as u32).unwrap_or(crate::ERR_NO_OS_ERROR);
                Event::FileError {
                    path: d.to_string_lossy().into_owned(),
                    code,
                    stage: "purge_delete_dir".to_string(),
                }
                .emit();
                stats.errors += 1;
            }
        }
    }
    Ok(())
}
