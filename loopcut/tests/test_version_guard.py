"""version_guard: a module whose file is from another Loopcut version than the running one is not loaded.
A throwaway package on disk stands in for the add-on; no Blender."""

import ast
import importlib
import importlib.abc
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

import loopcut  # noqa: E402
from loopcut import version_guard  # noqa: E402

INIT = 'bl_info = {\n    "name": "Stand-in",\n    "version": (%d, %d, %d),\n}\n'


class VersionGuardTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.package = f"guarded_{id(self)}"
        self.root = self.folder / self.package
        self.root.mkdir()
        self.stamp(0, 2, 18)
        for name in ("early", "lazy", "other"):
            (self.root / f"{name}.py").write_text(f"NAME = {name!r}\n", encoding="utf-8")
        sys.path.insert(0, str(self.folder))
        self.addCleanup(sys.path.remove, str(self.folder))
        importlib.import_module(self.package)
        self.started = version_guard._started
        version_guard._started = None
        self.addCleanup(setattr, version_guard, "_started", self.started)
        version_guard.register(self.package, self.root, (0, 2, 18))
        self.addCleanup(version_guard.unregister, self.package)
        self.addCleanup(self.forget)

    def stamp(self, *version):
        (self.root / "__init__.py").write_text(INIT % version, encoding="utf-8")

    def forget(self):
        for name in [n for n in sys.modules if n == self.package or n.startswith(self.package + ".")]:
            del sys.modules[name]

    def load(self, name):
        importlib.invalidate_caches()
        return importlib.import_module(f"{self.package}.{name}")

    def test_the_same_version_on_disk_loads_as_usual(self):
        self.assertEqual(self.load("lazy").NAME, "lazy")

    def test_imports_and_same_version_reloads_have_a_stamp(self):
        early = self.load("early")
        self.assertEqual(early.__loopcut_version__, (0, 2, 18))
        self.assertEqual(importlib.reload(early).__loopcut_version__, (0, 2, 18))
        version_guard.ensure_consistent(self.package)

    def disable_addon(self):
        source = Path(loopcut.__file__)
        node = next(n for n in ast.parse(source.read_text(encoding="utf-8")).body
                    if isinstance(n, ast.FunctionDef) and n.name == "unregister")
        stub = "def unregister(): pass\nregister = stop = shutdown_all = unregister\n"
        for name in ("account", "agent", "cloud", "dev_reload", "lifecycle", "mainthread", "session_logs",
                     "settings", "support", "telemetry", "worker_host"):
            (self.root / f"{name}.py").write_text(stub, encoding="utf-8")
        (self.root / "version_guard.py").write_text("from loopcut.version_guard import unregister\n",
                                                   encoding="utf-8")
        (self.root / "ui").mkdir()
        (self.root / "ui/__init__.py").write_text("", encoding="utf-8")
        (self.root / "ui/host.py").write_text("NATIVE = False\n" + stub, encoding="utf-8")
        importlib.invalidate_caches()  # The stubs were just written; on Windows a cached listing can miss them.
        namespace = {"__package__": self.package, "__name__": self.package}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
        namespace["unregister"]()

    def test_a_first_import_while_disabled_remains_consistent_after_reenable(self):
        self.load("early")
        self.disable_addon()
        lazy = self.load("lazy")
        version_guard.register(self.package, self.root, (0, 2, 18))
        version_guard.ensure_consistent(self.package)
        self.assertEqual(lazy.__loopcut_version__, (0, 2, 18))

    def test_a_first_import_while_disabled_still_refuses_changed_files(self):
        self.disable_addon()
        self.stamp(0, 2, 19)
        with self.assertRaises(version_guard.RestartNeeded):
            self.load("lazy")
        self.assertNotIn(f"{self.package}.lazy", sys.modules)

    def test_a_purged_package_can_register_a_fresh_reinstall(self):
        old_package = sys.modules[self.package]
        early = self.load("early")
        self.disable_addon()
        old_finder = next(f for f in sys.meta_path if getattr(f, "package", None) == self.package)
        self.forget()
        source = Path(loopcut.__file__).read_text(encoding="utf-8")
        source = source.replace(str(loopcut.bl_info["version"]), "(0, 3, 100)")
        (self.root / "__init__.py").write_text(source, encoding="utf-8")
        (self.root / "version_guard.py").write_text(
            Path(version_guard.__file__).read_text(encoding="utf-8"), encoding="utf-8")
        (self.root / "checkpoints.py").write_text("def remove_stale_sessions(): pass\n", encoding="utf-8")
        (self.root / "waves.py").write_text("def reap(): pass\n", encoding="utf-8")
        importlib.invalidate_caches()
        fresh = importlib.import_module(self.package)
        self.assertIsNot(fresh, old_package)
        fresh.register()
        guard = sys.modules[f"{self.package}.version_guard"]
        guard.ensure_consistent(self.package)
        self.assertEqual(guard._started, (0, 3, 100))
        self.assertNotIn(old_finder, sys.meta_path)
        self.assertEqual(sum(getattr(f, "package", None) == self.package for f in sys.meta_path), 1)
        current = self.load("early")
        self.assertIsNot(current, early)
        self.assertEqual(current.__loopcut_version__, (0, 3, 100))

    def test_an_orphaned_finder_stepping_aside_does_not_hide_the_next_finder(self):
        class Probe(importlib.abc.MetaPathFinder):
            seen = 0

            def find_spec(self, fullname, path=None, target=None):
                Probe.seen += 1
                return None

        orphan = version_guard._Finder(self.package, self.root, (0, 0, 1))
        orphan.package_module = object()  # Not the live package: this finder's lifetime has ended.
        probe, saved = Probe(), list(sys.meta_path)
        sys.meta_path[:0] = [orphan, probe]
        try:
            for finder in sys.meta_path:  # Python 3.13's import loop walks the live list like this.
                finder.find_spec("loopcut_unrelated_probe")
                if finder is probe:
                    break
            self.assertEqual(Probe.seen, 1)
            self.assertNotIn(orphan, sys.meta_path)
        finally:
            sys.meta_path[:] = saved

    def test_an_uninstalled_packages_finder_removes_itself_on_the_next_import(self):
        self.disable_addon()
        finder = next(f for f in sys.meta_path if getattr(f, "package", None) == self.package)
        self.forget()
        for path in self.root.glob("*.py"):
            path.unlink()
        name = self.package + "_unrelated"
        (self.folder / f"{name}.py").write_text("VALUE = 7\n", encoding="utf-8")
        self.addCleanup(sys.modules.pop, name, None)
        importlib.invalidate_caches()
        self.assertEqual(importlib.import_module(name).VALUE, 7)
        self.assertNotIn(finder, sys.meta_path)
        self.assertFalse(any(getattr(f, "package", None) == self.package for f in sys.meta_path))

    def test_a_cached_module_with_a_different_stamp_refuses_a_turn(self):
        stale = self.load("early")
        stale.__loopcut_version__ = (0, 2, 17)
        with self.assertRaises(version_guard.RestartNeeded):
            version_guard.ensure_consistent(self.package)

    def test_a_mixed_cached_module_shows_one_restart_card_and_keeps_the_draft(self):
        self.load("early").__loopcut_version__ = (0, 2, 17)
        session = {"items": [], "input": "Create a cube", "busy": False}
        check = version_guard.ensure_consistent
        with patch.object(version_guard, "ensure_consistent", side_effect=lambda package: check(self.package)):
            self.assertFalse(version_guard.allow_turn(session))
            self.assertFalse(version_guard.allow_turn(session))
        self.assertEqual(session, {"items": [{"kind": "error", "text": version_guard.MESSAGE}],
                                   "input": "Create a cube", "busy": False})

    def test_an_unstamped_cached_module_is_not_relabelled_on_register(self):
        stale = types.ModuleType(f"{self.package}.early")
        sys.modules[stale.__name__] = stale
        version_guard.unregister(self.package)
        version_guard.register(self.package, self.root, (0, 2, 18))
        with self.assertRaises(version_guard.RestartNeeded):
            version_guard.ensure_consistent(self.package)
        self.assertIsNone(getattr(stale, "__loopcut_version__", None))

    def test_first_guard_registration_refuses_legacy_cached_turn_functions(self):
        version_guard.unregister(self.package)
        version_guard._started = None
        self.stamp(0, 2, 19)
        sys.modules[self.package].__loopcut_version__ = (0, 2, 19)
        session = {"items": [], "input": "Create a cube", "busy": False}
        old_send, old_resume, old_convert = (Mock(return_value=True) for _ in range(3))
        agent = types.ModuleType(f"{self.package}.agent")
        agent.send, agent.resume, agent.turn_into_3d = old_send, old_resume, old_convert
        state = types.ModuleType(f"{self.package}.state")
        state.session = lambda: session
        state.item_error = lambda text: {"kind": "error", "text": text}
        sys.modules[agent.__name__], sys.modules[state.__name__] = agent, state
        version_guard.register(self.package, self.root, (0, 2, 19))
        self.assertFalse(agent.send("Create a cube"))
        self.assertFalse(agent.resume())
        self.assertFalse(agent.turn_into_3d(0))
        old_send.assert_not_called()
        old_resume.assert_not_called()
        old_convert.assert_not_called()
        self.assertEqual(session["items"], [{"kind": "error", "text": version_guard.MESSAGE}])
        self.assertEqual(session["input"], "Create a cube")

    def test_register_removes_an_import_hook_from_an_older_guard_class(self):
        legacy_type = type("_Finder", (), {"__module__": version_guard.__name__, "package": self.package})
        legacy = legacy_type()
        sys.meta_path.append(legacy)
        self.addCleanup(lambda: sys.meta_path.remove(legacy) if legacy in sys.meta_path else None)
        version_guard.register(self.package, self.root, (0, 2, 18))
        self.assertNotIn(legacy, sys.meta_path)

    def test_disk_replacement_refuses_a_turn_even_without_a_new_import(self):
        self.load("early")
        self.stamp(0, 2, 19)
        with self.assertRaises(version_guard.RestartNeeded):
            version_guard.ensure_consistent(self.package)

    def test_reloading_a_submodule_after_disk_replacement_is_refused(self):
        early = self.load("early")
        self.stamp(0, 2, 19)
        with self.assertRaises(version_guard.RestartNeeded):
            importlib.reload(early)

    def test_reloading_the_package_cannot_hide_older_loaded_modules(self):
        early = self.load("early")
        self.stamp(0, 2, 19)
        importlib.reload(sys.modules[self.package])
        version_guard.unregister(self.package)
        version_guard.register(self.package, self.root, (0, 2, 19))
        with self.assertRaises(version_guard.RestartNeeded):
            version_guard.ensure_consistent(self.package)
        self.assertEqual(early.__loopcut_version__, (0, 2, 18))

    def test_a_module_first_loaded_after_an_update_is_refused_with_the_restart_message(self):
        early = self.load("early")
        self.stamp(0, 2, 19)  # The installer replaced the files under this Blender.
        with self.assertRaises(version_guard.RestartNeeded) as caught:
            self.load("lazy")
        self.assertEqual(str(caught.exception), "Loopcut was updated while Blender was open. "
                                                "Restart Blender to finish the update.")
        self.assertNotIn(f"{self.package}.lazy", sys.modules)
        self.assertIs(self.load("early"), early, "what is loaded already stays as it is")
        self.stamp(0, 2, 17)  # Or an older build put back: just as mixed.
        with self.assertRaises(version_guard.RestartNeeded):
            self.load("other")

    def test_from_import_raises_it_too_not_a_vaguer_import_error(self):
        self.stamp(0, 2, 19)
        with self.assertRaises(version_guard.RestartNeeded):
            exec(f"from {self.package} import lazy", {})

    def test_an_unreadable_version_lets_the_import_through(self):
        (self.root / "__init__.py").write_text("bl_info = {}\n", encoding="utf-8")  # Mid-copy, say.
        self.assertEqual(self.load("lazy").NAME, "lazy")
        (self.root / "__init__.py").unlink()
        self.assertEqual(self.load("other").NAME, "other")

    def test_an_unreadable_version_also_allows_a_consistent_turn(self):
        self.load("early")
        for content in ("bl_info = {}\n", None):
            with self.subTest(content=content):
                init = self.root / "__init__.py"
                if content is None:
                    init.unlink()
                else:
                    init.write_text(content, encoding="utf-8")
                version_guard.ensure_consistent(self.package)
        self.load("early").__loopcut_version__ = (0, 2, 17)
        with self.assertRaises(version_guard.RestartNeeded):
            version_guard.ensure_consistent(self.package)

    def test_reloading_the_historical_guard_preserves_its_original_version(self):
        repo = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["git", "show", "abba2f12d76:scripts/addons_core/loopcut/version_guard.py"],
            cwd=repo, capture_output=True, text=True, timeout=10)
        if result.returncode:
            self.skipTest("historical guard source is not in this checkout")
        historical = result.stdout
        version_guard.unregister(self.package)
        path = self.root / "version_guard.py"
        path.write_text(historical, encoding="utf-8")
        legacy = self.load("version_guard")
        self.addCleanup(lambda: legacy.unregister(self.package))
        legacy.register(self.package, self.root, (0, 2, 18))
        old_finder = sys.meta_path[0]
        self.stamp(0, 2, 19)
        path.write_text(Path(version_guard.__file__).read_text(encoding="utf-8"), encoding="utf-8")
        importlib.invalidate_caches()
        importlib.reload(legacy)
        legacy.register(self.package, self.root, (0, 2, 19))
        self.assertEqual(legacy._started, (0, 2, 18))
        self.assertNotIn(old_finder, sys.meta_path)
        self.assertEqual(sum(getattr(f, "package", None) == self.package for f in sys.meta_path), 1)
        with self.assertRaises(legacy.RestartNeeded):
            legacy.ensure_consistent(self.package)

    def test_a_reregister_after_a_reloaded_init_keeps_the_version_blender_started_with(self):
        # Blender reloads __init__.py when its file changed, not the modules it imported: the newer bl_info
        # must not make the old modules look current.
        self.stamp(0, 2, 19)
        version_guard.register(self.package, self.root, (0, 2, 19))
        with self.assertRaises(version_guard.RestartNeeded):
            self.load("lazy")
        self.assertEqual(sum(isinstance(f, version_guard._Finder) and f.package == self.package
                             for f in sys.meta_path), 1)

    def test_other_packages_are_not_checked(self):
        self.stamp(0, 2, 19)
        self.assertIsNotNone(importlib.import_module("json.decoder"))

    def test_the_real_init_states_the_version_the_guard_reads(self):
        root = Path(loopcut.__file__).parent
        self.assertEqual(version_guard.version_on_disk(root), tuple(loopcut.bl_info["version"]))


