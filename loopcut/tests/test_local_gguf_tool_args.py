"""Regression tests for malformed local Strata tool-call JSON (no Blender required).

Run: python -m unittest discover -s loopcut/tests -p test_local_gguf_tool_args.py
"""
import errno
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
from loopcut import llm


class LocalGGUFToolArgumentsTest(unittest.TestCase):
    def test_repair_unescaped_newlines_in_complete_run_python_call(self):
        raw = '{"code":"import bpy\nprint(123)\n", "summary":"Demo"}'
        fixed = llm.normalize_tool_arguments("run_python", raw)
        self.assertEqual(json.loads(fixed), {"code": "import bpy\nprint(123)\n", "summary": "Demo"})
        self.assertNotEqual(fixed, raw)

    def test_repair_carriage_returns_tabs_without_double_escaping(self):
        raw = '{"code":"if True:\r\n\tprint(\\\"hi\\\")", "summary":"Demo"}'
        fixed = llm.normalize_tool_arguments("run_python", raw)
        self.assertEqual(json.loads(fixed)["code"], 'if True:\r\n\tprint("hi")')

    def test_valid_json_preserved_and_python_escape_sequences_intact(self):
        raw = json.dumps({"code": 'print("literal\\n")', "summary": "Test"})
        self.assertEqual(llm.normalize_tool_arguments("run_python", raw), raw)

    def test_do_not_guess_truncated_or_ambiguous_code(self):
        for raw in ('{"code":"import bpy\nprint(1)', '{"code":"print("bad")\n", "summary":"x"}',
                    '{"code":"print(2)\n", "summary":"x"'):
            with self.subTest(raw=raw):
                self.assertEqual(llm.normalize_tool_arguments("run_python", raw), raw)

    def test_other_tool_types_not_modified(self):
        raw = '{"command":"echo hi\n"}'
        self.assertEqual(llm.normalize_tool_arguments("shell", raw), raw)

    def test_streamed_run_python_fragments_can_be_reassembled_and_repaired(self):
        chunks = []
        for idx, value in enumerate(['{"code":"import bpy\n', 'print(1)\n", "summary":"demo"}']):
            chunks.append({'choices': [{'delta': {'tool_calls': [{'index': 0,
                'id': 'call1' if idx == 0 else None,
                'function': {'name': 'run_python' if idx == 0 else '', 'arguments': value}}]}}]})
        chunks.append({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]})
        raw = b''.join(b'data: ' + json.dumps(c).encode() + b'\n\n' for c in chunks) + b'data: [DONE]\n\n'
        response = io.BytesIO(raw)
        response.headers = {}
        completion = llm._read(response, lambda _: None, lambda: False)
        self.assertEqual(len(completion.tool_calls), 1)
        call = completion.tool_calls[0]
        call.arguments = llm.normalize_tool_arguments(call.name, call.arguments)
        self.assertEqual(json.loads(call.arguments)['code'], 'import bpy\nprint(1)\n')

    def test_local_connection_refused_gives_actionable_error_without_restart(self):
        def refuse(*args, **kwargs):
            raise urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, 'Connection refused'))
        with mock.patch.object(llm.urllib.request, 'urlopen', side_effect=refuse), \
             mock.patch.object(llm, '_wait', return_value=None):
            with self.assertRaises(llm.LLMError) as err:
                llm._open(lambda: llm.urllib.request.Request('http://127.0.0.1:8099/v1/chat/completions'),
                          'http://127.0.0.1:8099/v1', 1, lambda: False)
        self.assertEqual(err.exception.code, llm.CONNECTION_LOST)
        self.assertIn('earlyoom', str(err.exception))
        self.assertIn('Try again', str(err.exception))


if __name__ == '__main__':
    unittest.main()
