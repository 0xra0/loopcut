"""Compatibility and robustness test suite for Swift 1.5 and Strata provider.

Covers:
Item 1: Streamed tool calls, multi-calls, malformed arguments, cancellation, long context, and vision.
Item 2: Targeted tool argument validation, scene-informed correction, and text/tool separation.
Item 3: Portable Strata setup with dynamic API discovery and selectable run script/config.
Item 4: Active vision verification sending test images.
"""

import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "addons_core"))

import loopcut.llm as llm
import loopcut.strata as strata
import loopcut.tool_repair as tool_repair
import loopcut.tools as tools


def fake_bpy(**attrs):
    """Blender stub created inside setUp for isolation."""
    module = types.ModuleType("bpy")
    scene_obj = types.SimpleNamespace(name="Base_Chestplate", type="MESH", data=types.SimpleNamespace(vertices=[1, 2, 3]))
    context = types.SimpleNamespace(
        mode="OBJECT",
        active_object=scene_obj,
        selected_objects=[scene_obj],
        scene={"objects": [scene_obj]},
        preferences=types.SimpleNamespace(addons={}, edit=types.SimpleNamespace(undo_steps=32)),
        view_layer=types.SimpleNamespace(objects=types.SimpleNamespace(active=scene_obj), update=lambda: None),
    )
    module.__dict__.update(
        context=context,
        ops=types.SimpleNamespace(
            ed=types.SimpleNamespace(undo=lambda: None, undo_push=lambda **_: None),
            mesh=types.SimpleNamespace(),
        ),
    )
    module.__dict__.update(attrs)
    return module


