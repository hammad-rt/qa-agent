"""A stopped or paused run gets a report of the finished cases; the rest show as not run."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.models import CaseResult, PlannedCase, TestPlan
from agent_tester.run_state import RunState


def make(d):
    st = RunState(Path(d) / "7-20261104-100000")
    st.set_plan(TestPlan(task_id=7, cases=[PlannedCase(id=f"TC{i}", title=f"case {i}", expected="ok") for i in (1, 2, 3)]))
    st.set_result(CaseResult(case_id="TC1", verdict="PASS"))
    st.checkpoint("stopped by user (Ctrl-C)", status="paused")
    return st


class PartialReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = make(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_builder_marks_partial_and_not_run(self):
        from agent_tester.report.builder import build_report
        html = build_report(self.st, None, pdf=False).read_text()
        self.assertIn("Partial report.", html)
        self.assertIn("TC2, TC3", html)
        self.assertIn("not run", html)
        rj = json.loads((self.st.dir / "report.json").read_text())
        self.assertTrue(rj["partial"])
        self.assertEqual(rj["not_run"], ["TC2", "TC3"])

    def test_full_run_is_not_partial(self):
        from agent_tester.report.builder import build_report
        for c in ("TC2", "TC3"):
            self.st.set_result(CaseResult(case_id=c, verdict="FAIL"))
        html = build_report(self.st, None, pdf=False).read_text()
        self.assertNotIn("Partial report.", html)

    def test_selected_cases_only_count(self):
        from agent_tester.report.builder import build_report
        self.st.data["mode"] = {"cases": ["TC1"]}
        self.st.save()
        self.assertNotIn("Partial report.", build_report(self.st, None, pdf=False).read_text())

    def test_partial_report_helper(self):
        from agent_tester import main
        with mock.patch.dict("os.environ", {"REPORT_PDF": "0"}):
            main._partial_report(self.st, None)
        self.assertTrue((self.st.dir / "report.html").exists())

    def test_no_results_no_report(self):
        from agent_tester import main
        st = RunState(Path(self.tmp.name) / "8-20261104-100000")
        st.set_plan(TestPlan(task_id=8, cases=[PlannedCase(id="TC1", title="x")]))
        main._partial_report(st, None)
        self.assertFalse((st.dir / "report.html").exists())

    def test_web_endpoint_builds_report(self):
        from agent_tester.web import server
        d = self.st.dir
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            (d / "report.html").write_text("<html></html>")
            return mock.Mock(returncode=0, stdout="Report: x", stderr="")
        h = server.Handler.__new__(server.Handler)
        h.path = f"/api/runs/{d.name}/report"
        sent = []
        with mock.patch.object(server.subprocess, "run", fake_run), \
             mock.patch.object(server.Handler, "_body", lambda self: {}), \
             mock.patch.object(server.Handler, "_run", lambda self, n: d), \
             mock.patch.object(server.Handler, "_send", lambda self, code, body: sent.append((code, body))):
            h.do_POST()
        self.assertEqual(sent, [(200, {"ok": True})])
        self.assertEqual(calls[0][-2:], ["report", str(d)])


if __name__ == "__main__":
    unittest.main()
