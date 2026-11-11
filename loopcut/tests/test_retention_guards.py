"""Helper recovery and truthful completion against the released 0.2.21 paths; no Blender needed."""

import json
import types
import unittest
from unittest import mock

from test_orchestrator import LIVE, USER, agent, index_patch, orchestrator, reference_wave, state
from loopcut import config, llm, router, tools, waves


def setUpModule():
    patcher = index_patch()
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class HelperRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.session = state.reset()
        self.addCleanup(state.reset)
        self.session["items"].append(state.item_user(USER))
        self.session["messages"].append({"role": "user", "content": USER})
        self.turn = agent.Turn()
        self.turn.roles = types.SimpleNamespace(orchestrate=True, job_max_usd=0.4,
                                               worker_max_seconds=1800, max_workers=3)
        self.session["turn"] = self.turn
        for patcher in (
                mock.patch.object(orchestrator, "live_facts", lambda plan: json.loads(json.dumps(LIVE))),
                mock.patch.object(orchestrator, "layout", return_value=([], [], [], []))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, wave=None, **arguments):
        return self.result(json.dumps({"wave": wave or reference_wave(), **arguments}))

    def result(self, arguments):
        # The agent accounts for the result after tool dispatch; argument inspection alone has no owning turn.
        return orchestrator.plan_result(self.session, tools.execute("delegate", arguments))

    def invalid(self, number):
        wave = reference_wave()
        for worker in wave["workers"]:
            worker["requirements"] = [{"quote": f"a paraphrase the user never wrote {number}", "do": "build it"}]
        return self.execute(wave, dry_run=True)

    def offered(self):
        spec = types.SimpleNamespace(schemas=lambda: orchestrator.SCHEMAS)
        return {s["function"]["name"]: s for s in agent._session_schemas(self.session, spec)}

    def test_different_invalid_plans_stop_after_three_attempts_and_cannot_start_a_helper(self):
        for number in range(3):
            self.assertFalse(self.invalid(number).ok)
        self.assertNotIn("delegate", self.offered(), "changing arguments must not bypass the retry limit")
        self.assertTrue(orchestrator.starts_nothing("delegate", {"wave": reference_wave()}, self.turn),
                        "a blocked plan must not ask for approval to start helpers")
        with mock.patch.object(waves, "prepare") as prepare:
            result = self.execute()
        self.assertFalse(result.ok)
        prepare.assert_not_called()
        self.assertIn("directly", result.text)

    def test_quote_refusal_returns_exact_user_text_without_accepting_the_paraphrase(self):
        result = self.invalid(1)
        self.assertFalse(result.ok)
        self.assertIn(USER, result.text + result.model_note)
        self.assertIn("1 of 3", result.text + result.model_note)

    def test_bad_json_and_bad_argument_names_spend_the_same_retry_budget(self):
        self.result('{"wave":')
        self.result('{"wrong_argument": true}')
        self.invalid(3)
        self.assertNotIn("delegate", self.offered())

    def test_argument_inspection_outside_a_turn_does_not_spend_an_old_requests_budget(self):
        self.session["turn"] = None
        for _ in range(4):
            tools.execute("delegate", '{"wrong_argument": true}')
        self.assertFalse(orchestrator.planning_stopped(self.session))

    def test_an_ended_turn_does_not_relabel_an_unrelated_invalid_call_as_capped(self):
        original_title, _ = agent._describe(llm.ToolCall("unrelated", "delegate", "{}"))
        for number in range(3):
            self.invalid(number)
        self.session["turn"] = None
        title, _ = agent._describe(llm.ToolCall("unrelated", "delegate", "{}"))
        self.assertEqual(title, original_title, "ending a turn must not turn an unrelated call into a capped plan")

    def test_successful_preflight_does_not_erase_failed_attempts(self):
        self.invalid(1)
        self.assertTrue(self.execute(dry_run=True).ok)
        self.invalid(2)
        self.assertTrue(self.execute(dry_run=True).ok)
        self.invalid(3)
        self.assertNotIn("delegate", self.offered())

    def test_existing_helpers_can_still_be_collected_after_planning_stops(self):
        for number in range(3):
            self.invalid(number)
        self.turn.job = mock.Mock(stopped=False)
        with mock.patch.object(waves, "pending", return_value=True):
            self.assertEqual(self.offered()["delegate"], orchestrator.WAIT_SCHEMA)
            result = self.result('{"wait": true}')
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.then)

    def test_resume_keeps_the_limit_and_a_new_user_turn_gets_fresh_attempts(self):
        for number in range(3):
            self.invalid(number)
        self.session["turn"] = agent.Turn()
        self.assertNotIn("delegate", self.offered())
        self.session["messages"].append({"role": "user", "content": "Try the chair again"})
        self.assertIn("delegate", self.offered())

    def test_a_repaired_wave_starts_and_resets_the_budget(self):
        self.invalid(1)
        self.invalid(2)
        with mock.patch.object(orchestrator, "_snapshot", return_value={}), \
                mock.patch.object(waves, "prepare", return_value=types.SimpleNamespace(number=1)) as prepare:
            result = self.execute()
        self.assertTrue(result.ok, result.text)
        prepare.assert_called_once()
        self.assertIsNotNone(result.then)
        self.invalid(3)
        self.assertIn("delegate", self.offered())

    def test_collecting_does_not_reset_the_budget(self):
        self.invalid(1)
        self.invalid(2)
        self.turn.job = mock.Mock(stopped=False)
        with mock.patch.object(waves, "pending", return_value=True):
            self.assertTrue(self.result('{"wait": true}').ok)
        self.turn.job = None
        self.invalid(3)
        self.assertNotIn("delegate", self.offered())

    def test_ownership_conflicts_remain_refused_and_do_not_start_workers(self):
        wave = reference_wave()
        wave["workers"][0]["owns"] = ["Bottle"]
        with mock.patch.object(waves, "prepare") as prepare:
            result = self.execute(wave)
        self.assertFalse(result.ok)
        self.assertIn("shared.keep", result.text)
        prepare.assert_not_called()
        self.assertEqual(wave["shared"]["keep"]["names"], ["Bottle", "Box"])

    def test_account_limits_do_not_spend_the_plan_repair_budget(self):
        self.turn.roles.turn_workers = 1
        for _ in range(4):
            result = self.execute(dry_run=True)
            self.assertTrue(result.plan_capped, result.text)
        self.assertIn("delegate", self.offered())

    def test_router_does_not_send_direct_work_back_to_stopped_helper_planning(self):
        for number in range(3):
            self.invalid(number)
        with mock.patch.object(router, "nudge") as nudge, \
                mock.patch.object(agent, "_finish_gate", return_value=None):
            agent._orchestrator_final(self.session, self.turn, llm.Completion(text="Finished directly."))
        nudge.assert_not_called()

    def test_agent_continues_directly_after_refusals_and_tells_the_user_once(self):
        from loopcut.ui import layout
        self.session["busy"] = True
        self.turn.config = config.Config(api_key="k", base_url=config.SERVICE_BASE_URL, model="fast",
                                        reasoning_effort="low", auto_run=True, auto_look=False,
                                        max_steps=8, context_budget=40000)
        calls = []
        for number in range(3):
            wave = reference_wave()
            for worker in wave["workers"]:
                worker["requirements"] = [{"quote": f"invented quote {number}", "do": "build"}]
            calls.append(llm.Completion(tool_calls=[llm.ToolCall(str(number), "delegate", json.dumps(
                {"wave": wave, "dry_run": True}))]))
        calls += [llm.Completion(tool_calls=[llm.ToolCall("direct", "run_python", json.dumps(
            {"code": "print('built')", "summary": "Building the chair"}))]), llm.Completion(text="Built the chair.")]
        spec = agent.LoopSpec(role="orchestrator", system=lambda session: "", schemas=lambda: orchestrator.SCHEMAS,
                              save=lambda session: None, asks=False, checkpoints=False, changes_card=False,
                              scene_changed=lambda result: result.scene_after is not None)
        direct = mock.Mock(return_value=tools.ToolResult("Built the chair.", scene_before={}, scene_after={}))

        def run_tool(name, arguments):
            if name == "delegate":
                return tools.execute(name, arguments)
            self.assertNotIn("delegate", self.offered())
            drawn = layout.build(self.session, 900, 3000, 1.0, lambda f, s, t: len(t) * s * 0.5)
            self.assertFalse(any("Adjusting the helper plan" in p.get("text", "") for p in drawn["prims"]),
                             "a stopped plan must not still look like an active repair")
            return direct(name, arguments)

        with mock.patch.object(agent, "_request", side_effect=calls), mock.patch.object(agent, "_redraw"), \
                mock.patch.object(agent, "_note_level"), mock.patch.object(agent, "_record_level"):
            agent.run_loop(self.session, self.turn, spec, run_tool=run_tool)
        self.assertEqual(self.turn.outcome, "completed")
        direct.assert_called_once()
        notices = [i["text"] for i in self.session["items"] if i.get("kind") == "notice"]
        self.assertEqual(sum("Helper planning stopped" in n for n in notices), 1)
        self.assertTrue(any("Continue the remaining work directly" in m.get("content", "")
                            for m in self.session["messages"] if m.get("role") == "tool"))


class CompletionScopeTest(unittest.TestCase):
    def test_passing_existence_checks_do_not_certify_export_or_import_in_any_language(self):
        for request in ("Build the tower and export a GLB for Roblox",
                        "Сделай башню и экспортируй GLB для Roblox"):
            with self.subTest(request=request):
                rows = [("tower c1", {"kind": "exists", "name": "Tower", "quote": request}, {"status": "pass"})]
                block = orchestrator.verified_block(rows, requirements=[request])
                self.assertNotIn("1 of 1 covered", block)
                self.assertIn("selected checks only", block)
                self.assertEqual(orchestrator.checked_line(block, alone=True), "Checked: 1 of 1 pass")

    def test_a_runtime_footer_never_supplies_done_when_the_assistant_gave_no_summary(self):
        block = "Verified by Loopcut on this scene: 6 of 6 checks."
        self.assertEqual(orchestrator.checked_line(block, alone=True), "Checked: 6 of 6 pass")


if __name__ == "__main__":
    unittest.main()