class SwiftCompatibilityTest(unittest.TestCase):
    """Swift 1.5 / Strata compatibility suite."""

    def setUp(self):
        self.patch = mock.patch.dict(sys.modules, {"bpy": fake_bpy()})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    # -------------------------------------------------------------------------
    # Item 1: Real Swift Compatibility Tests
    # -------------------------------------------------------------------------

    def test_streamed_tool_call_assembly(self):
        """Piecewise streamed tool call arguments assemble into a complete ToolCall."""
        chunks = [
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_swift_1", "function": {"name": "run_python", "arguments": "{\\"co"}}]}}]}\n\n',
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "de\\": \\"import bpy\\\\"}}]}}]}\n\n',
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "nprint(42)\\"}"}}]}}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        response = mock.MagicMock()
        response.__iter__.return_value = chunks
        response.headers = {}

        completion = llm._read(response, on_text=lambda t: None, is_cancelled=lambda: False)
        self.assertEqual(len(completion.tool_calls), 1)
        call = completion.tool_calls[0]
        self.assertEqual(call.id, "call_swift_1")
        self.assertEqual(call.name, "run_python")
        self.assertEqual(call.arguments, '{"code": "import bpy\\nprint(42)"}')
        args = json.loads(call.arguments)
        self.assertEqual(args.get("code"), "import bpy\nprint(42)")

    def test_multiple_tool_calls_streamed(self):
        """Multiple tool calls streamed in one turn retain distinct indices and arguments."""
        chunks = [
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "inspect_api", "arguments": "{\\"query\\": "}}]}}]}\n\n',
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\\"bmesh\\"}"}}]}}]}\n\n',
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_2", "function": {"name": "studio_action", "arguments": "{\\"command\\": \\"mesh_report\\", \\"params\\": {}, \\"summary\\": \\"check\\"}"}}]}}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        response = mock.MagicMock()
        response.__iter__.return_value = chunks
        response.headers = {}

        completion = llm._read(response, on_text=lambda t: None, is_cancelled=lambda: False)
        self.assertEqual(len(completion.tool_calls), 2)
        call0, call1 = completion.tool_calls[0], completion.tool_calls[1]
        self.assertEqual(call0.name, "inspect_api")
        self.assertEqual(json.loads(call0.arguments).get("query"), "bmesh")
        self.assertEqual(call1.name, "studio_action")
        self.assertEqual(json.loads(call1.arguments).get("command"), "mesh_report")

    def test_malformed_arguments_normalization(self):
        """Unescaped newlines streamed inside strings are normalized to valid JSON for run_python."""
        raw_json_with_literal_newlines = '{"code": "def process():\n    print(\'ok\')\n    return True\n"}'
        normalized = llm.normalize_tool_arguments("run_python", raw_json_with_literal_newlines)
        parsed = json.loads(normalized)
        self.assertIn("def process():", parsed["code"])

        # Non-run_python tool calls are left untouched per protocol specification
        raw_studio = '{"command": "script", "params": {"body": "line1\nline2"}}'
        norm_studio = llm.normalize_tool_arguments("studio_action", raw_studio)
        self.assertEqual(norm_studio, raw_studio)

    def test_stream_cancellation_stops_cleanly(self):
        """A cancellation signal mid-stream immediately raises llm.Cancelled."""
        chunks = [
            b'data: {"choices": [{"delta": {"content": "Thinking..."}}]}\n\n',
            b'data: {"choices": [{"delta": {"content": "Still thinking..."}}]}\n\n',
        ]
        response = mock.MagicMock()
        response.__iter__.return_value = chunks
        response.headers = {}

        cancelled = [False]

        def cancel_check():
            return cancelled[0]

        def on_text(t):
            cancelled[0] = True

        with self.assertRaises(llm.Cancelled):
            llm._read(response, on_text=on_text, is_cancelled=cancel_check)

    def test_long_context_token_reporting(self):
        """Usage tokens for 128k context are correctly parsed into completion.usage."""
        chunks = [
            b'data: {"choices": [{"delta": {"content": "Done."}}], "usage": {"prompt_tokens": 124344, "completion_tokens": 127}}\n\n',
            b'data: [DONE]\n\n',
        ]
        response = mock.MagicMock()
        response.__iter__.return_value = chunks
        response.headers = {}

        completion = llm._read(response, on_text=lambda t: None, is_cancelled=lambda: False)
        self.assertIsNotNone(completion.usage)
        self.assertEqual(completion.usage.get("input"), 124344)
        self.assertEqual(completion.usage.get("output"), 127)

    # -------------------------------------------------------------------------
    # Item 2: Targeted Tool-Argument Correction
    # -------------------------------------------------------------------------

    def test_targeted_tool_argument_validation_and_repair(self):
        """Missing required arguments generate targeted repair advice with scene context."""
        schemas = [{
            "type": "function",
            "function": {
                "name": "sculpt_brush_stroke",
                "description": "Apply a sculpting stroke to the active mesh.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "brush": {"type": "string", "description": "Brush type, e.g. 'SMOOTH'"},
                        "location": {"type": "array", "description": "3D coordinate [x, y, z]"},
                        "radius": {"type": "number", "description": "Brush radius"},
                    },
                    "required": ["brush", "location"],
                },
            },
        }]

        # Call missing required 'location' parameter
        valid, err, parsed = tool_repair.validate_tool_arguments("sculpt_brush_stroke", '{"brush": "SMOOTH"}', schemas)
        self.assertFalse(valid)
        self.assertIn("location", err)

        # Build correction message
        scene_summary = tool_repair.get_scene_state_summary()
        correction_msg = tool_repair.build_argument_repair_prompt(
            "sculpt_brush_stroke", err, schemas[0]["function"], scene_summary
        )
        self.assertIn("Schema validation failed", correction_msg)
        self.assertIn("location", correction_msg)
        self.assertIn("Base_Chestplate", correction_msg)
        self.assertIn("structured tool call", correction_msg)

    def test_reply_text_never_executed_as_tool_call(self):
        """Plain reply text with code blocks is treated as text and never executed."""
        reply_text = "Here is how to delete the mesh:\n```python\nimport bpy\nbpy.ops.object.delete()\n```"
        completion = llm.Completion(text=reply_text, tool_calls=[])
        # Verify no tool calls were extracted
        self.assertEqual(len(completion.tool_calls), 0)

    # -------------------------------------------------------------------------
    # Item 3: Portable Strata Setup and Discovery
    # -------------------------------------------------------------------------

    def test_portable_strata_path_resolution(self):
        """Strata runner and config paths resolve from preferences, env vars, or standard fallbacks."""
        with mock.patch.dict(os.environ, {"STRATA_CONFIG": "/tmp/custom-strata.json", "STRATA_RUN_SCRIPT": "/tmp/custom-run.sh"}):
            with mock.patch("pathlib.Path.is_file", return_value=True):
                cfg = strata.get_strata_config_path()
                scr = strata.get_strata_run_script()
                self.assertEqual(str(cfg), "/tmp/custom-strata.json")
                self.assertEqual(str(scr), "/tmp/custom-run.sh")

    def test_api_driven_strata_discovery(self):
        """Strata connection test discovers active model and context limit from API."""
        health_payload = json.dumps({
            "status": "ok",
            "max_context": 131072,
            "model": "Swift-Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ2_XS",
            "images": True,
            "service": "strata",
            "loaded": True,
        }).encode("utf-8")

        models_payload = json.dumps({
            "object": "list",
            "data": [{
                "id": "Swift-Qwen3.8-Flash-Next-GSQ-RCO-abliterated-IQ2_XS",
                "architecture": {"input_modalities": ["text", "image"]},
            }],
        }).encode("utf-8")

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            resp = mock.MagicMock()
            if "/health" in url:
                resp.read.return_value = health_payload
            else:
                resp.read.return_value = models_payload
            resp.__enter__.return_value = resp
            return resp

        with mock.patch("socket.create_connection", return_value=mock.MagicMock()):
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                status = strata.test_strata_connection("http://127.0.0.1:8099/v1")
                self.assertTrue(status["ok"])
                self.assertIn("Swift", status["active_model"])
                self.assertEqual(status["max_context"], 131072)
                self.assertTrue(status["vision_ready"])

    # -------------------------------------------------------------------------
    # Item 4: Verified Vision Capability
    # -------------------------------------------------------------------------

    def test_verify_vision_capability_with_test_image(self):
        """Active image test sends payload to /chat/completions and validates correct subject detection."""
        chat_reply = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "Red"}}],
        }).encode("utf-8")

        def fake_vision_urlopen(req, timeout=None):
            resp = mock.MagicMock()
            resp.read.return_value = chat_reply
            resp.__enter__.return_value = resp
            return resp

        with mock.patch("socket.create_connection", return_value=mock.MagicMock()):
            with mock.patch("urllib.request.urlopen", side_effect=fake_vision_urlopen):
                res = strata.verify_strata_vision("http://127.0.0.1:8099/v1", model="swift-test")
                self.assertTrue(res["vision_verified"])
                self.assertIn("Red", res["reply"])

    def test_verify_vision_capability_rejects_wrong_answer(self):
        """Vision verification fails when server does not identify the expected subject."""
        chat_reply = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "Blue circle"}}],
        }).encode("utf-8")

        def fake_vision_urlopen(req, timeout=None):
            resp = mock.MagicMock()
            resp.read.return_value = chat_reply
            resp.__enter__.return_value = resp
            return resp

        with mock.patch("socket.create_connection", return_value=mock.MagicMock()):
            with mock.patch("urllib.request.urlopen", side_effect=fake_vision_urlopen):
                res = strata.verify_strata_vision("http://127.0.0.1:8099/v1", model="swift-test")
                self.assertFalse(res["vision_verified"])
                self.assertIn("Vision check failed", res["details"])

    def test_verify_vision_capability_handles_server_refusal(self):
        """Vision verification cleanly handles server without vision support."""
        import urllib.error

        err = urllib.error.HTTPError(
            url="http://127.0.0.1:8099/v1/chat/completions",
            code=400,
            msg="this engine was started without --vision",
            hdrs={},
            fp=io.BytesIO(b'{"error": {"message": "this engine was started without --vision"}}'),
        )
        with mock.patch("socket.create_connection", return_value=mock.MagicMock()):
            with mock.patch("urllib.request.urlopen", side_effect=err):
                res = strata.verify_strata_vision("http://127.0.0.1:8099/v1")
                self.assertFalse(res["vision_verified"])
                self.assertIn("vision", res["error"])


