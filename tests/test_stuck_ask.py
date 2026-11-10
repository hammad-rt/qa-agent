"""Loop/no-progress stop asks the user after explore (when someone can answer)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.agents.tester import TesterSession, STUCK_ASK_MAX_PER_CASE
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence

PACK = Path(__file__).resolve().parents[1] / "packs/savostore"


class StuckAsk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = RunState(Path(self.tmp.name) / "run")
        pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
        self.s = TesterSession(
            "crm",
            {"case_id": "E1", "title": "Earn", "steps": [{"n": 1, "text": "pay", "app": "crm"}],
             "area": "rewards"},
            self.st, pol, Evidence(self.st.dir), max_calls=30, max_minutes=5)
        self.s.current_step = 1
        self.s._explored = True
        self.s._recent = ["click:Save", "click:Save"]
        self.s.last_snapshot = "Page URL: https://example.test/index.php?module=AOS_Invoices\nSave"

    def tearDown(self):
        self.tmp.cleanup()

    def test_unattended_logs_question_and_blocks(self):
        with mock.patch.object(self.s, "_can_stuck_ask", return_value=False):
            out = self.s._maybe_stuck_ask(why="looped on Save", name="browser_click", repeats=4)
        self.assertIsNone(out)  # fall through to normal block
        self.assertTrue(any(a.get("tool") == "stuck_ask" for a in self.st.data["action_log"]))
        self.assertTrue(any("unattended" in n.lower() or "Stuck ask" in n for n in self.st.data["notes"]))

    def test_guidance_resets_counters(self):
        with mock.patch.object(self.s, "_can_stuck_ask", return_value=True), \
             mock.patch("agent_tester.auto.ask_line", return_value="1:use the Payments subpanel"):
            out = self.s._maybe_stuck_ask(why="looped", name="browser_click", repeats=4)
        self.assertIn("GUIDANCE", out["error"])
        self.assertIn("Payments subpanel", out["error"])
        self.assertFalse(self.s.result.limit_hit)
        self.assertEqual(self.s._sigs, {})
        self.assertFalse(self.s._explored)

    def test_skip_blocks_case(self):
        with mock.patch.object(self.s, "_can_stuck_ask", return_value=True), \
             mock.patch("agent_tester.auto.ask_line", return_value="2:no test data"):
            out = self.s._maybe_stuck_ask(why="looped", name="browser_click", repeats=4)
        self.assertTrue(self.s.result.stuck)
        self.assertIn("skipped by user", self.s.result.notes)
        self.assertIn("finish", out["error"].lower())

    def test_max_two_asks(self):
        self.s._stuck_asks = STUCK_ASK_MAX_PER_CASE
        with mock.patch.object(self.s, "_can_stuck_ask", return_value=True):
            self.assertIsNone(self.s._maybe_stuck_ask(why="x", name="browser_click", repeats=4))

    def test_budget_check_asks_after_explore(self):
        self.s._explored = True
        self.s.actions = 1
        self.s._sigs = {}
        self.s._recent = ["click:save"] * 6
        with mock.patch.object(self.s, "_maybe_stuck_ask", return_value={"error": "asked"}) as m:
            # Drive repeats via the real signature counter.
            out = None
            for _ in range(6):
                out = self.s._budget_check("browser_click", {"element": "Save", "ref": "e1"})
                if out and out.get("error") == "asked":
                    break
            self.assertEqual(out, {"error": "asked"})
            m.assert_called()


if __name__ == "__main__":
    unittest.main()