class SubmitTest(unittest.TestCase):
    def test_register_refreshes_a_legacy_guard_before_other_addon_imports(self):
        import ast
        import os
        source = Path(loopcut.__file__)
        register = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                        if isinstance(node, ast.FunctionDef) and node.name == "register")
        bootstrap = []
        for node in register.body:
            if isinstance(node, ast.Try):
                break
            bootstrap.append(node)
        namespace = {"__package__": "loopcut", "__name__": "loopcut", "__file__": str(source),
                     "bl_info": loopcut.bl_info, "os": os}
        with patch.dict(version_guard.__dict__), patch.object(version_guard, "register") as install, \
                patch("importlib.reload", return_value=version_guard) as reload:
            version_guard.__dict__.pop("allow_turn", None)
            exec(compile(ast.Module(body=bootstrap, type_ignores=[]), str(source), "exec"), namespace)
            reload.assert_called_once_with(version_guard)
            install.assert_called_once()

    def test_send_and_resume_check_before_configuring_or_mutating_the_turn(self):
        import ast
        source = Path(loopcut.__file__).parent / "agent.py"
        functions = {node.name: node for node in ast.parse(source.read_text(encoding="utf-8")).body
                     if isinstance(node, ast.FunctionDef) and node.name in {"send", "resume", "turn_into_3d"}}
        for name, args in (("send", ("Build a cube",)), ("resume", ()), ("turn_into_3d", (0,))):
            with self.subTest(name=name), patch.object(version_guard, "allow_turn", return_value=False,
                                                       create=True) as check:
                session = {"attachments": [], "busy": False, "items": [], "input": "Build a cube"}
                before = dict(session)
                namespace = {"__package__": "loopcut", "state": types.SimpleNamespace(
                    session=lambda: session, can_resume=lambda s: True),
                    "Turn": Mock(side_effect=AssertionError("turn must not start"))}
                exec(compile(ast.Module(body=[functions[name]], type_ignores=[]), str(source), "exec"), namespace)
                self.assertFalse(namespace[name](*args))
                check.assert_called_once_with(session)
                self.assertEqual(session, before)

    def test_a_refused_import_while_sending_is_a_chat_card_and_the_input_stays(self):
        # ui.host._submit, taken from the source: agent.send imports a few modules on first use itself.
        import ast
        import types
        source = Path(loopcut.__file__).parent / "ui" / "host.py"
        nodes = [node for node in ast.parse(source.read_text(encoding="utf-8")).body
                 if isinstance(node, ast.FunctionDef) and node.name == "_submit"]
        cleared = []

        def send(text):
            raise version_guard.RestartNeeded(version_guard.MESSAGE, name="loopcut.video_attach")
        host = {"__package__": "loopcut.ui", "agent": types.SimpleNamespace(send=send),
                "state": types.SimpleNamespace(item_error=lambda text: {"kind": "error", "text": text},
                                               ui={"mentions": ["@Cube"]}),
                "textedit": types.SimpleNamespace(set_text=lambda session, text: cleared.append(text))}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), host)
        session = {"input": "Make it red", "items": []}
        host["_submit"](session)
        self.assertEqual(session["items"], [{"kind": "error", "text": version_guard.MESSAGE}])
        self.assertEqual((session["input"], cleared), ("Make it red", []))


if __name__ == "__main__":
    unittest.main()
