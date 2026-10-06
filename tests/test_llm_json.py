"""JSON from the model: repair common slips, and ask the model to correct itself instead of crashing."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.agents import llm
from agent_tester.models import TestPlan

BROKEN = """Here is the plan:
```json
{
  "task_id": 180966,
  "cases": [
    {"id": "TC1", "title": "a", "expected": "x"}
    {"id": "TC2", "title": "b", "expected": "y"},
  ]
  "requirements": []
}
```"""


class Repair(unittest.TestCase):
    def test_missing_and_trailing_commas(self):
        d = llm.extract_json(BROKEN)
        self.assertEqual([c["id"] for c in d["cases"]], ["TC1", "TC2"])

    def test_valid_json_untouched(self):
        self.assertEqual(llm.extract_json('{"a": "x, y", "b": [1, 2]}'), {"a": "x, y", "b": [1, 2]})

    def test_no_json_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            llm.extract_json("sorry, I can't")


class SelfCorrect(unittest.TestCase):
    def run_with(self, replies, validate=None, attempts=3):
        seen = []

        async def fake(agent, prompt, settings, **kw):
            seen.append(prompt)
            return replies.pop(0)

        with tempfile.TemporaryDirectory() as d, mock.patch.object(llm, "run_agent", fake):
            try:
                out = asyncio.run(llm.run_json(None, "PLAN IT", None, validate=validate, label="planner",
                                               raw_dir=Path(d), attempts=attempts))
            finally:
                files = sorted(p.name for p in Path(d).iterdir())
        return out, seen, files

    def test_retries_with_error_then_succeeds(self):
        out, seen, files = self.run_with(["{not json at all", '{"task_id": 1, "cases": []}'],
                                         validate=TestPlan.model_validate)
        self.assertIsInstance(out, TestPlan)
        self.assertIn("could not be used", seen[1])
        self.assertEqual(files, ["planner_raw_1.txt"])

    def test_validation_error_is_fed_back(self):
        out, seen, _ = self.run_with(['{"task_id": "abc"}', '{"task_id": 2}'], validate=TestPlan.model_validate)
        self.assertEqual(out.task_id, 2)
        self.assertIn("task_id", seen[1])

    def test_gives_up_with_clear_error(self):
        with self.assertRaises(llm.LLMOutputError) as cm:
            self.run_with(["nope", "nope", "nope"])
        self.assertIn("3 times", str(cm.exception))


class JudgeCrash(unittest.TestCase):
    def test_judge_error_becomes_agent_incomplete(self):
        from agent_tester.coordinator import run_plan
        from agent_tester.models import PlannedCase, Step, StepResult
        from agent_tester.run_state import RunState
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "1-20261006-130000")
            plan = TestPlan(task_id=1, cases=[PlannedCase(id="TC1", title="t", steps=[Step(text="s", app="portal")], expected="x")])
            st.set_plan(plan)
            tester = lambda app, brief, state: StepResult(case_id=brief["case_id"], app=app, steps_done=[1])

            def judge(*a):
                raise llm.LLMOutputError("judge_TC1: the model returned unusable JSON 3 times")
            res = run_plan(plan, st, {"portal": tester}, judge)
            r = res[0] if isinstance(res, list) else st.result("TC1")
            self.assertEqual(st.result("TC1").verdict, "AGENT INCOMPLETE")
            self.assertIn("judge could not decide", st.result("TC1").note)


if __name__ == "__main__":
    unittest.main()
