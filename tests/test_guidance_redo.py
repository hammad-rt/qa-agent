"""Guidance that names finished cases must re-queue them (and dependents)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_tester import guidance_redo as G
from agent_tester.coordinator import run_plan
from agent_tester.models import CaseResult, CaseType, PlannedCase, Step, StepResult, TestPlan
from agent_tester.agents.tester import TesterSession
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



def _sp_plan():
    return TestPlan(task_id=183498, cases=[
        PlannedCase(id="TC2", title="Shipment Value read-only", area="shipping", type=CaseType.UI,
                    steps=[Step(text="Open a shipping only order and check Shipment Value", app="crm")]),
        PlannedCase(id="TC3", title="Parcel drives value", area="shipping", type=CaseType.UI,
                    steps=[Step(text="Change Parcel Value", app="crm")]),
        PlannedCase(id="TC4", title="Declined protection", area="shipping", type=CaseType.UI,
                    steps=[Step(text="Set protection to No", app="crm")]),
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

    def test_unconsumed_redo_note_listed(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.set_plan(_sp_plan())
            st.set_result(CaseResult(case_id="TC2", verdict="FAIL", note="editable"))
            st.set_result(CaseResult(case_id="TC3", verdict="PASS"))
            (st.dir / "guidance.jsonl").write_text(
                json.dumps({"text": "test tc2 again"}) + "\n")
            left = G.undelivered_guidance(st, st.plan)
            self.assertEqual(len(left), 1)
            self.assertEqual(left[0]["targets"], ["TC2"])
            self.assertIn("never consumed", left[0]["why"])
            G.report_undelivered(st, st.plan)
            self.assertTrue(any("test tc2 again" in n for n in st.data["notes"]))

    def test_redo_note_during_tc3_requeues_tc2_next(self):
        """TC2 finished FAIL; 'test tc2 again' arrives during TC3; TC2 runs next with the note."""
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.data["_skip_deploy_fetch"] = True
            plan = _sp_plan()
            st.set_plan(plan)
            order, redo_briefs = [], []

            def tester(app, brief, state):
                if brief.get("discovery"):
                    return StepResult(case_id=brief["case_id"], app=app, notes="mapped")
                cid = brief["case_id"]
                if cid == "TC2" and not (state.data.get("previous_attempts") or {}).get("TC2"):
                    order.append("TC2-first")
                    return StepResult(case_id="TC2", app=app, notes="first")
                if cid == "TC3":
                    (state.dir / "guidance.jsonl").write_text(
                        json.dumps({"text": "test tc2 again"}) + "\n")
                    sess = TesterSession.__new__(TesterSession)
                    sess.state = state
                    sess.case_id = "TC3"
                    sess.app = app
                    sess.result = StepResult(case_id="TC3", app=app)
                    self.assertIsNone(sess._new_guidance())
                    order.append("TC3")
                    return StepResult(case_id="TC3", app=app, notes="ok")
                if cid == "TC2":
                    order.append("TC2-redo")
                    redo_briefs.append(dict(brief))
                    return StepResult(case_id="TC2", app=app, notes="redo")
                order.append(cid)
                return StepResult(case_id=cid, app=app)

            def judge(brief, actions, results, state):
                cid = brief["case_id"]
                if cid == "TC2" and not (state.data.get("previous_attempts") or {}).get("TC2"):
                    return CaseResult(case_id="TC2", verdict="FAIL", note="editable")
                return CaseResult(case_id=cid, verdict="PASS")

            run_plan(plan, st, {"crm": tester}, judge)
            self.assertEqual(order, ["TC2-first", "TC3", "TC2-redo", "TC4"])
            self.assertTrue(redo_briefs)
            self.assertTrue(any("test tc2 again" in t.lower()
                                for t in (redo_briefs[0].get("redo_guidance") or [])))
            log = (st.dir / "progress.log").read_text() if (st.dir / "progress.log").exists() else ""
            notes = " ".join(st.data.get("notes") or [])
            self.assertTrue(
                "Redo of TC2 requested by guidance; it runs next" in log
                or "Redo of TC2 requested by guidance; it runs next" in notes)

    def test_stuck_ask_pauses_for_requested_redo(self):
        from agent_tester.preflight.pack_loader import load_pack
        from agent_tester.tools.policy import from_pack
        from agent_tester.tools.screenshots import Evidence
        pack = Path(__file__).resolve().parents[1] / "packs/savostore"
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            plan = _sp_plan()
            st.set_plan(plan)
            st.set_result(CaseResult(case_id="TC2", verdict="FAIL", note="editable"))
            pol = from_pack(load_pack(pack), "staging", approver=lambda *a: False)
            sess = TesterSession(
                "crm",
                {"case_id": "TC3", "title": "Parcel", "steps": [{"n": 1, "text": "edit", "app": "crm"}],
                 "area": "shipping"},
                st, pol, Evidence(st.dir), max_calls=30, max_minutes=5)
            sess.current_step = 1
            sess._explored = True
            (st.dir / "guidance.jsonl").write_text(
                json.dumps({"text": "test tc2 again"}) + "\n")
            out = sess._maybe_stuck_ask(why="looped", name="browser_click", repeats=4)
            self.assertTrue(sess.finished)
            self.assertTrue(sess.result.stuck)
            self.assertIn("paused for requested redo", sess.result.notes)
            self.assertIn("TC2", sess.result.notes)
            self.assertIn("paused for requested redo", out["error"])
            self.assertTrue(any(
                "Redo of TC2 requested by guidance; it runs next" in n
                for n in (st.data.get("notes") or [])))
            queued = G.apply_redo(st, plan)
            self.assertEqual(queued[0], "TC2")
            self.assertIsNone(st.result("TC2"))
            self.assertTrue(any("test tc2 again" in t
                                for t in (st.data.get("case_guidance") or {}).get("TC2") or []))




if __name__ == "__main__":
    unittest.main()
