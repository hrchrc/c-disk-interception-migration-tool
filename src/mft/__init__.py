# -*- coding: utf-8 -*-
"""MFT 性能优化模块包（src/mft/）

存放通过直接解析 NTFS 主文件表实现极速扫描的模块：
- mft_reader.py   MFT 读取核心（纯 Python + ctypes，不依赖 Everything/pywin32）
- mft_fast.pyx    Cython 加速扩展（编译后生成 .pyd，未编译时自动回退到纯 Python）
- mft_fast.cp313-win_amd64.pyd  Cython 编译产物（Python 3.13 × Windows x64）

注：.pyd 在 .gitignore 中被排除，用户首次使用需运行 `python src/setup.py build_ext --inplace` 编译。
"""
