"""browser_take_screenshot without a filename must still land in shots/ and in the case evidence."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tester.agents.tester import TesterSession
from agent_tester.run_state import RunState
from agent_tester.tools.policy import Policy
from agent_tester.tools.screenshots import Evidence


def _session(d):
    st = RunState(Path(d) / "run")
    pol = Policy.__new__(Policy)
    s = TesterSession.__new__(TesterSession)
    s.ev, s.case_id, s.brief, s.state = Evidence(st.dir), "TC1", {"title": "check wallet"}, st
    s.last_snapshot, s.keep_emails = "Page URL: https://x/\n- heading \"Wallet\"", ()
    from agent_tester.models import StepResult
    s.result = StepResult(case_id="TC1", app="portal")
    return s


class ScreenshotArgs(unittest.TestCase):
    def test_missing_filename_gets_absolute_shots_path(self):
        with tempfile.TemporaryDirectory() as d:
            s = _session(d)
            args = {"fullPage": True}
            s._fix_screenshot_args(args)
            p = Path(args["filename"])
            self.assertTrue(p.is_absolute())
            self.assertEqual(p.parent.resolve(), s.ev.shots.resolve())
            self.assertTrue(p.name.startswith("tc1-01-check-wallet"))
            self.assertEqual(len(s.result.snapshots), 1)

    def test_relative_filename_moved_under_shots(self):
        with tempfile.TemporaryDirectory() as d:
            s = _session(d)
            args = {"filename": "after-apply.png"}
            s._fix_screenshot_args(args)
            p = Path(args["filename"])
            self.assertEqual(p.parent.resolve(), s.ev.shots.resolve())
            self.assertIn("after-apply", p.name)

    def test_good_absolute_filename_kept(self):
        with tempfile.TemporaryDirectory() as d:
            s = _session(d)
            good = str((s.ev.shots / "tc1-05-x.png").resolve())
            args = {"filename": good}
            s._fix_screenshot_args(args)
            self.assertEqual(args["filename"], good)

    def test_after_tool_records_file_written_at_injected_path(self):
        with tempfile.TemporaryDirectory() as d:
            s = _session(d)
            s.off_site, s._xsrf_urls, s._last_nav_url = None, set(), None
            s.app, s.policy = "portal", None
            s._check_page_site = lambda *a: None
            args = {}
            s._fix_screenshot_args(args)
            Path(args["filename"]).write_bytes(b"png")
            s.after_tool(SimpleNamespace(name="browser_take_screenshot"), args, None, {"content": []})
            self.assertEqual(s.result.screenshots, [Path(args["filename"]).name])


if __name__ == "__main__":
    unittest.main()
