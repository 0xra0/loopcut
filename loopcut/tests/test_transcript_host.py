"""Exercise the panel's actual modal handlers with synthetic events and a fake window."""

import ast
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import state
from loopcut.ui import layout, textedit

N = types.SimpleNamespace


def measure(font, size, text):
    return len(text) * size * 0.6


class TranscriptHostTest(unittest.TestCase):
    def setUp(self):
        self.session = state.new_session()
        self.session["focused"] = True
        self.session["items"] = [{"kind": "user", "text": "First words\nNext line"},
                                 {"kind": "assistant", "text": "A second reply."}]
        self.ui = {}
        self.area = N(as_pointer=lambda: 1, tag_redraw=Mock())
        self.context = N(area=self.area, window_manager=N(clipboard="unchanged", modal_handler_add=Mock()),
                         preferences=N(inputs=N(mouse_double_click_time=500, drag_threshold_mouse=4)))
        self.ns = {"__name__": "loopcut.ui.host", "__package__": "loopcut.ui",
                   "bpy": N(types=N(Operator=object), props=N(BoolProperty=lambda **kw: None),
                            context=self.context), "sys": sys, "time": time,
                   "state": N(session=lambda: self.session, ui=self.ui), "layout": layout,
                   "textedit": textedit, "draw": N(measure=measure), "_displays": {},
                   "_find_area": lambda *args: self.area, "_window_region": lambda area: None,
                   "_local": lambda region, e: (e.x, e.y, 0 <= e.x < 420 and 0 <= e.y < 800),
                   "_update_mentions": Mock(), "_picker_key": Mock(return_value=False),
                   "_shown_card": Mock(return_value=None), "agent": N(stop=Mock(), decide=Mock()),
                   "SCROLL_STEP": 60, "POPUP_ACTIONS": {"noop", "copy_menu"}}
        path = Path(layout.__file__).with_name("host.py")
        nodes = [n for n in ast.parse(path.read_text()).body if isinstance(n, (ast.ClassDef, ast.FunctionDef))
                 and n.name in {"LOOPCUT_OT_interact", "_do_action", "_close_popups", "scroll"}]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), self.ns)
        self.op = self.ns["LOOPCUT_OT_interact"]()
        self.op.keyboard = False
        self.op.area_pointer = 1
        self.op._selecting = self.op._transcript = False
        self.op._input_click = None
        self.redraw()

    def redraw(self):
        self.display = layout.build(self.session, 420, 800, 1, measure, ui=self.ui)
        self.ns["_displays"][1] = self.display

    def event(self, type="LEFTMOUSE", value="PRESS", **values):
        return N(**dict({"type": type, "value": value, "x": 40, "y": 100, "unicode": "",
                         "shift": False, "oskey": False, "ctrl": False, "alt": False}, **values))

    def point(self, item, offset):
        p = next(p for p in self.display["prims"] if "src" in p and p["src"][0] == item
                 and p["src"][1] <= offset <= p["src"][2])
        text = self.display["transcript"][item]["text"]
        return {"x": p["x"] + measure(p["font"], p["size"], text[p["src"][1]:offset]),
                "y": p["y"] + p["h"] / 2}

    def click(self, item, offset, **values):
        return self.op._click(self.session, self.area, self.event(**dict(self.point(item, offset), **values)))

    def test_drag_across_messages_and_copy_with_either_modifier(self):
        self.op.invoke(self.context, self.event(**self.point(0, 0)))
        self.op.modal(self.context, self.event(type="MOUSEMOVE", value="NOTHING", **self.point(1, 8)))
        self.op.modal(self.context, self.event(value="RELEASE", **self.point(1, 8)))
        for command in ("ctrl", "oskey"):
            self.op.modal(self.context, self.event(type="C", **{command: True}))
            self.assertEqual(self.context.window_manager.clipboard, "First words\nNext line\n\nA second")
        self.assertEqual(self.session["input"], "")

    def test_input_shortcuts_keep_the_input_selection(self):
        self.session["first_prompt_typed"] = True
        textedit.set_text(self.session, "Editable text")
        textedit.select_all(self.session)
        self.op._key(self.context, self.session, self.event(type="C", ctrl=True))
        self.assertEqual(self.context.window_manager.clipboard, "Editable text")
        self.op._key(self.context, self.session, self.event(type="V", ctrl=True))
        self.assertEqual(self.session["input"], "Editable text")

    def test_copy_actions_use_the_current_display(self):
        for action, expected in ((("copy_message", 1), "A second reply."),
                                 (("copy_conversation", None), "You: First words\nNext line\n\nLoopcut: A second reply.")):
            self.ns["_do_action"](self.session, action, display=self.display)
            self.assertEqual(self.context.window_manager.clipboard, expected)

    def test_drag_release_recovery_and_viewport_copy_pass_through(self):
        self.op.invoke(self.context, self.event(**self.point(0, 0)))
        self.op.modal(self.context, self.event(type="MOUSEMOVE", value="NOTHING", **self.point(0, 5)))
        self.op.modal(self.context, self.event(type="MOUSEMOVE", value="NOTHING", type_prev="LEFTMOUSE",
                                              value_prev="RELEASE", **self.point(1, 5)))
        self.assertFalse(self.op._selecting)
        self.assertEqual(self.session["transcript_cursor"], (0, 5))
        result = self.op.modal(self.context, self.event(type="C", ctrl=True, x=500))
        self.assertIn("PASS_THROUGH", result)
        self.assertEqual(self.context.window_manager.clipboard, "unchanged")

    def test_tool_title_click_keeps_its_card_action(self):
        tool = state.item_tool("run_python", "Make an object", "")
        tool["status"] = "done"
        self.session["items"] = [tool]
        self.redraw()
        self.click(0, 2)
        self.assertTrue(tool["expanded"])
        self.assertFalse(self.op._transcript)

    def test_wheel_scroll_keeps_source_anchors(self):
        self.op.invoke(self.context, self.event(**self.point(0, 0)))
        self.session["transcript_cursor"] = (1, 8)
        self.op.modal(self.context, self.event(type="WHEELUPMOUSE"))
        self.assertEqual(self.session["scroll"], 60)
        self.assertEqual(self.session["transcript_cursor"], (1, 8))

    def test_double_click_word_and_downgraded_modal_press(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                self.op._input_click = None
                self.op._selecting = False
                self.click(0, 7)
                self.op.modal(self.context, self.event(value="RELEASE"))
                self.click(0, 7, value="DOUBLE_CLICK" if explicit else "PRESS")
                self.assertEqual(layout.selected_transcript(self.display, self.session), "words")

    def test_shift_click_extends_across_messages(self):
        self.click(0, 6)
        self.op.modal(self.context, self.event(value="RELEASE"))
        self.click(1, 8, shift=True)
        self.assertEqual(layout.selected_transcript(self.display, self.session), "words\nNext line\n\nA second")

    def test_shift_click_can_start_modal_on_transcript(self):
        result = self.op.invoke(self.context, self.event(shift=True, **self.point(0, 2)))
        self.assertIn("RUNNING_MODAL", result)
        self.assertTrue(self.op._transcript)

    def test_escape_clears_selection_without_stopping_a_turn(self):
        self.op.invoke(self.context, self.event(**self.point(0, 0)))
        self.session.update(busy=True, transcript_cursor=(1, 8))
        self.op.modal(self.context, self.event(type="ESC"))
        self.assertNotIn("transcript_anchor", self.session)
        self.ns["agent"].stop.assert_not_called()

    def test_click_away_clears_and_input_click_restores_typing(self):
        self.click(0, 0)
        self.session["transcript_cursor"] = (1, 8)
        box = self.display["input"]
        self.op._click(self.session, self.area, self.event(x=box["x"] + 1, y=box["y"] + 1))
        self.assertNotIn("transcript_anchor", self.session)
        self.assertFalse(self.op._transcript)

    def test_typing_after_a_click_on_chat_text_goes_to_the_input(self):
        self.op.invoke(self.context, self.event(**self.point(0, 2)))
        self.op.modal(self.context, self.event(value="RELEASE", **self.point(0, 2)))
        self.assertTrue(self.op._transcript)
        for char in "あb":
            self.op.modal(self.context, self.event(type="A", unicode=char))
        self.assertEqual(self.session["input"], "あb")
        self.assertFalse(self.op._transcript)
        self.assertNotIn("transcript_anchor", self.session)

    def test_input_double_click_still_selects_a_word(self):
        textedit.set_text(self.session, "Input words")
        self.redraw()
        box = self.display["input"]
        self.op._click(self.session, self.area, self.event(value="DOUBLE_CLICK", x=box["x"] + 1,
                                                          y=box["y"] + 1))
        self.assertEqual(textedit.selected_text(self.session), "Input")

    def test_escape_closes_conversation_menu_without_stopping(self):
        self.ui["copy_menu"] = True
        self.session["busy"] = True
        self.op.modal(self.context, self.event(type="ESC"))
        self.assertFalse(self.ui["copy_menu"])
        self.ns["agent"].stop.assert_not_called()

    def test_right_click_away_clears_transcript_selection(self):
        self.click(0, 0)
        self.session["transcript_cursor"] = (0, 5)
        result = self.op.modal(self.context, self.event(type="RIGHTMOUSE", x=500))
        self.assertIn("PASS_THROUGH", result)
        self.assertNotIn("transcript_anchor", self.session)

    def test_viewport_copy_passes_through_with_input_focus_too(self):
        self.session["first_prompt_typed"] = True
        textedit.set_text(self.session, "Editable text")
        textedit.select_all(self.session)
        result = self.op.modal(self.context, self.event(type="C", ctrl=True, x=500))
        self.assertIn("PASS_THROUGH", result)
        self.assertEqual(self.context.window_manager.clipboard, "unchanged")


if __name__ == "__main__":
    unittest.main()
