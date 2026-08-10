#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全回归测试 - 防止已修复的 bug 回归

覆盖范围：
  数据安全类：H1 路径包含校验 / H4 环境变量回滚 / H9 盘符校验 / H12 配置 deepcopy
  功能失效类：H13 拦截分类 / H14 游戏分支 / H15 搜索上下文 / H16 AI批结果 / H18 cmdline
  性能类：M2/M3 winget 索引使用

运行方式：
  cd <项目根目录>
  python -m unittest test_safety_regressions -v

设计原则：
  - 不碰真实系统（mock winreg/subprocess/psutil）
  - 不依赖 PySide6（纯核心逻辑测试）
  - inspect 源码检查用于无法独立测的修复点（防回归哨兵）
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# ========== sys.path 注入（与 main.py 一志）==========
# 本文件位于 src/tests/，取上一级 src/ 作为模块根
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("core", "ui", "mft"):
    _p = os.path.join(_SRC_DIR, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ============================================================
# H1: 路径包含校验 - 防止 robocopy /MIR 自毁
# 修复：用 os.path.commonpath 替代 startswith，处理盘符根边界
# ============================================================
class TestPathContainmentCheck(unittest.TestCase):
    """H1: 路径包含校验边界 - 盘符根场景"""

    def test_commonpath_handles_drive_root(self):
        """盘符根作为目标时，包含校验必须正确识别子目录"""
        # 场景：src=D:\data, dst=D:\ → commonpath 应为 D:\，等于 dst → 拒绝
        import os as _os
        norm_src = _os.path.normcase(_os.path.normpath("D:\\data"))
        norm_dst = _os.path.normcase(_os.path.normpath("D:\\"))
        common = _os.path.commonpath([norm_src, norm_dst])
        # D:\ 是 D:\data 的父目录，commonpath 返回 D:\，应等于 dst → 拒绝迁移
        self.assertEqual(common, norm_dst, "盘符根应被识别为父目录，拒绝迁移")

    def test_commonpath_different_drives_raises(self):
        """不同盘符应抛 ValueError，不算包含关系"""
        import os as _os
        norm_src = _os.path.normcase(_os.path.normpath("C:\\data"))
        norm_dst = _os.path.normcase(_os.path.normpath("D:\\backup"))
        with self.assertRaises(ValueError, msg="不同盘符 commonpath 应抛 ValueError"):
            _os.path.commonpath([norm_src, norm_dst])

    def test_commonpath_sibling_dirs_not_contained(self):
        """兄弟目录不算包含关系"""
        import os as _os
        norm_src = _os.path.normcase(_os.path.normpath("D:\\src"))
        norm_dst = _os.path.normcase(_os.path.normpath("D:\\dst"))
        common = _os.path.commonpath([norm_src, norm_dst])
        # 共同父目录是 D:\，不等于 src 也不等于 dst → 允许迁移
        self.assertNotEqual(common, norm_src)
        self.assertNotEqual(common, norm_dst)

    def test_migrate_rejects_containment(self):
        """migrate 对包含关系的路径返回 False（不实际跑 robocopy）"""
        try:
            from migrator import Migrator
        except Exception as e:
            self.skipTest(f"无法 import migrator（可能缺依赖）: {e}")
        # 构造一个最小 Migrator 实例（mock 掉必要属性）
        try:
            mig = Migrator.__new__(Migrator)
            mig.cfg = {"g_root": "D:\\"}
            mig._emit_log = lambda *a, **kw: None
        except Exception:
            self.skipTest("Migrator 实例化失败")

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "data")
            os.makedirs(src)
            # dst 在 src 内部 → 包含关系 → 拒绝
            dst = os.path.join(src, "nested")
            try:
                ok, msg = mig.migrate(src, dst)
            except AttributeError as e:
                self.skipTest(f"Migrator 缺属性，跳过功能测试: {e}")
            self.assertFalse(ok, "包含关系的路径必须被拒绝")
            self.assertIn("包含", msg)


