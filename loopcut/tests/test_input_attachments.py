"""Clipboard, model drops and save-before-open rules with synthetic Blender doubles."""

import ast
import base64
import contextlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

ADDONS = Path(__file__).resolve().parents[2] / "scripts/addons_core"
sys.path.insert(0, str(ADDONS))
fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import attachments, files, image_budget, state, tools  # noqa: E402
from loopcut.ui import layout, textedit  # noqa: E402


class ClipboardTest(unittest.TestCase):
    def setUp(self):
        self.session = state.new_session()
        self.image = types.SimpleNamespace(size=(16, 8), save=Mock())
        self.previous = object()
        self.space = types.SimpleNamespace(image=self.previous)
        self.area = types.SimpleNamespace(type="CONSOLE", ui_type="CONSOLE",
                                          spaces=types.SimpleNamespace(active=self.space),
                                          regions=[types.SimpleNamespace(type="WINDOW")])
        self.context = types.SimpleNamespace(area=self.area, temp_override=lambda **kw: contextlib.nullcontext())

        def paste():
            self.space.image = self.image
            return {"FINISHED"}

        self.paste = Mock(side_effect=paste)
        self.paste.poll.return_value = True
        self.images = types.SimpleNamespace(remove=Mock())
        self.bpy = types.SimpleNamespace(context=self.context, data=types.SimpleNamespace(images=self.images),
                                        ops=types.SimpleNamespace(image=types.SimpleNamespace(clipboard_paste=self.paste)))

    def test_clipboard_image_is_saved_attached_and_removed(self):
        with patch.object(attachments, "bpy", self.bpy), patch.object(attachments, "add", return_value=[]) as add:
            self.assertEqual(attachments.paste_image(self.session, self.context), [])
        self.paste.assert_called_once_with()
        self.image.save.assert_called_once_with()
        add.assert_called_once()
        path = Path(add.call_args.args[1][0])
        self.assertEqual(path.suffix, ".png")
        self.assertFalse(path.exists())
        self.assertIs(self.space.image, self.previous)
        self.assertEqual(self.area.type, "CONSOLE")
        self.images.remove.assert_called_once_with(self.image)

    def test_paste_attachment_has_a_readable_name(self):
        def add(session, paths):
            session["attachments"].append({"name": Path(paths[0]).name})
            return []
        with patch.object(attachments, "bpy", self.bpy), patch.object(attachments, "add", side_effect=add):
            self.assertEqual(attachments.paste_image(self.session, self.context), [])
        self.assertEqual(self.session["attachments"][0]["name"], "Clipboard image.png")

    def test_no_clipboard_image_is_visible_and_restores_editor(self):
        self.paste.poll.return_value = False
        with patch.object(attachments, "bpy", self.bpy):
            problems = attachments.paste_image(self.session, self.context)
        self.assertIn("clipboard", problems[0].lower())
        self.paste.assert_not_called()
        self.images.remove.assert_not_called()
        self.assertEqual(self.area.type, "CONSOLE")
        self.assertIs(self.space.image, self.previous)

    def test_save_failure_restores_editor_and_removes_image(self):
        self.image.save.side_effect = RuntimeError("cannot write")
        with patch.object(attachments, "bpy", self.bpy):
            self.assertIn("cannot write", attachments.paste_image(self.session, self.context)[0])
        self.assertIs(self.space.image, self.previous)
        self.images.remove.assert_called_once_with(self.image)
        self.assertEqual(self.area.type, "CONSOLE")


