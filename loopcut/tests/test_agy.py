"""Unit tests for the Antigravity CLI (agy) provider in Loopcut."""

import importlib
import importlib.util
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fake_preferences import AddonPreferences

PACKAGE = Path(__file__).resolve().parents[2] / "scripts/addons_core/loopcut"
NAME = "_f_agy_loopcut"


class Property:
    def __init__(self, **options):
        self.options = options

    def __get__(self, instance, owner):
        if instance is None:
            return self
        if "get" in self.options:
            return self.options["get"](instance)
        return instance.__dict__.get(self.name, self.options.get("default", ""))

    def __set__(self, instance, value):
        if "set" in self.options:
            self.options["set"](instance, value)
        else:
            instance.__dict__[self.name] = value


class Preferences(AddonPreferences):
    def is_property_set(self, name):
        return name in self.__dict__


def register(cls):
    for name, field in cls.__annotations__.items():
        field.name = name
        setattr(cls, name, field)


class TestAgyProvider(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake = types.ModuleType("bpy")
        fake.types = types.SimpleNamespace(
            AddonPreferences=Preferences,
            Operator=type("Operator", (), {}),
            Menu=type("Menu", (), {}),
        )
        fake.props = types.SimpleNamespace(
            **{
                name: Property
                for name in (
                    "StringProperty",
                    "EnumProperty",
                    "BoolProperty",
                    "IntProperty",
                )
            }
        )
        fake.context = types.SimpleNamespace(scene={}, preferences=types.SimpleNamespace(addons={}))
        fake.app = types.SimpleNamespace(driver_namespace={}, background=True)
        fake.utils = types.ModuleType("bpy.utils")
        fake.utils.previews = types.ModuleType("bpy.utils.previews")
        fake.utils.register_class = register
        cls.modules = patch.dict(
            sys.modules,
            {
                "bpy": fake,
                "bpy.utils": fake.utils,
                "bpy.utils.previews": fake.utils.previews,
            },
        )
        cls.modules.start()
        cls.addClassCleanup(cls.modules.stop)

        spec = importlib.util.spec_from_file_location(
            NAME, PACKAGE / "__init__.py", submodule_search_locations=[str(PACKAGE)]
        )
        package = importlib.util.module_from_spec(spec)
        sys.modules[NAME] = package
        spec.loader.exec_module(package)

        cls.config = importlib.import_module(NAME + ".config")
        cls.llm = importlib.import_module(NAME + ".llm")
        cls.settings = importlib.import_module(NAME + ".settings")
        cls.agy_cli = importlib.import_module(NAME + ".agy_cli")
        cls.settings.register()

    def test_config_agy_url_recognition(self):
        self.assertTrue(self.config.agy("agy://local"))
        self.assertTrue(self.config.agy("agy://localhost"))
        self.assertTrue(self.config.agy("agy"))
        self.assertFalse(self.config.agy("https://api.openai.com/v1"))
        self.assertFalse(self.config.agy("anthropic://api.anthropic.com/v1"))
        self.assertFalse(self.config.agy("http://127.0.0.1:11434/v1"))

    def test_check_base_url_allows_agy(self):
        # Should not raise ConfigError
        self.config._check_base_url("agy://local", "Test URL")

    def test_config_load_with_agy_url(self):
        env = {
            "LOOPCUT_BASE_URL": "agy://local",
            "LOOPCUT_MODEL": "claude-sonnet-4-6",
        }
        with patch.dict(os.environ, env, clear=True):
            loaded = self.config.load()
            self.assertEqual(loaded.base_url, "agy://local")
            self.assertEqual(loaded.model, "claude-sonnet-4-6")
            self.assertEqual(loaded.api_key, "agy-local")

    def test_settings_listed_models_for_agy(self):
        models = self.settings.listed_models("AGY")
        self.assertIn("claude-sonnet-4-6", models)
        self.assertEqual(models[0], "claude-sonnet-4-6")

    def test_agy_binary_finder(self):
        with patch.dict(os.environ, {"LOOPCUT_AGY_PATH": "/custom/path/to/agy"}):
            with patch("os.path.exists", return_value=True), patch("os.access", return_value=True):
                self.assertEqual(self.agy_cli.find_agy_binary(), "/custom/path/to/agy")

    def test_build_prompt(self):
        messages = [
            {"role": "system", "content": "You are Loopcut."},
            {"role": "user", "content": "Create a cube."},
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "run_python",
                    "description": "Run bpy code",
                    "parameters": {"type": "object", "properties": {"code": {"type": "string"}}},
                },
            }
        ]
        prompt = self.agy_cli.build_prompt(messages, tools)
        self.assertIn("System Instructions", prompt)
        self.assertIn("You are Loopcut.", prompt)
        self.assertIn("Available Tools", prompt)
        self.assertIn("run_python", prompt)
        self.assertIn("Create a cube.", prompt)

    def test_parse_tool_calls_json_block(self):
        raw = """I will create a cube for you.
```json
{
  "tool_calls": [
    {
      "name": "run_python",
      "arguments": {
        "code": "import bpy\\nbpy.ops.mesh.primitive_cube_add()"
      }
    }
  ]
}
```
"""
        clean_text, calls = self.agy_cli.parse_tool_calls(raw)
        self.assertEqual(clean_text, "I will create a cube for you.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "run_python")
        args = json.loads(calls[0].arguments)
        self.assertIn("primitive_cube_add", args["code"])

    def test_parse_tool_calls_raw_json(self):
        raw = '{"tool_calls": [{"name": "get_scene_info", "arguments": {}}]}'
        clean_text, calls = self.agy_cli.parse_tool_calls(raw)
        self.assertEqual(clean_text, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "get_scene_info")

    def test_parse_tool_calls_xml(self):
        raw = '<tool_call>{"name": "inspect_api", "arguments": {"name": "bpy.ops"}}</tool_call>'
        clean_text, calls = self.agy_cli.parse_tool_calls(raw)
        self.assertEqual(clean_text, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "inspect_api")

    def test_parse_tool_calls_plain_text(self):
        raw = "Hello! I can help you with Blender modeling and animation."
        clean_text, calls = self.agy_cli.parse_tool_calls(raw)
        self.assertEqual(clean_text, raw)
        self.assertEqual(len(calls), 0)

    def test_stream_chat_routes_to_agy(self):
        with patch.object(self.agy_cli, "stream") as mock_stream:
            mock_stream.return_value = self.llm.Completion(text="OK")
            comp = self.llm.stream_chat(
                base_url="agy://local",
                api_key="",
                model="claude-sonnet-4-6",
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                reasoning_effort="medium",
                on_text=lambda t: None,
                is_cancelled=lambda: False,
            )
            self.assertEqual(comp.text, "OK")
            self.assertTrue(mock_stream.called)

    def test_stream_mock_ndjson(self):
        ndjson_lines = [
            json.dumps({"event": "init", "init": {"model": "claude-sonnet-4-6"}}),
            json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "Hello "}}),
            json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "world!"}}),
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "usage": {"input_tokens": 100, "output_tokens": 10}}}),
        ]
        mock_proc = Mock()
        mock_proc.stdout = io.StringIO("\n".join(ndjson_lines) + "\n")
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        mock_proc.stdin = Mock()

        deltas = []
        with patch.object(self.agy_cli, "find_agy_binary", return_value="/bin/agy"), \
             patch("subprocess.Popen", return_value=mock_proc):
            comp = self.agy_cli.stream(
                model="claude-sonnet-4-6",
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                reasoning_effort="medium",
                on_text=deltas.append,
                is_cancelled=lambda: False,
            )
            self.assertEqual(comp.text, "Hello world!")
            self.assertEqual(deltas, ["Hello ", "world!"])
            self.assertEqual(comp.finish_reason, "stop")
            self.assertEqual(comp.usage, {"input": 100, "output": 10, "cached": 0})

    def test_stream_quota_exhausted(self):
        ndjson_lines = [
            json.dumps({"event": "result", "result": {"status": "ERROR", "error": "RESOURCE_EXHAUSTED (code 429): Individual quota reached."}}),
        ]
        mock_proc = Mock()
        mock_proc.stdout = io.StringIO("\n".join(ndjson_lines) + "\n")
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1
        mock_proc.stdin = Mock()

        with patch.object(self.agy_cli, "find_agy_binary", return_value="/bin/agy"), \
             patch("subprocess.Popen", return_value=mock_proc):
            with self.assertRaises(self.llm.LLMError) as ctx:
                self.agy_cli.stream(
                    model="claude-sonnet-4-6",
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    reasoning_effort="medium",
                    on_text=lambda t: None,
                    is_cancelled=lambda: False,
                )
            self.assertEqual(ctx.exception.status, 429)
            self.assertEqual(ctx.exception.code, "rate_limited")

    def test_resolve_model_and_labels(self):
        # Claude Sonnet 4.6
        self.assertEqual(self.agy_cli.resolve_model("claude-sonnet-4-6"), "claude-sonnet-4-6")
        self.assertEqual(self.agy_cli.resolve_model("claude sonnet 4.6"), "claude-sonnet-4-6")
        self.assertEqual(self.agy_cli.model_label("claude-sonnet-4-6"), "Claude Sonnet 4.6 (Thinking)")

        # Claude Opus 4.6
        self.assertEqual(self.agy_cli.resolve_model("claude-opus-4-6"), "claude-opus-4-6-thinking")
        self.assertEqual(self.agy_cli.resolve_model("claude opus 4.6"), "claude-opus-4-6-thinking")
        self.assertEqual(self.agy_cli.resolve_model("claude-opus-4-6-thinking"), "claude-opus-4-6-thinking")
        self.assertEqual(self.agy_cli.model_label("claude-opus-4-6-thinking"), "Claude Opus 4.6 (Thinking)")
        self.assertEqual(self.agy_cli.model_label("claude-opus-4-6"), "Claude Opus 4.6 (Thinking)")

        # GPT 120B
        self.assertEqual(self.agy_cli.resolve_model("gpt-120b"), "gpt-oss-120b-medium")
        self.assertEqual(self.agy_cli.resolve_model("gpt 120 b"), "gpt-oss-120b-medium")
        self.assertEqual(self.agy_cli.resolve_model("gpt-oss-120b"), "gpt-oss-120b-medium")
        self.assertEqual(self.agy_cli.resolve_model("gpt-oss-120b-medium"), "gpt-oss-120b-medium")
        self.assertEqual(self.agy_cli.model_label("gpt-oss-120b-medium"), "GPT-OSS 120B (Medium)")
        self.assertEqual(self.agy_cli.model_label("gpt-120b"), "GPT-OSS 120B (Medium)")

        # Gemini 3.8 Flash with effort
        self.assertEqual(self.agy_cli.resolve_model("gemini-3.8-flash", "high"), "gemini-3.8-flash-high")
        self.assertEqual(self.agy_cli.resolve_model("gemini-3.8-flash", "medium"), "gemini-3.8-flash-medium")
        self.assertEqual(self.agy_cli.resolve_model("gemini-3.8-flash", "low"), "gemini-3.8-flash-low")
        self.assertEqual(self.agy_cli.resolve_model("gemini-3.8-flash"), "gemini-3.8-flash-high")
        self.assertEqual(self.agy_cli.resolve_model("gemini 3.8 flash", "low"), "gemini-3.8-flash-low")
        self.assertEqual(self.agy_cli.model_label("gemini-3.8-flash"), "Gemini 3.8 Flash")

    def test_default_effort_high_in_config(self):
        env = {
            "LOOPCUT_BASE_URL": "agy://local",
            "LOOPCUT_MODEL": "claude-sonnet-4-6",
        }
        with patch.dict(os.environ, env, clear=True):
            loaded = self.config.load()
            self.assertEqual(loaded.reasoning_effort, "high")

    def test_stream_passes_model_and_effort_flags(self):
        mock_proc = Mock()
        mock_proc.stdout = io.StringIO('{"event": "result", "result": {"status": "SUCCESS"}}\n')
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0
        mock_proc.stdin = Mock()

        captured_cmd = []

        def fake_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return mock_proc

        with patch.object(self.agy_cli, "find_agy_binary", return_value="/bin/agy"), \
             patch("subprocess.Popen", side_effect=fake_popen):
            # Test gemini-3.8-flash with low effort
            self.agy_cli.stream(
                model="gemini-3.8-flash",
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                reasoning_effort="low",
                on_text=lambda t: None,
                is_cancelled=lambda: False,
            )
            self.assertIn("--model", captured_cmd)
            model_idx = captured_cmd.index("--model")
            self.assertEqual(captured_cmd[model_idx + 1], "gemini-3.8-flash-low")
            self.assertIn("--effort", captured_cmd)
            effort_idx = captured_cmd.index("--effort")
            self.assertEqual(captured_cmd[effort_idx + 1], "low")

        captured_cmd.clear()
        with patch.object(self.agy_cli, "find_agy_binary", return_value="/bin/agy"), \
             patch("subprocess.Popen", side_effect=fake_popen):
            # Test gpt 120 b with default (high) effort
            self.agy_cli.stream(
                model="gpt 120 b",
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                reasoning_effort="",
                on_text=lambda t: None,
                is_cancelled=lambda: False,
            )
            self.assertIn("--model", captured_cmd)
            model_idx = captured_cmd.index("--model")
            self.assertEqual(captured_cmd[model_idx + 1], "gpt-oss-120b-medium")
            self.assertIn("--effort", captured_cmd)
            effort_idx = captured_cmd.index("--effort")
            self.assertEqual(captured_cmd[effort_idx + 1], "high")

    def test_settings_effort_options_and_selection_for_agy(self):
        prefs = self.settings.LoopcutPreferences()
        prefs.provider = "AGY"
        prefs.model = "claude-sonnet-4-6"
        prefs.agy_effort = "high"

        with patch.object(self.settings, "preferences", return_value=prefs):
            opts = self.settings.effort_options()
            self.assertEqual([o[0] for o in opts], ["low", "medium", "high", "xhigh", "max"])
            # high is selected
            self.assertTrue(any(o[0] == "high" and o[2] is True for o in opts))

            # Change effort to low
            self.settings.choose_effort("low")
            self.assertEqual(prefs.agy_effort, "low")
            opts = self.settings.effort_options()
            self.assertTrue(any(o[0] == "low" and o[2] is True for o in opts))

            # Verify values() exports LOOPCUT_REASONING_EFFORT
            with patch.object(self.settings.credentials, "api_key", return_value="agy-local"):
                vals = self.settings.values()
                self.assertEqual(vals["LOOPCUT_REASONING_EFFORT"], "low")

    def test_settings_model_options_and_label_for_agy(self):
        prefs = self.settings.LoopcutPreferences()
        prefs.provider = "AGY"
        prefs.model = "gemini-3.8-flash"
        prefs.agy_effort = "high"

        with patch.object(self.settings, "preferences", return_value=prefs):
            options = self.settings.model_options()
            self.assertTrue(any(o[0] == "gemini-3.8-flash" and o[1] == "Gemini 3.8 Flash" for o in options))
            self.assertTrue(any(o[0] == "claude-opus-4-6-thinking" and "Opus" in o[1] for o in options))
            self.assertTrue(any(o[0] == "gpt-oss-120b-medium" and "120B" in o[1] for o in options))

            label = self.settings.model_label("gemini-3.8-flash")
            self.assertEqual(label, "agy · Gemini 3.8 Flash")

    def test_parse_tool_calls_markdown_python_code(self):
        raw = """I will fix the mesh orientation by resetting its rotation.
```python
import bpy

obj = bpy.data.objects.get("geometry_0")
if obj:
    obj.rotation_euler = (0, 0, 0)
```
Let me know if you need any adjustments."""
        # Code in a reply is shown to the user, never run: the model runs code only through a tool call.
        clean_text, calls = self.agy_cli.parse_tool_calls(raw)
        self.assertEqual(calls, [])
        self.assertIn("I will fix the mesh orientation", clean_text)
        self.assertIn("```python", clean_text)
        self.assertIn("rotation_euler", clean_text)

    def test_parse_tool_calls_single_json_call(self):
        raw = """```json
{
  "name": "get_object_info",
  "arguments": {
    "names": ["geometry_0", "world"]
  }
}
```"""
        clean_text, calls = self.agy_cli.parse_tool_calls(raw)
        self.assertEqual(clean_text, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "get_object_info")
        args = json.loads(calls[0].arguments)
        self.assertEqual(args["names"], ["geometry_0", "world"])

    def test_parse_tool_calls_list_of_json_calls(self):
        raw = """```json
[
  {"name": "capture_viewport", "arguments": {"angle": "front"}},
  {"name": "get_object_info", "arguments": {"names": ["geometry_0"]}}
]
```"""
        clean_text, calls = self.agy_cli.parse_tool_calls(raw)
        self.assertEqual(clean_text, "")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].name, "capture_viewport")
        self.assertEqual(calls[1].name, "get_object_info")

    def test_parse_tool_calls_multiline_unescaped_code_in_json(self):
        raw = """```json
{
  "tool_calls": [
    {
      "name": "run_python",
      "arguments": {
        "code": "import bpy
obj = bpy.data.objects['geometry_0']
obj.rotation_euler = (0, 0, 0)",
        "summary": "Fix rotation"
      }
    }
  ]
}
```"""
        clean_text, calls = self.agy_cli.parse_tool_calls(raw)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "run_python")
        args = json.loads(calls[0].arguments)
        self.assertIn("geometry_0", args["code"])
        self.assertEqual(args["summary"], "Fix rotation")

    def test_render_messages_tool_correlation(self):
        messages = [
            {"role": "user", "content": "Fix orientation"},
            {
                "role": "assistant",
                "content": "Checking setup first.",
                "tool_calls": [
                    {
                        "id": "call_12345",
                        "type": "function",
                        "function": {"name": "get_object_info", "arguments": '{"names": ["geometry_0"]}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_12345", "content": "Object geometry_0: rotation (1.57, 0, 0)"},
        ]
        rendered = "\n".join(self.agy_cli._render_messages(messages))
        self.assertIn("Called tool `get_object_info` (call_id: `call_12345`)", rendered)
        self.assertIn("Tool Result: `get_object_info` (call_id: `call_12345`)", rendered)
        self.assertIn("Object geometry_0: rotation", rendered)

    def test_render_messages_handles_data_uri_images(self):
        data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "Look at this"}, {"type": "image_url", "image_url": {"url": data_uri}}]}
        ]
        rendered = "\n".join(self.agy_cli._render_messages(messages))
        self.assertNotIn("iVBORw0KGgoAAAANSUhEUgAA", rendered)
        # agy runs with its file and shell tools off, so it cannot open an image file: it is told so,
        # rather than handed a path it would spend a turn failing to read, and nothing is written.
        self.assertIn(self.agy_cli.NO_IMAGES, rendered)
        self.assertEqual(list(self.agy_cli._workdir().iterdir()), [])
        self.assertEqual(self.agy_cli._workdir().stat().st_mode & 0o777, 0o700)


    def test_parse_tool_calls_dual_json_and_python_code(self):
        raw = """I will first inspect the mesh and then apply clean-up script.
```json
{
  "name": "get_object_info",
  "arguments": {"names": ["Vampire_Chestplate"]}
}
```
Now executing the clean-up:
```python
import bpy
import bmesh

obj = bpy.data.objects.get("Vampire_Chestplate")
print("Cleaned!")
```
"""
        clean_text, calls = self.agy_cli.parse_tool_calls(raw)
        self.assertEqual([c.name for c in calls], ["get_object_info"])
        args1 = json.loads(calls[0].arguments)
        self.assertEqual(args1["names"], ["Vampire_Chestplate"])
        self.assertIn("Cleaned!", clean_text)
        self.assertNotIn("```json", clean_text)

    def test_prompt_autonomous_mandate_and_addons_guidance(self):
        rendered = self.agy_cli._render_tools([{"function": {"name": "run_python", "description": "Run python code", "parameters": {}}}])
        self.assertIn("Autonomous Execution Mandate", rendered)
        self.assertIn("Do NOT stop after a single step or pause to ask conversational confirmation", rendered)
        self.assertIn("Utilizing Installed Blender Add-ons", rendered)
        self.assertIn("30-Second Deadline", rendered)
        self.assertNotIn("```python", rendered)  # Nothing invites code in a reply: it would only be shown.

        prompt = self.agy_cli.build_prompt([{"role": "user", "content": "Fix the mesh"}], [])
        self.assertIn("Do NOT pause to ask conversational permission for intermediate steps", prompt)

    def test_nested_raw_json_and_untagged_code_blocks(self):
        raw = """I will now execute the script:
{"name": "run_python", "arguments": {"code": "import bpy\ndata = {'x': 1, 'y': 2}\nprint(data)"}}
Done!"""
        clean_text, calls = self.agy_cli.parse_tool_calls(raw, {"run_python"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "run_python")
        args = json.loads(calls[0].arguments)
        self.assertIn("import bpy", args["code"])
        self.assertIn("data = {'x': 1, 'y': 2}", args["code"])

        # Test untagged code block containing Blender python
        raw_untagged = """Here is the operator:
```
import bpy
import bmesh
bm = bmesh.new()
```"""
        clean_text2, calls2 = self.agy_cli.parse_tool_calls(raw_untagged, {"run_python"})
        self.assertEqual(calls2, [])
        self.assertIn("bmesh.new()", clean_text2)

    def test_xml_tool_use_and_calls_extraction(self):
        raw_xml = """<tool_use>
{"name": "get_object_info", "arguments": {"names": ["Vampire_Chestplate"]}}
</tool_use>"""
        clean, calls = self.agy_cli.parse_tool_calls(raw_xml, {"get_object_info"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "get_object_info")
        args = json.loads(calls[0].arguments)
        self.assertEqual(args["names"], ["Vampire_Chestplate"])

    def test_runcommand_intercepted_and_converted_to_run_python(self):
        # When model emits RunCommand with python3 -c 'import bpy...', it is converted to run_python
        raw = """```json
{
  "name": "RunCommand",
  "arguments": {
    "command": "python3 -c 'import bpy\\nimport bmesh\\nbm = bmesh.new()\\nprint(len(bm.verts))'"
  }
}
```"""
        clean, calls = self.agy_cli.parse_tool_calls(raw, {"run_python"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "run_python")
        args = json.loads(calls[0].arguments)
        self.assertIn("import bpy", args["code"])
        self.assertIn("bmesh.new()", args["code"])

        # Test cat << 'EOF' pattern
        raw_cat = """```json
{
  "name": "run_command",
  "arguments": {
    "CommandLine": "cat << 'EOF' > /tmp/inspect.py\\nimport bpy\\nobj = bpy.data.objects['Vampire_Chestplate']\\nEOF\\npython3 /tmp/inspect.py"
  }
}
```"""
        clean_cat, calls_cat = self.agy_cli.parse_tool_calls(raw_cat, {"run_python"})
        self.assertEqual(len(calls_cat), 1)
        self.assertEqual(calls_cat[0].name, "run_python")
        args_cat = json.loads(calls_cat[0].arguments)
        self.assertIn("Vampire_Chestplate", args_cat["code"])

    def test_render_messages_truncates_massive_tool_results(self):
        huge_tool_output = "vertex_dump_data: " + ("0.12345, " * 5000)
        messages = [
            {"role": "user", "content": "Analyze vertices"},
            {"role": "tool", "content": huge_tool_output},
        ]
        rendered = "\n".join(self.agy_cli._render_messages(messages))
        self.assertIn("[truncated", rendered)
        self.assertLess(len(rendered), 15000)

    def test_agy_runs_strict_in_its_own_folder(self):
        with patch.object(self.agy_cli, "find_agy_binary", return_value="/usr/bin/agy"):
            with patch("subprocess.Popen") as mock_popen:
                mock_proc = Mock()
                mock_proc.stdin = Mock()
                mock_proc.stdout = io.StringIO('{"event": "result", "result": {"status": "SUCCESS"}}\n')
                mock_proc.stderr = io.StringIO("")
                mock_proc.poll.return_value = 0
                mock_proc.wait.return_value = 0
                mock_proc.returncode = 0
                mock_popen.return_value = mock_proc

                self.agy_cli.stream(
                    model="gemini-3.8-flash",
                    messages=[{"role": "user", "content": "Hi"}],
                    tools=[],
                    reasoning_effort="high",
                    on_text=lambda _: None,
                    is_cancelled=lambda: False,
                )

                args, kwargs = mock_popen.call_args
                cmd = args[0]
                # Loopcut takes only agy's text; agy's own shell and file tools stay off, whatever the
                # user's agy settings allow (those had toolPermission always-proceed).
                self.assertFalse(any(arg.startswith("--dangerously") for arg in cmd))  # check: ok asserts the bypass flag is absent
                held = json.loads(cmd[cmd.index("--settings") + 1])
                self.assertEqual(held, {"toolPermission": "strict", "allowNonWorkspaceAccess": False})
                self.assertEqual(kwargs.get("cwd"), self.agy_cli._workdir())
                self.assertNotIn("/tmp/agy_loopcut.log", cmd)
                self.assertTrue(kwargs.get("start_new_session"))

    def test_guidance_prohibits_in_process_render(self):
        prompt = self.agy_cli.build_prompt([{"role": "user", "content": "render front view"}], [])
        self.assertIn("NEVER call `bpy.ops.render.render()`", prompt)
        rendered = self.agy_cli._render_tools([{"function": {"name": "run_python", "description": "", "parameters": {}}}])
        self.assertIn("NEVER call `bpy.ops.render.render()`", rendered)

    def test_stream_retries_and_recovers_on_stream_interrupted(self):
        with patch.object(self.agy_cli, "find_agy_binary", return_value="/usr/bin/agy"), \
             patch.object(self.llm, "_wait") as mock_wait:

            calls = []

            def fake_popen(*args, **kwargs):
                mock_proc = Mock()
                mock_proc.stdin = Mock()
                mock_proc.poll.return_value = 0
                mock_proc.wait.return_value = 0
                mock_proc.returncode = 0
                mock_proc.stderr = io.StringIO("")
                if len(calls) == 0:
                    calls.append("fail")
                    # First attempt fails with transient stream interrupted error
                    mock_proc.stdout = io.StringIO(
                        '{"event": "result", "result": {"status": "ERROR", "error": "The stream was interrupted. Please continue the task you were working on."}}\n'
                    )
                else:
                    calls.append("success")
                    # Second attempt succeeds
                    mock_proc.stdout = io.StringIO(
                        '{"event": "step_update", "step_update": {"text_delta": "Success output"}}\n'
                        '{"event": "result", "result": {"status": "SUCCESS"}}\n'
                    )
                return mock_proc

            with patch("subprocess.Popen", side_effect=fake_popen):
                completion = self.agy_cli.stream(
                    model="gemini-3.8-flash",
                    messages=[{"role": "user", "content": "Hi"}],
                    tools=[],
                    reasoning_effort="high",
                    on_text=lambda _: None,
                    is_cancelled=lambda: False,
                )

                self.assertEqual(len(calls), 2)
                self.assertEqual(completion.text, "Success output")
                self.assertTrue(mock_wait.called)

    def test_stream_raises_connection_lost_when_retries_exhausted(self):
        with patch.object(self.agy_cli, "find_agy_binary", return_value="/usr/bin/agy"), \
             patch.object(self.llm, "_wait"):

            def fake_popen(*args, **kwargs):
                mock_proc = Mock()
                mock_proc.stdin = Mock()
                mock_proc.poll.return_value = 0
                mock_proc.wait.return_value = 0
                mock_proc.returncode = 0
                mock_proc.stderr = io.StringIO("")
                mock_proc.stdout = io.StringIO(
                    '{"event": "result", "result": {"status": "ERROR", "error": "The stream was interrupted. Please continue the task you were working on."}}\n'
                )
                return mock_proc

            with patch("subprocess.Popen", side_effect=fake_popen):
                with self.assertRaises(self.llm.LLMError) as ctx:
                    self.agy_cli.stream(
                        model="gemini-3.8-flash",
                        messages=[{"role": "user", "content": "Hi"}],
                        tools=[],
                        reasoning_effort="high",
                        on_text=lambda _: None,
                        is_cancelled=lambda: False,
                    )

                self.assertEqual(ctx.exception.code, self.llm.CONNECTION_LOST)
                self.assertIn("Antigravity CLI connection lost before reply", str(ctx.exception))

    def test_stream_retries_partial_stream_with_on_retry(self):
        with patch.object(self.agy_cli, "find_agy_binary", return_value="/usr/bin/agy"):
            calls = []
            retried = []

            def fake_popen(*args, **kwargs):
                mock_proc = Mock()
                mock_proc.stdin = Mock()
                mock_proc.poll.return_value = 0
                mock_proc.wait.return_value = 0
                mock_proc.returncode = 0
                mock_proc.stderr = io.StringIO("")
                if len(calls) == 0:
                    calls.append("fail_partial")
                    # Streams some delta, then gets interrupted
                    mock_proc.stdout = io.StringIO(
                        '{"event": "step_update", "step_update": {"text_delta": "Partial..."}}\n'
                        '{"event": "result", "result": {"status": "ERROR", "error": "connection reset by peer"}}\n'
                    )
                else:
                    calls.append("success")
                    mock_proc.stdout = io.StringIO(
                        '{"event": "step_update", "step_update": {"text_delta": "Full result"}}\n'
                        '{"event": "result", "result": {"status": "SUCCESS"}}\n'
                    )
                return mock_proc

            with patch("subprocess.Popen", side_effect=fake_popen):
                completion = self.agy_cli.stream(
                    model="gemini-3.8-flash",
                    messages=[{"role": "user", "content": "Hi"}],
                    tools=[],
                    reasoning_effort="high",
                    on_text=lambda _: None,
                    is_cancelled=lambda: False,
                    on_retry=lambda: retried.append(True),
                )

                self.assertEqual(len(calls), 2)
                self.assertEqual(len(retried), 1)
                self.assertEqual(completion.text, "Full result")


if __name__ == "__main__":
    unittest.main()
