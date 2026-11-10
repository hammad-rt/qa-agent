"""Guidance that names finished cases must re-queue them (and dependents)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_tester import guidance_redo as G
from agent_tester.models import CaseResult, CaseType, PlannedCase, Step, TestPlan
from agent_tester.run_state import RunState


def _plan():
    return TestPlan(task_id=1, cases=[
        PlannedCase(id="E1", title="Earn", area="rewards", type=CaseType.UI,
                    steps=[Step(text="Pay ORDER-EARN", app="portal")], depends_on=[]),
        PlannedCase(id="E2", title="After earn", area="rewards", type=CaseType.UI,
                    steps=[Step(text="Check wallet", app="portal")], depends_on=["E1"]),
        PlannedCase(id="B9", title="Other", area="rewards", type=CaseType.UI,
                    steps=[Step(text="Open ORDER-MULTI", app="portal")]),
    ])


class RedoDetect(unittest.TestCase):
    def test_redo_intent_multilingual(self):
        self.assertTrue(G.is_redo_intent("Re do E1, E2 from the portal side"))
        self.assertTrue(G.is_redo_intent("rerun E1 please"))
        self.assertTrue(G.is_redo_intent("encore E1"))
        self.assertFalse(G.is_redo_intent("before E1 set the earn % to 80"))

    def test_pending_redo_when_cases_finished(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.set_plan(_plan())
            st.set_result(CaseResult(case_id="E1", verdict="BLOCKED", note="no data"))
            st.set_result(CaseResult(case_id="E2", verdict="BLOCKED", note="depends"))
            (st.dir / "guidance.jsonl").write_text(
                json.dumps({"text": "Re do E1, E2 from the portal side"}) + "\n")
            hit = G.pending_redo(st, st.plan)
            self.assertIsNotNone(hit)
            self.assertEqual(set(hit["cases"]), {"E1", "E2"})

    def test_ambiguous_finished_only_is_redo(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.set_plan(_plan())
            st.set_result(CaseResult(case_id="E1", verdict="BLOCKED"))
            (st.dir / "guidance.jsonl").write_text(
                json.dumps({"text": "E1 from the portal side"}) + "\n")
            hit = G.pending_redo(st, st.plan)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["cases"], ["E1"])

    def test_apply_redo_reopens_dependents_once(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            plan = _plan()
            st.set_plan(plan)
            st.set_result(CaseResult(case_id="E1", verdict="BLOCKED"))
            st.set_result(CaseResult(case_id="E2", verdict="BLOCKED"))
            st.data["placeholders"] = {
                "ORDER-EARN": {"value": "A1", "value_key": "order_earn", "prep_id": "shipping_only_order"},
            }
            st.data["prep"] = {"shipping_only_order": {"value": "A1", "consumed": False}}
            (st.dir / "guidance.jsonl").write_text(
                json.dumps({"text": "redo E1"}) + "\n")
            pack = {"placeholders": {"items": [
                {"token": "ORDER-EARN", "mutating": True, "value": "order_earn"}]}}
            queued = G.apply_redo(st, plan, pack)
            self.assertEqual(queued, ["E1", "E2"])
            self.assertIsNone(st.result("E1"))
            self.assertEqual(st.data["previous_attempts"]["E1"][0]["verdict"], "BLOCKED")
            self.assertNotIn("ORDER-EARN", st.data.get("placeholders") or {})
            # Same note must not redo twice.
            self.assertIsNone(G.apply_redo(st, plan, pack))

    def test_undelivered_listed(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.set_plan(_plan())
            st.set_result(CaseResult(case_id="E1", verdict="PASS"))
            st.set_result(CaseResult(case_id="E2", verdict="PASS"))
            st.set_result(CaseResult(case_id="B9", verdict="PASS"))
            (st.dir / "guidance.jsonl").write_text(
                json.dumps({"text": "E1 needs a different approach"}) + "\n")
            # Not marked as redo-done and never delivered via action_log.
            left = G.undelivered_guidance(st, st.plan)
            self.assertEqual(len(left), 1)
            G.report_undelivered(st, st.plan)
            self.assertTrue(any("Undelivered" in n for n in st.data["notes"]))


if __name__ == "__main__":
    unittest.main()