class PanelInputTest(unittest.TestCase):
    def host_function(self, name, namespace):
        source = ADDONS / "loopcut/ui/host.py"
        tree = ast.parse(source.read_text())
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        namespace.update(__package__="loopcut.ui", Path=Path, sys=sys, state=state, textedit=textedit)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
        return namespace[name]

    def test_text_paste_stays_in_place_and_image_paste_leaves_modal(self):
        session = state.new_session()
        session["input"], session["cursor"], session["first_prompt_typed"] = "ab", 1, True
        save, redraw = Mock(), Mock()
        later = Mock()
        namespace = {"conversations": types.SimpleNamespace(save=save), "tag_redraw_all": redraw,
                     "_update_mentions": Mock(), "_paste_image_later": later}
        key = self.host_function("_key", namespace)
        event = types.SimpleNamespace(type="V", shift=False, oskey=False, ctrl=True, alt=False, unicode="")
        context = types.SimpleNamespace(window_manager=types.SimpleNamespace(clipboard="X"), window=object())
        operator = types.SimpleNamespace(area_pointer=123)
        with patch.object(state, "ui", {}), patch.object(attachments, "paste_image", return_value=["No clipboard image."]) as paste:
            self.assertTrue(key(operator, context, session, event))
            self.assertEqual(session["input"], "aXb")
            paste.assert_not_called()
            context.window_manager.clipboard = ""
            self.assertFalse(key(operator, context, session, event), "the modal must finish before changing its editor")
            paste.assert_not_called()
            later.assert_called_once_with(context.window, operator.area_pointer, session)
        self.assertEqual(session["input"], "aXb")
        save.assert_not_called()

    def test_image_paste_modal_finishes_and_consumes_the_key(self):
        session = state.new_session()
        session["focused"] = True
        area = types.SimpleNamespace(tag_redraw=Mock())
        later = Mock()
        namespace = {"_find_area": lambda *args: area, "_paste_image_later": later,
                     "_update_mentions": Mock()}
        key = self.host_function("_key", namespace)
        finish = self.host_function("_finish", namespace)
        modal = self.host_function("modal", namespace)
        operator = types.SimpleNamespace(area_pointer=123, _transcript=False)  # As invoke() leaves it.
        operator._key = lambda *args: key(operator, *args)
        operator._finish = lambda *args: finish(operator, *args)
        event = types.SimpleNamespace(type="V", value="PRESS", shift=False, oskey=False,
                                      ctrl=True, alt=False, unicode="")
        context = types.SimpleNamespace(window=object(), window_manager=types.SimpleNamespace(clipboard=""))
        with patch.object(state, "session", return_value=session), patch.object(state, "ui", {}):
            self.assertEqual(modal(operator, context, event), {"FINISHED"})
        later.assert_called_once_with(context.window, 123, session)
        self.assertFalse(session["focused"])

    def test_paste_timer_reports_missing_context_and_other_failures(self):
        for failure in ("closed", "region", "screen", "paste", "refocus", "cancelled"):
            with self.subTest(failure=failure):
                session = state.new_session()
                callbacks = []
                area = types.SimpleNamespace(as_pointer=lambda: 123)
                window = Mock()
                window.screen = types.SimpleNamespace(areas=[] if failure == "screen" else [area])
                if failure == "closed":
                    type(window).screen = PropertyMock(side_effect=ReferenceError("window closed"))
                region = Mock(side_effect=StopIteration() if failure == "region" else None)
                interact = Mock(side_effect=RuntimeError("cannot focus") if failure == "refocus" else None,
                                return_value={"CANCELLED"} if failure == "cancelled" else {"RUNNING_MODAL"})
                bpy = types.SimpleNamespace(context=types.SimpleNamespace(temp_override=lambda **kw: contextlib.nullcontext()),
                    app=types.SimpleNamespace(timers=types.SimpleNamespace(register=lambda fn, **kw: callbacks.append(fn))),
                    ops=types.SimpleNamespace(loopcut=types.SimpleNamespace(interact=interact)))
                save, redraw = Mock(), Mock()
                later = self.host_function("_paste_image_later", {"bpy": bpy, "is_host": lambda a: True,
                    "_window_region": region, "conversations": types.SimpleNamespace(save=save),
                    "tag_redraw_all": redraw})
                def paste(*args):
                    if failure == "paste":
                        raise RuntimeError("clipboard unavailable")
                    session["attachments"].append({"name": "Clipboard image.png"})
                    return []
                with patch.object(state, "session", return_value=session), patch.object(attachments, "paste_image", side_effect=paste):
                    later(window, 123, session)
                    self.assertIsNone(callbacks.pop(0)())
                self.assertTrue(any(i["kind"] == "error" and "paste" in i["text"].lower() for i in session["items"]))
                save.assert_called()
                redraw.assert_called()
                if failure not in {"refocus", "cancelled"}:
                    self.assertFalse(session["attachments"])

    def test_paste_scheduling_failure_is_visible_and_saved(self):
        session = state.new_session()
        bpy = types.SimpleNamespace(app=types.SimpleNamespace(timers=types.SimpleNamespace(
            register=Mock(side_effect=RuntimeError("timers unavailable")))))
        save, redraw = Mock(), Mock()
        later = self.host_function("_paste_image_later", {"bpy": bpy,
            "conversations": types.SimpleNamespace(save=save), "tag_redraw_all": redraw})
        with patch.object(state, "session", return_value=session):
            later(object(), 123, session)
        self.assertIn("not pasted", session["items"][-1]["text"])
        save.assert_called_once_with(session)
        redraw.assert_called_once()

    def test_two_pastes_run_outside_modal_and_reenter_input(self):
        session = state.new_session()
        area = types.SimpleNamespace(as_pointer=lambda: 123)
        window = types.SimpleNamespace(screen=types.SimpleNamespace(areas=[area]))
        callbacks = []
        active = [False]
        context = types.SimpleNamespace(area=area, temp_override=lambda **kw: contextlib.nullcontext())
        interact = Mock(side_effect=lambda *a, **kw: active.__setitem__(0, True) or {"RUNNING_MODAL"})
        bpy = types.SimpleNamespace(context=context, app=types.SimpleNamespace(timers=types.SimpleNamespace(
            register=lambda fn, **kw: callbacks.append(fn))), ops=types.SimpleNamespace(loopcut=types.SimpleNamespace(
                interact=interact)))
        save, redraw = Mock(), Mock()
        namespace = {"bpy": bpy, "is_host": lambda a: a is area, "_window_region": lambda a: object(),
                     "conversations": types.SimpleNamespace(save=save), "tag_redraw_all": redraw}
        later = self.host_function("_paste_image_later", namespace)
        def paste(*args):
            self.assertFalse(active[0], "paste changed the editor inside the modal handler")
            session["attachments"].append({"name": "Clipboard image.png"})
            return []
        with patch.object(state, "session", return_value=session), patch.object(attachments, "paste_image", side_effect=paste):
            for _ in range(2):
                active[0] = True
                later(window, 123, session)
                self.assertEqual(interact.call_count, len(session["attachments"]))
                active[0] = False  # modal._finish has now returned to Blender's event loop.
                callbacks.pop(0)()
                self.assertTrue(active[0])
                textedit.insert(session, "x")
        self.assertEqual(session["input"], "xx")
        self.assertEqual(len(session["attachments"]), 2)
        self.assertEqual(save.call_count, 2)
        active[0] = False
        with patch.object(state, "session", return_value=session), \
                patch.object(attachments, "paste_image", return_value=["No clipboard image."]):
            later(window, 123, session)
            callbacks.pop(0)()
        self.assertEqual(session["items"][-1], state.item_error("No clipboard image."))
        self.assertTrue(active[0], "a failed paste should restore input too")

    def test_paste_timer_does_not_follow_a_conversation_switch(self):
        callbacks = []
        area = types.SimpleNamespace(as_pointer=lambda: 123)
        window = types.SimpleNamespace(screen=types.SimpleNamespace(areas=[area]))
        bpy = types.SimpleNamespace(app=types.SimpleNamespace(timers=types.SimpleNamespace(
            register=lambda fn, **kw: callbacks.append(fn))))
        save, redraw = Mock(), Mock()
        session = state.new_session()
        current = state.new_session()
        later = self.host_function("_paste_image_later", {"bpy": bpy,
            "conversations": types.SimpleNamespace(save=save), "tag_redraw_all": redraw})
        with patch.object(state, "session", return_value=current), \
                patch.object(attachments, "paste_image") as paste:
            later(window, 123, session)
            callbacks.pop(0)()
        paste.assert_not_called()
        self.assertEqual(session["items"], [])
        self.assertIn("not pasted", current["items"][-1]["text"])
        self.assertIn("conversation changed", current["items"][-1]["text"])
        save.assert_called_once_with(current)
        redraw.assert_called_once()

    def test_paste_failure_after_switch_is_saved_only_in_current_session(self):
        for failure in ("paste", "schedule"):
            with self.subTest(failure=failure):
                old, current = state.new_session(), state.new_session()
                active = [old]
                callbacks = []
                area = types.SimpleNamespace(as_pointer=lambda: 123)
                window = types.SimpleNamespace(screen=types.SimpleNamespace(areas=[area]))
                def fail(*args, **kwargs):
                    active[0] = current
                    raise RuntimeError("clipboard unavailable")
                bpy = types.SimpleNamespace(context=types.SimpleNamespace(temp_override=lambda **kw: contextlib.nullcontext()),
                    app=types.SimpleNamespace(timers=types.SimpleNamespace(
                        register=fail if failure == "schedule" else lambda fn, **kw: callbacks.append(fn))))
                save, redraw = Mock(), Mock()
                later = self.host_function("_paste_image_later", {"bpy": bpy, "is_host": lambda a: True,
                    "_window_region": lambda a: object(), "conversations": types.SimpleNamespace(save=save),
                    "tag_redraw_all": redraw})
                with patch.object(state, "session", side_effect=lambda: active[0]), \
                        patch.object(attachments, "paste_image", side_effect=fail):
                    later(window, 123, old)
                    if callbacks:
                        callbacks.pop(0)()
                self.assertEqual(old["items"], [])
                self.assertEqual(old["attachments"], [])
                self.assertIn("clipboard unavailable", current["items"][-1]["text"])
                save.assert_called_once_with(current)
                redraw.assert_called_once()

    def test_drop_dispatch_sends_models_to_import_and_images_to_attachments(self):
        source = ast.parse((ADDONS / "loopcut/ui/host.py").read_text())
        cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "LOOPCUT_OT_attach_images")
        execute = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "execute")
        namespace = {"__package__": "loopcut.ui", "Path": Path, "state": state,
                     "conversations": types.SimpleNamespace(save=Mock()), "tag_redraw_all": Mock()}
        exec(compile(ast.Module(body=[execute], type_ignores=[]), "<drop>", "exec"), namespace)
        session = state.new_session()
        session["focused"] = True
        operator = types.SimpleNamespace(directory="/tmp", files=[types.SimpleNamespace(name=name)
                                         for name in ("shape.glb", "reference.png")])
        with patch.object(state, "session", return_value=session), \
                patch.object(attachments, "import_models", return_value=[]) as models, \
                patch.object(attachments, "add", return_value=[]) as images:
            self.assertEqual(namespace["execute"](operator, None), {"FINISHED"})
        models.assert_called_once_with(session, [str(Path("/tmp") / "shape.glb")])
        images.assert_called_once_with(session, [str(Path("/tmp") / "reference.png")])


