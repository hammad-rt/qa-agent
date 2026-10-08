"""Gap #1: evidence_name returns absolute paths under shots/; report embeds only existing PNGs."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_tester.config import ROOT
from agent_tester.models import CaseResult, PlannedCase, Step, TestPlan
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack

PACK = ROOT / "packs" / "savostore"


class EvidenceNamePaths(unittest.TestCase):
    def setUp(self):
        from agent_tester.agents.tester import TesterSession
        from agent_tester.tools.screenshots import Evidence
        self.tmp = tempfile.TemporaryDirectory()
        self.state = RunState(Path(self.tmp.name) / "run")
        pack = load_pack(PACK)
        policy = from_pack(pack, "staging", approver=lambda *a: True)
        brief = {"case_id": "TC1", "requirements": {}}
        self.ev = Evidence(self.state.dir)
        self.s = TesterSession("crm", brief, self.state, policy, self.ev, max_calls=20, max_minutes=5)
        self.tools = {f.__name__: f for f in self.s.tools()}

    def tearDown(self):
        self.tmp.cleanup()

    def test_evidence_name_returns_absolute_path_under_shots(self):
        out = self.tools["evidence_name"]("activity list")
        path = Path(out["filename"])
        self.assertTrue(path.is_absolute(), out["filename"])
        self.assertEqual(path.parent, self.ev.shots.resolve())
        self.assertTrue(path.name.startswith("tc1-01-"))
        self.assertTrue(path.name.endswith(".png"))
        self.assertEqual(self.s.result.screenshots, [])  # not until file exists

    def test_screenshot_appends_basename_only_when_file_exists(self):
        out = self.tools["evidence_name"]("showing order numbers")
        png = Path(out["filename"])
        tool = SimpleNamespace(name="browser_take_screenshot")
        self.s.after_tool(tool, {"filename": str(png)}, None, {"ok": True})
        self.assertEqual(self.s.result.screenshots, [])  # file not written yet
        png.write_bytes(b"fake-png")
        self.s.after_tool(tool, {"filename": str(png)}, None, {"ok": True})
        self.assertEqual(self.s.result.screenshots, [png.name])
        self.assertFalse(Path(self.s.result.screenshots[0]).is_absolute())


class HelperScreenshotPaths(unittest.TestCase):
    def test_mcp_browser_screenshot_uses_absolute_path_returns_basename(self):
        import asyncio
        from agent_tester.helpers import McpBrowser

        seen = []

        async def call(tool, args):
            seen.append((tool, dict(args)))
            return "ok"

        with tempfile.TemporaryDirectory() as d:
            shots = Path(d) / "shots"
            shots.mkdir()
            b = McpBrowser(call, evidence_dir=shots)
            name = asyncio.run(b.screenshot("login-crm-admin.png"))
            self.assertEqual(name, "login-crm-admin.png")
            self.assertEqual(seen[0][0], "browser_take_screenshot")
            fn = Path(seen[0][1]["filename"])
            self.assertTrue(fn.is_absolute())
            self.assertEqual(fn, (shots / "login-crm-admin.png").resolve())


class ReportEvidenceGating(unittest.TestCase):
    def test_report_embeds_existing_shots_skips_missing(self):
        from agent_tester.report.builder import build_report
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"REPORT_PDF": "0"}):
            st = RunState(Path(d) / "run")
            shots = st.dir / "shots"
            shots.mkdir(parents=True)
            present = "tc1-01-ok.png"
            missing = "tc1-02-gone.png"
            (shots / present).write_bytes(b"png")
            st.set_plan(TestPlan(task_id=1, cases=[PlannedCase(id="TC1", title="t",
                                                               steps=[Step(text="check", app="crm")])]))
            st.set_result(CaseResult(case_id="TC1", verdict="PASS", note="ok",
                                     evidence=[f"/abs/elsewhere/{present}", missing]))
            html = build_report(st).read_text()
            self.assertIn(f'src="shots/{present}"', html)
            self.assertNotIn("/abs/elsewhere/", html)  # basename only in src
            self.assertNotIn(f'src="shots/{missing}"', html)
            self.assertIn(f"missing evidence: {missing}", html)


if __name__ == "__main__":
    unittest.main()
