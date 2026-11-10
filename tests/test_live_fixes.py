"""Fixes from the first live run on #180966 (6 Oct): pre-approval crash, restore steps, record changes, replans."""
import tempfile
import unittest
from pathlib import Path

from agent_tester import gate
from agent_tester.config import ROOT
from agent_tester.coordinator import restore_pass
from agent_tester.ledger import Ledger
from agent_tester.models import CaseType, PlannedCase, Step, StepResult, TestPlan
from agent_tester.plan_checks import autofix, check_plan
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack

PACK = ROOT / "packs" / "savostore"


def threshold_plan():
    return TestPlan(task_id=180966, preapprovable_actions=["change_settings", "create_test_order"], cases=[
        PlannedCase(id="TC1", title="Admin threshold", type=CaseType.ADMIN_CONFIG, steps=[
            Step(text="Set threshold", app="crm", changes_data=True, action_type="change_settings")], expected="saved"),
        PlannedCase(id="TC2", title="Below threshold", steps=[Step(text="order", app="portal")], expected="optional"),
        PlannedCase(id="TC6", title="Snapshot", depends_on=["TC1"], steps=[
            Step(text="Change threshold", app="crm", changes_data=True, action_type="change_settings")], expected="kept"),
    ])


class PreapprovalCrash(unittest.TestCase):
    def test_gate_offers_change_settings_when_pack_allows(self):
        pol = from_pack(load_pack(PACK), "staging")
        out = []
        ok, pre, _corr = gate.plan_gate(threshold_plan(), input_fn=lambda _: "y", out=out.append, policy=pol)
        self.assertTrue(ok)
        self.assertIn("change_settings", pre)
        pol.preapprove(pre)                       # no PolicyViolation
        self.assertFalse(any("ask you before each" in o and "change_settings" in o for o in out))

    def test_autofix_keeps_change_settings_when_pack_allows(self):
        pack = load_pack(PACK)
        p = autofix(threshold_plan(), pack.config.get("preapprovable_actions", []))
        self.assertIn("change_settings", p.preapprovable_actions)

    def test_gate_asks_twice_when_plan_has_problems(self):
        prompts = []
        p = threshold_plan()
        gate.plan_gate(p, input_fn=lambda q: prompts.append(q) or "n", out=lambda *_: None,
                       issues=["TC1: admin case has no restore step"])
        self.assertTrue(any("anyway" in q for q in prompts))


class Restore(unittest.TestCase):
    def test_one_restore_after_last_settings_change(self):
        p = autofix(threshold_plan())
        tc6 = next(c for c in p.cases if c.id == "TC6")
        self.assertTrue(tc6.steps[-1].restores and tc6.steps[-1].inferred and tc6.steps[-1].app == "crm")
        tc1 = next(c for c in p.cases if c.id == "TC1")
        self.assertFalse(any(s.restores for s in tc1.steps))
        self.assertEqual([i for i in check_plan(p) if "restore" in i], [])

    def test_existing_restore_is_left_alone(self):
        p = threshold_plan()
        p.cases[2].steps.append(Step(text="restore", app="crm", changes_data=True, action_type="change_settings", restores=True))
        self.assertEqual(sum(s.restores for c in autofix(p).cases for s in c.steps), 1)

    def test_restore_pass_after_failed_case(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "180966-20261006-131500")
            st.set_plan(threshold_plan())
            led = Ledger(st)
            led.record_original("TC1", "crm", "Mandatory Shipment Protection Threshold (USD)", "150")
            seen = []

            def tester(app, brief, state):
                seen.append(brief)
                Ledger(state).record_restored(app, "Mandatory Shipment Protection Threshold (USD)", "150", True)
                return StepResult(case_id=brief["case_id"], app=app, steps_done=[1])

            left = restore_pass(st.plan, st, {"crm": tester}, led.unrestored())
            self.assertEqual(left, [])
            step = seen[0]["steps"][0]
            self.assertTrue(step["restores"] and step["action_type"] == "change_settings")
            self.assertIn("'150'", step["text"])


class Answers(unittest.TestCase):
    def test_user_answers_reach_the_planner_prompt(self):
        import asyncio
        from unittest import mock
        from agent_tester.agents import planner
        asks = []

        async def fake_run_json(agent, prompt, settings, **kw):
            asks.append(prompt)
            if kw.get("label") == "reviewer":
                return {"ok": True}
            return kw["validate"]({"cases": []})

        pack = load_pack(PACK)
        with mock.patch.object(planner, "run_json", fake_run_json), \
             mock.patch.object(planner, "build_planner", lambda *a, **k: None), \
             mock.patch.object(planner, "build_reviewer", lambda *a, **k: None):
            asyncio.run(planner.plan_task(None, pack, "task", 180966, "staging",
                                          answers={"data.client_exempt": "make sidney@ exempt in the CRM client record"}))
        self.assertIn("User answers at the plan gate", asks[0])
        self.assertIn("make sidney@ exempt", asks[0])

    def test_prompt_has_the_new_rules(self):
        from agent_tester.agents import prompts
        for phrase in ("Never make up amounts", "ONE restore step", "REOPEN", "change the record back"):
            self.assertIn(phrase, prompts.PLANNER)


if __name__ == "__main__":
    unittest.main()
