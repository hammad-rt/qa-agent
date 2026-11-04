import os, subprocess, sys, tempfile, unittest
from pathlib import Path

from agent_tester.web import server as S
from agent_tester.run_state import RunState
from agent_tester import coordinator as C


class CliLive(unittest.TestCase):
    def test_pid_file_marks_terminal_run_live(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "1-x"; d.mkdir()
            self.assertFalse(S._cli_on(d))
            p = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(5)", "agent_tester"])
            try:
                (d / "run.pid").write_text(f"{p.pid}\n")
                self.assertTrue(S._cli_on(d))
                self.assertTrue(S._live(d))
            finally:
                p.kill(); p.wait()
            self.assertFalse(S._cli_on(d))          # dead pid
            (d / "run.pid").write_text(f"{os.getpid()}\n")
            self.assertEqual(S._cli_on(d), b"agent_tester" in Path(f"/proc/{os.getpid()}/cmdline").read_bytes())

    def test_run_plan_writes_and_removes_pid(self):
        with tempfile.TemporaryDirectory() as t:
            st = RunState(Path(t) / "2-x"); seen = {}
            orig = C._run_plan
            def fake(plan, state, *a, **k):
                seen["pid"] = (state.dir / "run.pid").read_text().strip(); return {}
            C._run_plan = fake
            try:
                C.run_plan(None, st)
            finally:
                C._run_plan = orig
            self.assertEqual(seen["pid"], str(os.getpid()))
            self.assertFalse((st.dir / "run.pid").exists())


if __name__ == "__main__":
    unittest.main()
