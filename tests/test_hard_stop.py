import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tester.agents.tester import TesterHardStop, TesterSession, _hard_stop_in
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence

PACK = Path(__file__).resolve().parents[1] / "packs/savostore"


class HardStop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        st = RunState(Path(self.tmp.name) / "run")
        pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
        self.s = TesterSession("crm", {"case_id": "E1", "steps": []}, st, pol, Evidence(st.dir), max_calls=30, max_minutes=5)
        self.st = st

    def tearDown(self):
        self.tmp.cleanup()

    def test_ignored_loop_stop_ends_turn(self):
        nav = SimpleNamespace(name="browser_navigate")
        url = {"url": "https://staging5.rolustech.com:4220/index.php?module=AOS_Invoices&action=DetailView&record=x"}
        with self.assertRaises(TesterHardStop):
            for _ in range(40):
                self.s.before_tool(nav, dict(url), None)
        self.assertTrue(self.s.result.loop and self.s.result.stuck and self.s.finished)
        self.assertIn("Stopped by code", self.s.result.notes)
        self.assertTrue(any(a.get("tool") == "hard_stop" for a in self.st.data["action_log"]))

    def test_after_stop_reads_blocked_finish_allowed(self):
        self.s.result.limit_hit = True
        self.assertIn("error", self.s._budget_check("browser_snapshot", {}))
        self.assertIsNone(self.s._budget_check("finish", {}))

    def test_wrapped(self):
        try:
            try:
                raise TesterHardStop("x")
            except TesterHardStop as e:
                raise RuntimeError("wrap") from e
        except RuntimeError as w:
            self.assertTrue(_hard_stop_in(w))
        self.assertFalse(_hard_stop_in(ValueError("no")))


if __name__ == "__main__":
    unittest.main()
