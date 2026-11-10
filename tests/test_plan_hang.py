"""Planner hang: per-call idle timeout and plan deadline must cancel and free the semaphore."""
from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from agent_tester.agents import llm as L
from agent_tester.agents import planner as P
from agent_tester.models import TestPlan


class FakeEv:
    def __init__(self, text="", final=False):
        self.content = mock.Mock(parts=[mock.Mock(text=text)]) if text is not None else None
        self._final = final
        self.author = "planner"
        self.usage_metadata = None

    def is_final_response(self):
        return self._final


class PlanHangCancel(unittest.TestCase):
    def tearDown(self):
        L._sem = None

    def test_idle_no_text_fails_attempt_and_releases_sem(self):
        """Empty ADK keepalive events must fail the call and release the LLM semaphore."""

        class HangRunner:
            def __init__(self, *a, **k):
                pass

            async def run_async(self, **kw):
                # Keepalive-style empty events forever (smoke: ~every 3 min with no text).
                while True:
                    yield FakeEv(text="", final=False)
                    await asyncio.sleep(0.04)

        async def create_session(**kw):
            return None

        settings = mock.Mock(llm_max_concurrency=1)
        with mock.patch.object(L, "LLM_IDLE_TIMEOUT", 0.12), \
                mock.patch.object(L, "LLM_TIMEOUT", 2.0), \
                mock.patch("google.adk.runners.Runner", HangRunner), \
                mock.patch("google.adk.sessions.InMemorySessionService") as Svc, \
                mock.patch("google.genai.types"):
            Svc.return_value.create_session = create_session
            t0 = time.monotonic()
            with self.assertRaises(L.LLMOutputError) as cm:
                asyncio.run(L.run_agent(mock.Mock(), "plan", settings))
            elapsed = time.monotonic() - t0
        self.assertIn("no text", str(cm.exception).lower())
        self.assertLess(elapsed, 2.0)
        # Semaphore must be free for the next caller (retry must not starve).
        self.assertIsNotNone(L._sem)
        self.assertEqual(L._sem._value, 1)

    def test_plan_deadline_cancels_hung_batch_and_frees_slot(self):
        """A batch that never returns is cancelled by PLAN_MAX_SECONDS; retries are not blocked."""
        started = []
        held = asyncio.Event()

        async def hang(settings, pack, text, tid, env, only_case=None, repair=True, **kw):
            started.append(only_case)
            await held.wait()  # never set — simulates hung HTTP
            return TestPlan.model_validate({
                "task_id": tid, "env": env,
                "cases": [{"id": only_case, "title": "x", "type": "ui", "expected": "ok",
                           "steps": [{"text": "look"}]}],
            })

        text = "### A1\nbody\n### A2\nbody\n"
        ids = ["A1", "A2"]
        with mock.patch.object(P, "plan_task", hang), \
                mock.patch.object(P, "PLAN_BATCH_TOKENS", 10_000), \
                mock.patch.object(P, "PLAN_CONCURRENCY", 1), \
                mock.patch.object(P, "PLAN_MAX_SECONDS", 1), \
                mock.patch.object(P, "PLAN_EMPTY_RETRIES", 0), \
                mock.patch.object(P, "PLAN_MAX_ATTEMPTS", 4):
            t0 = time.monotonic()
            with self.assertRaises(L.LLMOutputError) as cm:
                asyncio.run(P.plan_in_batches(None, None, text, 7, "staging", ids, repair=False))
            elapsed = time.monotonic() - t0
        self.assertIn("time cap", str(cm.exception).lower())
        self.assertIn("Resume with:", str(cm.exception))
        self.assertLess(elapsed, 4.0)
        self.assertTrue(started)

    def test_size_batches_caps_near_six(self):
        ids = [f"C{i}" for i in range(1, 20)]
        text = "\n".join(f"### {i}\n" + ("word " * 50) for i in ids)
        with mock.patch.object(P, "PLAN_BATCH", 6), mock.patch.object(P, "PLAN_BATCH_TOKENS", 100_000):
            batches = P.size_batches(text, ids)
        self.assertTrue(all(len(b) <= 6 for b in batches), batches)
        self.assertGreater(len(batches), 2)


if __name__ == "__main__":
    unittest.main()
