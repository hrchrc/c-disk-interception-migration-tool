#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cython 扩展编译脚本。

用法：
    python src/setup.py build_ext --inplace

编译后会生成 mft_fast.cp<ver>-win_amd64.pyd 文件到 src/mft/ 目录下，
mft_reader.py（同目录）可直接 import mft_fast 使用。
"""

from setuptools import setup, Extension
from Cython.Build import cythonize

extensions = [
    Extension(
        # 8.8 修复:扩展名带 mft 包前缀,使 build_ext --inplace 输出到
        # src/mft/mft_fast.cp<ver>-win_amd64.pyd(mft_reader.py 同目录导入)。
        # 原 "mft_fast" 输出到 src/ 根目录,编译产物与导入位置不一致,
        # 导致 src/mft/ 下长期使用旧 .pyd(边界加固等修改不生效)。
        "mft.mft_fast",
        sources=["mft/mft_fast.pyx"],  # .pyx 已移至 src/mft/ 子目录
        extra_compile_args=["/O2", "/GL", "/GS-"],  # MSVC 优化参数
    )
]

setup(
    name="mft_fast",
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
            "cdivision": True,
            "initializedcheck": False,
            "always_allow_keywords": False,
        },
    ),
)
