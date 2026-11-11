"""Regression: a slow first ADK event (longer than one poll slice) must not cancel the stream.

Before the fix, asyncio.wait_for(aiter.__anext__(), 5s) cancelled the ADK runner after 5s,
so every real planner call failed with 'no text for the whole LLM call'.

Also: LLM_TIMEOUT must not cap a whole tester turn; idle skips tool-execution gaps;
an optional session cap pauses during human waits.
"""
import asyncio
import time
import unittest
from types import SimpleNamespace
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


class DelayedStream:
    """Each item is (delay, event). delay is waited just before yielding that event."""

    def __init__(self, pairs):
        self.pairs, self.cancelled = list(pairs), False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.pairs:
            raise StopAsyncIteration
        delay, item = self.pairs.pop(0)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return item


def _tool_ev(name="browser_click"):
    return SimpleNamespace(content=SimpleNamespace(parts=[
        SimpleNamespace(function_call=SimpleNamespace(name=name), function_response=None, text=None),
    ]))


def _text_ev(text, *, final=False):
    ev = SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(text=text, function_call=None)]),
        _final=final,
        author="agent",
        usage_metadata=None,
    )
    ev.is_final_response = lambda: ev._final
    return ev


async def _collect(stream, idle, deadline=None, session_cap_sec=None):
    out = []
    async for ev in llm._iter_events_with_idle(
            stream, idle_sec=idle, call_deadline=deadline, session_cap_sec=session_cap_sec):
        out.append(ev)
    return out


class IdleNoCancelTest(unittest.TestCase):
    def test_slow_first_event_longer_than_slice_is_not_cancelled(self):
        # poll slice 0.05s; each event arrives after 0.2s; idle budget is 1s.
        stream = SlowStream(0.2, ["a", "b"])
        with mock.patch.object(llm, "human_wait_active", return_value=False), \
                mock.patch.object(llm, "_POLL_SLICE", 0.05):
            out = asyncio.run(_collect(stream, idle=1.0))
        self.assertEqual(out, ["a", "b"])
        self.assertFalse(stream.cancelled)

    def test_idle_exceeded_still_raises_and_cancels(self):
        stream = SlowStream(2.0, ["a"])
        with mock.patch.object(llm, "human_wait_active", return_value=False):
            with self.assertRaises(llm.LLMOutputError):
                asyncio.run(_collect(stream, idle=0.2))
        self.assertTrue(stream.cancelled)

    def test_deadline_still_raises(self):
        # Adjusted: call_deadline is now an optional session cap, not the 180s
        # whole-turn / per-call cap (that cap was the bug). The path must still
        # raise when a deadline is set and expires with no human wait.
        stream = SlowStream(2.0, ["a"])
        with mock.patch.object(llm, "human_wait_active", return_value=False):
            with self.assertRaises(llm.LLMOutputError) as cm:
                asyncio.run(_collect(stream, idle=10.0, deadline=time.monotonic() + 0.2,
                                     session_cap_sec=0.2))
        self.assertIn("session cap", str(cm.exception))

    def test_idle_skips_gap_after_function_call(self):
        """A long tool-execution gap after a function_call must not count as model idle."""
        call_ev, resp_ev = _tool_ev(), _text_ev("ok")
        stream = DelayedStream([(0.02, call_ev), (0.4, resp_ev)])
        with mock.patch.object(llm, "human_wait_active", return_value=False), \
                mock.patch.object(llm, "_POLL_SLICE", 0.05):
            out = asyncio.run(_collect(stream, idle=0.15))
        self.assertEqual(out, [call_ev, resp_ev])
        self.assertFalse(stream.cancelled)

    def test_idle_still_fires_between_plain_events(self):
        stream = DelayedStream([(0.02, "a"), (0.4, "b")])
        with mock.patch.object(llm, "human_wait_active", return_value=False), \
                mock.patch.object(llm, "_POLL_SLICE", 0.05):
            with self.assertRaises(llm.LLMOutputError) as cm:
                asyncio.run(_collect(stream, idle=0.15))
        self.assertIn("no text", str(cm.exception).lower())
        self.assertTrue(stream.cancelled)

    def test_human_wait_extends_session_deadline(self):
        """A stuck-card wait longer than the session cap must not kill the turn."""
        stream = DelayedStream([(0.02, "a"), (0.4, "b")])
        waiting = {"on": False}

        async def go():
            out = []
            deadline = time.monotonic() + 0.2
            agen = llm._iter_events_with_idle(
                stream, idle_sec=5.0, call_deadline=deadline, session_cap_sec=0.2)
            out.append(await agen.__anext__())
            waiting["on"] = True

            async def end_wait():
                await asyncio.sleep(0.35)
                waiting["on"] = False

            t = asyncio.create_task(end_wait())
            out.append(await agen.__anext__())
            await t
            async for ev in agen:
                out.append(ev)
            return out

        with mock.patch.object(llm, "human_wait_active", side_effect=lambda: waiting["on"]), \
                mock.patch.object(llm, "_POLL_SLICE", 0.05):
            out = asyncio.run(go())
        self.assertEqual(out, ["a", "b"])
        self.assertFalse(stream.cancelled)

    def test_mock_empty_event_is_not_treated_as_tool(self):
        """FakeEv-style Mock parts must not look like function_call (would disable idle)."""
        ev = mock.Mock()
        ev.content = mock.Mock(parts=[mock.Mock(text="")])
        self.assertFalse(llm._event_starts_tool(ev))
        self.assertTrue(llm._event_starts_tool(_tool_ev()))


