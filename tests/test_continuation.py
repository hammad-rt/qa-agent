"""A long flow that runs out of budget while still making step progress gets a continuation turn."""
import tempfile
import unittest
from pathlib import Path

from agent_tester.coordinator import _maybe_continue
from agent_tester.models import StepResult
from agent_tester.run_state import RunState

BRIEF = {"case_id": "TC2", "steps": [{"n": i} for i in range(1, 5)]}


class Continuation(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.state = RunState(Path(self.d.name) / "run")

    def tearDown(self):
        self.d.cleanup()

    def test_continues_from_reached_steps_and_merges(self):
        seen = []

        def tester(app, brief, state):
            seen.append(brief.get("continue_from"))
            return StepResult(case_id="TC2", app=app, steps_done=[3, 4], screenshots=["b.png"], values={"x": 1})

        first = StepResult(case_id="TC2", app="portal", steps_done=[1, 2], limit_hit=True, notes="at step 3",
                           screenshots=["a.png"])
        out = _maybe_continue("portal", BRIEF, first, self.state, tester, 0)
        self.assertEqual(seen[0]["steps_done"], [1, 2])
        self.assertEqual(out.steps_done, [1, 2, 3, 4])
        self.assertEqual(out.screenshots, ["a.png", "b.png"])
        self.assertFalse(out.limit_hit)

    def test_no_continuation_after_loop_or_without_progress(self):
        calls = []
        t = lambda *a: calls.append(1) or StepResult(case_id="TC2", app="portal")
        loop = StepResult(case_id="TC2", app="portal", steps_done=[1], limit_hit=True, loop=True)
        none = StepResult(case_id="TC2", app="portal", steps_done=[], limit_hit=True)
        _maybe_continue("portal", BRIEF, loop, self.state, t, 0)
        _maybe_continue("portal", BRIEF, none, self.state, t, 0)
        self.assertEqual(calls, [])

    def test_no_continuation_after_data_change(self):
        self.state.log_action(case_id="TC2", app="portal", tool="browser_click", args={}, approval="approved")
        calls = []
        t = lambda *a: calls.append(1) or StepResult(case_id="TC2", app="portal")
        sr = StepResult(case_id="TC2", app="portal", steps_done=[1], limit_hit=True)
        self.assertIs(_maybe_continue("portal", BRIEF, sr, self.state, t, 0), sr)
        self.assertEqual(calls, [])

    def test_stops_when_continuation_makes_no_progress(self):
        calls = []

        def t(app, brief, state):
            calls.append(1)
            return StepResult(case_id="TC2", app=app, steps_done=[], limit_hit=True)

        sr = StepResult(case_id="TC2", app="portal", steps_done=[1], limit_hit=True)
        out = _maybe_continue("portal", BRIEF, sr, self.state, t, 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(out.limit_hit)


if __name__ == "__main__":
    unittest.main()
