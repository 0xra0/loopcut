"""Checkout bootstrap installs the version hook before scripts import add-on children."""

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import Mock, patch


REPO = Path(__file__).resolve().parents[2]


class CheckoutTest(unittest.TestCase):
    def test_driver_uses_the_runner_blender_environment(self):
        spec = importlib.util.spec_from_file_location("mixed_check", REPO / "loopcut/harness/fq_update_mixed_check.py")
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)

        def prepare(repo, out):
            case = out / "synthetic"
            case.mkdir()
            (case / "summary.json").write_text('{"ok": true}', encoding="utf-8")
            return [{"name": "synthetic"}]

        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {"LOOPCUT_BLENDER": sys.executable}), \
                patch.object(sys, "argv", ["check", "--out", str(Path(folder) / "results")]), \
                patch.object(check, "prepare_cases", side_effect=prepare), \
                patch.object(check.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run:
            check.run()
            self.assertEqual(run.call_args.args[0][0], str(Path(sys.executable).resolve()))

    def run_cached_msi_witness(self, *, turn_guard, bypass=False):
        source = REPO / "loopcut/harness/fq_update_mixed_check.py"
        spec = importlib.util.spec_from_file_location("mixed_check", source)
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        callbacks, exits = [], []
        session = {"input": "", "items": [], "busy": False}
        guard = types.SimpleNamespace(MESSAGE="Restart Blender", _started=(8, 0, 0))
        item_error = lambda text: {"kind": "error", "text": text}

        def refuse(*args):
            session["items"].append(item_error(guard.MESSAGE))
            return False

        if turn_guard:
            guard.allow_turn = refuse
        agent = types.SimpleNamespace(_configure=Mock(side_effect=AssertionError("original configure ran")))

        def submit(current):
            if bypass:
                return refuse()
            if turn_guard and not guard.allow_turn(current):
                return
            try:
                agent._configure()
            except ImportError:
                refuse()

        window = types.SimpleNamespace(screen=types.SimpleNamespace(areas=[
            types.SimpleNamespace(width=100, height=100)]))
        bpy = types.SimpleNamespace(
            app=types.SimpleNamespace(timers=types.SimpleNamespace(
                is_registered=lambda timer: False, register=lambda tick, **kw: callbacks.append(tick))),
            context=types.SimpleNamespace(window_manager=types.SimpleNamespace(windows=[window]),
                                          temp_override=lambda **kw: nullcontext()),
            ops=types.SimpleNamespace(loopcut=types.SimpleNamespace(open=lambda: None),
                                      screen=types.SimpleNamespace(screenshot=lambda **kw: screenshot(**kw))))
        host = types.SimpleNamespace(_submit=submit, tag_redraw_all=lambda: None,
                                     _displays={0: {"prims": [{"text": guard.MESSAGE}]}})
        package = types.ModuleType("loopcut")
        package.bl_info = {"version": (8, 0, 0)}
        package.agent, package.llm, package.version_guard = agent, object(), guard
        package.state = types.SimpleNamespace(reset=lambda: None, session=lambda: session, item_error=item_error)
        package.onboarding = types.SimpleNamespace(start=lambda: None)
        ui = types.ModuleType("loopcut.ui")
        ui.host = host
        addon_utils = types.SimpleNamespace(disable=lambda *a, **kw: None, enable=lambda *a, **kw: package)
        modules = {"loopcut": package, "loopcut.ui": ui, "loopcut.llm": package.llm,
                   "loopcut.version_guard": guard, "addon_utils": addon_utils, "bpy": bpy}
        modules["loopcut.chatgpt_responses"] = None  # The response import must still go through the guard.

        def screenshot(*, filepath):
            Path(filepath).write_bytes(b"synthetic screenshot")
            return {"FINISHED"}

        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, modules), \
                patch.object(check.os, "_exit", side_effect=exits.append), \
                patch.object(check.importlib, "import_module", side_effect=ImportError(guard.MESSAGE)):
            out = Path(folder)
            package.__file__ = str(out / "__init__.py")
            Path(package.__file__).write_text('bl_info = {"version": (8, 0, 0)}\n', encoding="utf-8")
            # msi_witness requires the lazy module to be absent before it arms the timer.
            sys.modules.pop("loopcut.chatgpt_responses", None)
            check.msi_witness(out)
            Path(package.__file__).write_text('bl_info = {"version": (8, 0, 1)}\n', encoding="utf-8")
            (out / "install-complete.json").write_text(
                json.dumps({"version": "8.0.1", "reenable": False}), encoding="utf-8")
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            if not exits:
                callbacks[0]()
            self.assertEqual(len(exits), 1)
            return json.loads((out / "summary.json").read_text(encoding="utf-8")), exits[0]

    def test_cached_msi_check_identifies_turn_start_and_configure_time_refusals(self):
        for turn_guard, expected in ((True, "turn-start"), (False, "configure-time")):
            with self.subTest(expected=expected):
                summary, code = self.run_cached_msi_witness(turn_guard=turn_guard)
                self.assertEqual(code, 0, summary)
                self.assertEqual(summary.get("refusal_path"), expected)
                self.assertEqual(summary.get("expected_refusal_path"), expected)

    def test_cached_msi_check_rejects_a_card_without_the_expected_refusal_path(self):
        summary, code = self.run_cached_msi_witness(turn_guard=True, bypass=True)
        self.assertEqual(code, 1, summary)
        self.assertIn("expected turn-start refusal", summary["error"])

    def test_timer_screenshot_has_a_window_override(self):
        source = REPO / "loopcut/harness/fq_update_mixed_check.py"
        node = next(n for n in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
                    if isinstance(n, ast.FunctionDef) and n.name == "rendered_card")
        active = []
        window = object()

        @contextmanager
        def override(**kwargs):
            self.assertIs(kwargs["window"], window)
            active.append(window)
            try:
                yield
            finally:
                active.pop()

        def screenshot(*, filepath):
            if not active:
                raise RuntimeError("timer screenshot needs a window override")
            Path(filepath).write_bytes(b"synthetic screenshot")
            return {"FINISHED"}

        guard = types.SimpleNamespace(MESSAGE="Restart Blender")
        host = types.SimpleNamespace(_displays={0: {"prims": [{"text": guard.MESSAGE}]}})
        package = types.ModuleType("loopcut")
        package.version_guard = guard
        ui = types.ModuleType("loopcut.ui")
        ui.host = host
        bpy = types.SimpleNamespace(context=types.SimpleNamespace(
            window_manager=types.SimpleNamespace(windows=[window]), temp_override=override),
            ops=types.SimpleNamespace(screen=types.SimpleNamespace(screenshot=screenshot)))
        with tempfile.TemporaryDirectory() as folder, patch.dict(sys.modules, {"loopcut": package, "loopcut.ui": ui}):
            finish = Mock()
            namespace = {"bpy": bpy, "out": Path(folder), "summary": {}, "json": json,
                         "asserted": self.assertTrue, "finish": finish}
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
            namespace["rendered_card"]()
            finish.assert_called_once_with(0)
            self.assertTrue((Path(folder) / "restart.png").is_file())

    def test_mixed_check_uses_historical_modules_and_a_no_reenable_case(self):
        for ref in ("5cc0a53a36a", "abba2f12d76"):
            result = subprocess.run(["git", "cat-file", "-e", ref + ":scripts/addons_core/loopcut/__init__.py"],
                                    cwd=REPO, capture_output=True, timeout=10)
            if result.returncode:
                self.skipTest("historical add-on source is not in this checkout")
        spec = importlib.util.spec_from_file_location("mixed_check", REPO / "loopcut/harness/fq_update_mixed_check.py")
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            cases = check.prepare_cases(REPO, out)
            self.assertEqual({case["name"] for case in cases},
                             {"legacy-reenable", "guarded-reenable", "running-no-reenable"})
            legacy = out / "legacy-reenable/addons/loopcut"
            base = out / "guarded-reenable/addons/loopcut"
            self.assertNotIn("\ndef opened(", (legacy / "llm.py").read_text(encoding="utf-8"))
            self.assertFalse((legacy / "version_guard.py").exists())
            self.assertNotIn("def allow_turn(", (base / "version_guard.py").read_text(encoding="utf-8"))
            for case in cases:
                fresh = out / case["name"] / "replacement"
                self.assertIn("def allow_turn(", (fresh / "version_guard.py").read_text(encoding="utf-8"))
            self.assertFalse(next(c for c in cases if c["name"] == "running-no-reenable")["reenable"])

    def test_children_imported_before_addon_register_are_stamped_and_can_send(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "loopcut"
            root.mkdir()
            (root / "__init__.py").write_text('bl_info = {"version": (8, 0, 0)}\n', encoding="utf-8")
            (root / "agent.py").write_text('def send(text): return True\ndef resume(): return True\n',
                                          encoding="utf-8")
            (root / "version_guard.py").write_bytes(
                (REPO / "scripts/addons_core/loopcut/version_guard.py").read_bytes())
            script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import checkout
checkout.PACKAGE = Path(sys.argv[2])
checkout.use()
from loopcut import agent, version_guard
version_guard.register('loopcut', checkout.PACKAGE, (8, 0, 0))
version_guard.ensure_consistent()
assert agent.__loopcut_version__ == (8, 0, 0)
assert agent.send('Create a cube') is True
assert agent.resume() is True
"""
            result = subprocess.run([sys.executable, "-c", script, str(REPO / "loopcut/harness"), str(root)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
