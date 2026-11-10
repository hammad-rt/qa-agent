"""Human-readable stuck asks: no raw refs/JSON, resolved steps, suggestions."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_tester.stuck_summary import (
    agent_chat_reply,
    build_now_doing,
    build_stuck_summary,
    format_stuck_prompt,
    humanize_tried,
    is_continue,
    resolve_step,
)


SNAP = """Page URL: https://example.test/wizard/billing
Page Title: Checkout
- heading "Billing Currency" [level=2] [ref=e10]
- radio "U.S Dollars" [ref=e20]
- radio "Euro" [ref=e21]
- button "Continue" [disabled] [ref=e30]
- checkbox "Agree to the above statement" [checked] [ref=f1e147]
- button "Back" [ref=e31]
"""


class StuckSummary(unittest.TestCase):
    def test_resolve_step_never_question_mark(self):
        brief = {"case_id": "PREP-order_earn-create-0", "title": "Create order",
                 "steps": [{"n": 1, "text": "open create form", "app": "crm"}]}
        r = resolve_step(brief, None, case_id="PREP-order_earn-create-0")
        self.assertNotIn("?", r["step_label"])
        self.assertEqual(r["kind"], "data_prep")
        self.assertIn("data prep", r["step_label"].lower())
        r2 = resolve_step({"case_id": "TC1", "steps": [{"n": 1, "text": "agree"}, {"n": 2, "text": "pick currency"}]}, 2)
        self.assertIn("step 2", r2["step_label"])
        self.assertIn("pick currency", r2["step_label"])

    def test_humanize_no_raw_refs(self):
        tried = humanize_tried(["click:e59", "click:checkbox Agree to the above statement",
                                "click:checkbox Agree to the above statement", "click:button Continue"])
        blob = " ".join(tried)
        self.assertNotRegex(blob, r"\be\d+\b")
        self.assertIn("Agree", blob)
        self.assertIn("times", blob)

    def test_build_and_format_no_json_leak(self):
        summary = build_stuck_summary(
            brief={"case_id": "TC1", "title": "Wizard", "steps": [
                {"n": 1, "text": "agree to terms"}, {"n": 2, "text": "choose billing currency"}]},
            case_id="TC1", app="portal", current_step=2,
            why="browser_click was repeated 3 times",
            recent=["click:checkbox Agree to the above statement"] * 3 + ["click:button Continue"],
            snapshot=SNAP, timeout_sec=600,
        )
        text = format_stuck_prompt(summary)
        self.assertNotIn("step ?", text)
        self.assertNotIn('"content"', text)
        self.assertNotIn("[ref=", text)
        self.assertNotRegex(text, r"\bf1e\d+\b")
        self.assertIn("Billing Currency", text)
        self.assertIn("Continue", text)
        self.assertTrue(summary["suggestions"])
        self.assertIn("disabled", " ".join(summary["seeing"]).lower() + summary["guess"].lower())

    def test_json_dict_snapshot_not_dumped(self):
        raw = {"content": [{"type": "text", "text": SNAP}], "url": "https://example.test/x"}
        summary = build_stuck_summary(
            brief={"case_id": "TC1", "steps": [{"n": 1, "text": "go"}]},
            case_id="TC1", app="portal", current_step=1, why="looped",
            recent=["click:e1"], snapshot=raw,
        )
        text = format_stuck_prompt(summary)
        self.assertNotIn('"type": "text"', text)
        self.assertNotIn("[ref=", text)

    def test_continue_and_chat_reply(self):
        self.assertTrue(is_continue("go"))
        self.assertTrue(is_continue("Continue"))
        self.assertFalse(is_continue("select dollars"))
        s = build_stuck_summary(
            brief={"case_id": "TC1", "steps": [{"n": 1, "text": "x"}]},
            case_id="TC1", app="portal", current_step=1, why="x",
            recent=[], snapshot=SNAP,
        )
        reply = agent_chat_reply(s, "pick U.S Dollars")
        self.assertIn("go", reply.lower())
        self.assertNotIn("[ref=", reply)

    def test_now_doing(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "shots").mkdir()
            (d / "shots" / "a.png").write_bytes(b"x")
            nd = build_now_doing(
                brief={"case_id": "TC1", "steps": [{"n": 1, "text": "agree"}, {"n": 2, "text": "currency"}]},
                case_id="TC1", app="portal", current_step=2, snapshot=SNAP, run_dir=d,
            )
            self.assertIn("2/2", nd["line"])
            self.assertEqual(nd["screenshot"], "a.png")
            self.assertIn("Billing", nd["page"] + nd["line"])


if __name__ == "__main__":
    unittest.main()