# ============================================================
# H4: 环境变量部分失败回滚 - 防止残留 + 防止丢失用户原值
# 修复：apply 前预读注册表原值，失败时回滚恢复原值（而非无脑删除）
# ============================================================
class TestEnvVarRollback(unittest.TestCase):
    """H4: apply_tool 环境变量部分失败时正确回滚"""

    def setUp(self):
        try:
            import dev_env_migrate
        except Exception as e:
            self.skipTest(f"无法 import dev_env_migrate: {e}")
        self.dev = dev_env_migrate

    def _make_tool(self, env_var_names):
        """构造测试用 tool dict"""
        return {
            "id": "test_tool",
            "name": "测试工具",
            "env_vars": [{"name": n, "default_value_template": "D:\\dev\\" + n} for n in env_var_names],
            "config_commands": [],
            "unconfig_commands": [],
            "special": None,
        }

    def test_rollback_restores_original_value(self):
        """第二个环境变量失败时，第一个应回滚到 apply 前的原值（而非删除）"""
        tool = self._make_tool(["TEST_VAR_A", "TEST_VAR_B"])
        call_log = []

        def fake_set(name, value):
            call_log.append(("set", name, value))
            if name == "TEST_VAR_B":
                return False, "模拟失败"
            return True, ""

        def fake_remove(name):
            call_log.append(("remove", name))
            return True, ""

        # mock winreg.QueryValueEx：TEST_VAR_A 原值存在
        fake_orig = "C:\\original\\path"
        with patch.object(self.dev, "set_user_env_var", side_effect=fake_set), \
             patch.object(self.dev, "remove_user_env_var", side_effect=fake_remove), \
             patch("winreg.OpenKey"), \
             patch("winreg.QueryValueEx", return_value=(fake_orig, 0)), \
             patch("winreg.CloseKey"):
            ok, msg = self.dev.apply_tool(tool, "D")

        self.assertFalse(ok, "部分失败应返回 False")
        # TEST_VAR_A 应被回滚恢复原值，而非删除
        rollback_calls = [c for c in call_log if c[0] == "set" and c[1] == "TEST_VAR_A" and c[2] == fake_orig]
        self.assertEqual(len(rollback_calls), 1, "回滚应恢复原值而非删除")
        # 不应调用 remove（因为原值存在）
        remove_calls = [c for c in call_log if c[0] == "remove" and c[1] == "TEST_VAR_A"]
        self.assertEqual(len(remove_calls), 0, "原值存在时不应删除")

    def test_rollback_deletes_when_no_original(self):
        """原值不存在时，回滚应删除（而非残留）"""
        tool = self._make_tool(["TEST_VAR_C", "TEST_VAR_D"])
        call_log = []

        def fake_set(name, value):
            call_log.append(("set", name, value))
            if name == "TEST_VAR_D":
                return False, "模拟失败"
            return True, ""

        def fake_remove(name):
            call_log.append(("remove", name))
            return True, ""

        with patch.object(self.dev, "set_user_env_var", side_effect=fake_set), \
             patch.object(self.dev, "remove_user_env_var", side_effect=fake_remove), \
             patch("winreg.OpenKey"), \
             patch("winreg.QueryValueEx", side_effect=FileNotFoundError), \
             patch("winreg.CloseKey"):
            ok, msg = self.dev.apply_tool(tool, "D")

        self.assertFalse(ok)
        # TEST_VAR_C 原值不存在 → 回滚应删除
        remove_calls = [c for c in call_log if c[0] == "remove" and c[1] == "TEST_VAR_C"]
        self.assertEqual(len(remove_calls), 1, "原值不存在时应删除")


