# C盘拦迁器 (C Drive Relocator)

Windows 下的 C 盘空间管理工具。把 C 盘中占用空间的目录迁移到其他盘，原位置创建符号链接，软件不需要重新安装即可继续使用。

## 功能

- **目录迁移**：扫描 C 盘目录，把大目录迁移到目标盘，C 盘原位置自动创建符号链接
- **迁移/还原**：支持还原（数据搬回 C 盘）、修复断链、改迁到新位置
- **Rust 复制引擎**：多线程流水线复制，BLAKE3 哈希校验，断点续传（中断后可继续）
- **开发环境迁移**：30+ 开发工具的环境变量/配置迁移（Java、Node.js、Python、Docker 等）
- **安装器拦截**：监控安装程序向 C 盘写入大目录，迁移前可拦截确认
- **配置快照**：开发环境配置快照与回滚
- **AI 识别**：用大模型识别目录用途（可选功能，需配置 API Key，支持 13 个国内外平台）
- **界面**：中文

## 下载

Windows 便携版 exe 在 [Releases](https://github.com/hrchrc/c-drive-relocator/releases) 页面：单文件，已内置 Rust 引擎，下载后直接运行（建议以管理员身份运行）。

## 从源码运行

要求：Python 3.13，Windows 10/11

```bash
pip install -r requirements.txt
python src/main.py
```

部分功能需要管理员权限（迁移、符号链接、环境变量操作）。

## 构建 Rust 引擎（可选）

源码在 `rust-engine/` 目录。如不使用预编译引擎，可自行构建：

```bash
cd rust-engine
cargo build --release
```

构建产物 `rust-migrate-engine.exe` 放到 `bin/` 目录。

## 许可证

[MIT](LICENSE)
