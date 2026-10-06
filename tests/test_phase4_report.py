"""Phase 4: bug drafts, still-present bugs, approval before posting, run modes, progress, email, PDF."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.config import ROOT
from agent_tester.models import CaseResult, PlannedCase, Step, TestPlan
from agent_tester.modes import cases_for_bug, filter_plan, latest_plan, parse_cases
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.progress import Progress
from agent_tester.report import bug_drafts
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack

PACK = ROOT / "packs" / "savostore"


def make_state(d, task=7):
    st = RunState(Path(d) / f"{task}-20261006-100000")
    st.set_plan(TestPlan(task_id=task, cases=[
        PlannedCase(id="TC1", title="Apply rewards", steps=[Step(text="apply 50")], expected="up to 50"),
        PlannedCase(id="TC2", title="Pay", depends_on=["TC1"], expected="wallet drops once"),
        PlannedCase(id="TC3", title="Reload", expected="cleared"),
        PlannedCase(id="TC4", title="Cap", expected="10% of shipping")]))
    st.set_result(CaseResult(case_id="TC1", verdict="FAIL", actual="up to shows 40", severity="High", bug_title="Wrong limit"))
    st.set_result(CaseResult(case_id="TC2", verdict="FAIL", known_bug=1, actual="committed before pay"))
    st.set_result(CaseResult(case_id="TC3", verdict="PARTIAL", actual="half cleared, card 4111 1111 1111 1111"))
    st.set_result(CaseResult(case_id="TC4", verdict="PASS"))
    return st


class Drafts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pack = load_pack(PACK)
        self.st = make_state(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_known_bug_is_still_present_not_new(self):
        out = bug_drafts.build_drafts(self.st, self.pack)
        self.assertEqual([d["linked_cases"] for d in out["drafts"]], [["TC1"], ["TC3"]])
        self.assertEqual([(b["n"], b["cases"]) for b in out["still_present"]], [(1, ["TC2"])])
        self.assertEqual(out["drafts"][1]["severity"], "Medium")          # PARTIAL default
        self.assertNotIn("4111 1111", (self.st.dir / "bug_drafts.json").read_text())
        self.assertIn("D1", self.st.result("TC1").bug_draft_ids)

    def test_posting_needs_approval_each_time(self):
        bug_drafts.build_drafts(self.st, self.pack)
        asked, posted = [], []
        policy = from_pack(self.pack, "staging", approver=lambda a, r, d: (asked.append(a) or len(asked) == 1))
        ids = bug_drafts.review_and_post(self.st.dir, lambda d, body: posted.append(body) or 9001, policy, out=lambda *_: None)
        self.assertEqual(ids, [9001])
        self.assertEqual(len(asked), 2)                                 # asked once per draft
        data = bug_drafts.load(self.st.dir)
        self.assertEqual([d["status"] for d in data["drafts"]], ["posted", "skipped"])
        bug_drafts.build_drafts(self.st, self.pack)                     # rebuild keeps posting status
        self.assertEqual(bug_drafts.load(self.st.dir)["drafts"][0]["posted_issue"], 9001)

    def test_no_approver_posts_nothing(self):
        bug_drafts.build_drafts(self.st, self.pack)
        policy = from_pack(self.pack, "staging")
        self.assertEqual(bug_drafts.review_and_post(self.st.dir, lambda d, b: 1, policy, out=lambda *_: None), [])

    def test_report_lists_drafts_and_still_present(self):
        from agent_tester.report.builder import build_report
        with mock.patch.dict(os.environ, {"REPORT_PDF": "0"}):
            html = build_report(self.st, self.pack).read_text()
        for s in ("Wrong limit", "still present", "7-20261006-100000"):
            self.assertIn(s.lower(), html.lower())
        rep = json.loads((self.st.dir / "report.json").read_text())
        self.assertEqual(len(rep["bug_drafts"]["drafts"]), 2)


class Modes(unittest.TestCase):
    def test_parse_and_filter_with_dependencies(self):
        self.assertEqual(parse_cases("TC2, TC3"), ["TC2", "TC3"])
        with tempfile.TemporaryDirectory() as d:
            plan = make_state(d).plan
        self.assertEqual([c.id for c in filter_plan(plan, ["TC2"]).cases], ["TC1", "TC2"])
        self.assertEqual(len(filter_plan(plan, []).cases), 4)
        with self.assertRaises(ValueError):
            filter_plan(plan, ["TC9"])

    def test_retest_bug_and_latest_plan(self):
        with tempfile.TemporaryDirectory() as d:
            st = make_state(d)
            self.assertEqual(cases_for_bug(Path(d), 7, 1)[0], ["TC2"])
            self.assertEqual(latest_plan(Path(d), 7)[0], None)          # not approved yet
            st.data["approved"] = True
            st.save()
            self.assertEqual(len(latest_plan(Path(d), 7)[0].cases), 4)
            (st.dir / "bug_drafts.json").write_text(json.dumps({"drafts": [{"posted_issue": 555, "linked_cases": ["TC1"]}]}))
            self.assertEqual(cases_for_bug(Path(d), 7, 555)[0], ["TC1"])
            self.assertEqual(cases_for_bug(Path(d), 7, 404), ([], None))


class ProgressAndEmail(unittest.TestCase):
    def test_progress_lines_and_halfway(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "r")
            lines, mails = [], []
            p = Progress(st, 4, out=lines.append, notifier=lambda s, b, a: mails.append(s))
            p.started("task #7")
            p.case_done(CaseResult(case_id="TC1", verdict="PASS"))
            p.case_done(CaseResult(case_id="TC2", verdict="FAIL", known_bug=1))
            self.assertIn("[2/4] TC2: FAIL (still present: bug 1)", lines[-1])
            self.assertEqual(mails, ["QA run started: task #7", "QA run halfway"])
            self.assertIn("TC1: PASS", (st.dir / "progress.log").read_text())

    def _env(self):
        return {"REPORT_EMAIL_TO": "me@example.com", "SMTP_USER": "me@example.com", "SMTP_APP_PASSWORD": "x"}

    def test_email_own_inbox_only_unless_confirmed(self):
        from agent_tester.notify import EmailNotAllowed, make_notifier
        sent = []

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): pass
            def login(self, u, p): pass
            def send_message(self, m): sent.append(m["To"])

        with mock.patch.dict(os.environ, self._env()):
            n = make_notifier(confirm=lambda q: "n", smtp_factory=FakeSMTP, out=lambda *_: None)
            self.assertTrue(n("Report", "body", None))
            with self.assertRaises(EmailNotAllowed):
                n("Report", "body", None, to="boss@example.com")
            n2 = make_notifier(confirm=lambda q: "y", smtp_factory=FakeSMTP, out=lambda *_: None)
            n2("Report", "body", None, to="boss@example.com")
        self.assertEqual(sent, ["me@example.com", "boss@example.com"])

    def test_email_not_configured(self):
        from agent_tester.notify import EmailNotAllowed, make_notifier
        with mock.patch.dict(os.environ, {"REPORT_EMAIL_TO": ""}):
            with self.assertRaises(EmailNotAllowed):
                make_notifier()("s", "b", None)


class Pdf(unittest.TestCase):
    def test_html_to_pdf_with_fake_browser(self):
        from agent_tester.report.pdf import html_to_pdf
        with tempfile.TemporaryDirectory() as d:
            html = Path(d) / "report.html"
            html.write_text("<h1>x</h1>")

            def fake_run(cmd, **k):
                Path(next(c for c in cmd if c.startswith("--print-to-pdf=")).split("=", 1)[1]).write_bytes(b"%PDF")
            self.assertEqual(html_to_pdf(html, browser="chrome", run=fake_run), html.with_suffix(".pdf"))

            def broken(cmd, **k):
                raise OSError("no")
            self.assertIsNone(html_to_pdf(html, pdf=Path(d) / "b.pdf", browser="chrome", run=broken))


if __name__ == "__main__":
    unittest.main()
