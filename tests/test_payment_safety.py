"""Preprod card pay, production payment refusal, and the M-Pesa phone gate."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tester.coordinator import _run_case
from agent_tester.data_prep import (
    _create_parts_for_env, case_calls_for_mpesa, mpesa_gate, mpesa_phone,
)
from agent_tester.facts_pick import safety_rules_text
from agent_tester.models import CaseType, PlannedCase, Step, TestPlan
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState


PACK = Path(__file__).resolve().parents[1] / "packs" / "savostore"


class PayEnvs(unittest.TestCase):
    def test_preprod_card_allowed_production_blocked(self):
        parts = [{"app": "portal", "text": "pay with Stripe 4242 for Client"}]
        plan_pre = TestPlan(task_id=1, env="preprod")
        plan_local = TestPlan(task_id=1, env="local")
        plan_prod = TestPlan(task_id=1, env="prod")
        self.assertEqual(_create_parts_for_env(plan_pre, {}, parts), parts)
        self.assertEqual(_create_parts_for_env(plan_local, {}, parts), parts)
        err = _create_parts_for_env(plan_prod, {}, parts)
        self.assertIsInstance(err, str)
        self.assertIn("payment", err.lower())

    def test_do_not_pay_create_is_kept_on_production(self):
        parts = [{"app": "crm", "text": "Add a parcel. Do not pay and do not apply rewards."}]
        plan_prod = TestPlan(task_id=1, env="prod")
        self.assertEqual(_create_parts_for_env(plan_prod, {}, parts), parts)


class MpesaGate(unittest.TestCase):
    def test_detects_case_and_phone(self):
        c = PlannedCase(
            id="TC1", title="Pay by M-Pesa", type=CaseType.UI,
            steps=[Step(text="Pay the invoice with M-Pesa phone +254700111222", app="portal")],
            expected="Paid")
        self.assertTrue(case_calls_for_mpesa(c))
        self.assertEqual(mpesa_phone(case=c), "+254700111222")
        self.assertIsNone(mpesa_gate(case=c))

    def test_generic_card_pay_is_not_mpesa(self):
        c = PlannedCase(
            id="TC1", title="Pay by card", type=CaseType.UI,
            steps=[Step(text="Pay the invoice with the Stripe test card", app="portal")],
            expected="Paid")
        self.assertFalse(case_calls_for_mpesa(c))
        self.assertIsNone(mpesa_gate(case=c))

    def test_no_phone_blocks(self):
        c = PlannedCase(
            id="TC1", title="Pay by M-Pesa", type=CaseType.UI,
            steps=[Step(text="Complete payment with M-Pesa", app="portal")],
            expected="Paid")
        why = mpesa_gate(case=c)
        self.assertIsNotNone(why)
        self.assertRegex(why.lower(), r"phone|m-?pesa")

    def test_mpesa_create_blocked_without_phone(self):
        parts = [{"app": "portal", "text": "pay with M-Pesa for the test client"}]
        err = _create_parts_for_env(TestPlan(task_id=1, env="staging"), {}, parts)
        self.assertIsInstance(err, str)
        self.assertRegex(err.lower(), r"m-?pesa|phone")

    def test_mpesa_create_allowed_with_phone(self):
        parts = [{"app": "portal", "text": "pay with M-Pesa phone +254712345678 for the test client"}]
        out = _create_parts_for_env(TestPlan(task_id=1, env="staging"), {}, parts)
        self.assertEqual(out, parts)

    def test_run_case_skips_without_phone(self):
        plan = TestPlan(task_id=1, env="staging", cases=[])
        case = PlannedCase(
            id="TC9", title="M-Pesa checkout", type=CaseType.UI,
            steps=[Step(text="Pay with M-Pesa", app="portal")], expected="ok")
        st = RunState(Path(tempfile.mkdtemp()) / "run")
        st.set_plan(plan)
        r = _run_case(plan, case, st, {"portal": object()}, judge=None, max_retries=0)
        self.assertEqual(r.verdict, "SKIPPED")
        self.assertRegex((r.note or "").lower(), r"phone|m-?pesa")


class PackSafetyRules(unittest.TestCase):
    def test_savostore_safety_rules_cover_card_and_mpesa(self):
        text = safety_rules_text(load_pack(PACK))
        self.assertIn("4242", text)
        self.assertIn("preprod", text)
        self.assertRegex(text.lower(), r"m-?pesa")
        self.assertIn("production", text.lower())

    def test_empty_when_pack_has_none(self):
        self.assertEqual(safety_rules_text(SimpleNamespace(config={})), "")


if __name__ == "__main__":
    unittest.main()
