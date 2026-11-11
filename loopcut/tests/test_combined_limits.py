"""A saved request can recover when either of two account limits clears."""

import ast
import json
import queue
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_agent_loop as loop_tests
import loopcut
from loopcut import agent, chatgpt_auth, config, llm, mainthread, nudges, state
from loopcut.ui import layout


class CombinedLimitsTest(unittest.TestCase):
    run_replies = loop_tests.UnrunCompletionTest.run_replies
    reset = "2099-01-02T03:04:05Z"

    def setUp(self):
        patcher = mock.patch.dict(state.ui, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def host(self):
        source = Path(agent.__file__).parent / "ui" / "host.py"
        nodes = [node for node in ast.parse(source.read_text()).body if isinstance(node, ast.FunctionDef)
                 and node.name in {"_do_action", "_local_limit_action", "_start_upgrade"}]
        namespace = {"__package__": "loopcut.ui", "state": state, "agent": agent, "config": config,
                     "account": mock.Mock(), "conversations": mock.Mock(), "config_changed": mock.Mock(),
                     "settings": types.SimpleNamespace(preferences=lambda: self.prefs)}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
        return types.SimpleNamespace(**namespace)

    def stopped(self, marker=None):
        error = llm.LLMError("Plan limit", 429, "subscription_sharing_usage_limit_exceeded",
                             {"resets_at": self.reset})
        session, _, _, _ = self.run_replies([error], base_url="chatgpt-plan://")
        session["items"][-1]["resolved"] = "Continuing on Fast"
        if marker is not None:  # As loaded from a damaged saved chat.
            next(m for m in session["messages"] if "loopcut_chatgpt_limit" in m)["loopcut_chatgpt_limit"] = marker
        refusal = llm.LLMError("Credits exhausted", 402, "insufficient_credits", {
            "plan": "free", "upgrade": {"url": "https://example.invalid/plans", "plan": "pro", "label": "Get Pro"}},
            account={"plan": "free", "billing": "credits"})
        with mock.patch.object(agent, "_on_main", side_effect=lambda fn: fn()), \
                mock.patch.object(config, "on_service", return_value=True), \
                mock.patch.object(config, "billing_open", return_value=True), \
                mock.patch.object(nudges, "watch_chatgpt_reset", create=True) as watch, \
                mock.patch.object(agent, "_update_account"):
            session, turn, _, _ = self.run_replies([refusal], session=session, resume=True,
                                                  base_url=config.SERVICE_BASE_URL)
        return session, turn, watch

    def test_both_limits_share_one_card_and_preserve_the_request(self):
        session, _, watch = self.stopped()
        cards = [item for item in session["items"] if item["kind"] in ("offer", "limit")]
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["chatgpt_limit"]["resets_at"], self.reset)
        self.assertEqual(card["upgrade"]["plan"], "pro")
        self.assertTrue(state.can_resume(session))
        self.assertEqual(session["messages"][0]["content"], "Build a prop")
        watch.assert_not_called()

    def test_card_draws_reset_upgrade_and_saved_request_without_another_paywall(self):
        session, _, _ = self.stopped()
        with mock.patch.object(config, "billing_open", return_value=True):
            frame = layout.build(session, 440, 900, 1, lambda font, size, text: len(text) * size * .6,
                                 "fast", None, {"billing": "credits", "chatgpt_plan": True})
        text = " ".join(p.get("text", "") for p in frame["prims"])
        self.assertIn("ChatGPT", text)
        self.assertIn(state.reset_label(self.reset), text)
        self.assertIn("saved", text)
        actions = [hit["action"][0] for hit in frame["hits"]]
        self.assertIn("limit_upgrade", actions)
        self.assertIn("limit_chatgpt", actions)
        self.assertIn("Continue on ChatGPT at reset", text)
        self.assertIn(("open_web", chatgpt_auth.USAGE_URL), [hit["action"] for hit in frame["hits"]])

    def test_saved_request_can_run_after_upgrade_and_keeps_limit_context_on_reload(self):
        session, _, _ = self.stopped()
        restored = state.new_session()
        restored.update(json.loads(json.dumps({k: session[k] for k in ("items", "messages")})))
        card = next(i for i in restored["items"] if i["kind"] == "limit")
        self.assertEqual(card["chatgpt_limit"]["resets_at"], self.reset)
        card["status"] = "done"
        restored, turn, requests, _ = self.run_replies([llm.Completion(text="Done.")], session=restored, resume=True)
        self.assertEqual((turn.outcome, requests), ("completed", 1))

    def test_upgrade_callback_resumes_only_the_same_stopped_request(self):
        from loopcut import cloud
        for new_request in (False, True):
            with self.subTest(new_request=new_request):
                session, _, _ = self.stopped()
                card = next(i for i in session["items"] if i["kind"] == "limit")
                host = self.host()
                with mock.patch.object(config, "billing_open", return_value=True), \
                        mock.patch.object(state, "session", return_value=session), \
                        mock.patch.object(agent, "resume") as resume, mock.patch.object(cloud, "_runtime", None):
                    host._start_upgrade(session, card)
                    self.assertEqual(card["status"], "waiting")
                    if new_request:
                        session["messages"].append({"role": "user", "content": "Create a sphere"})
                    host.account.watch_plan.call_args.args[1]({"plan": "pro"})
                    self.assertEqual(resume.call_count, 0 if new_request else 1)

    def test_reset_requires_a_click_then_uses_the_chatgpt_action_once(self):
        session, _, _ = self.stopped()
        card = next(i for i in session["items"] if i["kind"] == "limit")
        self.prefs = types.SimpleNamespace(provider="LOOPCUT")
        host = self.host()
        timers = mock.Mock()
        import loopcut.ui as ui
        with mock.patch.dict(sys.modules, {"bpy": types.SimpleNamespace(app=types.SimpleNamespace(timers=timers))}), \
                mock.patch.object(ui, "host", host, create=True), \
                mock.patch.object(loopcut, "settings", host.settings, create=True), \
                mock.patch.object(state, "session", return_value=session), \
                mock.patch.object(nudges.time, "time", return_value=0) as now, \
                mock.patch.object(agent, "resume", return_value=True) as resume:
            index = session["items"].index(card)
            host._do_action(session, ("limit_chatgpt", index))
            timers.register.assert_called_once()
            self.assertEqual(self.prefs.provider, "LOOPCUT")
            resume.assert_not_called()
            callback = timers.register.call_args.args[0]
            self.assertEqual(callback(), 30)
            resume.assert_not_called()
            now.return_value = 10_000_000_000
            self.assertIsNone(callback())
            resume.assert_called_once()
            self.assertEqual((self.prefs.provider, card["status"]), ("CHATGPT", "done"))
            self.assertIsNone(callback())
            resume.assert_called_once()

    def test_scheduled_reset_can_be_cancelled_without_changing_the_model(self):
        session, _, _ = self.stopped()
        card = next(i for i in session["items"] if i["kind"] == "limit")
        self.prefs = types.SimpleNamespace(provider="LOOPCUT")
        host, timers = self.host(), mock.Mock()
        with mock.patch.dict(sys.modules, {"bpy": types.SimpleNamespace(app=types.SimpleNamespace(timers=timers))}), \
                mock.patch.object(state, "session", return_value=session), \
                mock.patch.object(nudges.time, "time", return_value=0) as now, \
                mock.patch.object(agent, "resume") as resume:
            action = ("limit_chatgpt", session["items"].index(card))
            host._do_action(session, action)
            self.assertEqual(timers.register.call_count, 1)
            host._do_action(session, action)
            now.return_value = 10_000_000_000
            self.assertIsNone(timers.register.call_args.args[0]())
            self.assertEqual(self.prefs.provider, "LOOPCUT")
            resume.assert_not_called()

    def test_scheduled_reset_survives_file_load_revert_and_checkpoint_restore(self):
        for load_kind in ("open", "revert", "checkpoint"):
            with self.subTest(load_kind=load_kind):
                session, _, _ = self.stopped()
                card = next(i for i in session["items"] if i["kind"] == "limit")
                registered = {}
                timers = mock.Mock()
                timers.register.side_effect = lambda callback, **kw: registered.update({callback: kw})
                with mock.patch.dict(sys.modules, {"bpy": types.SimpleNamespace(app=types.SimpleNamespace(timers=timers))}), \
                        mock.patch.object(state, "session", return_value=session), \
                        mock.patch.object(nudges.time, "time", return_value=0) as now:
                    self.assertTrue(nudges.watch_chatgpt_reset(session, card))
                    # Blender removes non-persistent callbacks on every blend-file load.
                    retained = [fn for fn, kw in registered.items() if kw.get("persistent", False)]
                    self.assertEqual(len(retained), 1)
                    self.assertTrue(nudges.chatgpt_reset_pending(card))
                    now.return_value = 10_000_000_000
                    import loopcut.ui as ui
                    host = mock.Mock()
                    prefs = types.SimpleNamespace(provider="LOOPCUT")
                    with mock.patch.object(ui, "host", host, create=True), \
                            mock.patch.object(loopcut, "settings", types.SimpleNamespace(preferences=lambda: prefs), create=True):
                        self.assertIsNone(retained[0]())
                        host._do_action.assert_called_once_with(session, ("limit_chatgpt", session["items"].index(card)))
                    self.assertFalse(nudges.chatgpt_reset_pending(card))

    def test_combined_refusal_uses_the_real_main_thread_queue_and_hosted_reply(self):
        session, _, _, _ = self.run_replies([llm.LLMError("Plan limit", 429,
            "subscription_sharing_usage_limit_exceeded", {"resets_at": self.reset})], base_url="chatgpt-plan://")
        refusal = llm.LLMError("Credits exhausted", 402, "insufficient_credits", {"plan": "free"},
                               account={"plan": "free", "billing": "credits", "credits_balance": 0})
        result, errors = [], []

        def run():
            try:
                result.append(self.run_replies([refusal], session=session, resume=True,
                                               base_url=config.SERVICE_BASE_URL))
            except BaseException as ex:
                errors.append(ex)

        def drain():
            while not mainthread._jobs.empty():
                fn, future = mainthread._jobs.get_nowait()
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(fn())
                    except BaseException as ex:
                        future.set_exception(ex)

        with mock.patch.object(config, "on_service", return_value=True), \
                mock.patch.object(config, "billing_open", return_value=True), \
                mock.patch.object(mainthread, "_jobs", queue.Queue()), \
                mock.patch.object(agent, "_update_account") as update, \
                mock.patch.object(nudges, "watch_chatgpt_reset", create=True) as watch:
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            deadline = time.monotonic() + 3
            while worker.is_alive() and time.monotonic() < deadline:
                drain()
                worker.join(.01)
            drain()
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors, errors)
            self.assertEqual(len(result), 1)
            update.assert_called_once_with(refusal.account)
            watch.assert_not_called()
        self.assertTrue(state.can_resume(session))
        self.assertEqual(len([i for i in session["items"] if i["kind"] in ("offer", "limit")]), 1)

    def test_unknown_reset_never_invents_a_time_or_schedules_a_retry(self):
        session, _, _ = self.stopped()
        card = next(i for i in session["items"] if i["kind"] == "limit")
        card["chatgpt_limit"]["resets_at"] = ""
        timers = mock.Mock()
        with mock.patch.dict(sys.modules, {"bpy": types.SimpleNamespace(app=types.SimpleNamespace(timers=timers))}):
            nudges.watch_chatgpt_reset(session, card)
        timers.register.assert_not_called()

    def test_a_damaged_saved_reset_marker_still_draws_the_card(self):
        for marker, expected in (({"resets_at": 5}, "1970-01-01T00:00:05+00:00"), ("x", "")):
            with self.subTest(marker=marker):
                session, _, _ = self.stopped(marker)
                card = next(i for i in session["items"] if i["kind"] == "limit")
                self.assertEqual(card["chatgpt_limit"], {"resets_at": expected})
                self.assertEqual(nudges.chatgpt_reset_wait(card), 0.0)
                with mock.patch.object(config, "billing_open", return_value=True):
                    frame = layout.build(session, 440, 900, 1, lambda font, size, text: len(text) * size * .6,
                                         "fast", None, {"billing": "credits", "chatgpt_plan": True})
                text = " ".join(p.get("text", "") for p in frame["prims"])
                self.assertIn("ChatGPT resets" if expected else "Check ChatGPT usage", text)
                self.assertNotIn("Your ChatGPT plan can carry on now", text)

    def test_epoch_and_iso_resets_are_supported_but_invalid_times_are_omitted(self):
        self.assertEqual(nudges.chatgpt_reset({"reset_at": 1}), "1970-01-01T00:00:01+00:00")
        self.assertEqual(nudges.chatgpt_reset({"resets_at": self.reset}), self.reset)
        self.assertEqual(nudges.chatgpt_reset({"resets_at": "unknown"}), "")


if __name__ == "__main__":
    unittest.main()