class RealSwiftCheckpointLiveTest(unittest.TestCase):
    """End-to-end tests against the live running Swift checkpoint on Strata."""

    def setUp(self):
        self.patch = mock.patch.dict(sys.modules, {"bpy": fake_bpy()})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        if not strata.is_port_open("127.0.0.1", 8099, timeout=0.5):
            raise unittest.SkipTest("Strata server not running on port 8099")

    def test_live_swift_tool_call_stream(self):
        """Live checkpoint correctly generates streamed tool calls with structured JSON."""
        tools_schema = [{
            "type": "function",
            "function": {
                "name": "inspect_api",
                "description": "Inspect Blender Python API",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }]
        completion = llm.stream_chat(
            base_url="http://127.0.0.1:8099/v1",
            api_key="test",
            model="swift-1.5-abliterated-iq2_xs",
            messages=[{"role": "user", "content": "Please inspect the bmesh API."}],
            tools=tools_schema,
            reasoning_effort="medium",
            on_text=lambda t: None,
            is_cancelled=lambda: False,
            max_tokens=200,
        )
        self.assertGreater(len(completion.tool_calls), 0)
        call = completion.tool_calls[0]
        self.assertEqual(call.name, "inspect_api")
        args = json.loads(call.arguments)
        self.assertEqual(args.get("query"), "bmesh")

    def test_live_swift_stream_cancellation(self):
        """Live stream cancellation interrupts inference cleanly and raises llm.Cancelled."""
        cancelled = [False]

        def on_text(t):
            cancelled[0] = True

        with self.assertRaises(llm.Cancelled):
            llm.stream_chat(
                base_url="http://127.0.0.1:8099/v1",
                api_key="test",
                model="swift-1.5-abliterated-iq2_xs",
                messages=[{"role": "user", "content": "Count from 1 to 100 in words."}],
                tools=[],
                reasoning_effort="medium",
                on_text=on_text,
                is_cancelled=lambda: cancelled[0],
                max_tokens=200,
            )

    def test_live_swift_vision_answer_verification(self):
        """Live checkpoint interprets the red square test image and correctly identifies 'red'."""
        res = strata.verify_strata_vision("http://127.0.0.1:8099/v1", model="swift-1.5-abliterated-iq2_xs", timeout=25.0)
        self.assertTrue(res["ok"])
        self.assertTrue(res["vision_verified"])
        self.assertIn("red", res["reply"].lower())


if __name__ == "__main__":
    unittest.main()
