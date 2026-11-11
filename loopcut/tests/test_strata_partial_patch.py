"""Strata/Loopcut regression tests on latest source without Blender running.

Run: python3 -m unittest discover -s loopcut/tests -p test_strata_partial_patch.py -v
"""
import ast
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "addons_core"))
from loopcut import llm

AGENT_SOURCE = (ROOT / "scripts" / "addons_core" / "loopcut" / "agent.py").read_text()
AGENT_AST = ast.parse(AGENT_SOURCE)
# These helpers depend only on the pure-Python llm client, not the running Blender API.
_HELPERS = [node for node in AGENT_AST.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in ("_normalize_tool_calls", "_completion_unusable")]
assert len(_HELPERS) == 2, "Expected tool normalization and cap-check helpers"
_namespace = {"llm": llm, "json": json}
exec(compile(ast.Module(body=_HELPERS, type_ignores=[]), str(ROOT / 'agent.py'), 'exec'), _namespace)
normalize = _namespace['_normalize_tool_calls']
unusable = _namespace['_completion_unusable']


class StrataPartialPatchTest(unittest.TestCase):
    def test_duplicate_normalizer_removed(self):
        source = (ROOT / "scripts" / "addons_core" / "loopcut" / "llm.py").read_text()
        self.assertEqual(source.count("def normalize_tool_arguments("), 1)

    def test_complete_json_repaired_before_history(self):
        raw = '{"code":"import bpy\nprint(123)\n","summary":"Demo"}'
        completion = llm.Completion(tool_calls=[llm.ToolCall(id="c1", name="run_python", arguments=raw)])
        self.assertIs(normalize(completion), completion)
        saved = completion.as_message("reasoning-model")
        fixed = saved['tool_calls'][0]['function']['arguments']
        self.assertEqual(json.loads(fixed)['code'], 'import bpy\nprint(123)\n')
        self.assertNotEqual(fixed, raw)

    def test_other_tools_untouched(self):
        raw = '{"command":"echo a\nb"}'
        completion = llm.Completion(tool_calls=[llm.ToolCall(name="shell", arguments=raw)])
        normalize(completion)
        self.assertEqual(completion.tool_calls[0].arguments, raw)

    def test_truncated_remains_invalid(self):
        raw = '{"code":"import bpy\nprint(123)'
        completion = llm.Completion(tool_calls=[llm.ToolCall(name="run_python", arguments=raw)],
                                    finish_reason="length")
        normalize(completion)
        self.assertEqual(completion.tool_calls[0].arguments, raw)
        self.assertTrue(unusable(completion))

    def test_complete_capped_reply_becomes_usable(self):
        raw = '{"code":"print(3)\n","summary":"safe"}'
        completion = llm.Completion(tool_calls=[llm.ToolCall(name="run_python", arguments=raw)],
                                    finish_reason="length")
        normalize(completion)
        self.assertFalse(unusable(completion))
        self.assertEqual(json.loads(completion.tool_calls[0].arguments)['code'], 'print(3)\n')

    def test_idempotent(self):
        raw = '{"code":"print(3)\n"}'
        completion = llm.Completion(tool_calls=[llm.ToolCall(name="run_python", arguments=raw)])
        normalize(completion)
        once = completion.tool_calls[0].arguments
        normalize(completion)
        self.assertEqual(completion.tool_calls[0].arguments, once)

    def test_request_and_main_loop_are_both_wired(self):
        request = AGENT_SOURCE.split('def _request(', 1)[1].split('def _drop_copied_records(', 1)[0]
        self.assertIn('return _normalize_tool_calls(completion)', request)
        self.assertLess(request.index('return _normalize_tool_calls(completion)'),
                        request.index('if _completion_unusable(completion):'))
        main = AGENT_SOURCE.split('completion = first if first is not None else _request(', 1)[1]
        self.assertLess(main.index('_normalize_tool_calls(completion)'),
                        main.index('messages.append(completion.as_message('))


if __name__ == '__main__':
    unittest.main()
