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
        self.s._capture_stuck_ask_shot = lambda: None
        with mock.patch.object(self.s, "_can_stuck_ask", return_value=False):
            out = self.s._maybe_stuck_ask(why="looped on Save", name="browser_click", repeats=4)
        self.assertIsNone(out)  # fall through to normal block
        self.assertTrue(any(a.get("tool") == "stuck_ask" for a in self.st.data["action_log"]))
        self.assertTrue(any("unattended" in n.lower() or "Stuck ask" in n for n in self.st.data["notes"]))

    def test_guidance_resets_counters(self):
        # Conversational: chat turn then explicit go — only then continue with guidance.
        self.s._capture_stuck_ask_shot = lambda: None
        with mock.patch.object(self.s, "_can_stuck_ask", return_value=True), \
             mock.patch.object(self.s, "_ask_stuck_line",
                               side_effect=["use the Payments subpanel", "go"]):
            out = self.s._maybe_stuck_ask(why="looped", name="browser_click", repeats=4)
        self.assertIn("GUIDANCE", out["error"])
        self.assertIn("Payments subpanel", out["error"])
        self.assertFalse(self.s.result.limit_hit)
        self.assertEqual(self.s._sigs, {})
        self.assertFalse(self.s._explored)
        self.assertTrue(self.s._forbidden_after_stuck)
        chat = (self.st.dir / "stuck_chat.json")
        self.assertTrue(chat.is_file())

    def test_skip_blocks_case(self):
        self.s._capture_stuck_ask_shot = lambda: None
        with mock.patch.object(self.s, "_can_stuck_ask", return_value=True), \
             mock.patch.object(self.s, "_ask_stuck_line", return_value="2:no test data"):
            out = self.s._maybe_stuck_ask(why="looped", name="browser_click", repeats=4)
        self.assertTrue(self.s.result.stuck)
        self.assertIn("skipped by user", self.s.result.notes)
        self.assertIn("finish", out["error"].lower())

    def test_question_is_human_readable(self):
        self.s._capture_stuck_ask_shot = lambda: None
        self.s.last_snapshot = (
            'Page URL: https://example.test/x\nPage Title: Checkout\n'
            '- heading "Billing Currency" [ref=e1]\n'
            '- button "Continue" [disabled] [ref=e2]\n'
        )
        self.s._recent = ["click:checkbox Agree to the above statement",
                          "click:checkbox Agree to the above statement"]
        q = self.s._stuck_question("browser_click was repeated 3 times")
        self.assertNotIn("step ?", q)
        self.assertNotIn("[ref=", q)
        self.assertNotIn('"content"', q)
        self.assertIn("Billing Currency", q)

    def test_max_two_asks(self):
        self.s._stuck_asks = STUCK_ASK_MAX_PER_CASE
        self.s._capture_stuck_ask_shot = lambda: None
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