class WholeTurnCapRemoved(unittest.TestCase):
    def tearDown(self):
        llm._sem = None

    def _patch_runner(self, runner_cls):
        async def create_session(**kw):
            return None
        settings = mock.Mock(llm_max_concurrency=1)
        svc = mock.patch("google.adk.sessions.InMemorySessionService")
        run = mock.patch("google.adk.runners.Runner", runner_cls)
        typ = mock.patch("google.genai.types")
        return settings, svc, run, typ, create_session

    def test_run_agent_does_not_cap_whole_turn_at_llm_timeout(self):
        """A turn longer than LLM_TIMEOUT that keeps emitting events must complete.

        This is the regression: testers/discovery were killed at 180s mid-session.
        """

        class StreamRunner:
            def __init__(self, *a, **k):
                pass

            async def run_async(self, **kw):
                for i in range(5):
                    await asyncio.sleep(0.08)
                    yield _text_ev("x" if i < 4 else "done", final=(i == 4))

        settings, svc_p, run_p, typ_p, create_session = self._patch_runner(StreamRunner)
        with mock.patch.object(llm, "LLM_TIMEOUT", 0.15), \
                mock.patch.object(llm, "LLM_IDLE_TIMEOUT", 1.0), \
                mock.patch.object(llm, "TESTER_SESSION_MAX_SEC", 0), \
                run_p, svc_p as Svc, typ_p:
            Svc.return_value.create_session = create_session
            text = asyncio.run(llm.run_agent(mock.Mock(), "go", settings))
        self.assertEqual(text, "done")

    def test_run_agent_optional_session_cap_still_fires(self):
        class SlowRunner:
            def __init__(self, *a, **k):
                pass

            async def run_async(self, **kw):
                yield _text_ev("start", final=False)
                await asyncio.sleep(1.0)
                yield _text_ev("done", final=True)

        settings, svc_p, run_p, typ_p, create_session = self._patch_runner(SlowRunner)
        with mock.patch.object(llm, "LLM_TIMEOUT", 10.0), \
                mock.patch.object(llm, "LLM_IDLE_TIMEOUT", 10.0), \
                mock.patch.object(llm, "TESTER_SESSION_MAX_SEC", 0.2), \
                mock.patch.object(llm, "_POLL_SLICE", 0.05), \
                run_p, svc_p as Svc, typ_p:
            Svc.return_value.create_session = create_session
            with self.assertRaises(llm.LLMOutputError) as cm:
                asyncio.run(llm.run_agent(mock.Mock(), "go", settings))
        self.assertIn("session cap", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
