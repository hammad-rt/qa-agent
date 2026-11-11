"""Regression: a slow first ADK event (longer than one poll slice) must not cancel the stream.

Before the fix, asyncio.wait_for(aiter.__anext__(), 5s) cancelled the ADK runner after 5s,
so every real planner call failed with 'no text for the whole LLM call'.
"""
import asyncio
import unittest
from unittest import mock

from agent_tester.agents import llm


class SlowStream:
    def __init__(self, delay, items):
        self.delay, self.items, self.cancelled = delay, list(items), False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if not self.items:
            raise StopAsyncIteration
        return self.items.pop(0)


async def _collect(stream, idle, deadline=None):
    out = []
    async for ev in llm._iter_events_with_idle(stream, idle_sec=idle, call_deadline=deadline):
        out.append(ev)
    return out


class IdleNoCancelTest(unittest.TestCase):
    def test_slow_first_event_longer_than_slice_is_not_cancelled(self):
        # slice = min(5, idle) = 0.05s; event arrives after 0.2s; idle budget is 1s.
        stream = SlowStream(0.2, ["a", "b"])
        with mock.patch.object(llm, "human_wait_active", return_value=False):
            out = asyncio.run(self._run_small_slice(stream, idle=1.0))
        self.assertEqual(out, ["a", "b"])
        self.assertFalse(stream.cancelled)

    async def _run_small_slice(self, stream, idle):
        # Force a tiny poll slice by passing idle via a wrapper: slice = min(5, idle_sec),
        # so emulate a 0.05s slice with a 1s idle budget by patching asyncio.wait's timeout.
        real_wait = asyncio.wait

        async def small_wait(fs, timeout=None, **kw):
            return await real_wait(fs, timeout=min(timeout, 0.05) if timeout else timeout, **kw)

        with mock.patch.object(llm.asyncio, "wait", small_wait):
            return await _collect(stream, idle)

    def test_idle_exceeded_still_raises_and_cancels(self):
        stream = SlowStream(2.0, ["a"])
        with mock.patch.object(llm, "human_wait_active", return_value=False):
            with self.assertRaises(llm.LLMOutputError):
                asyncio.run(_collect(stream, idle=0.2))
        self.assertTrue(stream.cancelled)

    def test_deadline_still_raises(self):
        import time
        stream = SlowStream(2.0, ["a"])
        with mock.patch.object(llm, "human_wait_active", return_value=False):
            with self.assertRaises(llm.LLMOutputError) as cm:
                asyncio.run(_collect(stream, idle=10.0, deadline=time.monotonic() + 0.2))
        self.assertIn("per-call timeout", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
