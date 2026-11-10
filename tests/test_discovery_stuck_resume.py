"""Resumed discovery: repeating the same click must stuck-ask after STUCK_MAX_ATTEMPTS."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.agents.tester import LOOP_STOP, TesterSession
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence

PACK = Path(__file__).resolve().parents[1] / "packs/savostore"


class DiscoveryStuckResume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = RunState(Path(self.tmp.name) / "run")
        # Simulate a resume: prior actions logged, counters start empty (as on a new session).
        self.st.data["action_log"] = [
            {"case_id": "DISCO-admin", "tool": "browser_click", "args": {"element": "All menu"},
             "outcome": "ok"},
        ]
        self.st.save()
        pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
        self.s = TesterSession(
            "crm",
            {"case_id": "DISCO-admin", "title": "Discover admin", "discovery": True,
             "area": "admin",
             "steps": [{"n": 1, "text": "Find Quick Repair", "app": "crm"}]},
            self.st, pol, Evidence(self.st.dir), max_calls=40, max_minutes=10)
        self.s.current_step = 1
        self.s._explored = False
        # No element refs in the snapshot so intent falls back to the stable "All menu" label
        # (refs change every snapshot in real discovery runs).
        self.s.last_snapshot = "Page URL: https://crm.example/index.php\nPage Title: Home\n"

    def tearDown(self):
        self.tmp.cleanup()

    def test_resumed_discovery_all_menu_asks_after_three(self):
        """Same 'All menu' click with changing page hash still hits stuck ask at attempt 3."""
        asks = []

        def fake_ask(**kw):
            asks.append(kw)
            return {"error": "STUCK_ASK"}

        # Page hash flips each click (menu open/close) — previously reset counters forever.
        with mock.patch.object(self.s, "_maybe_stuck_ask", side_effect=fake_ask), \
                mock.patch.object(self.s, "_start_exploring",
                                  side_effect=AssertionError("discovery must not explore-first")):
            out = None
            for i in range(LOOP_STOP + 2):
                self.s._page_hash = f"hash-{i}"
                out = self.s._budget_check(
                    "browser_click", {"element": "All menu", "ref": f"e{10 + i}"})
                if out and out.get("error") == "STUCK_ASK":
                    break
        self.assertEqual(out, {"error": "STUCK_ASK"}, out)
        self.assertEqual(len(asks), 1)
        self.assertGreaterEqual(asks[0]["repeats"], LOOP_STOP)

    def test_discovery_stuck_ask_without_prior_explore(self):
        """_maybe_stuck_ask must open on discovery even when _explored is still False."""
        self.s._explored = False
        self.s._stuck_asks = 0
        with mock.patch.object(self.s, "_can_stuck_ask", return_value=True), \
                mock.patch.object(self.s, "_ask_stuck_line",
                                  side_effect=["try the Admin link under user menu", "go"]):
            out = self.s._maybe_stuck_ask(
                why="browser_click was repeated 3 times", name="browser_click", repeats=3)
        self.assertIn("GUIDANCE", out["error"])
        self.assertEqual(self.s._stuck_asks, 1)


if __name__ == "__main__":
    unittest.main()
