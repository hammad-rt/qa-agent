"""Same-run rerun: attempt archive + data-plan reverify (reuse vs recreate)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_tester.models import PlannedCase, Step, TestPlan
from agent_tester.rerun_prep import (
    criteria_still_match,
    expand_rerun_queue,
    prepare_same_run_rerun,
    reverify_data_plan,
)
from agent_tester.run_state import RunState


PACK = {
    "placeholders": {
        "token_pattern": r"(?i)\b(?:ORDER|CONTACT)-[A-Z0-9_-]+\b",
        "items": [
            {
                "placeholder": "ORDER-A",
                "description": "order A",
                "find_app": "crm",
                "find": {"module": "AOS_Orders", "min_value": 10},
                "create": [{"app": "portal", "text": "create order A"}],
                "value": "order_a",
                "mutating": True,
            },
            {
                "placeholder": "CONTACT-1",
                "description": "contact",
                "find_app": "crm",
                "find": {"email": "c@example.com"},
                "lookup": "contact_by_email",
                "create": [],
                "value": "contact_id",
                "mutating": False,
            },
        ]
    }
}


def _plan():
    return TestPlan(task_id=1, cases=[
        PlannedCase(id="TC1", title="setup", area="a",
                    steps=[Step(text="Use ORDER-A", app="portal")], expected="ok"),
        PlannedCase(id="TC2", title="pay", area="a", depends_on=["TC1"],
                    steps=[Step(text="Pay ORDER-A for CONTACT-1", app="portal")], expected="ok"),
        PlannedCase(id="TC3", title="other", area="a",
                    steps=[Step(text="noop", app="portal")], expected="ok"),
    ])


class ArchiveAttempt(unittest.TestCase):
    def test_archive_copies_results_and_bumps_attempt(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState.new(Path(d), 1)
            st.data["results"] = {"TC1": {"case_id": "TC1", "verdict": "PASS"}}
            (st.dir / "report.html").write_text("<html>ok</html>")
            (st.dir / "shots").mkdir()
            (st.dir / "shots" / "a.png").write_bytes(b"png")
            (st.dir / "trace.jsonl").write_text("{}\n")
            n = st.archive_attempt()
            self.assertEqual(n, 1)
            self.assertEqual(st.data["attempt"], 2)
            arch = st.dir / "attempts" / "1"
            self.assertTrue((arch / "results.json").is_file())
            meta = json.loads((arch / "results.json").read_text())
            self.assertEqual(meta["verdicts"]["TC1"]["verdict"], "PASS")
            self.assertTrue((arch / "report.html").is_file())
            self.assertTrue((arch / "shots" / "a.png").is_file())
            self.assertTrue((arch / "trace.jsonl").is_file())
            self.assertEqual(st.data["attempt_history"][0]["attempt"], 1)


class CriteriaMatch(unittest.TestCase):
    def test_consumed_forces_recreate(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.data["placeholders"] = {"ORDER-A": {"value": "99", "prep_id": "order_a"}}
            st.data["prep"] = {"order_a": {"value": "99", "consumed": True}}
            ok, why = criteria_still_match(st, "ORDER-A", {"id": "order_a", "min_value": 1})
            self.assertFalse(ok)
            self.assertIn("consumed", why)

    def test_min_value_floor(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.data["placeholders"] = {"ORDER-A": {"value": "5", "prep_id": "order_a"}}
            st.data["prep"] = {"order_a": {"value": "5"}}
            ok, why = criteria_still_match(st, "ORDER-A", {"id": "order_a", "min_value": 10})
            self.assertFalse(ok)
            self.assertIn("min_value", why)

    def test_lookup_mismatch_recreates(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.data["placeholders"] = {"CONTACT-1": {"value": "old-id", "prep_id": "contact_1"}}
            st.data["prep"] = {"contact_1": {"value": "old-id"}}
            rule = {"id": "contact_1", "find": {"email": "c@example.com"}, "lookup": "contact_by_email"}

            def lookup(_rule, _crit):
                return {"value": "new-id", "record_id": "new-id"}

            ok, why = criteria_still_match(st, "CONTACT-1", rule, lookup=lookup)
            self.assertFalse(ok)
            self.assertIn("no longer matches", why)

    def test_lookup_confirm_reuses(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.data["placeholders"] = {"CONTACT-1": {"value": "same", "prep_id": "contact_1"}}
            st.data["prep"] = {"contact_1": {"value": "same"}}
            rule = {"id": "contact_1", "find": {"email": "c@example.com"}}

            def lookup(_rule, _crit):
                return {"value": "same", "record_id": "same"}

            ok, why = criteria_still_match(st, "CONTACT-1", rule, lookup=lookup)
            self.assertTrue(ok)
            self.assertIn("lookup confirmed", why)


class ReverifyAndQueue(unittest.TestCase):
    def test_reverify_invalidates_recreate_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.data["placeholders"] = {
                "ORDER-A": {"value": "1", "prep_id": "order_a", "value_key": "order_a"},
                "CONTACT-1": {"value": "c1", "prep_id": "contact_1", "value_key": "contact_id"},
            }
            st.data["prep"] = {
                "order_a": {"value": "1", "consumed": True},
                "contact_1": {"value": "c1"},
            }
            st.data["values"] = {"order_a": {"value": "1"}, "contact_id": {"value": "c1"}}
            plan = _plan()
            out = reverify_data_plan(st, plan, PACK, case_ids=["TC1", "TC2"])
            self.assertEqual(out["ORDER-A"]["action"], "recreate")
            self.assertEqual(out["CONTACT-1"]["action"], "reuse")
            self.assertNotIn("ORDER-A", st.data["placeholders"])
            self.assertIn("CONTACT-1", st.data["placeholders"])

    def test_lookup_criteria_on_reverify(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.data["placeholders"] = {
                "ORDER-A": {"value": "50", "prep_id": "order_a"},
                "CONTACT-1": {"value": "gone", "prep_id": "contact_1"},
            }
            st.data["prep"] = {
                "order_a": {"value": "50"},
                "contact_1": {"value": "gone"},
            }
            plan = _plan()

            def lookup(rule, crit):
                tok = (rule.get("token") or rule.get("name") or "").upper()
                # CONTACT find fails; ORDER passes
                if "email" in crit or rule.get("lookup") == "contact_by_email":
                    return {}
                return {"value": "50"}

            # rule_from_item sets token — lookup sees find email on CONTACT
            out = reverify_data_plan(st, plan, PACK, case_ids=["TC2"], lookup=lookup)
            self.assertEqual(out["CONTACT-1"]["action"], "recreate")
            self.assertEqual(out["ORDER-A"]["action"], "reuse")

    def test_expand_queue_pulls_dependents_and_recreate_cases(self):
        plan = _plan()
        # Only TC3 requested, but ORDER-A recreate should pull TC1/TC2; TC1 roots pull TC2 via depends_on
        q = expand_rerun_queue(plan, ["TC1"], {
            "ORDER-A": {"action": "recreate", "cases": ["TC1", "TC2"]},
        })
        self.assertEqual(q, ["TC1", "TC2"])

    def test_prepare_same_run_archives_and_reopens(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState.new(Path(d), 1)
            plan = _plan()
            st.set_plan(plan)
            st.data["approved"] = True
            st.data["results"] = {
                "TC1": {"case_id": "TC1", "verdict": "PASS"},
                "TC2": {"case_id": "TC2", "verdict": "FAIL"},
                "TC3": {"case_id": "TC3", "verdict": "PASS"},
            }
            st.data["placeholders"] = {
                "ORDER-A": {"value": "50", "prep_id": "order_a", "value_key": "order_a"},
            }
            st.data["prep"] = {"order_a": {"value": "50", "consumed": True}}
            summary = prepare_same_run_rerun(st, plan, PACK, ["TC2"], archive=True)
            self.assertEqual(summary["archived_attempt"], 1)
            self.assertEqual(summary["attempt"], 2)
            self.assertIn("TC2", summary["cases"])
            self.assertIn("TC1", summary["cases"])  # recreate ORDER-A + depends
            self.assertNotIn("TC2", st.data["results"])
            self.assertEqual(st.data["previous_attempts"]["TC2"][0]["verdict"], "FAIL")
            self.assertEqual(st.data["data_reuse"]["ORDER-A"]["action"], "recreate")
            self.assertTrue((st.dir / "attempts" / "1" / "results.json").is_file())


if __name__ == "__main__":
    unittest.main()
