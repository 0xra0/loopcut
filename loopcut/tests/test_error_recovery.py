"""Synthetic transport failures and continuation through the real agent loop."""

import ast
import io
import json
import subprocess
import sys
import threading
import unittest
import urllib.error
from email.message import Message
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_agent_loop as loop_tests
from loopcut import agent, anthropic_messages, chatgpt_responses, config, context, llm, nudges, router, state


class TransportRecoveryTest(unittest.TestCase):
    def test_harness_finds_checkout_with_an_isolated_python_path(self):
        harness = Path(__file__).resolve().parents[1] / "harness" / "fq_errors_check.py"
        code = '''import runpy, sys, types
from unittest import mock
namespace = runpy.run_path(sys.argv[1])
sys.modules["bpy"] = types.ModuleType("bpy")
with mock.patch("importlib.util.spec_from_file_location", side_effect=RuntimeError("checkout reached")):
    try:
        namespace["child"]()
    except RuntimeError as ex:
        assert str(ex) == "checkout reached"
    else:
        raise AssertionError("checkout was not loaded")
'''
        result = subprocess.run([sys.executable, "-I", "-c", code, str(harness)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_harness_installs_the_version_guard_before_importing_the_agent(self):
        harness = Path(__file__).resolve().parents[1] / "harness" / "fq_errors_check.py"
        code = '''import ast, runpy, sys, types
namespace = runpy.run_path(sys.argv[1])
sys.path.insert(0, str(__import__("pathlib").Path(sys.argv[1]).parent))
import checkout
sys.modules["loopcut"] = types.ModuleType("loopcut")
def disable(name):
    stale = types.ModuleType("loopcut.agent")
    stale.STALE = True
    sys.modules["loopcut.agent"] = stale
sys.modules["addon_utils"] = types.SimpleNamespace(disable=disable)
sys.modules["bpy"] = types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={}))
tree = ast.parse(__import__("pathlib").Path(sys.argv[1]).read_text())
child = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "child")
prefix = []
for statement in child.body:
    if isinstance(statement, ast.ImportFrom) and statement.module == "loopcut":
        break
    prefix.append(statement)
exec(compile(ast.Module(body=prefix, type_ignores=[]), sys.argv[1], "exec"), namespace)
assert "loopcut.agent" not in sys.modules
assert any(getattr(f, "package", None) == "loopcut" for f in sys.meta_path)
from loopcut import agent, state
assert not hasattr(agent, "STALE")
assert agent.state is state
'''
        result = subprocess.run([sys.executable, "-I", "-c", code, str(harness)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_stream_dropped_mid_reply_restarts_only_once(self):
        def broken(*args):
            raise chatgpt_responses._Interrupted(True) from EOFError("stream ended")

        with mock.patch.object(chatgpt_responses, "_open", return_value=mock.Mock(headers={})), \
                mock.patch.object(chatgpt_responses, "_read", side_effect=broken) as reads:
            restart = mock.Mock()
            with self.assertRaises(llm.LLMError) as caught:
                chatgpt_responses._stream(model="synthetic", messages=[{"role": "user", "content": "Add a cube"}],
                    tools=[], reasoning_effort="low", on_text=lambda text: None, is_cancelled=lambda: False,
                    on_retry=restart)
        self.assertEqual(caught.exception.code, llm.CONNECTION_LOST)
        self.assertEqual(reads.call_count, 2)
        restart.assert_called_once()

    def open_errors(self, status, *, retry_after=None):
        headers = Message()
        if retry_after is not None:
            headers["Retry-After"] = retry_after

        def post(*args):
            raise urllib.error.HTTPError("https://example.invalid", status, "busy", headers,
                                         io.BytesIO(json.dumps({"error": {"code": "server_busy"}}).encode()))

        with mock.patch.object(chatgpt_responses.chatgpt_auth, "access_token", return_value="synthetic"), \
                mock.patch.object(chatgpt_responses, "_post", side_effect=post) as requests, \
                mock.patch.object(llm, "_wait") as wait:
            with self.assertRaises(llm.LLMError) as caught:
                chatgpt_responses._open(lambda: {}, lambda: False, mock.Mock())
        return caught.exception, requests.call_count, [call.args[0] for call in wait.call_args_list]

    def test_exhausted_503_retains_status_and_offers_another_try(self):
        error, attempts, waits = self.open_errors(503)
        self.assertEqual(waits, [1, 3, 8, 20, 45])
        self.assertEqual(attempts, 6)
        self.assertEqual((error.status, error.code), (503, llm.STREAM_FAILED))
        self.assertEqual(error.details["error_code"], "server_busy")

    def test_429_honours_retry_after_without_shortening_later_backoff(self):
        error, attempts, waits = self.open_errors(429, retry_after="40")
        self.assertEqual(waits, [40, 40])
        self.assertEqual((attempts, error.code), (3, llm.STREAM_FAILED))
        self.assertIn("40", str(error))

    def test_http_date_retry_after_is_supported(self):
        with mock.patch.object(llm.time, "time", return_value=0):
            _, _, waits = self.open_errors(429, retry_after="Thu, 01 Jan 1970 00:00:50 GMT")
        self.assertEqual(waits, [50])

    def test_long_retry_after_is_shown_without_a_hidden_wait(self):
        error, attempts, waits = self.open_errors(429, retry_after="121")
        self.assertEqual((attempts, waits), (1, []))
        self.assertEqual(error.code, llm.STREAM_FAILED)
        self.assertEqual(error.details["retry_after"], 121)
        self.assertIn("2 minutes 1 second", str(error))

    def test_long_server_waits_preserve_the_remaining_minutes_and_seconds(self):
        for seconds, expected in ((121, "2 minutes 1 second"), (3599, "59 minutes 59 seconds"),
                                  (3601, "1 hour 1 second"), (3660, "1 hour 1 minute"),
                                  (86399, "23 hours 59 minutes 59 seconds")):
            with self.subTest(seconds=seconds):
                error, attempts, waits = self.open_errors(429, retry_after=str(seconds))
                self.assertEqual((attempts, waits), (1, []))
                self.assertEqual(error.details["retry_after"], seconds)
                self.assertIn(f"wait {expected} before another request", str(error))

    def test_server_waits_use_human_units_and_are_clamped(self):
        for header, expected, seconds in (("3600", "1 hour", 3600),
                                          ("999999999999999", "24 hours", 86400),
                                          ("Fri, 31 Dec 9999 23:59:59 GMT", "24 hours", 86400)):
            with self.subTest(header=header):
                error, attempts, waits = self.open_errors(429, retry_after=header)
                self.assertEqual((attempts, waits), (1, []))
                self.assertEqual(error.details["retry_after"], seconds)
                self.assertIn("ChatGPT asked", str(error))
                self.assertIn(expected, str(error))

    def test_a_local_backoff_does_not_claim_chatgpt_requested_it(self):
        headers = Message()
        headers["Retry-After"] = "40"
        failures = [urllib.error.HTTPError("https://example.invalid", 503, "busy", header, io.BytesIO(b"{}"))
                    for header in (headers, {}, {}, {}, {})]
        with mock.patch.object(chatgpt_responses.chatgpt_auth, "access_token", return_value="synthetic"), \
                mock.patch.object(chatgpt_responses, "_post", side_effect=failures), \
                mock.patch.object(llm, "_wait"):
            with self.assertRaises(llm.LLMError) as caught:
                chatgpt_responses._open(lambda: {}, lambda: False, mock.Mock())
        self.assertNotIn("ChatGPT asked", str(caught.exception))
        self.assertNotIn("retry_after", caught.exception.details)

    def test_stream_reopens_share_the_http_and_stream_backoff_budget(self):
        def failure():
            return urllib.error.HTTPError("https://example.invalid", 503, "busy", {}, io.BytesIO(b"{}"))

        responses = [*[failure() for _ in range(5)], mock.Mock(headers={}),
                     *[failure() for _ in range(5)], mock.Mock(headers={}), *[failure() for _ in range(6)]]
        with mock.patch.object(chatgpt_responses.chatgpt_auth, "access_token", return_value="synthetic"), \
                mock.patch.object(chatgpt_responses, "_post", side_effect=responses), \
                mock.patch.object(chatgpt_responses, "_read", side_effect=chatgpt_responses._Interrupted(False)), \
                mock.patch.object(llm, "_wait") as wait:
            with self.assertRaises(llm.LLMError):
                chatgpt_responses._stream(model="synthetic", messages=[], tools=[], reasoning_effort="low",
                                          on_text=lambda text: None, is_cancelled=lambda: False)
        self.assertLessEqual(sum(call.args[0] for call in wait.call_args_list), llm.CHATGPT_RETRY_WAIT)

    def test_partial_stream_reopen_keeps_the_backoff_already_spent(self):
        def failure():
            return urllib.error.HTTPError("https://example.invalid", 503, "busy", {}, io.BytesIO(b"{}"))

        responses = [*[failure() for _ in range(5)], mock.Mock(headers={}), *[failure() for _ in range(6)]]
        with mock.patch.object(chatgpt_responses.chatgpt_auth, "access_token", return_value="synthetic"), \
                mock.patch.object(chatgpt_responses, "_post", side_effect=responses), \
                mock.patch.object(chatgpt_responses, "_read", side_effect=chatgpt_responses._Interrupted(True)), \
                mock.patch.object(llm, "_wait") as wait:
            with self.assertRaises(llm.LLMError):
                chatgpt_responses._stream(model="synthetic", messages=[], tools=[], reasoning_effort="low",
                    on_text=lambda text: None, is_cancelled=lambda: False, on_retry=mock.Mock())
        self.assertLessEqual(sum(call.args[0] for call in wait.call_args_list), llm.CHATGPT_RETRY_WAIT)

    def test_harness_reset_has_time_for_a_slow_setup_before_the_click(self):
        source = Path(__file__).resolve().parents[1] / "harness" / "fq_errors_check.py"
        tree = ast.parse(source.read_text())
        reset = next(node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "reset" for target in node.targets))
        namespace = {"datetime": datetime, "timezone": timezone, "time": mock.Mock(time=lambda: 0),
                     "recovery": "reset"}
        timestamp = eval(compile(ast.Expression(reset), str(source), "eval"), namespace)
        self.assertGreaterEqual(datetime.fromisoformat(timestamp).timestamp(), 30)

    def test_http_and_connection_retries_have_separate_counters_and_one_wait_budget(self):
        failures = [urllib.error.HTTPError("https://example.invalid", 503, "busy", {}, io.BytesIO(b"{}"))
                    for _ in range(3)]
        response = mock.Mock()
        with mock.patch.object(chatgpt_responses.chatgpt_auth, "access_token", return_value="synthetic"), \
                mock.patch.object(chatgpt_responses, "_post", side_effect=[*failures, ConnectionResetError(), response]), \
                mock.patch.object(llm, "_wait") as wait:
            self.assertIs(chatgpt_responses._open(lambda: {}, lambda: False, mock.Mock()), response)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [1, 3, 8, 1])

    def test_stop_interrupts_the_real_backoff_before_another_post(self):
        waiting, stopped = threading.Event(), threading.Event()
        errors = []
        real_wait = llm._wait

        def wait(delay, cancelled):
            waiting.set()
            real_wait(delay, cancelled)

        def run():
            try:
                chatgpt_responses._open(lambda: {}, stopped.is_set, mock.Mock())
            except BaseException as ex:
                errors.append(ex)

        failure = urllib.error.HTTPError("https://example.invalid", 503, "busy", {}, io.BytesIO(b"{}"))
        with mock.patch.object(chatgpt_responses.chatgpt_auth, "access_token", return_value="synthetic"), \
                mock.patch.object(chatgpt_responses, "_post", side_effect=failure) as post, \
                mock.patch.object(llm, "_wait", side_effect=wait):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            try:
                self.assertTrue(waiting.wait(2))
            finally:
                stopped.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(post.call_count, 1)
        self.assertIsInstance(errors[0], llm.Cancelled)

    def test_router_and_anthropic_keep_the_thirty_second_cap(self):
        headers = Message()
        headers["Retry-After"] = "120"

        def failure(*args, **kwargs):
            raise urllib.error.HTTPError("https://example.invalid", 503, "busy", headers, io.BytesIO(b"{}"))

        request = urllib.request.Request("https://example.invalid")
        with mock.patch.object(urllib.request, "urlopen", side_effect=failure) as post, \
                mock.patch.object(llm, "_wait") as wait:
            with self.assertRaises(llm.LLMError):
                llm._open(lambda: request, "https://example.invalid", 1, lambda: False,
                          retry_delays=router.RETRY_DELAYS)
            self.assertEqual(post.call_count, 2)
            self.assertEqual([call.args[0] for call in wait.call_args_list], [30])
        with mock.patch.object(anthropic_messages, "_post", side_effect=failure), \
                mock.patch.object(llm, "_wait") as wait:
            with self.assertRaises(llm.LLMError):
                anthropic_messages._open(lambda: ({}, []), "https://example.invalid", "synthetic", 1,
                                         "Anthropic", "synthetic", {}, lambda: False)
            self.assertEqual([call.args[0] for call in wait.call_args_list], [30, 30, 30])

    def test_five_503s_can_recover_on_the_sixth_attempt(self):
        failure = lambda: urllib.error.HTTPError("https://example.invalid", 503, "busy", {}, io.BytesIO(b"{}"))
        response = mock.Mock()
        with mock.patch.object(chatgpt_responses.chatgpt_auth, "access_token", return_value="synthetic"), \
                mock.patch.object(chatgpt_responses, "_post", side_effect=[failure() for _ in range(5)] + [response]), \
                mock.patch.object(llm, "_wait"):
            self.assertIs(chatgpt_responses._open(lambda: {}, lambda: False, mock.Mock()), response)


class ErrorCardRecoveryTest(unittest.TestCase):
    run_replies = loop_tests.UnrunCompletionTest.run_replies

    def test_transient_http_and_dropped_reply_cards_offer_try_again(self):
        for error in (llm.LLMError("Busy", 503), llm.LLMError("Slow down", 429),
                      llm.connection_lost("ChatGPT", EOFError("stream ended"), while_replying=True)):
            with self.subTest(code=error.code, status=error.status):
                session, _, _, _ = self.run_replies([error])
                self.assertTrue(session["items"][-1].get("retry"))
                self.assertTrue(state.can_resume(session))

    def test_region_refusals_offer_fast(self):
        for code in ("unsupported_country_region_territory", "country_not_supported"):
            with self.subTest(code=code):
                session, _, _, _ = self.run_replies([llm.LLMError("Region unavailable", 403, code)])
                card = session["items"][-1]
                self.assertEqual(card["kind"], "offer")
                self.assertIn("continue_loopcut", [action["do"] for action in card["actions"]])

    def test_resume_identifies_the_failed_reply_and_keeps_the_original_turn(self):
        session, _, _, _ = self.run_replies([
            llm.LLMError("Busy", 503, llm.STREAM_FAILED)])
        request = session["messages"][0]
        session, turn, _, _ = self.run_replies([llm.Completion(text="Done.")], session=session, resume=True)
        continuation = session["messages"][-2]
        self.assertTrue(continuation.get(context.STEP_LIMIT))
        self.assertIn("stream_failed", continuation["content"])
        self.assertIn("continue from there", continuation["content"])
        self.assertIs(session["messages"][0], request)
        self.assertEqual(turn.outcome, "completed")

    def test_resume_names_the_preceding_tool_from_the_current_turn(self):
        session, _, _, _ = self.run_replies([loop_tests.UnrunCompletionTest.call(),
            llm.LLMError("Busy", 503, llm.STREAM_FAILED)])
        session, _, _, _ = self.run_replies([llm.Completion(text="Done.")], session=session, resume=True)
        self.assertIn("model reply after run_python", session["messages"][-2]["content"])

    def test_another_failure_replaces_the_previous_retry_note(self):
        failure = llm.LLMError("Busy", 503, llm.STREAM_FAILED)
        session, _, _, _ = self.run_replies([failure])
        for _ in range(2):
            session, _, _, _ = self.run_replies([failure], session=session, resume=True)
        notes = [m for m in session["messages"] if m.get(context.STEP_LIMIT)]
        self.assertEqual(len(notes), 1)

    def test_resume_preserves_answered_retry_notes_from_previous_turns(self):
        note = {"role": "user", context.STEP_LIMIT: True,
                "content": agent.RETRY_NOTE_HEAD + "model reply failed (connection_lost); continue from there."}
        history = [{"role": "user", "content": "Create a cube"}, note,
                   {"role": "assistant", "content": "Done."}]
        failure = llm.LLMError("Busy", 503, llm.STREAM_FAILED)
        session = state.new_session()
        session["messages"] = list(history)
        session, _, _, _ = self.run_replies([failure], session=session)
        request = session["messages"][len(history)]
        # A tool can finish between retry notes, so the current note need not be trailing.
        session, _, _, _ = self.run_replies([loop_tests.UnrunCompletionTest.call(), failure],
                                           session=session, resume=True)
        session, _, _, _ = self.run_replies([llm.Completion(text="Done.")], session=session, resume=True)
        self.assertEqual(session["messages"][:len(history)], history)
        self.assertIs(session["messages"][len(history)], request)
        notes = [m for m in session["messages"][len(history) + 1:]
                 if m.get(context.STEP_LIMIT) and str(m.get("content", "")).startswith(agent.RETRY_NOTE_HEAD)]
        self.assertEqual(len(notes), 1)
        self.assertIn("model reply after run_python", notes[0]["content"])

    def test_every_retryable_error_records_the_failed_step(self):
        def stalled(turn):
            turn.stop_reason = "stalled"
            raise llm.Cancelled()

        for failure, own in ((stalled, None), (llm.LLMError("Upstream", 502, "upstream_error"), None),
                             (llm.LLMError("Slow down", 429), "Slow down"), (ConnectionResetError(), None)):
            with self.subTest(failure=str(failure)), \
                    mock.patch.object(config, "on_service", return_value=True), \
                    mock.patch.object(agent.own_key, "chat_text", return_value=own):
                session, _, _, _ = self.run_replies([failure])
                self.assertTrue(session["messages"][0].get("loopcut_failed_step"))


    def test_a_damaged_failed_step_marker_still_resumes(self):
        with mock.patch.object(config, "on_service", return_value=True), \
                mock.patch.object(agent.own_key, "chat_text", return_value=None):
            session, _, _, _ = self.run_replies([llm.LLMError("Upstream", 502, "upstream_error")])
            session["messages"][0]["loopcut_failed_step"] = "damaged"
            session, _, _, _ = self.run_replies([llm.Completion(text="Done.")], session=session, resume=True)
        self.assertEqual(session["messages"][-1].get("content"), "Done.")

if __name__ == "__main__":
    unittest.main()
