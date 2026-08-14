#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""覆盖确认 + 合并复制行为测试（开发环境区 NEED_CONFIRM_OVERWRITE 修复验证）

覆盖场景：
1. 非空目标目录 → migrate() 返回 NEED_CONFIRM_OVERWRITE，源/目标均未被修改（复现缺陷）
2. force_overwrite=True + merge=True → 迁移成功（合并复制）：
   - 目标含源全部文件
   - 目标中原有文件保留（合并语义，不 purge）
   - 源位置变为符号链接/junction 指向目标（需要建链权限，非管理员跳过）
3. merge=False（默认）不回归：非空目标仍被拦截

安全约束（遵守全局删除铁律）：
- mock migrator.save_all，防写真实 config.json
- cfg 用独立 dict，不碰真实 state.json
- 临时目录 tempfile.mkdtemp(prefix='test_confirm_overwrite_')，清理前校验前缀 + 打印路径
- 不 shell 拼串删除

运行：python src/tests/test_confirm_overwrite.py
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (SRC_ROOT, os.path.join(SRC_ROOT, "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 引擎 exe 路径（bin/rust-migrate-engine.exe），缺失时跳过真实迁移测试
_ENGINE_EXE = os.path.join(os.path.dirname(SRC_ROOT), "bin", "rust-migrate-engine.exe")


class OverwriteConfirmTest(unittest.TestCase):
    """NEED_CONFIRM_OVERWRITE 拦截 + merge 合并复制行为"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="test_confirm_overwrite_")
        # 源目录：3 个小文件（含子目录）
        self.src = os.path.join(self._tmp, "src_data")
        os.makedirs(self.src)
        with open(os.path.join(self.src, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("A" * 100)
        with open(os.path.join(self.src, "b.txt"), "w", encoding="utf-8") as fh:
            fh.write("B" * 100)
        os.makedirs(os.path.join(self.src, "sub"))
        with open(os.path.join(self.src, "sub", "c.txt"), "w", encoding="utf-8") as fh:
            fh.write("C" * 100)

    def tearDown(self):
        base = os.path.basename(self._tmp)
        if not base.startswith("test_confirm_overwrite_"):
            raise AssertionError(f"临时目录前缀不匹配，拒绝清理: {self._tmp}")
        print(f"[清理] 临时目录: {self._tmp}")
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make_migrator(self):
        from migrator import Migrator
        cfg = {"g_root": "D:\\", "verify_hash": True,
               "migrated": [], "pending_migrations": [], "dst_index": {}}
        return Migrator(cfg), cfg

    def _check_engine(self):
        if not os.path.isfile(_ENGINE_EXE):
            self.skipTest(f"引擎缺失: {_ENGINE_EXE}")

    def test_nonempty_dst_returns_confirm(self):
        """复现：实际数据位置（目标\源名）已存在且非空 → 返回 NEED_CONFIRM_OVERWRITE"""
        self._check_engine()
        dst = os.path.join(self._tmp, "dst1")
        os.makedirs(dst)
        # 包裹语义：数据落在 dst\src_data（源文件夹名），在那里放文件触发确认
        dst_data = os.path.join(dst, "src_data")
        os.makedirs(dst_data)
        extra = os.path.join(dst_data, "existing.txt")
        with open(extra, "w", encoding="utf-8") as fh:
            fh.write("keep me")
        mig, _ = self._make_migrator()
        with mock.patch("migrator.save_all"):
            ok, msg = mig.migrate(self.src, dst)
        self.assertFalse(ok, "非空数据位置必须被拦截")
        self.assertIn("NEED_CONFIRM_OVERWRITE", msg)
        # 源/目标均未被修改（拦截发生在复制前）
        self.assertTrue(os.path.isfile(os.path.join(self.src, "a.txt")))
        self.assertTrue(os.path.isfile(extra), "目标原有文件必须原样保留")
        from utils import is_symlink
        self.assertFalse(is_symlink(self.src), "源不应被改成链接（未执行迁移）")

    def test_merge_overwrite_keeps_dst_extra_files(self):
        """包裹语义 + 合并复制：数据进目标\源名子目录，目标根原有文件保留（核心修复验证）"""
        self._check_engine()
        dst = os.path.join(self._tmp, "dst2")
        os.makedirs(dst)
        extra = os.path.join(dst, "keep_me.txt")  # 目标根原有文件
        with open(extra, "w", encoding="utf-8") as fh:
            fh.write("original file in dst root, must survive")
        mig, cfg = self._make_migrator()
        with mock.patch("migrator.save_all"):
            ok, msg = mig.migrate(self.src, dst, force_overwrite=True, merge=True)
        self.assertTrue(ok, f"merge 迁移应成功: {msg}")
        # 包裹结构：数据在 dst\src_data\...（源文件夹名）
        dst_data = os.path.join(dst, "src_data")
        for rel in ("a.txt", "b.txt", os.path.join("sub", "c.txt")):
            self.assertTrue(os.path.isfile(os.path.join(dst_data, rel)),
                            f"目标缺少源文件: {rel}")
        # 目标根原有文件保留（包裹语义天然隔离，不 purge 不覆盖）
        self.assertTrue(os.path.isfile(extra), "目标根原有文件必须保留")
        # 目标根不应被铺源内容（数据在子目录）
        self.assertEqual(sorted(os.listdir(dst)), ["keep_me.txt", "src_data"],
                         "目标根应只有原有文件 + 源文件夹")
        # 源位置应变为链接指向目标内的源文件夹（mklink /D 需管理员，/J junction 兜底）
        from utils import is_symlink
        if not is_symlink(self.src):
            self.skipTest("非管理员或建链失败：跳过链接断言")
        # 迁移记录已写入 cfg（内存，未落盘）
        self.assertTrue(any(m.get("src") == self.src for m in cfg.get("migrated", [])),
                        "migrated 记录应写入 cfg")

    def test_merge_overwrites_same_name_file(self):
        """合并复制：数据位置内同名旧文件被源版本覆盖；目标根同名文件不受影响（包裹隔离）"""
        self._check_engine()
        dst = os.path.join(self._tmp, "dst4")
        os.makedirs(dst)
        # 数据位置（dst\src_data）放同名旧文件（内容/大小与源不同）
        dst_data = os.path.join(dst, "src_data")
        os.makedirs(dst_data)
        old = os.path.join(dst_data, "a.txt")
        with open(old, "w", encoding="utf-8") as fh:
            fh.write("OLD CONTENT")
        # 目标根也放同名文件（包裹隔离，不应被影响）
        root_same = os.path.join(dst, "a.txt")
        with open(root_same, "w", encoding="utf-8") as fh:
            fh.write("ROOT OWN FILE")
        mig, _ = self._make_migrator()
        with mock.patch("migrator.save_all"):
            ok, msg = mig.migrate(self.src, dst, force_overwrite=True, merge=True)
        self.assertTrue(ok, f"merge 迁移应成功: {msg}")
        # 数据位置内同名文件被源版本覆盖（引擎大小/mtime 不一致时重拷）
        with open(os.path.join(dst_data, "a.txt"), "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "A" * 100,
                             "数据位置内同名文件应被源版本覆盖（非跳过）")
        # 目标根同名文件原样保留（包裹语义隔离）
        with open(root_same, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ROOT OWN FILE",
                             "目标根同名文件不受影响（包裹语义）")

    def test_empty_source_rejected(self):
        """空源目录防御：0 文件源拒绝迁移（防止"空迁移假成功"事故重演）"""
        empty_src = os.path.join(self._tmp, "empty_src")
        os.makedirs(empty_src)
        dst = os.path.join(self._tmp, "dst5")
        os.makedirs(dst)
        mig, _ = self._make_migrator()
        with mock.patch("migrator.save_all"):
            ok, msg = mig.migrate(empty_src, dst, force_overwrite=True, merge=True)
        self.assertFalse(ok, "空源目录必须被拒绝")
        self.assertIn("源目录为空", msg)
        # 目标未被写任何内容、源未被删除、未建链接
        self.assertEqual(os.listdir(dst), [], "目标不应被写内容")
        self.assertTrue(os.path.isdir(empty_src), "空源不应被删除")
        from utils import is_symlink
        self.assertFalse(is_symlink(empty_src), "空源不应被改成链接")

    def test_default_target_no_nesting(self):
        """默认目标（无 dst_path）：数据在 g_root\源名（包裹单层，不嵌套 appdata）"""
        self._check_engine()
        g_root = os.path.join(self._tmp, "groot")
        os.makedirs(g_root)
        from migrator import Migrator
        cfg = {"g_root": g_root, "verify_hash": True,
               "migrated": [], "pending_migrations": [], "dst_index": {}}
        mig = Migrator(cfg)
        with mock.patch("migrator.save_all"):
            ok, msg = mig.migrate(self.src)  # 无 dst_path → 默认 g_root
        self.assertTrue(ok, f"默认目标迁移应成功: {msg}")
        # 数据在 g_root\src_data（包裹单层，无 appdata 嵌套）
        dst_data = os.path.join(g_root, "src_data")
        self.assertTrue(os.path.isfile(os.path.join(dst_data, "a.txt")),
                        "默认目标数据应位于 g_root\\源名（单层包裹）")
        self.assertFalse(os.path.isdir(os.path.join(dst_data, "appdata")),
                         "不应出现 appdata 嵌套")

    def test_symlink_empty_target_rejected(self):
        """改迁空源防御：链接指向空目录时拒绝（防'空改迁假成功'）"""
        empty = os.path.join(self._tmp, "empty_real")
        os.makedirs(empty)
        link = os.path.join(self._tmp, "emptylink")
        try:
            os.symlink(empty, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("创建符号链接需要管理员/开发者模式，跳过")
        dst = os.path.join(self._tmp, "dst6")
        mig, _ = self._make_migrator()
        with mock.patch("migrator.save_all"):
            ok, msg = mig.migrate(link, dst, force_overwrite=True, merge=True)
        self.assertFalse(ok, "空真实数据目录必须拒绝改迁")
        self.assertIn("为空", msg)

    def test_fix_broken_link_repoints_wrong_target(self):
        """fix_broken_link：链接存在但指向与目标不一致时重建链接（防"修复无效"）"""
        # 真实数据目录（正确目标，含文件）
        data = os.path.join(self._tmp, "fix_data")
        os.makedirs(data)
        with open(os.path.join(data, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("A" * 100)
        # 链接指向错误的目录
        wrong = os.path.join(self._tmp, "wrong_target")
        os.makedirs(wrong)
        link = os.path.join(self._tmp, "fixlink")
        try:
            os.symlink(wrong, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("创建符号链接需要管理员/开发者模式，跳过")
        mig, _ = self._make_migrator()
        with mock.patch("migrator.save_all"):
            ok, msg = mig.fix_broken_link(link, data)
        self.assertTrue(ok, f"修复应成功: {msg}")
        # 链接被重建指向正确目标（用 realpath 解析，避免 \\?\ 前缀转义问题）
        self.assertTrue(os.path.islink(link), "链接应存在")
        self.assertEqual(os.path.normcase(os.path.realpath(link)),
                         os.path.normcase(os.path.normpath(data)),
                         "链接应重建指向正确目标")
        # 通过链接访问数据
        self.assertTrue(os.path.isfile(os.path.join(link, "a.txt")),
                        "通过链接应能访问真实数据")

    def test_clean_env_var_residues(self):
        """启动自愈：注册表无值但进程有的管理变量被清除（残留恢复默认）"""
        from dev_env_migrate import clean_env_var_residues
        name = "ANDROID_HOME"
        old = os.environ.get(name)
        os.environ[name] = r"D:\fake_residue_path"
        try:
            with mock.patch("dev_env_migrate._registry_env_exists", return_value=False):
                cleaned = clean_env_var_residues()
            self.assertIn(name, cleaned, "注册表无值的残留变量应被清除")
            self.assertNotIn(name, os.environ, "清除后进程环境不应再有该变量")
        finally:
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

    def test_clean_env_var_residues_keeps_registry_value(self):
        """启动自愈：注册表有值的变量不被清除（用户/软件真实配置）"""
        from dev_env_migrate import clean_env_var_residues
        name = "ANDROID_HOME"
        old = os.environ.get(name)
        os.environ[name] = r"D:\valid_path"
        try:
            with mock.patch("dev_env_migrate._registry_env_exists", return_value=True):
                cleaned = clean_env_var_residues()
            self.assertNotIn(name, cleaned, "注册表有值的变量不应被清除")
            self.assertEqual(os.environ.get(name), r"D:\valid_path",
                             "注册表有值的变量应原样保留")
        finally:
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

    def test_same_name_target_no_wrapping(self):
        """防套娃：目标路径文件夹名与源文件夹名相同（忽略大小写）→ 数据直接放目标"""
        self._check_engine()
        # 目标 basename 与源名相同（src_data），但路径不同（parent\src_data）
        dst = os.path.join(self._tmp, "parent", "src_data")
        os.makedirs(dst)
        mig, _ = self._make_migrator()
        with mock.patch("migrator.save_all"):
            ok, msg = mig.migrate(self.src, dst, force_overwrite=True, merge=True)
        self.assertTrue(ok, f"迁移应成功: {msg}")
        # 数据直接放目标（不套娃：dst\src_data 不应存在）
        self.assertTrue(os.path.isfile(os.path.join(dst, "a.txt")),
                        "同名目标时数据应直接放目标目录")
        self.assertFalse(os.path.isdir(os.path.join(dst, "src_data")),
                         "不应出现 目标\\源名 套娃")
        # 源变链接
        from utils import is_symlink
        if not is_symlink(self.src):
            self.skipTest("非管理员或建链失败：跳过链接断言")

    def test_empty_dst_no_confirm(self):
        """目标数据位置为空目录（无文件）→ 不拦截，直接迁移（不弹确认）"""
        self._check_engine()
        dst = os.path.join(self._tmp, "dst_empty")
        os.makedirs(dst)
        dst_data = os.path.join(dst, "src_data")  # 包裹位置：空目录
        os.makedirs(dst_data)
        mig, _ = self._make_migrator()
        with mock.patch("migrator.save_all"):
            ok, msg = mig.migrate(self.src, dst)  # 不带 force：空目录不应拦截
        self.assertTrue(ok, f"空目标位置不应拦截: {msg}")
        self.assertTrue(os.path.isfile(os.path.join(dst_data, "a.txt")),
                        "数据应迁入空目标位置")

    def test_migrate_tool_data_same_source_target(self):
        """源==目标：提示另选新路径（而非静默"无需迁移"成功）"""
        from dev_env_migrate import migrate_tool_data
        tool = {"id": "android_sdk", "name": "Android SDK/NDK",
                "special": None, "env_vars": [], "current_path_fn": "android_sdk_path"}
        # 源与目标相同（如环境变量残留指向目标路径）
        ok, msg, _ = migrate_tool_data(
            tool, "D", config={},
            source_path_override=r"D:\target",
            target_path_override=r"D:\target")
        self.assertFalse(ok, "源==目标应提示另选而非成功")
        self.assertIn("当前路径已是目标路径", msg)

    def test_nested_source_wrap_copied(self):
        """套娃源照搬：源本身连续同名段（dve\\dve）→ 目标保留 dve\\dve 结构"""
        self._check_engine()
        # 构造套娃源：tmp\nested\dve\dve
        nested_src = os.path.join(self._tmp, "nested", "dve", "dve")
        os.makedirs(nested_src)
        with open(os.path.join(nested_src, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("A" * 100)
        dst = os.path.join(self._tmp, "dst_nested")
        os.makedirs(dst)
        mig, _ = self._make_migrator()
        with mock.patch("migrator.save_all"):
            ok, msg = mig.migrate(nested_src, dst, force_overwrite=True, merge=True)
        self.assertTrue(ok, f"套娃源迁移应成功: {msg}")
        # 照搬：dst\dve\dve\a.txt（源结构完整保留）
        self.assertTrue(os.path.isfile(os.path.join(dst, "dve", "dve", "a.txt")),
                        "套娃源结构应照搬到目标（dve\\dve 两层保留）")
        # 源变链接
        from utils import is_symlink
        if not is_symlink(nested_src):
            self.skipTest("非管理员或建链失败：跳过链接断言")

    def test_restore_nested_source(self):
        """套娃源回滚：迁移后还原，数据回源、链接删除、记录清理、目标清空"""
        self._check_engine()
        nested_src = os.path.join(self._tmp, "nested2", "dve", "dve")
        os.makedirs(nested_src)
        with open(os.path.join(nested_src, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("A" * 100)
        dst = os.path.join(self._tmp, "dst_nested2")
        os.makedirs(dst)
        mig, cfg = self._make_migrator()
        with mock.patch("migrator.save_all"):
            ok, msg = mig.migrate(nested_src, dst, force_overwrite=True, merge=True)
        self.assertTrue(ok, f"套娃源迁移应成功: {msg}")
        # 还原（回滚）
        with mock.patch("migrator.save_all"):
            ok2, msg2 = mig.restore(nested_src)
        self.assertTrue(ok2, f"套娃源还原应成功: {msg2}")
        # 数据回源（真实目录）+ 无链接
        from utils import is_symlink
        self.assertFalse(is_symlink(nested_src), "还原后源不应是链接")
        self.assertTrue(os.path.isfile(os.path.join(nested_src, "a.txt")),
                        "还原后数据应回源目录")
        # 记录已清理
        self.assertFalse(any(m.get("src") == nested_src for m in cfg.get("migrated", [])),
                         "还原后迁移记录应移除")
        # 目标数据位置清空（保留文件夹本身）
        d = os.path.join(dst, "dve", "dve")
        if os.path.isdir(d):
            self.assertEqual(os.listdir(d), [], "还原后目标数据位置应清空")

    def test_merge_does_not_bypass_nonempty_check(self):
        """merge=True 不绕过非空检测（数据位置非空仍需 force_overwrite=True 配合）"""
        self._check_engine()
        dst = os.path.join(self._tmp, "dst3")
        os.makedirs(dst)
        dst_data = os.path.join(dst, "src_data")
        os.makedirs(dst_data)
        with open(os.path.join(dst_data, "x.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        mig, _ = self._make_migrator()
        with mock.patch("migrator.save_all"):
            ok, msg = mig.migrate(self.src, dst, force_overwrite=False, merge=True)
        self.assertFalse(ok, "merge 不应绕过非空检测（必须配 force_overwrite）")
        self.assertIn("NEED_CONFIRM_OVERWRITE", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
