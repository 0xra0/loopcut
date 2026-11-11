"""Clipboard text comes from the transcript's displayed content."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import state
from loopcut.ui import layout


def measure(font, size, text):
    return len(text) * size * 0.6


def build(items, width=420, height=1200, **session_values):
    session = state.new_session()
    session.update(items=items, **session_values)
    return layout.build(session, width, height, 1.0, measure)


class TranscriptCopyTest(unittest.TestCase):
    def test_message_copy_uses_displayed_markdown_and_code(self):
        display = build([{"kind": "user", "text": "Keep this\nline."},
                         {"kind": "assistant", "text": "## Ready\n**Plain** `text`\n\n```py\na = 1\nprint(a)\n```"}])
        self.assertEqual(layout.copy_text(display, 0), "Keep this\nline.")
        self.assertEqual(layout.copy_text(display, 1), "Ready\nPlain text\n\na = 1\nprint(a)")
        actions = [h["action"] for h in display["hits"]]
        self.assertIn(("copy_message", 0), actions)
        self.assertIn(("copy_message", 1), actions)
        self.assertIn(("copy_menu", None), actions)

    def test_conversation_excludes_payloads_and_undrawn_fields(self):
        tool = state.item_tool("run_python", "Make a sphere", "HIDDEN_ARGUMENT")
        tool.update(status="done", output="HIDDEN_PAYLOAD", reasoning="HIDDEN_REASONING",
                    guidance="HIDDEN_GUIDANCE", system="HIDDEN_SYSTEM", expanded=True)
        display = build([{"kind": "user", "text": "A sphere, please.", "hidden": "HIDDEN_USER"},
                         tool, {"kind": "assistant", "text": "**Ready.**", "reasoning": "HIDDEN_REPLY"}])
        self.assertEqual(layout.copy_text(display), "You: A sphere, please.\n\nMake a sphere\n\nLoopcut: Ready.")

    def test_copy_includes_offscreen_turns_and_uses_drawn_tool_title(self):
        tool = state.item_tool("run_python", "A long descriptive title " * 40, "")
        tool["status"] = "done"
        display = build([{"kind": "user", "text": "First message"}, tool,
                         {"kind": "assistant", "text": "Last message\n" * 30}], height=500)
        title = display["transcript"][1]["text"]
        self.assertTrue(title.endswith("…"))
        self.assertLess(len(title), len(tool["summary"]))
        self.assertTrue(layout.copy_text(display).startswith("You: First message\n\n" + title))

    def test_notices_and_errors_copy_only_their_displayed_text(self):
        notice = state.item_notice("Scene saved.")
        notice["guidance"] = "HIDDEN_GUIDANCE"
        display = build([notice, state.item_error("Connection interrupted.")])
        self.assertEqual(layout.copy_text(display), "Scene saved.\n\nConnection interrupted.")

    def test_render_choice_copies_its_drawn_title(self):
        from loopcut.ui import render_choice
        tool = state.item_tool("render", "HIDDEN_SUMMARY", "")
        tool.update(status="awaiting", render_choice={"count": 3, "frames": [1, 3, 1],
                                                      "width": 640, "height": 480, "fps": 24,
                                                      "engine": "BLENDER_EEVEE"})
        with patch.object(render_choice.render_offer, "options", return_value=([], "")):
            display = build([tool])
        self.assertEqual(layout.copy_text(display), "Render 3 frames")


class TranscriptSelectionTest(unittest.TestCase):
    def lines(self, display, item=0):
        return [p for p in display["prims"] if p.get("src", (-1,))[0] == item]

    def test_point_maps_to_item_and_source_offset(self):
        text = "First word and a second line with more words."
        display = build([{"kind": "user", "text": text}], width=240)
        lines = self.lines(display)
        self.assertGreater(len(lines), 1)
        for line in lines:
            index = layout.transcript_index(display, line["x"], line["y"] + line["h"] / 2, measure)
            self.assertEqual(index, tuple(line["src"][:2]))
            self.assertEqual(text[line["src"][1]:line["src"][1] + len(line["text"])], line["text"])
        line = lines[0]
        self.assertEqual(layout.transcript_index(display, line["x"] + measure("ui", line["size"], "Fir"),
                                                 line["y"] + 1, measure), (0, 3))
        self.assertIsNone(layout.transcript_index(display, 1, 1, measure))

    def test_scaled_proportional_glyph_midpoints(self):
        def proportional(font, size, text):
            return size * sum(0.8 if char == "W" else 0.2 for char in text)

        session = state.new_session()
        session["items"] = [{"kind": "user", "text": "Wi"}]
        display = layout.build(session, 630, 900, 1.5, proportional)
        line = self.lines(display)[0]
        for dx, offset in ((0.39, 0), (0.41, 1), (0.89, 1), (0.91, 2)):
            with self.subTest(dx=dx):
                self.assertEqual(layout.transcript_index(display, line["x"] + dx * line["size"],
                                                         line["y"] + 1, proportional), (0, offset))

    def test_copy_menu_remains_available_in_a_compact_header(self):
        display = build([{"kind": "user", "text": "Text"}], width=240)
        title = next(h for h in display["hits"] if h["id"] == "header.title")
        self.assertEqual(title["action"], ("copy_menu", None))
        self.assertFalse(any(h["id"] == "header.copy" for h in display["hits"]))

    def test_soft_wraps_do_not_insert_spaces_or_newlines(self):
        for text in ("Words keep their original spaces " * 8,
                     "\u3042\u3044\u3046\u3048\u304a\u304b\u304d\u304f\u3051\u3053" * 12):
            with self.subTest(text=text):
                display = build([{"kind": "user", "text": text}], width=240)
                selected = layout.selected_transcript(display, {"transcript_anchor": (0, 0),
                                                                 "transcript_cursor": (0, len(text))})
                self.assertEqual(selected.encode("utf-8"), text.encode("utf-8"))
                self.assertGreater(len(self.lines(display)), 1)

    def test_code_and_real_newlines_survive_cross_message_reverse_selection(self):
        display = build([{"kind": "user", "text": "Start\n\nagain"},
                         {"kind": "assistant", "text": "```py\nx = 1\n\nprint(x)\n```"}])
        session = {"transcript_anchor": (1, 16), "transcript_cursor": (0, 0)}
        self.assertEqual(layout.selected_transcript(display, session), "Start\n\nagain\n\nx = 1\n\nprint(x)")
        self.assertTrue(any(p["font"] == "mono" for p in self.lines(display, 1)))

    def test_selection_highlights_and_anchors_survive_streaming_and_scroll(self):
        text = "A growing reply with many words. " * 35
        items = [{"kind": "assistant", "text": text, "streaming": True}]
        selection = {"transcript_anchor": (0, 4), "transcript_cursor": (0, 20)}
        first = build(items, **selection)
        self.assertTrue(any(p["id"].startswith("chat.selection") for p in first["prims"]))
        items[0]["text"] += "More text."
        scrolled = build(items, height=500, scroll=200, **selection)
        self.assertEqual(layout.selected_transcript(scrolled, selection), text[4:20])
        self.assertEqual(selection["transcript_anchor"], (0, 4))

    def test_drag_clamps_to_visible_text_and_never_maps_header_or_input(self):
        display = build([{"kind": "user", "text": "Select this\nnext line"}])
        lines = self.lines(display)
        self.assertEqual(layout.transcript_index(display, -50, -50, measure, clamp=True), (0, 0))
        self.assertEqual(layout.transcript_index(display, 999, 9999, measure, clamp=True), (0, 21))
        self.assertIsNone(layout.transcript_index(display, 100, display["height"] - 20, measure))

    def test_absent_item_invalidates_selection(self):
        display = build([{"kind": "user", "text": "Text"}])
        self.assertEqual(layout.selected_transcript(display, {"transcript_anchor": (0, 0),
                                                              "transcript_cursor": (8, 5)}), "")


if __name__ == "__main__":
    unittest.main()
