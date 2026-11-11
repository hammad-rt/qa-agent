"""Checkbox / radio / switch click guidance: report state, do not re-click a checked box."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_tester.agents.tester import STUCK_MAX_ATTEMPTS, TesterSession
from agent_tester.page_state import (
    empty_required_fields,
    find_toggle_line,
    toggle_is_checked,
    toggle_state_hint,
    trim_page,
)
from agent_tester.preflight.packs import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence

PACK = Path(__file__).resolve().parents[1] / "packs" / "savostore"

UNCHECKED = (
    "Page URL: https://example.test/create\n"
    "Page Title: New shipment\n"
    "- main:\n"
    '  - heading "Important shipping note" [level=1] [ref=e106]\n'
    "  - generic [ref=e118] [cursor=pointer]:\n"
    '    - checkbox "Agree to the above statement." [ref=e120]\n'
    '    - generic [ref=e121]: Agree to the above statement.\n'
    '  - button "Continue" [disabled] [ref=e124]\n'
    '  - alert [ref=e96]: Create New Shipments\n'
)

CHECKED = (
    "Page URL: https://example.test/create\n"
    "Page Title: New shipment\n"
    "- main:\n"
    '  - heading "Important shipping note" [level=1] [ref=e106]\n'
    "  - generic [ref=e118] [cursor=pointer]:\n"
    '    - checkbox "Agree to the above statement." [checked] [active] [ref=e120]\n'
    '    - generic [ref=e121]: Agree to the above statement.\n'
    '  - button "Continue" [ref=e124] [cursor=pointer]\n'
    '  - alert [ref=e96]: Create New Shipments\n'
)

STILL_DISABLED = (
    "Page URL: https://example.test/create\n"
    "Page Title: New shipment\n"
    "- main:\n"
    '    - checkbox "Agree to the protection policy." [checked] [ref=e200]\n'
    '    - textbox "Weight" [required] [ref=e201]:\n'
    '    - button "Continue" [disabled] [ref=e202]\n'
)


def _sess(tmp: Path) -> TesterSession:
    st = RunState(tmp / "run")
    pol = from_pack(load_pack(PACK), "staging", auto=True, approver=lambda *a: True)
    s = TesterSession(
        "portal",
        {"case_id": "TC1", "title": "create an order", "steps": [
            {"n": 1, "text": "create an order", "app": "portal"}]},
        st, pol, Evidence(st.dir), max_calls=40, max_minutes=5,
    )
    s.current_step = 1
    s.max_calls = 100
    s._last_act = 0.0
    return s


class ToggleSnapshotParsing(unittest.TestCase):
    def test_reads_checked_and_unchecked_from_playwright_snapshot(self):
        self.assertIs(toggle_is_checked(UNCHECKED, ref="e120"), False)
        self.assertIs(toggle_is_checked(CHECKED, ref="e120"), True)
        self.assertIn("[checked]", find_toggle_line(CHECKED, ref="e120") or "")

    def test_label_match_ignores_role_suffix_and_punctuation(self):
        line = find_toggle_line(
            CHECKED, label="Agree to the above statement checkbox")
        self.assertIsNotNone(line)
        self.assertIn("e120", line)

    def test_wrapper_ref_finds_child_checkbox(self):
        line = find_toggle_line(CHECKED, ref="e118")
        self.assertIsNotNone(line)
        self.assertIn("checkbox", line)
        self.assertTrue(toggle_is_checked(CHECKED, ref="e118"))

    def test_does_not_treat_unchecked_or_false_as_checked(self):
        snap = (
            '- checkbox "Opt in" [unchecked] [ref=e1]\n'
            '- checkbox "Other" [checked=false] [ref=e2]\n'
        )
        self.assertIs(toggle_is_checked(snap, ref="e1"), False)
        self.assertIs(toggle_is_checked(snap, ref="e2"), False)

    def test_hint_after_check_points_to_enabled_continue(self):
        hint = toggle_state_hint(
            CHECKED, label="Agree to the above statement checkbox",
            ref="e120", before=UNCHECKED)
        self.assertIsNotNone(hint)
        low = hint.lower()
        self.assertIn("is now checked", low)
        self.assertIn("do not click it again", low)
        self.assertIn("would uncheck", low)
        self.assertIn("continue", low)
        self.assertIn("enabled", low)

    def test_hint_when_submit_still_disabled_lists_empty_required(self):
        hint = toggle_state_hint(
            STILL_DISABLED, label="Agree to the protection policy.", ref="e200")
        low = hint.lower()
        self.assertIn("is now checked", low)
        self.assertIn("still disabled", low)
        self.assertIn("weight", low)
        self.assertIn("empty", low)

    def test_already_checked_wording(self):
        hint = toggle_state_hint(CHECKED, ref="e120", already=True)
        self.assertIn("already checked", hint.lower())
        self.assertIn("Continue", hint)

    def test_trim_keeps_disabled_continue_button(self):
        out = trim_page(UNCHECKED)
        self.assertIn("[disabled]", out)
        self.assertIn('button "Continue"', out)
        self.assertIn("e124", out)

    def test_empty_required_skips_filled_and_checked(self):
        snap = (
            '- checkbox "Terms" [required] [checked] [ref=e1]\n'
            '- textbox "Weight" [required] [ref=e2]:\n'
            '- textbox "City" [required] [ref=e3]: Paris\n'
        )
        msgs = empty_required_fields(snap)
        blob = " ".join(msgs).lower()
        self.assertIn("weight", blob)
        self.assertNotIn("terms", blob)
        self.assertNotIn("city", blob)

    def test_radio_selected_is_checked(self):
        snap = '- radio "USD" [checked] [ref=e1]\n- radio "KES" [ref=e2]\n'
        self.assertIs(toggle_is_checked(snap, ref="e1"), True)
        self.assertIs(toggle_is_checked(snap, ref="e2"), False)
        hint = toggle_state_hint(snap, ref="e1", already=True)
        self.assertIn("already checked", hint.lower())
        self.assertNotIn("would uncheck", hint.lower())


class CheckboxClickSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _sess(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def t(self, name):
        return SimpleNamespace(name=name)

    def test_after_click_reports_checked_and_allows_continue(self):
        self.s._remember_page(UNCHECKED)
        allowed = self.s.before_tool(
            self.t("browser_click"),
            {"element": "Agree to the above statement checkbox", "target": "e120"},
            None,
        )
        self.assertIsNone(allowed, allowed)
        out = self.s.after_tool(
            self.t("browser_click"),
            {"element": "Agree to the above statement checkbox", "target": "e120"},
            None,
            {"text": CHECKED},
        )
        blob = str(out).lower()
        self.assertIn("is now checked", blob)
        self.assertIn("do not click it again", blob)
        self.assertIn("continue", blob)
        self.assertFalse(self.s._need_fresh_snap)
        self.assertIsNone(self.s._no_change_click)
        nxt = self.s.before_tool(
            self.t("browser_click"),
            {"element": "Continue", "target": "e124"},
            None,
        )
        self.assertIsNone(nxt, nxt)

    def test_reclick_of_checked_box_is_guidance_not_stuck(self):
        asks = []

        def fake_ask(**kw):
            asks.append(kw)
            return {"error": "STUCK_ASK"}

        self.s._remember_page(CHECKED)
        with mock.patch.object(self.s, "_maybe_stuck_ask", side_effect=fake_ask):
            outs = []
            for _ in range(STUCK_MAX_ATTEMPTS + 2):
                outs.append(self.s.before_tool(
                    self.t("browser_click"),
                    {"element": "Agree to the above statement checkbox",
                     "target": "e120"},
                    None,
                ))
        self.assertTrue(outs)
        for out in outs:
            self.assertIsNotNone(out)
            err = out.get("error", "")
            self.assertIn("already checked", err.lower())
            self.assertIn("Continue", err)
            self.assertNotEqual(out, {"error": "STUCK_ASK"})
        self.assertEqual(asks, [])
        # Repeat counters must stay unused so a later different click is not poisoned.
        self.assertFalse(getattr(self.s, "_sigs", {}) or {})

    def test_unchecked_click_is_allowed(self):
        self.s._remember_page(UNCHECKED)
        out = self.s.before_tool(
            self.t("browser_click"),
            {"element": "Agree to the above statement checkbox", "target": "e120"},
            None,
        )
        self.assertIsNone(out, out)

    def test_disabled_continue_lists_empty_required_fields(self):
        self.s._remember_page(STILL_DISABLED)
        out = self.s.before_tool(
            self.t("browser_click"),
            {"element": "Continue", "target": "e202"},
            None,
        )
        self.assertIsNotNone(out)
        err = out["error"].lower()
        self.assertIn("disabled", err)
        self.assertIn("weight", err)
        self.assertIn("empty", err)

    def test_other_controls_still_ask_at_three(self):
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


if __name__ == "__main__":
    unittest.main()


class UncheckIntentTests(unittest.TestCase):
    def _sess(self, steps):
        from types import SimpleNamespace
        from agent_tester.agents.tester import TesterSession
        s = TesterSession.__new__(TesterSession)
        s.brief = {"steps": steps}
        s.result = SimpleNamespace(steps_done=[])
        return s

    def test_untick_step_allows_click(self):
        s = self._sess([{"n": 1, "text": "Untick the shipment protection box"}])
        self.assertTrue(s._steps_want_uncheck())

    def test_normal_step_blocks(self):
        s = self._sess([{"n": 1, "text": "Tick the agreement box and click Continue"}])
        self.assertFalse(s._steps_want_uncheck())

    def test_done_step_ignored(self):
        from types import SimpleNamespace
        s = self._sess([{"n": 1, "text": "Uncheck the box"}, {"n": 2, "text": "Click Save"}])
        s.result = SimpleNamespace(steps_done=[1])
        self.assertFalse(s._steps_want_uncheck())
