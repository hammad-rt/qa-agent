"""Fresh stuck-ask answers (ask_id + stdin flush) and submit validation before loop."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester import auto
from agent_tester.agents.tester import TesterSession
from agent_tester.page_state import form_validation_messages
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence

PACK = Path(__file__).resolve().parents[1] / "packs/savostore"


class AskFresh(unittest.TestCase):
    def test_flush_drops_buffered_lines(self):
        buf = io.StringIO("stale-contact-answer\nstale-2\n")
        with mock.patch.object(auto.sys, "stdin", buf), \
             mock.patch("agent_tester.auto.select.select", side_effect=[([buf], [], []), ([], [], [])]):
            # First select says ready (consume one), second empty — but flush loops until empty.
            # Simulate multiple ready reads then empty.
            calls = {"n": 0}

            def sel(r, w, x, t=0):
                calls["n"] += 1
                if buf.tell() < len(buf.getvalue()):
                    return ([buf], [], [])
                return ([], [], [])

            with mock.patch("agent_tester.auto.select.select", side_effect=sel):
                n = auto.flush_stdin()
        self.assertGreaterEqual(n, 1)
        self.assertEqual(buf.read(), "")

    def test_ask_line_ignores_mismatched_ask_id(self):
        lines = [
            json.dumps({"ask_id": "old-contact", "text": "look up CONTACT-E0 by email"}),
            json.dumps({"ask_id": "stuck-PREP-1", "text": "1:fill the shipping address"}),
        ]
        it = iter(lines)

        class FakeIn:
            def readline(self):
                try:
                    return next(it) + "\n"
                except StopIteration:
                    return ""

            def fileno(self):
                return 0

            closed = False

            def isatty(self):
                return False

        out = io.StringIO()
        with mock.patch.dict("os.environ", {"QA_WEB": "1"}), \
             mock.patch.object(auto.sys, "stdin", FakeIn()), \
             mock.patch.object(auto.sys, "stdout", out), \
             mock.patch("agent_tester.auto.flush_stdin", return_value=0), \
             mock.patch("agent_tester.auto.select.select", return_value=([], [], [])):
            ans = auto.ask_line("Stuck on PREP?", ask_id="stuck-PREP-1")
        self.assertEqual(ans, "1:fill the shipping address")
        marker = out.getvalue()
        self.assertIn(auto.ASK_MARK, marker)
        self.assertIn("stuck-PREP-1", marker)

    def test_second_stuck_ask_gets_new_answer_not_old(self):
        """CONTACT-E0 answer must not auto-fill PREP-order_earn-create-0."""
        st = RunState(Path(tempfile.mkdtemp()) / "run")
        pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
        s = TesterSession(
            "portal",
            {"case_id": "PREP-order_earn-create-0", "title": "create", "steps": [
                {"n": 1, "text": "create", "app": "portal"}], "area": "rewards"},
            st, pol, Evidence(st.dir), max_calls=30, max_minutes=5)
        s._explored = True
        answers = iter(["1:open contact by email", "1:use one package only"])

        def fake_ask(prompt="", secret=False, ask_id=None):
            # Simulate leftover tagged with a different id being ignored by ask_line itself —
            # here we just return the next fresh answer and assert ask_id changes.
            fake_ask.ids.append(ask_id)
            return next(answers)

        fake_ask.ids = []
        with mock.patch.object(s, "_can_stuck_ask", return_value=True), \
             mock.patch("agent_tester.auto.ask_line", side_effect=fake_ask):
            s.case_id = "CONTACT-E0"
            out1 = s._maybe_stuck_ask(why="contact", name="browser_click", repeats=4)
            s.case_id = "PREP-order_earn-create-0"
            s._explored = True
            out2 = s._maybe_stuck_ask(why="create form", name="browser_click", repeats=4)
        self.assertIn("open contact by email", out1["error"])
        self.assertIn("one package only", out2["error"])
        self.assertNotEqual(fake_ask.ids[0], fake_ask.ids[1])
        notes = " ".join(st.data["notes"])
        self.assertIn("PREP-order_earn-create-0", notes)
        self.assertIn("one package only", notes)
        self.assertNotIn("answer=1:open contact by email", [
            n for n in st.data["notes"] if "PREP-order_earn-create-0" in n][0])


class FormValidation(unittest.TestCase):
    def test_extracts_required_and_alert(self):
        snap = """
Page URL: https://portal/shipping
- alert [ref=e1]: "Shipping address is required"
- textbox "City" [ref=e2] [required]: 
- status [ref=e3]: "Please fill out this field"
- textbox "OK" [ref=e4]: "done"
"""
        msgs = form_validation_messages(snap)
        blob = " ".join(msgs).lower()
        self.assertTrue(any("shipping address" in m.lower() for m in msgs) or "required" in blob)
        self.assertTrue(any("fill out" in m.lower() or "required" in m.lower() for m in msgs))

    def test_submit_repeat_feeds_validation_before_loop(self):
        import agent_tester.agents.tester as T
        st = RunState(Path(tempfile.mkdtemp()) / "run")
        pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
        s = TesterSession(
            "portal",
            {"case_id": "PREP-x-create-0", "title": "create", "steps": [
                {"n": 1, "text": "create", "app": "portal"}], "area": "rewards"},
            st, pol, Evidence(st.dir), max_calls=40, max_minutes=5)
        s._page_state = (
            '- alert: "Package weight is required"\n'
            '- textbox "Weight" [required]:\n'
        )
        s._explored = False
        outs = []
        for _ in range(T.STUCK_MAX_ATTEMPTS):
            outs.append(s._budget_check("browser_click", {"element": "Continue", "ref": "e9"}))
        # At warn_at, feed validation (still counts as an attempt); at max → lookup ladder / explore.
        self.assertTrue(any(o and "Package weight is required" in o.get("error", "") for o in outs))
        self.assertTrue(any(o and "Explore before you act" in o.get("error", "") for o in outs))
        self.assertFalse(s.result.loop)
        self.assertFalse(any(
            a.get("tool") == "loop_detected" for a in st.data["action_log"]))
        self.assertTrue(any(
            a.get("tool") == "stuck_lookup" or a.get("tool") == "explore"
            for a in st.data["action_log"]))


class WebAnswerAskId(unittest.TestCase):
    def test_stale_ask_id_rejected(self):
        from agent_tester.web import server as S
        j = S.Job.__new__(S.Job)
        j.lock = __import__("threading").Lock()
        j.pending = {"prompt": "Stuck?", "secret": False, "ask_id": "ask-new"}
        j._answered_ask_ids = set()
        j.out = ""
        j.proc = mock.Mock()
        j.proc.poll.return_value = None
        j.proc.stdin = mock.Mock()
        j._persist = lambda: None
        self.assertFalse(j.answer("old text", ask_id="ask-old"))
        j.proc.stdin.write.assert_not_called()
        self.assertTrue(j.answer("fresh", ask_id="ask-new"))
        j.proc.stdin.write.assert_called_once()
        # Same ask_id again is rejected.
        j.pending = {"prompt": "Stuck?", "secret": False, "ask_id": "ask-new"}
        self.assertFalse(j.answer("again", ask_id="ask-new"))


if __name__ == "__main__":
    unittest.main()
