"""Stuck-card screenshot: use the existing session, never a second browser, fall back fast.

Also: empty page title falls back to the URL path instead of '(untitled)'.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_tester.agents.tester import TesterSession, _apply_page_title_fallback
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence

PACK = Path(__file__).resolve().parents[1] / "packs/savostore"


class CaptureStuckAskShot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = RunState(Path(self.tmp.name) / "run")
        pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
        self.s = TesterSession(
            "crm",
            {"case_id": "E1", "title": "Earn", "steps": [{"n": 1, "text": "pay", "app": "crm"}],
             "area": "rewards"},
            self.st, pol, Evidence(self.st.dir), max_calls=30, max_minutes=5)
        self.s.current_step = 1
        self.s.last_snapshot = (
            "Page URL: https://example.test/index.php?module=Orders\n"
            "Page Title: \n"
            '- menuitem "Orders" [ref=e1]\n'
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _write_shot(self, name: str) -> Path:
        p = self.s.ev.shots / name
        p.write_bytes(b"png")
        if name not in self.s.result.screenshots:
            self.s.result.screenshots.append(name)
        return p

    def test_running_loop_does_not_spawn_second_loop_or_block(self):
        self._write_shot("e1-01-orders.png")
        self.s.toolset = object()
        self.s._tool_context = object()
        ran = {"n": 0}

        def guard(*a, **k):
            ran["n"] += 1
            raise AssertionError("asyncio.run must not start a second loop")

        async def go():
            with mock.patch("asyncio.run", side_effect=guard), \
                    mock.patch("concurrent.futures.ThreadPoolExecutor") as pool:
                t0 = time.monotonic()
                name = self.s._capture_stuck_ask_shot()
                elapsed = time.monotonic() - t0
            return name, elapsed, pool.called

        name, elapsed, pooled = asyncio.run(go())
        self.assertEqual(name, "e1-01-orders.png")
        self.assertLess(elapsed, 1.0)
        self.assertFalse(pooled)
        self.assertEqual(ran["n"], 0)

    def test_prefers_prefetched_fresh_shot(self):
        self._write_shot("e1-01-orders.png")
        self._write_shot("e1-02-stuck-ask.png")
        self.s._fresh_stuck_shot = "e1-02-stuck-ask.png"
        self.assertEqual(self.s._capture_stuck_ask_shot(), "e1-02-stuck-ask.png")

    def test_skips_other_app_login_helper_shot(self):
        self._write_shot("login-portal-admin.png")
        got = self.s._capture_stuck_ask_shot()
        self.assertIsNone(got)

    def test_page_title_falls_back_to_path(self):
        s = _apply_page_title_fallback({
            "page_title": "(untitled)",
            "page_path": "/index.php?module=Orders",
        })
        self.assertEqual(s["page_title"], "/index.php?module=Orders")
        kept = _apply_page_title_fallback({"page_title": "Home", "page_path": "/"})
        self.assertEqual(kept["page_title"], "Home")
        empty = _apply_page_title_fallback({"page_title": "(untitled)", "page_path": "(unknown page)"})
        self.assertEqual(empty["page_title"], "(untitled)")
        # Blank "Page Title:" line must not keep the next accessibility row as the title.
        absorbed = _apply_page_title_fallback(
            {"page_title": 'menuitem "Orders"', "page_path": "/index.php?module=Orders"},
            "Page URL: https://example.test/index.php?module=Orders\nPage Title: \n- menuitem \"Orders\"\n",
        )
        self.assertEqual(absorbed["page_title"], "/index.php?module=Orders")

    def test_stuck_summary_untitled_uses_url_path(self):
        summary = self.s._stuck_summary("looped on Orders")
        self.assertNotEqual(summary["page_title"], "(untitled)")
        self.assertIn("/index.php", summary["page_title"])
        self.assertIn("/index.php", summary["page_path"])

    def test_prepare_uses_existing_session_and_stores_name(self):
        async def fake_call(name, args):
            if name == "browser_snapshot":
                return (
                    "Page URL: https://example.test/index.php?module=Orders\n"
                    "Page Title: \n"
                )
            if name == "browser_take_screenshot":
                Path(args["filename"]).write_bytes(b"png")
                return "ok"
            return ""

        self.s.toolset = object()
        with mock.patch("agent_tester.helpers.adk_caller", return_value=fake_call):
            name = asyncio.run(self.s._prepare_stuck_ask_shot(object()))
        self.assertIsNotNone(name)
        self.assertIn("stuck-ask", name)
        self.assertEqual(self.s._fresh_stuck_shot, name)
        self.assertEqual(self.s._capture_stuck_ask_shot(), name)
        self.assertTrue((self.s.ev.shots / name).is_file())

    def test_before_tool_async_prefetches_when_likely(self):
        self.s._recent = ["click:orders", "click:orders"]
        self.s.toolset = object()
        tool = SimpleNamespace(name="browser_click")

        async def prep(tc):  # noqa: ARG001
            self.s._fresh_stuck_shot = "e1-02-stuck-ask.png"
            return "e1-02-stuck-ask.png"

        async def go():
            with mock.patch.object(self.s, "_prepare_stuck_ask_shot", side_effect=prep) as m, \
                    mock.patch.object(self.s, "before_tool", return_value={"ok": 1}) as bt:
                out = await self.s.before_tool_async(tool, {"element": "Orders"}, object())
            return out, m.await_count, bt.called

        out, n, called = asyncio.run(go())
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(n, 1)
        self.assertTrue(called)

    def test_before_tool_async_skips_prefetch_when_not_likely(self):
        self.s._recent = ["click:orders"]
        self.s.toolset = object()
        tool = SimpleNamespace(name="browser_click")

        async def go():
            with mock.patch.object(self.s, "_prepare_stuck_ask_shot") as m, \
                    mock.patch.object(self.s, "before_tool", return_value=None):
                await self.s.before_tool_async(tool, {"element": "Orders"}, object())
            return m.await_count

        self.assertEqual(asyncio.run(go()), 0)

    def test_prefetch_timeout_falls_back_quickly(self):
        self.s._recent = ["click:orders", "click:orders"]
        self.s.toolset = object()
        self._write_shot("e1-01-orders.png")

        async def hang(tc):  # noqa: ARG001
            await asyncio.sleep(5)
            return "late.png"

        async def go():
            with mock.patch.object(self.s, "_prepare_stuck_ask_shot", side_effect=hang), \
                    mock.patch("agent_tester.agents.tester._STUCK_SHOT_WAIT", 0.15), \
                    mock.patch.object(self.s, "before_tool", return_value=None):
                t0 = time.monotonic()
                await self.s.before_tool_async(
                    SimpleNamespace(name="browser_click"), {"element": "Orders"}, object())
                elapsed = time.monotonic() - t0
            return elapsed, self.s._capture_stuck_ask_shot()

        elapsed, name = asyncio.run(go())
        self.assertLess(elapsed, 1.0)
        self.assertEqual(name, "e1-01-orders.png")


if __name__ == "__main__":
    unittest.main()
