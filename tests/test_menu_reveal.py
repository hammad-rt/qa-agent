"""Dropdown / post-click page state: menus stay visible, snapshots aren't stuck, no stale reattach."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_tester.agents.tester import STUCK_MAX_ATTEMPTS, TesterSession
from agent_tester.preflight.packs import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence

PACK = Path(__file__).resolve().parents[1] / "packs" / "savostore"

CLOSED = (
    "Page URL: https://example.test/app\n"
    "Page Title: Home\n"
    "- navigation:\n"
    "  - menubar:\n"
    '    - menuitem "Orders" [ref=f1e11]\n'
    "    - menuitem \"Contacts\" [ref=e3]\n"
    "- main:\n"
    '  - heading "Dashboard" [ref=e4]\n'
)

OPEN = (
    "Page URL: https://example.test/app\n"
    "Page Title: Home\n"
    "- navigation:\n"
    "  - menubar:\n"
    '    - menuitem "Orders" [expanded] [ref=f1e11]:\n'
    "      - menu:\n"
    '        - menuitem "Shipping Only Orders" [ref=f1e20]\n'
    '        - menuitem "Buy and Ship Orders" [ref=f1e21]\n'
    "    - menuitem \"Contacts\" [ref=e3]\n"
    "- main:\n"
    '  - heading "Dashboard" [ref=e4]\n'
)


def _sess(tmp: Path) -> TesterSession:
    st = RunState(tmp / "run")
    pol = from_pack(load_pack(PACK), "staging", auto=True, approver=lambda *a: True)
    s = TesterSession(
        "crm",
        {"case_id": "TC1", "title": "open a menu", "steps": [{"n": 1, "text": "open Orders", "app": "crm"}]},
        st, pol, Evidence(st.dir), max_calls=40, max_minutes=5,
    )
    s.current_step = 1
    s.max_calls = 100
    s._last_act = 0.0
    return s


class NeedFreshSnapShortCircuit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _sess(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def t(self, name):
        return SimpleNamespace(name=name)

    def test_need_fresh_snap_takes_real_snapshot_and_unblocks_next_click(self):
        """Regression: short-circuit ran before the flag cleared, so every later click was refused."""
        self.s._remember_page(OPEN)
        self.s._need_fresh_snap = True
        self.s._page_fresh = True
        self.s._last_act = 0.0
        before = self.s.calls
        out = self.s.before_tool(self.t("browser_snapshot"), {}, None)
        self.assertIsNone(out, out)
        self.assertGreater(self.s.calls, before)
        self.assertFalse(self.s._need_fresh_snap)
        # A different control can now be clicked (not refused forever).
        allowed = self.s.before_tool(
            self.t("browser_click"),
            {"element": "Shipping Only Orders", "target": "f1e20"},
            None,
        )
        self.assertIsNone(allowed, allowed)

    def test_unchanged_short_circuit_returns_full_page_state(self):
        pad = "\n".join(f'- listitem "Row {i}" [ref=e{200 + i}]' for i in range(80))
        page = OPEN + pad
        self.s._remember_page(page)
        self.s._page_fresh = True
        self.s._need_fresh_snap = False
        stored = self.s._page_state
        self.assertGreater(len(stored), 500)
        self.assertIn("Shipping Only Orders", stored)
        r = self.s.before_tool(self.t("browser_snapshot"), {}, None)
        self.assertIn("unchanged", r["result"].lower())
        self.assertEqual(r["page_state"], stored)
        self.assertIn("Shipping Only Orders", r["page_state"])
        self.assertGreater(len(r["page_state"]), 500)


class MenuRevealAfterClick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _sess(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def t(self, name):
        return SimpleNamespace(name=name)

    def test_click_that_opens_menu_hints_and_allows_item_click(self):
        self.s._remember_page(CLOSED)
        out = self.s.after_tool(
            self.t("browser_click"),
            {"element": "Orders", "target": "f1e11"},
            None,
            {"text": OPEN},
        )
        blob = str(out).lower()
        self.assertIn("the menu opened; pick one of:", blob)
        self.assertIn("shipping only orders", blob)
        self.assertFalse(self.s._need_fresh_snap)
        self.assertIsNone(self.s._no_change_click)
        self.assertIn("Shipping Only Orders", self.s.last_snapshot)
        # Choosing an item is progress, not a repeat of the toggle.
        allowed = self.s.before_tool(
            self.t("browser_click"),
            {"element": "Shipping Only Orders", "target": "f1e20"},
            None,
        )
        self.assertIsNone(allowed, allowed)

    def test_toggle_then_item_is_not_a_loop(self):
        asks = []

        def fake_ask(**kw):
            asks.append(kw)
            return {"error": "STUCK_ASK"}

        with mock.patch.object(self.s, "_maybe_stuck_ask", side_effect=fake_ask):
            self.s.last_snapshot = CLOSED
            self.s._budget_check("browser_click", {"element": "Orders", "target": "f1e11"})
            self.s._remember_page(OPEN)
            self.s.last_snapshot = OPEN
            out = self.s._budget_check(
                "browser_click",
                {"element": "Shipping Only Orders", "target": "f1e20"},
            )
        self.assertIsNone(out, out)
        self.assertEqual(asks, [])


class RepeatRecoveryAndStuck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _sess(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_identical_clicks_without_menu_still_ask_at_three(self):
        asks = []

        def fake_ask(**kw):
            asks.append(kw)
            return {"error": "STUCK_ASK"}

        snap = (
            "Page URL: https://example.test/app\n"
            "Page Title: Home\n"
            '- button "Submit form" [ref=e9]\n'
        )
        with mock.patch.object(self.s, "_maybe_stuck_ask", side_effect=fake_ask):
            out = None
            for i in range(STUCK_MAX_ATTEMPTS + 1):
                self.s.last_snapshot = snap
                self.s._page_state = snap
                self.s._page_hash = f"h{i}"
                out = self.s._budget_check(
                    "browser_click", {"element": "Submit form", "ref": "e9"})
                if out and out.get("error") == "STUCK_ASK":
                    break
        self.assertEqual(out, {"error": "STUCK_ASK"}, out)
        self.assertEqual(len(asks), 1)

    def test_open_menu_recovers_once_before_stuck_ask(self):
        asks = []

        def fake_ask(**kw):
            asks.append(kw)
            return {"error": "STUCK_ASK"}

        self.s._remember_page(OPEN)
        with mock.patch.object(self.s, "_maybe_stuck_ask", side_effect=fake_ask):
            out = None
            for i in range(STUCK_MAX_ATTEMPTS):
                out = self.s._budget_check(
                    "browser_click", {"element": "Orders", "target": "f1e11"})
            # 3rd attempt surfaces the open menu instead of asking.
            self.assertIsNotNone(out)
            self.assertIn("the menu opened; pick one of:", out["error"])
            self.assertIn("Shipping Only Orders", out["error"])
            self.assertEqual(asks, [])
            # Next identical click asks.
            out = self.s._budget_check(
                "browser_click", {"element": "Orders", "target": "f1e11"})
        self.assertEqual(out, {"error": "STUCK_ASK"}, out)
        self.assertEqual(len(asks), 1)


class CaptureAndNoStaleReattach(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _sess(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def t(self, name):
        return SimpleNamespace(name=name)

    def test_does_not_reattach_stale_page_when_click_has_no_tree(self):
        stale = CLOSED + '- heading "STALE_UNIQUE_MARK" [ref=e99]\n'
        self.s._remember_page(stale)
        out = self.s.after_tool(
            self.t("browser_click"),
            {"element": "Orders", "target": "f1e11"},
            None,
            {"text": "clicked"},
        )
        self.assertFalse(self.s._page_fresh)
        self.assertTrue(self.s._need_fresh_snap)
        self.assertNotIn("page state (auto)", str(out).lower())
        self.assertNotIn("STALE_UNIQUE_MARK", str(out))

    def test_post_click_snapshot_uses_same_session_toolset(self):
        class FakeTool:
            name = "browser_snapshot"

            async def run_async(self, args, tool_context):
                return {"content": [{"text": OPEN}]}

        class FakeToolset:
            async def get_tools(self):
                return [FakeTool()]

        stale = CLOSED + '- heading "STALE_UNIQUE_MARK" [ref=e99]\n'
        self.s._remember_page(stale)
        self.s.toolset = FakeToolset()
        out = self.s.after_tool(
            self.t("browser_click"),
            {"element": "Orders", "target": "f1e11"},
            object(),
            {"text": "clicked"},
        )
        self.assertIn("Shipping Only Orders", self.s._page_state)
        self.assertIn("the menu opened; pick one of:", str(out).lower())
        self.assertNotIn("STALE_UNIQUE_MARK", self.s._page_state)


if __name__ == "__main__":
    unittest.main()
