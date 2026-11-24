"""核心引擎单元测试。运行：python3 -I tests/test_core.py（从仓库根目录）。"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from app.elfparse import ElfError, ElfFile, is_x86_64_elf  # noqa: E402
from app.resolver import simulate_both_orders, simulate_order  # noqa: E402
from app.service import list_plugins, run_simulation  # noqa: E402

SAMPLES = os.path.join(REPO_ROOT, "samples")
ALPHA = os.path.join(SAMPLES, "plugin_alpha.so")
BETA = os.path.join(SAMPLES, "plugin_beta.so")
FOO_A = os.path.join(SAMPLES, "vendor_alpha", "libfoo.so.1")
FOO_B = os.path.join(SAMPLES, "vendor_beta", "libfoo.so.1")


class TestElfParse(unittest.TestCase):
    def test_dynamic_tags(self):
        elf = ElfFile(ALPHA)
        self.assertEqual(elf.needed, ["libfoo.so.1", "libc.so.6"])
        self.assertEqual(elf.runpath, ["$ORIGIN/vendor_alpha"])
        self.assertIsNone(elf.soname)  # 插件没有 SONAME

    def test_soname(self):
        self.assertEqual(ElfFile(FOO_A).soname, "libfoo.so.1")
        self.assertEqual(ElfFile(FOO_B).soname, "libfoo.so.1")

    def test_versioned_requirements(self):
        self.assertEqual(ElfFile(ALPHA).versioned_requirements("libfoo.so.1"),
                         [("foo", "FOO_1.0")])
        self.assertEqual(ElfFile(BETA).versioned_requirements("libfoo.so.1"),
                         [("foo", "FOO_2.0")])
        # 系统库条目不影响对私有库的提取
        self.assertEqual(ElfFile(ALPHA).versioned_requirements("nosuch.so"), [])

    def test_strong_provided_versions(self):
        self.assertEqual(ElfFile(FOO_A).strong_provided_versions("foo"),
                         {"FOO_1.0"})
        self.assertEqual(ElfFile(FOO_B).strong_provided_versions("foo"),
                         {"FOO_1.0", "FOO_2.0"})
        self.assertEqual(ElfFile(FOO_A).strong_provided_versions("nosuch"),
                         set())

    def test_probe(self):
        self.assertTrue(is_x86_64_elf(ALPHA))
        self.assertFalse(is_x86_64_elf(os.path.join(SAMPLES, "src", "foo_v1.c")))

    def test_reject_non_elf(self):
        with self.assertRaises(ElfError):
            ElfFile(os.path.join(SAMPLES, "src", "foo_v1.c"))


class TestSimulate(unittest.TestCase):
    def test_order_alpha_beta_blocked(self):
        result = simulate_order([ALPHA, BETA], SAMPLES)
        self.assertEqual(result["status"], "blocked")
        block = result["block"]
        self.assertIn("foo@FOO_2.0", block["reason"])
        self.assertEqual(block["hit_path"], FOO_A)
        self.assertEqual([c["name"] for c in block["chain"]],
                         ["plugin_beta.so", "libfoo.so.1"])
        self.assertEqual(block["missing"], [("foo", "FOO_2.0")])

    def test_order_beta_alpha_ok(self):
        result = simulate_order([BETA, ALPHA], SAMPLES)
        self.assertEqual(result["status"], "ok")
        paths = [l["path"] for l in result["loads"] if not l["reused"]]
        self.assertEqual(paths, [BETA, FOO_B, ALPHA])
        # 复用事件存在
        self.assertTrue(any("复用" in e and FOO_B in e
                            for e in result["events"]))
        # 系统库被跳过
        self.assertTrue(any("libc.so.6" in e and "跳过" in e
                            for e in result["events"]))

    def test_both_orders_independent(self):
        result = simulate_both_orders(SAMPLES, "plugin_alpha.so",
                                      "plugin_beta.so")
        self.assertEqual(result["orders"][0]["status"], "blocked")
        self.assertEqual(result["orders"][1]["status"], "ok")
        # 两种顺序各自从空状态开始：可行顺序里装的是 vendor_beta 那份
        ok_paths = [l["path"] for l in result["orders"][1]["loads"]]
        self.assertNotIn(FOO_A, ok_paths)

    def test_missing_runpath_entry(self):
        # 把插件放到没有私有库的布局里：RUNPATH 指向的目录不存在 -> 阻断
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(ALPHA, os.path.join(tmp, "plugin_alpha.so"))
            result = simulate_order([os.path.join(tmp, "plugin_alpha.so")], tmp)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("libfoo.so.1", result["block"]["reason"])
            self.assertIsNone(result["block"]["hit_path"])

    def test_soname_mismatch_blocked(self):
        # 私有库文件名与 SONAME 不一致 -> 阻断，且给出命中路径
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "vendor_alpha"))
            shutil.copy(ALPHA, os.path.join(tmp, "plugin_alpha.so"))
            # 现场构建一个文件名叫 libfoo.so.1、SONAME 却是 libother.so 的库
            src = os.path.join(tmp, "x.c")
            with open(src, "w") as fh:
                fh.write("int foo(void){return 1;}\n")
            subprocess.run(
                ["gcc", "-shared", "-fPIC", "-Wl,--soname,libother.so",
                 "-o", os.path.join(tmp, "vendor_alpha", "libfoo.so.1"), src],
                check=True)
            result = simulate_order([os.path.join(tmp, "plugin_alpha.so")], tmp)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("SONAME", result["block"]["reason"])
            self.assertTrue(result["block"]["hit_path"].endswith("libfoo.so.1"))

    def test_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "sub"))
            shutil.copy(ALPHA, os.path.join(tmp, "sub", "plugin_alpha.so"))
            # RUNPATH 改为 $ORIGIN/.. 仍能留在挂载目录内；这里验证选择器越界
            from app.resolver import resolve_inside
            with self.assertRaises(ElfError):
                resolve_inside(os.path.join(tmp, "sub"), "../plugin_alpha.so")

    def test_expand_origin_forms(self):
        from app.resolver import _expand_origin
        self.assertEqual(_expand_origin("$ORIGIN", "/x"), "/x")
        self.assertEqual(_expand_origin("$ORIGIN/a/b", "/x"), "/x/a/b")
        self.assertEqual(_expand_origin("${ORIGIN}/a", "/x"), "/x/a")
        self.assertIsNone(_expand_origin("/abs/path", "/x"))
        self.assertIsNone(_expand_origin("$OTHER/a", "/x"))

    def test_non_origin_runpath_blocked(self):
        # RUNPATH 只有非 $ORIGIN 条目 -> 找不到库，原因里说明只支持 $ORIGIN
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "libs"))
            shutil.copy(FOO_A, os.path.join(tmp, "libs", "libfoo.so.1"))
            src = os.path.join(tmp, "p.c")
            with open(src, "w") as fh:
                fh.write("extern int foo(void); int p(void){return foo();}\n")
            plugin = os.path.join(tmp, "p.so")
            subprocess.run(
                ["gcc", "-shared", "-fPIC", "-Wl,-rpath,/etc",
                 "-o", plugin, src, os.path.join(tmp, "libs", "libfoo.so.1")],
                check=True)
            result = simulate_order([plugin], tmp)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("$ORIGIN", result["block"]["reason"])
            self.assertIn("/etc", result["block"]["reason"])


class TestService(unittest.TestCase):
    def test_list_plugins(self):
        plugins = list_plugins(SAMPLES)
        self.assertIn("plugin_alpha.so", plugins)
        self.assertIn("plugin_beta.so", plugins)
        self.assertIn(os.path.join("vendor_alpha", "libfoo.so.1"), plugins)

    def test_run_simulation_validation(self):
        with self.assertRaises(ValueError):
            run_simulation(SAMPLES, "plugin_alpha.so", "plugin_alpha.so")
        with self.assertRaises(ValueError):
            run_simulation(SAMPLES, "plugin_alpha.so", "nope.so")
        result = run_simulation(SAMPLES, "plugin_alpha.so", "plugin_beta.so")
        self.assertEqual(len(result["orders"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