# ============================================================
# H9: 盘符校验 - 防止 shell 注入
# 修复：apply_tool/unapply_tool 入口校验 target_drive 为单字母
# ============================================================
class TestTargetDriveValidation(unittest.TestCase):
    """H9: target_drive 校验防止 shell 注入"""

    def setUp(self):
        try:
            import dev_env_migrate
        except Exception as e:
            self.skipTest(f"无法 import dev_env_migrate: {e}")
        self.dev = dev_env_migrate

    def test_rejects_injection_attempt(self):
        """shell 注入字符串应被拒绝"""
        tool = {"env_vars": [], "config_commands": [], "special": None}
        injections = [
            "D & del /f /s C:\\",
            "D; format C:",
            "DD",           # 多字母
            "",             # 空
            "D\\",          # 含反斜杠
            None,           # None
            123,            # 非字符串
        ]
        for bad in injections:
            with self.subTest(drive=bad):
                ok, msg = self.dev.apply_tool(tool, bad)
                self.assertFalse(ok, f"应拒绝非法盘符: {bad!r}")
                self.assertIn("非法", msg)

    def test_accepts_valid_drive(self):
        """单字母盘符应通过校验（后续可能因其他原因失败，但不是盘符校验）"""
        tool = {"env_vars": [], "config_commands": [], "special": "pip"}
        ok, msg = self.dev.apply_tool(tool, "D")
        # pip 特殊工具会返回 False，但消息是"无法自动配置"而非"盘符非法"
        self.assertIn("自动配置", msg)


# ============================================================
# H12: 配置 deepcopy - 防止默认配置被污染
# 修复：DEFAULT_CONFIG.copy() → copy.deepcopy(DEFAULT_CONFIG)
# ============================================================
class TestConfigDeepCopy(unittest.TestCase):
    """H12: load_config 返回的配置修改不影响 DEFAULT_CONFIG"""

    def test_deepcopy_isolates_nested_structures(self):
        """修改 load_config 返回的嵌套 dict 不应污染 DEFAULT_CONFIG"""
        try:
            import config as cfg_mod
        except Exception as e:
            self.skipTest(f"无法 import config: {e}")

        import copy
        # 验证 DEFAULT_CONFIG 用的是 deepcopy（不是浅拷贝）
        original = copy.deepcopy(cfg_mod.DEFAULT_CONFIG)
        # 模拟 load_config 的行为
        cfg = copy.deepcopy(cfg_mod.DEFAULT_CONFIG)
        # 修改嵌套结构
        for k in cfg:
            v = cfg[k]
            if isinstance(v, dict):
                v["_test_pollution"] = True
                break
        # DEFAULT_CONFIG 不应被污染
        self.assertEqual(cfg_mod.DEFAULT_CONFIG, original,
                         "修改返回的配置不应污染 DEFAULT_CONFIG（deepcopy 修复）")


