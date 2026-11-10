"""STUCK_MAX_ATTEMPTS cap → lookup ladder; preprod card pay; M-Pesa phone gate."""
from __future__ import annotations

from agent_tester import testing_hermetic as hermetic  # noqa: F401

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.agents.tester import STUCK_MAX_ATTEMPTS, TesterSession
from agent_tester.coordinator import _run_case
from agent_tester.data_prep import (
    _create_parts_for_env, case_calls_for_mpesa, mpesa_gate, mpesa_phone,
)
from agent_tester.models import CaseType, PlannedCase, Step, TestPlan
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence
from agent_tester.preflight.pack_loader import load_pack


PACK = Path(__file__).resolve().parents[1] / "packs" / "savostore"


class StuckMaxAttempts(unittest.TestCase):
    def test_env_default_is_three(self):
        self.assertEqual(STUCK_MAX_ATTEMPTS, max(1, int(os.environ.get("STUCK_MAX_ATTEMPTS", "3"))))

    def test_third_repeat_goes_to_ladder_not_longer_retry(self):
        st = RunState(Path(tempfile.mkdtemp()) / "run")
        pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
        s = TesterSession(
            "portal",
            {"case_id": "TC1", "title": "t", "steps": [{"n": 1, "text": "x", "app": "portal"}]},
            st, pol, Evidence(st.dir), max_calls=40, max_minutes=5)
        outs = [s._budget_check("browser_click", {"element": "Continue", "ref": "e1"})
                for _ in range(STUCK_MAX_ATTEMPTS)]
        self.assertTrue(any(o and "Explore before you act" in o.get("error", "") for o in outs))
        self.assertTrue(any(a.get("tool") in ("stuck_lookup", "explore") for a in st.data["action_log"]))
        self.assertFalse(s.result.loop)


class PayEnvs(unittest.TestCase):
    def test_preprod_card_allowed_production_blocked(self):
        parts = [{"app": "portal", "text": "pay with Stripe 4242 for Client"}]
        plan_pre = TestPlan(task_id=1, env="preprod")
        plan_prod = TestPlan(task_id=1, env="prod")
        self.assertEqual(_create_parts_for_env(plan_pre, {}, parts), parts)
        err = _create_parts_for_env(plan_prod, {}, parts)
        self.assertIsInstance(err, str)
        self.assertIn("payment", err.lower())


class MpesaGate(unittest.TestCase):
    def test_detects_case_and_phone(self):
        c = PlannedCase(
            id="TC1", title="Pay by M-Pesa", type=CaseType.UI,
            steps=[Step(text="Pay the invoice with M-Pesa phone +254700111222", app="portal")],
            expected="Paid")
        self.assertTrue(case_calls_for_mpesa(c))
        self.assertEqual(mpesa_phone(case=c), "+254700111222")
        self.assertIsNone(mpesa_gate(case=c))

    def test_no_phone_blocks(self):
        c = PlannedCase(
            id="TC1", title="Pay by M-Pesa", type=CaseType.UI,
            steps=[Step(text="Complete payment with M-Pesa", app="portal")],
            expected="Paid")
        why = mpesa_gate(case=c)
        self.assertIsNotNone(why)
        self.assertRegex(why.lower(), r"phone|m-?pesa")

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


if __name__ == "__main__":
    unittest.main()
