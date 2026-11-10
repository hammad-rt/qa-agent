"""Planner batching: empty / {} LLM replies split, retry, and stop with a clear error."""
from __future__ import annotations

import asyncio
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from agent_tester.agents import llm as L
from agent_tester.agents import planner as P
from agent_tester.models import TestPlan


def _plan(tid, env, ids):
    return TestPlan.model_validate({
        "task_id": tid, "env": env,
        "cases": [{"id": i, "title": i, "type": "ui", "expected": "ok",
                   "steps": [{"text": "look"}]} for i in ids],
    })


class PlanEmptyOutput(unittest.TestCase):
    def test_run_json_rejects_empty_object_and_logs(self):
        async def fake_agent(_agent, _ask, _settings, **kw):
            return "{}"

        buf = io.StringIO()
        with mock.patch.object(L, "run_agent", fake_agent), redirect_stdout(buf):
            with self.assertRaises(L.LLMOutputError) as cm:
                asyncio.run(L.run_json(None, "plan", mock.Mock(), validate=lambda d: d, attempts=2))
        self.assertIn("empty object", str(cm.exception))
        self.assertIn("attempt 1/2", buf.getvalue())

    def test_run_json_rejects_blank_text(self):
        async def fake_agent(_agent, _ask, _settings, **kw):
            return "   "

        with mock.patch.object(L, "run_agent", fake_agent):
            with self.assertRaises(L.LLMOutputError) as cm:
                asyncio.run(L.run_json(None, "plan", mock.Mock(), validate=lambda d: d, attempts=1))
        self.assertIn("unusable JSON", str(cm.exception))

    def test_batch_splits_on_empty_then_succeeds(self):
        """First call for a 4-id batch returns {}; split halves then succeed."""
        calls = []

        async def fake(settings, pack, text, tid, env, only_case=None, repair=True, **kw):
            ids = only_case.split(",")
            calls.append(list(ids))
            if len(ids) >= 4:
                raise L.LLMOutputError("planner: empty object {}")
            return _plan(tid, env, ids)

        text = "\n".join(f"### A{i}\n" + ("case body " * 200) for i in range(1, 5))
        ids = [f"A{i}" for i in range(1, 5)]
        buf = io.StringIO()
        with mock.patch.object(P, "plan_task", fake), \
                mock.patch.object(P, "PLAN_BATCH_TOKENS", 10_000), \
                mock.patch.object(P, "PLAN_CONCURRENCY", 1), \
                mock.patch.object(P, "PLAN_EMPTY_RETRIES", 2), \
                mock.patch.object(P, "PLAN_MAX_ATTEMPTS", 8), \
                redirect_stdout(buf):
            plan = asyncio.run(P.plan_in_batches(None, None, text, 1, "staging", ids, repair=False))
        self.assertEqual([c.id for c in plan.cases], ids)
        self.assertTrue(any(len(c) >= 4 for c in calls))
        self.assertTrue(any(len(c) == 2 for c in calls))
        self.assertIn("empty plan", buf.getvalue())
        self.assertIn("retrying with", buf.getvalue())

    def test_fewer_cases_than_asked_counts_as_failure(self):
        """Real-shaped partial plan (subset of asked ids) must fail and split/retry."""
        calls = []

        async def partial(settings, pack, text, tid, env, only_case=None, repair=True, **kw):
            ids = only_case.split(",")
            calls.append(list(ids))
            if len(ids) > 1:
                return _plan(tid, env, ids[:1])  # fewer than asked
            return _plan(tid, env, ids)

        text = "### A1\nbody\n### A2\nbody\n### A3\nbody\n### A4\nbody\n"
        ids = ["A1", "A2", "A3", "A4"]
        buf = io.StringIO()
        with mock.patch.object(P, "plan_task", partial), \
                mock.patch.object(P, "PLAN_BATCH_TOKENS", 10_000), \
                mock.patch.object(P, "PLAN_CONCURRENCY", 1), \
                mock.patch.object(P, "PLAN_EMPTY_RETRIES", 2), \
                mock.patch.object(P, "PLAN_MAX_ATTEMPTS", 10), \
                redirect_stdout(buf):
            plan = asyncio.run(P.plan_in_batches(None, None, text, 1, "staging", ids, repair=False))
        self.assertEqual(sorted(c.id for c in plan.cases), ids)
        self.assertTrue(any(len(c) == 1 for c in calls), calls)
        self.assertIn("empty plan", buf.getvalue())

    def test_batch_gives_up_with_resume_hint(self):
        async def always_empty(settings, pack, text, tid, env, only_case=None, repair=True, **kw):
            raise L.LLMOutputError("planner: the reply was an empty object {}")

        ids = ["A1", "A2"]
        text = "### A1\nbody\n### A2\nbody\n"
        with mock.patch.object(P, "plan_task", always_empty), \
                mock.patch.object(P, "PLAN_BATCH_TOKENS", 10_000), \
                mock.patch.object(P, "PLAN_CONCURRENCY", 1), \
                mock.patch.object(P, "PLAN_EMPTY_RETRIES", 1), \
                mock.patch.object(P, "PLAN_MAX_ATTEMPTS", 6):
            with self.assertRaises(L.LLMOutputError) as cm:
                asyncio.run(P.plan_in_batches(None, None, text, 42, "staging", ids, repair=False))
        msg = str(cm.exception)
        self.assertIn("Resume with:", msg)
        self.assertIn("42", msg)

    def test_status_line_tracks_each_batch(self):
        async def fake(settings, pack, text, tid, env, only_case=None, repair=True, **kw):
            await asyncio.sleep(0.01)
            return _plan(tid, env, only_case.split(","))

        # Force 2 batches via tiny token budget.
        text = "\n".join(f"### B{i}\n" + ("x" * 800) for i in range(1, 5))
        ids = [f"B{i}" for i in range(1, 5)]
        buf = io.StringIO()
        with mock.patch.object(P, "plan_task", fake), \
                mock.patch.object(P, "PLAN_BATCH_TOKENS", 100), \
                mock.patch.object(P, "PLAN_CONCURRENCY", 2), \
                redirect_stdout(buf):
            asyncio.run(P.plan_in_batches(None, None, text, 1, "staging", ids, repair=False))
        out = buf.getvalue()
        self.assertIn("Batch 1/", out)
        self.assertIn("starting", out)
        self.assertIn("Planning status:", out)
        self.assertIn("done", out)


if __name__ == "__main__":
    unittest.main()
