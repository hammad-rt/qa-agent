"""Human-readable stuck asks: no raw refs/JSON, resolved steps, suggestions."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_tester.stuck_summary import (
    _clean,
    agent_chat_reply,
    build_now_doing,
    build_stuck_summary,
    format_stuck_prompt,
    humanize_tried,
    is_continue,
    resolve_step,
    suggestion_inferred,
    suggestion_text,
)


ROOT = Path(__file__).resolve().parents[1]
GOLDEN_STUCK = ROOT / "tests" / "fixtures" / "golden_traces" / "loop_165177" / "stuck_ask.json"

SNAP = """Page URL: https://portal.example/wizard/billing
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

    def test_word_boundary_truncation(self):
        long = "clicked \"Request a purchase quote for the full catalogue bundle today\" button"
        clipped = _clean(long, 48)
        self.assertTrue(clipped.endswith("…"), clipped)
        # Never ends mid-word with a partial token before the ellipsis.
        stem = clipped[:-1].rstrip()
        self.assertFalse(stem.endswith("purchas"), clipped)
        self.assertNotIn("browser_click", clipped)

    def test_humanize_strips_tool_names(self):
        tried = humanize_tried([
            "browser_click",
            "click:button Request a purchase quote",
            "click:button Request a purchase quote",
            "click:button Request a purchase quote",
        ])
        blob = " ".join(tried)
        self.assertNotIn("browser_click", blob)
        self.assertNotRegex(blob, r"\{|\[ref=")
        self.assertIn("Request a purchase quote", blob)
        self.assertIn("times", blob)


class StuckAskIntentFromGolden(unittest.TestCase):
    """Card intent lines + inferred suggestions from the redacted 165177 stuck shape."""

    @classmethod
    def setUpClass(cls):
        raw = json.loads(GOLDEN_STUCK.read_text())
        cls.fx = raw
        cls.summary = build_stuck_summary(
            brief=raw["brief"],
            case_id=raw["case_id"],
            app=raw["app"],
            current_step=raw["current_step"],
            why=raw["why"],
            recent=raw["recent"],
            snapshot=raw["snapshot"],
            timeout_sec=600,
        )
        cls.prompt = format_stuck_prompt(cls.summary)

    def test_three_line_intent_layout(self):
        s = self.summary
        self.assertTrue(s.get("question"), s)
        self.assertTrue(s.get("outcome"), s)
        self.assertTrue(s.get("doing_now"), s)
        self.assertTrue(s.get("need_from_user"), s)
        # Question leads the CLI prompt; three lines are explicit.
        self.assertTrue(self.prompt.startswith(s["question"]) or s["question"] in self.prompt[:200])
        self.assertIn("Outcome:", self.prompt)
        self.assertIn("Doing now:", self.prompt)
        self.assertIn("Need from you:", self.prompt)
        self.assertIn("find how to request a purchase quote", s["outcome"].lower() + s["question"].lower() + s["doing_now"].lower())

    def test_tried_humanised_no_json_or_tools(self):
        tried = self.summary["tried"]
        self.assertTrue(tried)
        blob = " ".join(tried) + "\n" + self.prompt
        self.assertNotIn("browser_click", blob)
        self.assertNotIn('"content"', blob)
        self.assertNotIn("[ref=", blob)
        self.assertNotRegex(blob, r"\be67\b")
        self.assertIn("Request a purchase quote", blob)
        self.assertIn("times", " ".join(tried))

    def test_inferred_label_on_heuristic_suggestion(self):
        sugs = self.summary["suggestions"]
        self.assertTrue(sugs, "loop fixture should yield at least one real suggestion")
        # Repeated click with no disabled/required → heuristic suggestion is inferred.
        inferred = [s for s in sugs if suggestion_inferred(s)]
        self.assertTrue(inferred, sugs)
        for s in inferred:
            self.assertTrue(suggestion_text(s))
            self.assertNotRegex(suggestion_text(s), r"(?i)fix:\s*\w+$|placeholder|TODO|lorem")
        self.assertIn("[inferred]", self.prompt)

    def test_page_derived_suggestion_not_inferred_when_available(self):
        # When the page exposes a required control, that suggestion is not inferred.
        snap = SNAP + '- radio "Currency" [required] [ref=e40]\n'
        summary = build_stuck_summary(
            brief={"case_id": "TC1", "title": "Wizard", "steps": [
                {"n": 1, "text": "agree"}, {"n": 2, "text": "choose billing currency"}]},
            case_id="TC1", app="portal", current_step=2,
            why="browser_click was repeated 3 times",
            recent=["click:checkbox Agree to the above statement"] * 3,
            snapshot=snap, timeout_sec=600,
        )
        page_sugs = [s for s in summary["suggestions"] if not suggestion_inferred(s)]
        self.assertTrue(page_sugs, summary["suggestions"])
        self.assertTrue(all(suggestion_text(s) for s in page_sugs))


class StuckCardDom(unittest.TestCase):
    """Web stuck card renders question + intent lines + inferred badge (PR #16 focus intact)."""

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "agent_tester" / "web" / "index.html").read_text()

    def test_card_has_intent_and_inferred_hooks(self):
        self.assertIn("ask-question", self.html)
        self.assertIn("intent-lines", self.html)
        self.assertIn("need_from_user", self.html)
        self.assertIn("doing_now", self.html)
        self.assertIn("sugInferred", self.html)
        self.assertIn('class="inf"', self.html)
        self.assertIn("tried-list", self.html)

    def test_focus_fix_from_pr16_intact(self):
        compact = self.html.replace(" ", "")
        self.assertNotIn("&&!j.stuck", compact)
        self.assertNotIn("JSON.stringify(j.stuck", self.html)
        self.assertIn("Same ask (incl. stuck): never rewrite pin", self.html)
        self.assertIn("typingInField", self.html)
        self.assertIn("stuckAct('go')", self.html)
        self.assertIn("stuckAct('skip')", self.html)
        self.assertIn("stuckAct('stop')", self.html)


if __name__ == "__main__":
    unittest.main()
