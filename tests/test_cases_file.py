import tempfile, unittest
from pathlib import Path
from unittest import mock

from agent_tester import casegen
from agent_tester.main import read_cases_file, with_cases_file
from agent_tester.preflight.redmine import TaskBundle


class CasesFile(unittest.TestCase):
    def test_file_stands_in_for_test_cases_task(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "test-cases.md"
            f.write_text("## TC1 Earn points\nSteps: place an order\nExpected: points added\n")
            text = read_cases_file(f)
            b = TaskBundle(issue={"id": 165177, "subject": "Savo Rewards", "project": {"id": 7}}, test_case_task=None)
            self.assertTrue(casegen.needs_generation(b))
            b2 = with_cases_file(b, text, f.name)
            self.assertFalse(casegen.needs_generation(b2))
            self.assertIn("Earn points", b2.all_text())
            self.assertIsNone(b.test_case_task)        # the original bundle is untouched

    def test_rejects_wrong_type_missing_or_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(read_cases_file(Path(d) / "nope.md"))
            (Path(d) / "x.pdf").write_text("a")
            self.assertIsNone(read_cases_file(Path(d) / "x.pdf"))
            (Path(d) / "e.md").write_text("  \n")
            self.assertIsNone(read_cases_file(Path(d) / "e.md"))


if __name__ == "__main__":
    unittest.main()


class CaseIdsAndBatches(unittest.TestCase):
    def test_ids_from_headings(self):
        from agent_tester.main import case_ids_in
        text = ("# Title\n## Context\n| E0, E1 | 165348 |\n### E0. Baseline\n### B1 (also B16-2 and C6). Hard reload\n"
                "### B16-3. Away\n### A12-final. Invariant\n### TC3: Something\n#### C8 (also C11). No pending\n")
        self.assertEqual(case_ids_in(text), ["E0", "B1", "B16-3", "A12-final", "TC3", "C8"])

    def test_batches_keep_order_and_ids(self):
        import asyncio
        from agent_tester.agents import planner as P
        from agent_tester.models import TestPlan
        calls = []

        async def fake(settings, pack, text, tid, env, only_case=None, repair=True, **kw):
            calls.append(only_case)
            ids = only_case.split(",")
            return TestPlan.model_validate({"task_id": tid, "env": env, "cases": [
                {"id": i, "title": i, "type": "ui", "expected": "ok", "steps": [{"text": "look"}]} for i in ids]})
        orig, P.plan_task = P.plan_task, fake
        # Force several token-budget batches (short ids alone fit in one call).
        text = "\n".join(f"### A{i}\n" + ("case body " * 400) for i in range(1, 13))
        try:
            ids = [f"A{i}" for i in range(1, 13)]
            with mock.patch.object(P, "PLAN_BATCH_TOKENS", 400), mock.patch.object(P, "PLAN_CONCURRENCY", 2):
                plan = asyncio.run(P.plan_in_batches(None, None, text, 1, "staging", ids, repair=False))
        finally:
            P.plan_task = orig
        self.assertEqual([c.id for c in plan.cases], ids)
        self.assertGreaterEqual(len(calls), 2)