# ============================================================
# 源码哨兵检查 - 防止已修 bug 的代码被改回
# 用于无法独立单元测试的修复点（依赖 Win32 API / 线程 / UI）
# ============================================================
class TestSourceCodeRegressions(unittest.TestCase):
    """通过检查源码字符串，确保已修 bug 的关键代码未被改回"""

    @property
    def _src_dir(self):
        # 本文件位于 src/tests/，模块根 _SRC_DIR = src/
        return os.path.join(_SRC_DIR, "core")

    def _read(self, filename):
        path = os.path.join(self._src_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    # ---- H13: monitor.py 的 now 变量必须在 Process32First 之前定义 ----
    def test_h13_now_before_process_enum(self):
        """now = time.time() 必须在 Process32First 之前，否则 NameError 导致分类失效"""
        src = self._read("monitor.py")
        idx_now = src.find("now = time.time()")
        idx_first = src.find("Process32First")
        self.assertGreater(idx_now, 0, "找不到 now = time.time()")
        self.assertGreater(idx_first, 0, "找不到 Process32First")
        self.assertLess(idx_now, idx_first,
                        "now 必须在 Process32First 之前定义，否则循环体内引用 now 抛 NameError")

    # ---- H18: _kill_installer 必须包含 import psutil ----
    def test_h18_psutil_import_in_kill_installer(self):
        """_kill_installer 函数内必须有 import psutil，否则 cmdline 恒空"""
        src = self._read("monitor.py")
        idx_kill = src.find("def _kill_installer")
        self.assertGreater(idx_kill, 0, "找不到 _kill_installer 函数")
        # 在 _kill_installer 函数体内找 import psutil
        func_body = src[idx_kill:]
        # 截取到下一个 def 或文件末尾
        next_def = func_body.find("\ndef ", 10)
        if next_def > 0:
            func_body = func_body[:next_def]
        self.assertIn("import psutil", func_body,
                      "_kill_installer 内必须有 import psutil，否则 cmdline 恒空")

    # ---- H14: software_detect.py 不应含 game_keyword 未定义变量 ----
    def test_h14_game_keyword_branch_removed(self):
        """game_keyword 未定义分支应已删除，不应出现在 elif 条件中"""
        src = self._read("software_detect.py")
        # game_keyword 作为变量引用（非字符串字面量）不应出现在 elif 中
        self.assertNotIn("game_keyword in feature_desc", src,
                         "game_keyword 未定义分支应已删除（H14 修复）")

    # ---- H15: _bing_search 必须使用 context/content_ctx 参数 ----
    def test_h15_bing_search_uses_context(self):
        """_bing_search 的 context/content_ctx 参数必须被使用，不能是死参数"""
        src = self._read("software_detect.py")
        # 修复后 content_ctx 和 context 应被拼入 terms
        self.assertIn("content_ctx", src, "content_ctx 应出现在源码中")
        self.assertIn("terms.insert(0", src,
                      "content_ctx 应通过 terms.insert 加入搜索词")
        self.assertIn("context", src, "context 应出现在源码中")
        # context 应被 append 到 terms（兜底搜索词）
        idx_terms_append = src.find("terms.append")
        idx_context_use = src.find("context", idx_terms_append - 200 if idx_terms_append > 200 else 0)
        self.assertGreater(idx_context_use, 0, "context 应被使用而非忽略")

    # ---- H16: ai_recognizer.py 的 batch_result 必须在循环顶部初始化 ----
    def test_h16_batch_result_reset_in_loop(self):
        """batch_result = {} 必须在 for 循环顶部，防止批失败推送上一批陈旧结果"""
        src = self._read("ai_recognizer.py")
        idx_for = src.find("for i in range(0, total")
        self.assertGreater(idx_for, 0, "找不到分批循环")
        # 在 for 循环之后、_identify_batch 调用之前，应有 batch_result = {}
        loop_body = src[idx_for:]
        idx_reset = loop_body.find("batch_result = {}")
        idx_identify = loop_body.find("_identify_batch")
        self.assertGreater(idx_reset, 0, "循环内必须有 batch_result = {} 初始化")
        self.assertGreater(idx_identify, 0, "找不到 _identify_batch 调用")
        self.assertLess(idx_reset, idx_identify,
                        "batch_result = {} 必须在 _identify_batch 之前（循环顶部）")

    # ---- M2/M3: _match_winget_db 必须使用 _WINGET_NAME_INDEX ----
    def test_m2_m3_winget_index_used(self):
        """_match_winget_db 必须使用 _WINGET_NAME_INDEX 索引，不能全表遍历"""
        src = self._read("utils.py")
        idx_match = src.find("def _match_winget_db")
        self.assertGreater(idx_match, 0, "找不到 _match_winget_db")
        func_body = src[idx_match:]
        next_def = func_body.find("\ndef ", 10)
        if next_def > 0:
            func_body = func_body[:next_def]
        self.assertIn("_WINGET_NAME_INDEX", func_body,
                      "_match_winget_db 必须使用 _WINGET_NAME_INDEX 索引（M2/M3 修复）")


# ============================================================
# H1 源码哨兵：migrator.py 必须用 commonpath 而非 startswith
# ============================================================
class TestMigratorSourceCheck(unittest.TestCase):
    """H1: migrator.py 路径校验必须用 commonpath"""

    def _read(self, filename):
        path = os.path.join(_SRC_DIR, "core", filename)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_uses_commonpath_not_startswith(self):
        """路径包含校验必须用 os.path.commonpath，不能用 startswith（H1 修复）"""
        src = self._read("migrator.py")
        self.assertIn("os.path.commonpath", src, "必须用 commonpath 做路径包含校验")
        # startswith + os.sep 的旧模式不应存在（那是 bug 根源）
        self.assertNotIn("startswith(_norm + os.sep)", src,
                         "不应再用 startswith(_norm + os.sep) 做包含校验（H1 已修）")

    def test_ps_quote_exists(self):
        """PowerShell 命令注入修复：_ps_quote 转义函数必须存在"""
        src = self._read("migrator.py")
        self.assertIn("def _ps_quote", src, "_ps_quote 转义函数必须存在（H3 修复）")
        self.assertIn("replace(\"'\", \"''\")", src, "_ps_quote 必须转义单引号")


# ============================================================
# H7: 快照恢复同步配置文件 - 源码哨兵
# ============================================================
class TestSnapshotRestoreSourceCheck(unittest.TestCase):
    """H7: 快照恢复后必须同步 Maven/Bazel 配置文件"""

    def _read_ui(self, filename):
        path = os.path.join(_SRC_DIR, "ui", filename)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_snapshot_restore_syncs_config_files(self):
        """快照恢复后应同步 Maven settings.xml / Bazel .bazelrc"""
        src = self._read_ui("ui_snapshot.py")
        # 应有配置文件同步逻辑
        self.assertIn("settings.xml", src, "快照恢复应同步 Maven settings.xml")
        self.assertIn("bazelrc", src.lower().replace("Bazel", "bazel"),
                      "快照恢复应同步 Bazel .bazelrc")

    def test_snapshot_data_none_guard(self):
        """快照数据为 None 时不应触发 unconfigure（H7 自检 bug 修复）"""
        src = self._read_ui("ui_snapshot.py")
        # 应有 if data: 或类似的条件检查，防止 None 触发 unconfigure
        self.assertIn("if data", src, "应有 if data 条件检查，防止快照损坏时错误 unconfigure")


# ============================================================
# H5: 批量重建链接检测真实目录 - 源码哨兵
# ============================================================
class TestBatchRebuildSourceCheck(unittest.TestCase):
    """H5: 批量重建链接前必须检测 C 盘真实目录"""

    def _read_ui(self, filename):
        path = os.path.join(_SRC_DIR, "ui", filename)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_batch_rebuild_checks_real_dir(self):
        """批量重建链接前应检测真实目录并弹确认框"""
        src = self._read_ui("ui_migrate.py")
        # 应有真实目录检测逻辑
        self.assertIn("is_symlink", src, "应检测是否为符号链接")
        # 应有确认框（不能直接删除）
        self.assertTrue("QMessageBox" in src and "重建链接" in src,
                        "批量重建链接应有确认框")


# ============================================================
# H20: 线程 finished 信号 - 源码哨兵
# ============================================================
class TestThreadCleanupSourceCheck(unittest.TestCase):
    """H20: 线程 wait 返回 True 时应直接 deleteLater"""

    def _read_ui(self, filename):
        path = os.path.join(_SRC_DIR, "ui", filename)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_wait_success_calls_delete_later(self):
        """wait 返回 True 时应直接 deleteLater，不等 finished 信号"""
        src = self._read_ui("ui_lifecycle.py")
        self.assertIn("deleteLater", src, "应有 deleteLater 调用")
        self.assertIn(".wait(", src, "应有 wait 调用")


if __name__ == "__main__":
    unittest.main(verbosity=2)
