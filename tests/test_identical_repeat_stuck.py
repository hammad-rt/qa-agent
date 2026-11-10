"""Identical tool calls must stuck-ask at STUCK_MAX even when snapshots/page-hash change.

Reproduces the laptop smoke failure: tester_portal / CRM lookup clicking the same
label repeatedly while each MCP snapshot differs slightly — no stuck ask fired.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.agents.tester import LOOP_STOP, STUCK_MAX_ATTEMPTS, TesterSession
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence

PACK = Path(__file__).resolve().parents[1] / "packs/savostore"


def _snap(i: int, label: str = "Your Savo Rewards", ref: str = "e40") -> str:
    """Slightly different snapshot each time (fake MCP), same clickable label."""
    return (
        f"Page URL: https://portal.example/rewards?t={i}\n"
        f"Page Title: Rewards {i}\n"
        f'- link "{label}" [ref={ref}]\n'
        f"- text \"balance {i}.0\"\n"
    )


class IdenticalRepeatStuck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = RunState(Path(self.tmp.name) / "run")
        pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
        # Non-discovery portal case (the real smoke path after resume).
        self.s = TesterSession(
            "portal",
            {"case_id": "C1", "title": "Final checks (read-only)", "area": "rewards",
             "read_only": True,
             "steps": [{"n": 1, "text": "Read Your Savo Rewards wallet", "app": "portal"}]},
            self.st, pol, Evidence(self.st.dir), max_calls=40, max_minutes=10)
        self.s.current_step = 1
        self.s._explored = False

    def tearDown(self):
        self.tmp.cleanup()

    def test_portal_same_label_changing_snapshot_asks_at_three(self):
        asks = []

        def fake_ask(**kw):
            asks.append(kw)
            return {"error": "STUCK_ASK"}

        with mock.patch.object(self.s, "_maybe_stuck_ask", side_effect=fake_ask), \
                mock.patch.object(self.s, "_start_exploring",
                                  side_effect=AssertionError("must not explore-first on identical clicks")):
            out = None
            for i in range(STUCK_MAX_ATTEMPTS + 2):
                # Fake MCP: new snapshot + page hash + ref every time; same label.
                self.s.last_snapshot = _snap(i, ref=f"e{40 + i}")
                self.s._page_hash = f"ph-{i}-{i * 17}"
                out = self.s._budget_check(
                    "browser_click",
                    {"element": "Your Savo Rewards", "ref": f"e{40 + i}"})
                if out and out.get("error") == "STUCK_ASK":
                    break
        self.assertEqual(out, {"error": "STUCK_ASK"}, out)
        self.assertEqual(len(asks), 1)
        self.assertGreaterEqual(asks[0]["repeats"], LOOP_STOP)

    def test_ref_only_clicks_enrich_label_from_snapshot(self):
        """Model often omits element=; label from snapshot must still group repeats."""
        asks = []

        def fake_ask(**kw):
            asks.append(kw)
            return {"error": "STUCK_ASK"}

        with mock.patch.object(self.s, "_maybe_stuck_ask", side_effect=fake_ask), \
                mock.patch.object(self.s, "_start_exploring",
                                  side_effect=AssertionError("must not explore-first")):
            out = None
            for i in range(STUCK_MAX_ATTEMPTS + 2):
                ref = f"e{100 + i}"
                self.s.last_snapshot = _snap(i, label="Search icon...", ref=ref)
                self.s._page_hash = f"hash{i}"
                # before_tool enrichment path: call via a thin wrapper that fills element.
                args = {"ref": ref}
                if not args.get("element"):
                    from agent_tester.agents.tester import _label_of
                    lab = _label_of(ref, self.s.last_snapshot)
                    if lab:
                        args["element"] = lab
                out = self.s._budget_check("browser_click", args)
                if out and out.get("error") == "STUCK_ASK":
                    break
        self.assertEqual(out, {"error": "STUCK_ASK"}, out)
        self.assertEqual(len(asks), 1)

    def test_mark_step_refused_after_identical_repeats(self):
        self.s._recent = ["click:your savo rewards"] * LOOP_STOP
        self.assertEqual(self.s._identical_action_tail(LOOP_STOP), "click:your savo rewards")
        tool = mock.Mock()
        tool.name = "mark_step"
        out = self.s.before_tool(tool, {"n": 1}, None)
        self.assertIsInstance(out, dict)
        self.assertIn("Cannot mark this step done", out.get("error", ""))
        self.assertNotIn(1, self.s.result.steps_done)

    def test_contact_lookup_same_click_asks(self):
        pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
        s = TesterSession(
            "crm",
            {"case_id": "CONTACT-C1", "title": "Resolve contact", "area": "rewards",
             "steps": [{"n": 1, "text": "Find the test client contact", "app": "crm"}]},
            self.st, pol, Evidence(self.st.dir), max_calls=40, max_minutes=10)
        s.current_step = 1
        asks = []

        def fake_ask(**kw):
            asks.append(kw)
            return {"error": "STUCK_ASK"}

        with mock.patch.object(s, "_maybe_stuck_ask", side_effect=fake_ask), \
                mock.patch.object(s, "_start_exploring",
                                  side_effect=AssertionError("no explore-first")):
            out = None
            for i in range(4):
                s.last_snapshot = _snap(i, label="Search icon...", ref=f"e{i}")
                s._page_hash = f"c{i}"
                out = s._budget_check("browser_click",
                                      {"element": "Search icon...", "ref": f"e{i}"})
                if out and out.get("error") == "STUCK_ASK":
                    break
        self.assertEqual(out, {"error": "STUCK_ASK"}, out)
        self.assertEqual(len(asks), 1)


if __name__ == "__main__":
    unittest.main()
