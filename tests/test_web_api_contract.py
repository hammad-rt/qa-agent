"""Offline stdlib regression: JSON fields the web UI JS reads must stay present and shaped."""
from __future__ import annotations

from agent_tester import testing_hermetic as hermetic  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.web import server as W


SUMMARY_KEYS = {
    "id", "task", "env", "status", "cases", "done", "counts", "current", "started",
    "elapsed_sec", "phase", "phase_steps", "pre_tasks", "attempt", "last", "mtime",
    "label", "report",
}
DETAIL_KEYS = {
    "summary", "plan", "results", "values", "checkpoints", "ledger", "action_log",
    "shots", "progress", "reports", "model", "mode", "data_plan", "phase",
    "attempt", "attempt_history", "data_reuse", "previous_attempts",
}
PHASE_KEYS = {"active", "steps", "pre_tasks", "current_case"}


def _fixture(root: Path) -> Path:
    d = root / "99-20261110-120000"
    d.mkdir()
    (d / "shots").mkdir()
    state = {
        "status": "paused",
        "attempt": 2,
        "plan": {
            "task_id": 99, "env": "staging",
            "cases": [
                {"id": "TC1", "title": "A", "steps": [{"text": "go", "app": "portal"}],
                 "expected": "ok", "type": "ui", "area": "orders"},
                {"id": "TC2", "title": "B", "steps": [{"text": "pay", "app": "portal"}],
                 "expected": "paid", "type": "ui", "area": "orders"},
            ],
        },
        "results": {"TC1": {"verdict": "PASS"}},
        "action_log": [
            {"seq": 1, "case_id": "DISCO-orders-portal", "app": "portal", "tool": "browser_navigate",
             "args": {}, "outcome": "ok"},
            {"seq": 2, "case_id": "CONTACT-1", "app": "crm", "tool": "browser_type",
             "args": {}, "outcome": "ok"},
            {"seq": 3, "case_id": "PREP-order-find", "app": "crm", "tool": "browser_click",
             "args": {}, "outcome": "ok"},
            {"seq": 4, "case_id": "TC1", "app": "portal", "tool": "browser_click",
             "args": {}, "outcome": "ok"},
        ],
        "checkpoints": [
            {"at": "2026-11-10T12:00:00", "label": "start"},
            {"at": "2026-11-10T12:10:00", "label": "paused"},
        ],
        "data_plan": [{"token": "ORDER-1", "find": "f", "create": [], "cases": ["TC1"]}],
        "data_reuse": {"ORDER-1": {"action": "reuse", "why": "ok"}},
        "attempt_history": [{"attempt": 1, "verdicts": {"TC1": {"verdict": "FAIL"}}}],
        "previous_attempts": {"TC1": [{"verdict": "FAIL"}]},
        "mode": {"subtasks": True},
        "values": {},
        "notes": [],
    }
    (d / "state.json").write_text(json.dumps(state))
    (d / "report.html").write_text("<html></html>")
    (d / "shots" / "a.png").write_bytes(b"x")
    return d


class WebApiContract(unittest.TestCase):
    def test_summary_detail_phase_and_guide_queue(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            d = _fixture(root)
            with mock.patch.object(W, "RUNS", root):
                s = W.run_summary(d)
                self.assertTrue(SUMMARY_KEYS.issubset(s.keys()), sorted(SUMMARY_KEYS - s.keys()))
                self.assertEqual(s["status"], "paused")
                self.assertEqual(s["attempt"], 2)
                self.assertIsInstance(s["phase_steps"], list)
                self.assertIsInstance(s["pre_tasks"], list)
                self.assertTrue(s["pre_tasks"], "expected DISCO/CONTACT/PREP pre_tasks from action_log")
                kinds = {p["kind"] for p in s["pre_tasks"]}
                self.assertTrue({"discovery", "contact", "prep"} <= kinds)

                detail = W.run_detail(d)
                self.assertTrue(DETAIL_KEYS.issubset(detail.keys()), sorted(DETAIL_KEYS - detail.keys()))
                self.assertEqual(detail["summary"]["id"], d.name)
                phase = detail["phase"]
                self.assertTrue(PHASE_KEYS.issubset(phase.keys()))
                self.assertTrue(any(st.get("label") for st in phase["steps"]))
                self.assertIsInstance(detail["data_plan"], list)
                self.assertEqual(detail["data_plan"][0]["token"], "ORDER-1")
                self.assertIsInstance(detail["attempt_history"], list)
                self.assertIsInstance(detail["previous_attempts"], dict)
                self.assertTrue(detail["shots"])

                # Offline guidance: store even when the run is not live.
                class H(W.Handler):
                    def __init__(self):
                        self.path = f"/api/runs/{d.name}/guide"
                        self.headers = {"Content-Length": "0"}
                        self._code = None
                        self._resp = None

                    def _body(self):
                        return {"text": "use Advanced Search"}

                    def _run(self, name):
                        return d if name == d.name else None

                    def _send(self, code, obj, **kw):
                        self._code, self._resp = code, obj
                        return None

                h = H()
                h.do_POST()
                self.assertEqual(h._code, 200)
                self.assertTrue(h._resp.get("ok"))
                self.assertTrue(h._resp.get("queued"))
                lines = (d / "guidance.jsonl").read_text().strip().splitlines()
                self.assertEqual(json.loads(lines[-1])["text"], "use Advanced Search")
                self.assertEqual(json.loads(lines[-1])["when"], "queued")


if __name__ == "__main__":
    unittest.main()
