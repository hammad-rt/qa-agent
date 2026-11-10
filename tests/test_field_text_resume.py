"""Regression: list-valued case fields must not crash change-text / resume discovery."""
from __future__ import annotations

from agent_tester import testing_hermetic as hermetic  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tester.coordinator import _discovery_change_text
from agent_tester.discovery_map import ticket_case_blob
from agent_tester.field_text import field_text
from agent_tester.models import CaseType, PlannedCase, Step, TestPlan
from agent_tester.run_state import RunState


class FieldTextHelper(unittest.TestCase):
    def test_flattens_nested_lists_and_reads_text(self):
        self.assertEqual(field_text(None), "")
        self.assertEqual(field_text(""), "")
        self.assertEqual(field_text("hi"), "hi")
        self.assertEqual(field_text(["a", "b"]), "a\nb")
        self.assertEqual(field_text(["a", ["b", None, ""], "c"], sep=" "), "a b c")
        self.assertEqual(field_text({"text": ["step", "two"]}), "step\ntwo")
        self.assertEqual(field_text(SimpleNamespace(text="from obj")), "from obj")
        self.assertEqual(field_text(3), "3")


class TicketCaseBlobLists(unittest.TestCase):
    def test_list_expected_preconditions_and_step_dicts(self):
        plan = SimpleNamespace(description=None, subject=None, title=None, summary=None,
                               notes=None, ticket_text=None)
        case = SimpleNamespace(
            title="Create order",
            expected=["Order saved", "Status is New"],
            preconditions=["Logged in", ["On staging", None]],
            notes=["watch stock"],
            acceptance=None,
            steps=[
                {"text": ["Open Orders", "Click New"]},
                SimpleNamespace(text="Fill customer"),
                {"text": "Save"},
            ],
        )
        blob = ticket_case_blob(plan, case)
        self.assertIn("Create order", blob)
        self.assertIn("Order saved", blob)
        self.assertIn("Status is New", blob)
        self.assertIn("Logged in", blob)
        self.assertIn("On staging", blob)
        self.assertIn("Open Orders", blob)
        self.assertIn("Click New", blob)
        self.assertIn("Fill customer", blob)
        self.assertIn("Save", blob)
        self.assertIn("watch stock", blob)
        self.assertIsInstance(blob, str)
        self.assertEqual(blob, "\n".join([
            "Create order",
            "Order saved\nStatus is New",
            "Logged in\nOn staging",
            "watch stock",
            "Open Orders\nClick New",
            "Fill customer",
            "Save",
        ]))


class ResumeChangeTextPath(unittest.TestCase):
    def test_state_json_plan_with_preconditions_feeds_change_text(self):
        """Real resume shape: plan in state.json → PlannedCase (preconditions list[str]) → change text."""
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "42-20260101-120000"
            run.mkdir()
            state_path = run / "state.json"
            payload = {
                "status": "paused",
                "approved": True,
                "plan": {
                    "task_id": 42,
                    "env": "staging",
                    "cases_source": "task",
                    "cases": [{
                        "id": "TC1",
                        "title": "Portal checkout",
                        "type": "ui",
                        "area": "orders",
                        "preconditions": [
                            "Logged in to the portal",
                            "Test customer has a wallet balance",
                        ],
                        "depends_on": [],
                        "steps": [
                            {"text": "Open the cart", "app": "portal"},
                            {"text": "Place the order", "app": "portal"},
                        ],
                        "expected": "Order confirmation is shown",
                        "evidence_required": [],
                        "values_to_record": [],
                        "conflicts": [],
                        "generated": False,
                    }],
                    "requirements": [],
                    "global_conflicts": [],
                    "data_to_create": [],
                    "preapprovable_actions": [],
                    "baseline_steps": [],
                },
                "results": {},
                "values": {},
                "action_log": [],
                "checkpoints": [],
                "notes": [],
                "ticket_texts": ["Ticket mentions redesigned Orders screen"],
                "attempt": 1,
            }
            state_path.write_text(json.dumps(payload))
            state = RunState(run)
            plan = state.plan
            self.assertIsInstance(plan, TestPlan)
            case = plan.cases[0]
            self.assertIsInstance(case.preconditions, list)
            self.assertTrue(case.preconditions)

            # This is the resume crash site: ensure_discovery → _discovery_change_text → ticket_case_blob.
            text = _discovery_change_text(plan, case, state)
            self.assertIn("Portal checkout", text)
            self.assertIn("Logged in to the portal", text)
            self.assertIn("Order confirmation is shown", text)
            self.assertIn("redesigned Orders screen", text)

            # Typed PlannedCase path (same list preconditions) also stays join-safe.
            typed = PlannedCase(
                id="TC2", title="CRM view", type=CaseType.UI, area="orders",
                preconditions=["Admin session"], steps=[Step(text="Open Accounts", app="crm")],
                expected="List loads")
            self.assertIn("Admin session", ticket_case_blob(plan, typed))


if __name__ == "__main__":
    unittest.main()
