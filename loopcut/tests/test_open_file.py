"""Consent and save-before-open rules with synthetic file operators."""

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/addons_core"))
fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import agent, files, live_file, state, tools, workspace  # noqa: E402
from loopcut.ui import layout  # noqa: E402


class OpenFileTest(unittest.TestCase):
    def setUp(self):
        self.work = self.enterContext(tempfile.TemporaryDirectory())
        self.current, self.target = (Path(self.work).resolve() / name for name in ("current.blend", "other.blend"))
        self.current.write_bytes(b"BLENDER-current")
        self.target.write_bytes(b"BLENDER-target")
        self.events = []
        self.data = types.SimpleNamespace(filepath=str(self.current), is_dirty=True, images=[])
        self.save = Mock(side_effect=lambda: self.events.append("save") or {"FINISHED"})
        self.open = Mock(side_effect=lambda **kw: self.events.append("open") or {"FINISHED"})
        self.bpy = types.SimpleNamespace(data=self.data, ops=types.SimpleNamespace(
            wm=types.SimpleNamespace(save_mainfile=self.save, open_mainfile=self.open)))
        self.module = importlib.import_module("loopcut.open_file")
        self.enterContext(patch.object(self.module, "bpy", self.bpy))
        self.scratch = Path(self.work).resolve() / "scratch"
        self.original_temp_roots = files._temp_roots
        self.enterContext(patch.object(files, "_temp_roots", return_value=(self.scratch,)))
        self.enterContext(patch.object(files, "_forbidden_roots", return_value=()))

    def test_dirty_file_saves_before_opening_and_keeps_script_preference(self):
        self.assertTrue(self.module.open_file(str(self.target)).ok)
        self.assertEqual(self.events, ["save", "open"])
        self.open.assert_called_once_with(filepath=str(self.target), load_ui=False)

    def test_scratch_and_workspace_targets_are_refused_before_saving(self):
        data = Path(self.work).resolve() / "data"
        ws = data / "workspaces" / "fixture"
        targets = (self.scratch / "blender" / "other.blend", self.scratch / "Loopcut" / "other.blend",
                   ws / "generated-workspace.blend", data / "other.blend")
        with patch.object(files, "_forbidden_roots", return_value=(data,)), \
                patch.object(workspace, "_ROOTS", {ws}), patch.object(workspace, "current", return_value=ws):
            for target in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"BLENDER-fixture")
                with self.subTest(target=target), self.assertRaisesRegex(tools.ToolError, "project folder"):
                    self.module.open_file(str(target))
            with self.assertRaisesRegex(tools.ToolError, "project folder"):
                self.module.open_file("generated-workspace.blend")
        self.assertEqual(self.events, [])

    def test_symlink_to_scratch_cannot_be_opened(self):
        self.scratch.mkdir()
        target = self.scratch / "other.blend"
        target.write_bytes(b"BLENDER-fixture")
        link = Path(self.work) / "linked.blend"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(tools.ToolError, "project folder"):
            self.module.open_file(str(link))
        self.assertEqual(self.events, [])

    def test_consent_shows_resolved_target_for_each_relative_form(self):
        ws = Path(self.work).resolve() / "workspace"
        with patch.object(files, "blend_base", return_value=(self.current.parent, "")), \
                patch.object(workspace, "current", return_value=ws):
            for raw, expected in (("other.blend", ws / "other.blend"), ("//other.blend", self.target)):
                with self.subTest(raw=raw):
                    _, detail = self.module.describe({"path": raw})
                    self.assertIn(f"Open: {expected}", detail)

    def test_relative_open_uses_the_same_resolution_as_the_card(self):
        with patch.object(files, "blend_base", return_value=(self.current.parent, "")), \
                patch.object(workspace, "current", return_value=None):
            self.assertIn(str(self.target), self.module.describe({"path": "//other.blend"})[1])
            self.module.open_file("//other.blend")
        self.open.assert_called_once_with(filepath=str(self.target), load_ui=False)

    def test_dirty_images_refuse_even_when_the_scene_was_saved(self):
        self.data.images = [types.SimpleNamespace(name="PaintPixels", is_dirty=True, get={}.get),
                            types.SimpleNamespace(name="BakePixels", is_dirty=True, get={}.get),
                            types.SimpleNamespace(name="CleanImage", is_dirty=False, get={}.get)]
        for dirty in (False, True):
            self.data.is_dirty = dirty
            with self.subTest(scene_dirty=dirty):
                with self.assertRaises(tools.ToolError) as caught:
                    self.module.open_file(str(self.target))
                message = str(caught.exception)
                for name in ("PaintPixels", "BakePixels"):
                    self.assertIn(name, message)
                self.assertRegex(message.lower(), "sav.*pack|pack.*sav")
                self.assertNotIn("CleanImage", message)
        self.assertEqual(self.events, [])

    def test_baked_mask_temp_images_do_not_block_opening(self):
        from loopcut import ownership
        self.data.images = [types.SimpleNamespace(name=f"Mask_{kind}", is_dirty=True, is_float=True,
                                                 get={ownership.TEMP: True, "lc_paint_mask": kind}.get)
                            for kind in ("edge", "cavity", "ao")]
        for dirty in (False, True):
            with self.subTest(scene_dirty=dirty):
                self.events.clear()
                self.data.is_dirty = dirty
                self.assertEqual(self.module.prepare(str(self.target)), self.target)
                self.assertTrue(self.module.open_file(str(self.target)).ok)
                self.assertEqual(self.events, ["save", "open"] if dirty else ["open"])

    def dynamic_driver(self):
        from loopcut import codex_app, config
        driver = object.__new__(codex_app.CodexTurn)
        driver.session = state.new_session()
        driver.session["auto_run"] = True
        driver.turn = agent.Turn()
        driver.cfg = config.Config("", "", "fixture", "", True, False, 3, 40000)
        driver.run_tool = Mock(return_value=tools.ToolResult("Opened."))
        return driver

    def test_invalid_target_is_refused_before_consent(self):
        self.scratch.mkdir()
        temporary = self.scratch / "other.blend"
        temporary.write_bytes(b"BLENDER-fixture")
        self.data.images = []
        for target in (temporary, self.target.parent / "missing.blend", self.current):
            driver = self.dynamic_driver()
            card = state.item_tool("open_file", "Opening", "")
            with self.subTest(target=target), patch.object(agent, "_redraw"), \
                    patch.object(agent, "_on_main", side_effect=lambda fn, **kw: fn()), \
                    patch.object(agent, "_wait_for_approval", return_value=True) as wait:
                result = driver.run_dynamic(card, "open_file", json.dumps({"path": str(target)}))
            self.assertFalse(result[1])
            wait.assert_not_called()
            driver.run_tool.assert_not_called()

    def test_relative_consent_target_survives_save_as(self):
        driver = self.dynamic_driver()
        card = state.item_tool("open_file", "Opening", "")
        base = Mock(return_value=(self.current.parent, ""))
        def approve(turn):
            self.assertIn(str(self.target), card["code"])
            base.return_value = (self.current.parent / "moved", "")
            return True
        with patch.object(agent, "_redraw"), patch.object(agent, "_on_main", side_effect=lambda fn, **kw: fn()), \
                patch.object(agent, "_wait_for_approval", side_effect=approve), patch.object(files, "blend_base", base):
            self.assertTrue(driver.run_dynamic(card, "open_file", json.dumps({"path": "//other.blend"}))[1])
        self.assertEqual(json.loads(driver.run_tool.call_args.args[1]), {"path": str(self.target)})

    def test_target_is_rechecked_if_images_change_during_consent(self):
        driver = self.dynamic_driver()
        card = state.item_tool("open_file", "Opening", "")
        driver.run_tool.side_effect = lambda name, args: tools.execute(name, args)
        def approve(turn):
            self.assertIn("Image edits need saving or packing first", card["code"])
            self.data.images = [types.SimpleNamespace(name="LatePaint", is_dirty=True, get={}.get)]
            return True
        with patch.object(agent, "_redraw"), patch.object(agent, "_wait_for_approval", side_effect=approve):
            result = driver.run_dynamic(card, "open_file", json.dumps({"path": str(self.target)}))
        self.assertFalse(result[1])
        self.assertIn("LatePaint", result[0])
        self.assertEqual(self.events, [])

    def test_canonical_target_cannot_redirect_after_consent(self):
        other = self.target.with_name("replacement.blend")
        other.write_bytes(b"BLENDER-replacement")
        driver = self.dynamic_driver()
        card = state.item_tool("open_file", "Opening", "")
        driver.run_tool.side_effect = lambda name, args: tools.execute(name, args)
        def approve(turn):
            self.assertIn(str(self.target), card["code"])
            self.target.unlink()
            try:
                self.target.symlink_to(other)
            except OSError:
                self.skipTest("symlinks unavailable")
            return True
        with patch.object(agent, "_redraw"), patch.object(agent, "_wait_for_approval", side_effect=approve):
            result = driver.run_dynamic(card, "open_file", json.dumps({"path": str(self.target)}))
        self.assertFalse(result[1])
        self.assertIn("path changed", result[0])
        self.assertEqual(self.events, [])

    def test_system_tmp_aliases_are_temp_roots_even_with_another_tmpdir(self):
        self.scratch.mkdir()
        with patch.object(files.tempfile, "gettempdir", return_value=str(self.scratch)), \
                patch.dict(files.os.environ, {"TEMP": str(self.current.parent / "windows-temp")}), \
                patch.object(files, "_temp_roots", wraps=self.original_temp_roots):
            roots = files._temp_roots()
        expected = [self.current.parent / "windows-temp"]
        if files.os.name != "nt":
            expected.append(Path("/tmp"))  # resolve() also checks its canonical system alias.
        for root in expected:
            self.assertIn(root.resolve(), roots)

    def test_clean_file_opens_without_save(self):
        self.data.is_dirty = False
        self.assertTrue(self.module.open_file(str(self.target)).ok)
        self.assertEqual(self.events, ["open"])

    def test_unchanged_untitled_scene_can_open_without_a_save(self):
        self.data.filepath, self.data.is_dirty = "", False
        self.assertTrue(self.module.open_file(str(self.target)).ok)
        self.assertEqual(self.events, ["open"])

    def test_save_cancel_or_error_never_opens_target(self):
        for outcome in ({"CANCELLED"}, RuntimeError("cannot write")):
            with self.subTest(outcome=outcome):
                self.save.side_effect = outcome if isinstance(outcome, Exception) else lambda: outcome
                with self.assertRaisesRegex(tools.ToolError, "save"):
                    self.module.open_file(str(self.target))
                self.open.assert_not_called()

    def test_untitled_scene_needs_manual_save_first(self):
        self.data.filepath = ""
        with self.assertRaisesRegex(tools.ToolError, "Save As"):
            self.module.open_file(str(self.target))
        self.open.assert_not_called()
        self.save.assert_not_called()

    def test_invalid_target_or_same_file_never_saves(self):
        for target in (self.current, Path(self.work) / "missing.blend", Path(self.work) / "shape.obj"):
            with self.subTest(target=target):
                with self.assertRaises(tools.ToolError):
                    self.module.open_file(str(target))
                self.assertEqual(self.events, [])

    def test_open_file_is_discoverable_and_always_needs_consent(self):
        from loopcut import codex_app, workspace
        self.assertIn("open_file", [s["function"]["name"] for s in tools.SCHEMAS])
        self.assertIn("open_file", workspace.LIVE_TOOLS)
        self.assertIn("open_file", codex_app.DYNAMIC)
        approval = agent._approval("open_file", '{"path":"other.blend"}', ())
        self.assertTrue(agent._asks(approval, "open_file", {"auto_run": True}, types.SimpleNamespace(auto_run=True)))
        with patch.object(files, "blend_base", return_value=(self.current.parent, "")):
            summary, detail = agent._describe(types.SimpleNamespace(name="open_file", arguments='{"path":"other.blend"}'))
        self.assertIn("other.blend", summary)
        self.assertIn("save", (summary + detail).lower())

    def test_open_file_card_has_no_always_allow(self):
        card = state.item_tool("open_file", "Open other.blend", "")
        state.awaiting(card, always_ask=True)
        f = layout.Frame(800, 600, 1, lambda font, size, text: len(text) * size * 0.6)
        layout._item_tool(f, 0, card, 0, 0, 800)
        self.assertNotIn("approve_always", [h["action"][0] for h in f.hits])

    def test_codex_dynamic_call_also_waits_for_consent_with_auto_run(self):
        from loopcut import codex_app, config
        driver = object.__new__(codex_app.CodexTurn)
        driver.session = state.new_session()
        driver.session["auto_run"] = True
        driver.turn = agent.Turn()
        driver.cfg = config.Config("", "", "fixture", "", True, False, 3, 40000)
        driver.run_tool = Mock()
        card = state.item_tool("open_file", "Save and open a file", "")
        with patch.object(agent, "_redraw"), patch.object(agent, "_wait_for_approval", return_value=False) as wait:
            result = driver.run_dynamic(card, "open_file", json.dumps({"path": str(self.target)}))
        wait.assert_called_once_with(driver.turn)
        driver.run_tool.assert_not_called()
        self.assertTrue(card["always_ask"])
        self.assertFalse(result[1])
        self.assertEqual(card["status"], "rejected")

    def test_guard_points_at_open_file(self):
        self.assertIn("open_file", live_file.refusal("wm.open_mainfile"))

    def test_guard_keeps_background_read_option(self):
        self.assertIn("run_blender", live_file.refusal("wm.open_mainfile"))
        self.assertIn("libraries.load", live_file.refusal("wm.open_mainfile"))


if __name__ == "__main__":
    unittest.main()
