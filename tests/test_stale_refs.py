"""After wizard clicks: refuse stale refs; page-change resets repeat counter; disabled Continue nudge."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_tester.agents.tester import TesterSession
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence

PACK = Path(__file__).resolve().parents[1] / "packs/savostore"

SNAP1 = '''Page URL: https://example.test/w/1
Page Title: Terms
- checkbox "Agree to the above statement" [ref=f1e147]
- button "Continue" [ref=f1e151]
'''
SNAP2 = '''Page URL: https://example.test/w/2
Page Title: Checkout
- heading "Billing Currency" [level=2] [ref=e10]
- radio "U.S Dollars" [ref=e20]
- button "Continue" [disabled] [ref=e30]
'''


class StaleRefs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = RunState(Path(self.tmp.name) / "run")
        pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
        self.s = TesterSession(
            "portal",
            {"case_id": "TC1", "title": "Wizard", "steps": [
                {"n": 1, "text": "agree"}, {"n": 2, "text": "currency"}], "area": "onboard"},
            self.st, pol, Evidence(self.st.dir), max_calls=40, max_minutes=5)
        self.s.last_snapshot = SNAP1
        self.s._page_hash = "aaaa"
        self.s._page_fresh = True

    def tearDown(self):
        self.tmp.cleanup()

    def _tool(self, name):
        return SimpleNamespace(name=name)

    def test_stale_ref_refused(self):
        self.s.last_snapshot = SNAP2
        out = self.s.before_tool(self._tool("browser_click"),
                                 {"element": "Agree", "target": "f1e147"}, None)
        self.assertIsInstance(out, dict)
        self.assertIn("stale", out["error"].lower())
        self.assertIn("browser_snapshot", out["error"])

    def test_disabled_continue_nudge(self):
        self.s.last_snapshot = SNAP2
        out = self.s.before_tool(self._tool("browser_click"),
                                 {"element": "Continue", "target": "e30"}, None)
        self.assertIsInstance(out, dict)
        self.assertIn("disabled", out["error"].lower())
        self.assertIn("required", out["error"].lower())

    def test_need_fresh_snap_blocks_next_action(self):
        self.s._need_fresh_snap = True
        out = self.s.before_tool(self._tool("browser_click"),
                                 {"element": "Continue", "target": "f1e151"}, None)
        self.assertIsInstance(out, dict)
        self.assertIn("fresh browser_snapshot", out["error"])

    def test_page_change_resets_repeat_counter(self):
        self.s._explored = False
        args = {"element": "Continue", "target": "f1e151"}
        # Count three clicks on page A.
        self.s.last_snapshot = SNAP1
        self.s._page_hash = "pageA"
        for _ in range(2):
            self.s._budget_check("browser_click", args)
        self.assertGreaterEqual(self.s._sigs.get("click:button continue", 0)
                                or max(self.s._sigs.values()), 2)
        # Same label on a new page: counter resets, so we do not immediately stop.
        self.s._page_hash = "pageB"
        self.s.last_snapshot = SNAP2.replace("[disabled] ", "")
        out = self.s._budget_check("browser_click", {"element": "Continue", "target": "e30"})
        # Should warn at most, not hard-stop on first click of the new page.
        if out:
            self.assertNotIn("repeated", (out.get("error") or "").lower())


if __name__ == "__main__":
    unittest.main()