class DropTest(unittest.TestCase):
    def test_model_extensions_are_accepted_by_file_handler_and_browser(self):
        for extension in (".glb", ".gltf", ".fbx", ".obj"):
            self.assertIn(extension, attachments.ACCEPTED)
        tree = ast.parse((ADDONS / "loopcut/ui/host.py").read_text())
        handler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LOOPCUT_FH_images")
        value = next(n.value for n in handler.body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "bl_file_extensions" for t in n.targets))
        accepted = eval(compile(ast.Expression(value), "<handler>", "eval"), {"attachments": attachments})
        for extension in (".glb", ".gltf", ".fbx", ".obj"):
            self.assertIn(extension, accepted.split(";"))

    def test_models_import_into_scene_with_visible_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / ("shape" + suffix) for suffix in (".glb", ".gltf", ".fbx", ".obj")]
            for path in paths:
                path.write_bytes(b"synthetic model")
            gltf, fbx, obj = (Mock(return_value={"FINISHED"}) for _ in range(3))
            undo = Mock(return_value={"FINISHED"})
            bpy = types.SimpleNamespace(ops=types.SimpleNamespace(ed=types.SimpleNamespace(undo_push=undo),
                import_scene=types.SimpleNamespace(gltf=gltf, fbx=fbx), wm=types.SimpleNamespace(obj_import=obj)))
            session = state.new_session()
            with patch.object(attachments, "bpy", bpy):
                self.assertEqual(attachments.import_models(session, [str(p) for p in paths]), [])
            self.assertEqual(gltf.call_count, 2)
            fbx.assert_called_once_with(filepath=str(paths[2]))
            obj.assert_called_once_with(filepath=str(paths[3]))
            self.assertEqual(len(session["items"]), 4)
            self.assertTrue(all("Imported" in item["text"] and "scene" in item["text"] for item in session["items"]))
            self.assertEqual(session["attachments"], [])
            undo.assert_called_once_with(message="Import model")
            self.assertTrue(all(p.name in str(session["between_turns"]) for p in paths))

    def test_undo_failure_keeps_drop_errors_notices_and_saved_state(self):
        for undo_result in (RuntimeError("undo unavailable"), {"CANCELLED"}):
            with self.subTest(undo=undo_result), tempfile.TemporaryDirectory() as tmp:
                paths = [Path(tmp) / name for name in ("shape.glb", "invalid.fbx")]
                for path in paths:
                    path.write_bytes(b"synthetic model")
                undo = Mock(side_effect=undo_result) if isinstance(undo_result, Exception) else Mock(return_value=undo_result)
                bpy = types.SimpleNamespace(ops=types.SimpleNamespace(ed=types.SimpleNamespace(undo_push=undo),
                    import_scene=types.SimpleNamespace(gltf=Mock(return_value={"FINISHED"}),
                        fbx=Mock(side_effect=RuntimeError("invalid data")))))
                session = state.new_session()
                save, redraw = Mock(), Mock()
                tree = ast.parse((ADDONS / "loopcut/ui/host.py").read_text())
                cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LOOPCUT_OT_attach_images")
                node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "execute")
                namespace = {"__package__": "loopcut.ui", "Path": Path, "state": state,
                             "conversations": types.SimpleNamespace(save=save), "tag_redraw_all": redraw}
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<drop>", "exec"), namespace)
                execute = namespace["execute"]
                operator = types.SimpleNamespace(directory=tmp, files=[types.SimpleNamespace(name=p.name) for p in paths])
                with patch.object(attachments, "bpy", bpy), patch.object(state, "session", return_value=session):
                    self.assertEqual(execute(operator, None), {"CANCELLED"})
                self.assertTrue(any("Imported shape.glb" in i.get("text", "") for i in session["items"]))
                self.assertTrue(any("invalid data" in i.get("text", "") for i in session["items"]))
                self.assertTrue(any("undo" in i.get("text", "").lower() for i in session["items"]))
                save.assert_called_once_with(session)
                redraw.assert_called_once()

    def test_partial_import_failure_still_attempts_an_undo_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.glb"
            path.write_bytes(b"synthetic model")
            undo = Mock(return_value={"FINISHED"})
            bpy = types.SimpleNamespace(ops=types.SimpleNamespace(ed=types.SimpleNamespace(undo_push=undo),
                import_scene=types.SimpleNamespace(gltf=Mock(side_effect=RuntimeError("partial import")))))
            with patch.object(attachments, "bpy", bpy):
                problems = attachments.import_models(state.new_session(), [str(path)])
            self.assertIn("partial import", problems[0])
            undo.assert_called_once_with(message="Import model")

    def test_hidden_notice_names_file_and_skips_loopcut_internals(self):
        from loopcut import mainthread, workspace
        root = Path(tempfile.gettempdir()).resolve() / "project"
        ws = root / "workspace"
        data = root / "data"
        for path, notice in ((root / ".private" / "reference.png", True),
                             (root / ".loopcut" / "state.json", False),
                             (ws / ".loopcut" / "state.json", False),
                             (ws / ".outputs" / "render.png", False),
                             (data / ".keys" / "key.json", False)):
            session = state.new_session()
            with self.subTest(path=path), patch.object(state, "session", return_value=session), \
                    patch.object(mainthread, "request_redraw"), patch.object(workspace, "_ROOTS", {ws}), \
                    patch.object(files, "_forbidden_roots", return_value=(data,)):
                with self.assertRaises(tools.ToolError):
                    files.check(path)
            self.assertEqual(bool(session["items"]), notice)
            if notice:
                self.assertIn(str(path), session["items"][-1]["text"])

    def test_internal_paths_are_off_limits_without_a_copy_request(self):
        from loopcut import mainthread, workspace
        root = Path(tempfile.gettempdir()).resolve() / "project"
        ws, data = root / "workspace", root / "data"
        for path in (root / ".loopcut" / "state.json", ws / ".outputs" / "render.png",
                     data / ".keys" / "key.json", data / "state.json"):
            session = state.new_session()
            with self.subTest(path=path), patch.object(state, "session", return_value=session), \
                    patch.object(mainthread, "request_redraw") as redraw, \
                    patch.object(workspace, "_ROOTS", {ws}), \
                    patch.object(files, "_forbidden_roots", return_value=(data,)):
                with self.assertRaises(tools.ToolError) as caught:
                    files.check(path)
            message = str(caught.exception)
            self.assertIn("off limits", message)
            self.assertNotIn("copy", message.lower())
            self.assertIn(str(path), message)
            self.assertEqual(session["items"], [])
            redraw.assert_not_called()

    def test_large_model_is_refused_before_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shape.fbx"
            with path.open("wb") as stream:
                stream.truncate(attachments.MAX_FILE_BYTES + 1)
            importer = Mock(return_value={"FINISHED"})
            bpy = types.SimpleNamespace(ops=types.SimpleNamespace(import_scene=types.SimpleNamespace(fbx=importer)))
            with patch.object(attachments, "bpy", bpy):
                problems = attachments.import_models(state.new_session(), [str(path)])
            self.assertIn("MB", problems[0])
            importer.assert_not_called()

    def test_missing_and_invalid_model_errors_do_not_blame_disabled_importer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shape.glb"
            importer = Mock(side_effect=RuntimeError("invalid data"))
            bpy = types.SimpleNamespace(ops=types.SimpleNamespace(import_scene=types.SimpleNamespace(gltf=importer)))
            with patch.object(attachments, "bpy", bpy):
                missing = attachments.import_models(state.new_session(), [str(path)])
                path.write_bytes(b"invalid")
                invalid = attachments.import_models(state.new_session(), [str(path)])
            self.assertIn("missing", missing[0])
            self.assertIn("invalid data", invalid[0])
            self.assertNotIn("enabled", " ".join(missing + invalid))

    def test_unavailable_importer_has_specific_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shape.glb"
            path.write_bytes(b"synthetic model")
            bpy = types.SimpleNamespace(ops=types.SimpleNamespace(import_scene=types.SimpleNamespace()))
            with patch.object(attachments, "bpy", bpy):
                problems = attachments.import_models(state.new_session(), [str(path)])
            self.assertIn("unavailable", problems[0])
            self.assertIn("File > Import", problems[0])

    def test_hidden_copy_is_refused_without_touching_either_path(self):
        session = state.new_session()
        with tempfile.TemporaryDirectory() as tmp, patch.object(state, "session", return_value=session):
            source = Path(tmp) / ".private" / "reference.png"
            source.parent.mkdir()
            source.write_bytes(b"synthetic image")
            target = Path(tmp) / "reference.png"
            with patch.object(files.shutil, "copy2") as copy:
                with self.assertRaisesRegex(tools.ToolError, "do not copy it yourself"):
                    files.move_file(str(source), str(target), copy=True)
            copy.assert_not_called()
            self.assertTrue(source.exists())
            self.assertFalse(target.exists())
            self.assertTrue(session["items"])

    def test_hidden_guard_tells_the_user_and_model_to_wait_for_a_user_copy(self):
        from loopcut import mainthread, workspace
        for in_workspace in (False,):
            session = state.new_session()
            root = Path(tempfile.gettempdir()).resolve() / "project"
            path = root / ".private" / "reference.png"
            with self.subTest(in_workspace=in_workspace), patch.object(state, "session", return_value=session), \
                    patch.object(mainthread, "request_redraw"), \
                    patch.object(workspace, "inside", return_value=root if in_workspace else None):
                with self.assertRaises(tools.ToolError) as caught:
                    files.check(path)
                self.assertIn("Ask the user to copy", str(caught.exception))
                self.assertIn("do not copy it yourself", str(caught.exception))
                self.assertTrue(any("copy it into the project folder" in i.get("text", "") for i in session["items"]))

    def test_cancelled_import_reports_failure_without_success_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shape.glb"
            path.touch()
            bpy = types.SimpleNamespace(ops=types.SimpleNamespace(import_scene=types.SimpleNamespace(
                gltf=Mock(return_value={"CANCELLED"}))))
            session = state.new_session()
            with patch.object(attachments, "bpy", bpy):
                self.assertIn("could not import", attachments.import_models(session, [str(path)])[0].lower())
            self.assertEqual(session["items"], [])

    def test_hidden_refusal_offers_a_project_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(tools.ToolError, "copy it into the project folder"):
                files.check(Path(tmp) / ".private" / "reference.png")


class RetryNoticeTest(unittest.TestCase):
    def test_dropped_retry_images_are_reported_without_changing_stored_messages(self):
        url = "data:image/png;base64," + base64.b64encode(b"image").decode()
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]
        notices = []
        with image_budget.notifying(notices.append):
            sized = image_budget.Sized(messages)
            self.assertTrue(sized.tighten())
            self.assertEqual(image_budget._inline_urls(sized.messages()), {})
            self.assertFalse(sized.tighten())
        self.assertEqual(len(notices), 1)
        self.assertIn("without", notices[0])
        self.assertIn("image", notices[0])
        self.assertIn("attach", notices[0].lower())
        self.assertEqual(messages[0]["content"][0]["image_url"]["url"], url)




if __name__ == "__main__":
    unittest.main()
