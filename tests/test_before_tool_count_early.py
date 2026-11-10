"""before_tool must count identical clicks BEFORE need_fresh_snap / stale-ref / policy deny.

Models the 171ca8d C1 resume trace: Administrator menu / Log out / Create a New Shipment
loops appeared in trace.jsonl but skipped _budget_check (action_log 1/1/0, no stuck ask).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_tester.agents.tester import LOOP_STOP, STUCK_MAX_ATTEMPTS, TesterSession
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence

PACK = Path(__file__).resolve().parents[1] / "packs/savostore"

SNAP_ADMIN = '''Page URL: https://crm.example/index.php
Page Title: Admin
- menuitem "Administrator menu" [ref=f2e31]
- link "Users" [ref=e99]
'''
SNAP_NO_ADMIN = '''Page URL: https://crm.example/index.php
Page Title: Admin
- link "Users" [ref=e99]
'''
SNAP_PORTAL = '''Page URL: https://portal.example/
Page Title: Home
- button "Log out button" [ref=e25]
- link "Create a New Shipment" [ref=f1e59]
'''
SNAP_NO_SHIP = '''Page URL: https://portal.example/
Page Title: Home
- button "Log out button" [ref=e25]
'''


class BeforeToolCountEarly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = RunState(Path(self.tmp.name) / "run")
        pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
        self.s = TesterSession(
            "crm",
            {"case_id": "DISCO-admin-inc", "title": "discover", "discovery": True,
             "steps": [{"n": 1, "text": "map admin menu", "app": "crm"}]},
            self.st, pol, Evidence(self.st.dir), max_calls=40, max_minutes=10)
        self.s.current_step = 1
        self.s._explored = False

    def tearDown(self):
        self.tmp.cleanup()

    def _tool(self, name="browser_click"):
        return SimpleNamespace(name=name)

    def test_need_fresh_snap_same_target_stuck_asks_at_three(self):
        """Trace loop 1: Administrator menu @ f2e31 with _need_fresh_snap always True."""
        asks = []

        def fake_ask(**kw):
            asks.append(kw)
            return {"error": "STUCK_ASK"}

        self.s.last_snapshot = SNAP_ADMIN
        self.s._need_fresh_snap = True
        with mock.patch.object(self.s, "_maybe_stuck_ask", side_effect=fake_ask):
            out = None
            for _ in range(STUCK_MAX_ATTEMPTS + 2):
                out = self.s.before_tool(
                    self._tool(),
                    {"element": "Administrator menu", "target": "f2e31"}, None)
                if out and out.get("error") == "STUCK_ASK":
                    break
        self.assertEqual(out, {"error": "STUCK_ASK"}, out)
        self.assertEqual(len(asks), 1)
        # Attempts were recorded even though the tool never ran.
        alog = [a for a in self.st.data["action_log"] if a.get("tool") == "browser_click"]
        self.assertGreaterEqual(len(alog) + len(asks), LOOP_STOP - 1)

    def test_stale_ref_same_target_stuck_asks_at_three(self):
        """Trace loop 3 shape: Create a New Shipment @ f1e59 missing from snapshot."""
        pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
        s = TesterSession(
            "portal",
            {"case_id": "PREP-wallet-create-0", "title": "prep create",
             "steps": [{"n": 1, "text": "create shipment", "app": "portal"}]},
            self.st, pol, Evidence(self.st.dir), max_calls=40, max_minutes=10)
        s.current_step = 1
        s._explored = False
        s.last_snapshot = SNAP_NO_SHIP
        s._need_fresh_snap = False
        asks = []

        def fake_ask(**kw):
            asks.append(kw)
            return {"error": "STUCK_ASK"}

        with mock.patch.object(s, "_maybe_stuck_ask", side_effect=fake_ask):
            out = None
            for _ in range(STUCK_MAX_ATTEMPTS + 2):
                out = s.before_tool(
                    self._tool(),
                    {"element": "Create a New Shipment", "target": "f1e59"}, None)
                if out and out.get("error") == "STUCK_ASK":
                    break
        self.assertEqual(out, {"error": "STUCK_ASK"}, out)
        self.assertEqual(len(asks), 1)
        stale = [a for a in self.st.data["action_log"]
                 if a.get("tool") == "browser_click" and a.get("outcome") == "stale_ref"]
        self.assertGreaterEqual(len(stale), 1)

    def test_logout_policy_deny_counts_toward_stuck(self):
        """Trace loop 2: Log out @ e25 denied by policy still counts as a repeat."""
        pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
        s = TesterSession(
            "portal",
            {"case_id": "DISCO-admin-inc", "title": "discover portal", "discovery": True,
             "steps": [{"n": 1, "text": "map portal", "app": "portal"}]},
            self.st, pol, Evidence(self.st.dir), max_calls=40, max_minutes=10)
        s.current_step = 1
        s._explored = False
        s.last_snapshot = SNAP_PORTAL
        s._need_fresh_snap = False
        asks = []

        def fake_ask(**kw):
            asks.append(kw)
            return {"error": "STUCK_ASK"}

        with mock.patch.object(s, "_maybe_stuck_ask", side_effect=fake_ask):
            out = None
            for _ in range(STUCK_MAX_ATTEMPTS + 2):
                out = s.before_tool(
                    self._tool(),
                    {"element": "Log out button", "target": "e25"}, None)
                if out and out.get("error") == "STUCK_ASK":
                    break
        self.assertEqual(out, {"error": "STUCK_ASK"}, out)
        self.assertEqual(len(asks), 1)

    def test_mark_step_refused_after_fresh_snap_refusals(self):
        self.s.last_snapshot = SNAP_ADMIN
        self.s._need_fresh_snap = True
        # Burn LOOP_STOP identical refusals via before_tool (counted into _recent).
        with mock.patch.object(self.s, "_maybe_stuck_ask", return_value=None), \
                mock.patch.object(self.s, "_can_stuck_ask", return_value=False):
            for _ in range(LOOP_STOP):
                try:
                    self.s.before_tool(
                        self._tool(),
                        {"element": "Administrator menu", "target": "f2e31"}, None)
                except Exception:
                    break
        self.assertIsNotNone(self.s._identical_action_tail(LOOP_STOP))
        out = self.s.before_tool(self._tool("mark_step"), {"n": 1}, None)
        self.assertIsInstance(out, dict)
        self.assertIn("Cannot mark this step done", out.get("error", ""))


if __name__ == "__main__":
    unittest.main()
