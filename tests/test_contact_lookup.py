"""Contact lookup before mutating prep: run-state id, search ladder, ask_user over stuck."""
from __future__ import annotations

import unittest
from types import SimpleNamespace as N
from unittest import mock

from agent_tester import data_prep as D
from agent_tester.agents.tester import EXPLORE_TEXT, EXPLORE_TEXT_ASK, TesterSession
from agent_tester.coordinator import run_plan
from agent_tester.models import CaseResult, CaseType, PlannedCase, Requirement, Step, StepResult, TestPlan
from agent_tester.run_state import RunState
from pathlib import Path
import tempfile


RULE = {
    "id": "o", "value": "test_order", "app": "crm", "area": "rewards",
    "find": "find for {client}", "create": "make for {client}",
    "client_login_app": "portal",
    "when": {"areas": ["rewards"], "any": ["invoice"]},
}


class State:
    def __init__(self):
        self.data, self.values, self.points = {"action_log": []}, {}, []

    def save(self):
        pass

    def checkpoint(self, m, **k):
        self.points.append(m)


def case():
    return PlannedCase(id="E0", title="Open the invoice and check wallet", area="rewards",
                       steps=[Step(text="open invoice", app="crm")], type=CaseType.UI)


class ContactLookup(unittest.TestCase):
    def test_ladder_text_prefers_ask_user(self):
        text = D.contact_lookup_brief_text("user@example.com")
        self.assertIn("ask_user", text)
        self.assertIn("Advanced Search", text)
        self.assertIn("global search", text)
        self.assertIn("user@example.com", text)
        self.assertIn(D.LOOKUP_VALUE, text)
        self.assertIn("finish(stuck=True)", text)
        self.assertIn("does not answer", text)

    def test_client_label_prefers_record_id(self):
        lab = D.client_label("Ignored Name", "user@example.com", "abc-123")
        self.assertIn("abc-123", lab)
        self.assertIn("user@example.com", lab)
        self.assertNotIn("Ignored Name", lab)
        self.assertIn("record id", lab.lower())

    def test_ensure_contact_stores_id_in_run_state_only(self):
        st = State()
        plan = N(task_id=1, env="staging",
                 requirements=[Requirement(key="account.portal.customer", kind="account", value="u@x.com")])
        briefs = []

        def tester(app, brief, state):
            briefs.append(brief)
            self.assertTrue(brief.get("contact_lookup"))
            self.assertTrue(brief.get("prefer_ask_user"))
            self.assertTrue(brief.get("read_only"))
            self.assertIn("ask_user", brief["steps"][0]["text"])
            return N(values={"crm_contact_id": "rec-7", "crm_contact_email": "u@x.com"},
                     notes="found", stuck=False, limit_hit=False)

        r = D.ensure_contact(plan, case(), st, {"crm": tester}, [RULE])
        self.assertIsNone(r)
        self.assertEqual(st.data["contact"]["record_id"], "rec-7")
        self.assertEqual(st.data["contact"]["email"], "u@x.com")
        self.assertEqual(st.values["crm_contact_id"]["value"], "rec-7")
        self.assertTrue(briefs[0]["contact_lookup"])
        self.assertIn("ask_user", briefs[0]["steps"][0]["text"])
        # Never taught as a pack fact / learned line from this helper.
        self.assertNotIn("learned", str(st.data.get("contact")))

    def test_failed_lookup_with_ask_user_no_answer_blocks(self):
        st = State()
        st.data["action_log"] = [{
            "case_id": "CONTACT-E0", "tool": "ask_user",
            "args": {"question": "which contact?", "answer": ""},
        }]
        plan = N(task_id=1, env="staging",
                 requirements=[Requirement(key="account.portal.customer", kind="account", value="u@x.com")])

        def tester(app, brief, state):
            return N(values={"crm_contact_id": "none"}, notes="not found", stuck=True, limit_hit=False)

        r = D.ensure_contact(plan, case(), st, {"crm": tester}, [RULE])
        self.assertEqual(r.verdict, "BLOCKED")
        self.assertIn("no answer", r.note)
        self.assertTrue(st.data["contact"]["failed"])
        self.assertTrue(st.data["contact"]["asked"])

    def test_prep_uses_resolved_contact_id(self):
        st = State()
        st.data["contact"] = {"email": "u@x.com", "record_id": "rec-9", "failed": False}
        plan = N(env="staging", requirements=[N(key="account.portal.customer", value="u@x.com")])
        calls = []

        def tester(app, brief, state):
            calls.append(brief["steps"][0]["text"])
            return N(values={"test_order": "A1", "test_order_record": "r1"}, notes="", stuck=False, limit_hit=False)

        with mock.patch("agent_tester.coordinator.brief_for",
                        side_effect=lambda p, c, s, st: {"case_id": c.id, "steps": [{"text": c.steps[0].text}]}):
            r = D._ensure_one(plan, case(), st, {"crm": tester}, dict(RULE))
        self.assertIsNone(r)
        self.assertIn("rec-9", calls[0])
        self.assertIn("record id", calls[0].lower())

    def test_explore_prefers_ask_on_contact_lookup(self):
        self.assertIn("ask_user", EXPLORE_TEXT_ASK)
        self.assertNotEqual(EXPLORE_TEXT, EXPLORE_TEXT_ASK)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = RunState(Path(tmp.name) / "run")
        from agent_tester.tools.policy import Policy
        from agent_tester.tools.screenshots import Evidence
        brief = {"case_id": "CONTACT-E0", "contact_lookup": True, "prefer_ask_user": True,
                 "steps": [{"n": 1, "text": "find", "app": "crm"}], "requirements": {}}
        s = TesterSession("crm", brief, state, Policy(allow_hosts=[], risky_patterns=[],
                                                     preapprovable=[], always_ask=[], env="staging"),
                          Evidence(state.dir), max_calls=20, max_minutes=5)
        out = s._start_exploring("browser_click", "no progress")
        self.assertIn("ask_user", out["error"])
        self.assertIn("do not answer", out["error"])


class ContactBeforePrepOrder(unittest.TestCase):
    def test_lookup_before_prep_in_run_plan(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = RunState(Path(tmp.name) / "run")
        order = []
        c = PlannedCase(id="E0", title="check invoice rewards", area="rewards", type=CaseType.UI,
                        steps=[Step(text="open invoice", app="crm")])
        plan = TestPlan(task_id=1, cases=[c], requirements=[
            Requirement(key="account.portal.customer", kind="account", value="u@x.com")])

        def tester(app, brief, state):
            if brief.get("discovery"):
                order.append("discovery")
                return StepResult(case_id=brief["case_id"], app=app, notes="mapped contacts list")
            if brief.get("contact_lookup"):
                order.append("contact")
                return StepResult(case_id=brief["case_id"], app=app,
                                  values={"crm_contact_id": "42", "crm_contact_email": "u@x.com"})
            if brief.get("data_prep"):
                order.append("prep")
                return StepResult(case_id=brief["case_id"], app=app,
                                  values={"test_order": "A1", "test_order_record": "r1"})
            order.append("case")
            return StepResult(case_id=brief["case_id"], app=app)

        judge = lambda b, a, r, s: CaseResult(case_id=b["case_id"], verdict="PASS")
        run_plan(plan, state, {"crm": tester}, judge, data_prep=[RULE])
        self.assertEqual(order[:3], ["discovery", "contact", "prep"])
        self.assertIn("case", order)
        self.assertEqual(state.data["contact"]["record_id"], "42")


if __name__ == "__main__":
    unittest.main()
