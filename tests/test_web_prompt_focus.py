"""Polling must not steal focus or wipe typed text in #guideIn / #stuckGuide / #ans.

Reproduces the live-run bug: renderPrompt rewrote pin.innerHTML on every stuck poll
(and re-focused) because the early-return skipped when j.stuck was set and the
focus key included JSON.stringify(j.stuck).
"""
from __future__ import annotations

import json
import threading
import time
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "agent_tester" / "web"


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class WebPromptFocus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._stuck = {
            "case_id": "C1", "goal": "Read wallet", "app": "portal", "step_label": "step 1",
            "page_title": "Home", "page_path": "/", "tried": ["click A"], "seeing": ["menu"],
            "guess": "loop", "why": "repeated click", "suggestions": ["try Search"],
            "thread": [], "screenshot": None, "timeout_sec": 600,
        }
        cls._tick = 0
        cls._job = {
            "id": "job-focus", "running": True, "waiting": True, "run_dir": "demo-run",
            "prompt": "Stuck on C1 — Options: (1) send a chat (2) skip (3) stop",
            "ask_id": "ask-focus-1", "secret": False,
            "prompt_kind": "stuck_chat", "prompt_title": "Stuck — waiting for your answer",
            "prompt_timeout_sec": 600, "waiting_since": time.time() - 5,
            "stuck": dict(cls._stuck), "out": "", "len": 0, "code": None,
        }

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=str(WEB), **k)

            def log_message(self, *a):  # noqa: ARG002
                return

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path == "/api/version":
                    return self._json({"stale": False, "version": "test"})
                if path == "/api/jobs":
                    return self._json([cls._job])
                if path.startswith("/api/jobs/") and path.endswith("/out"):
                    # Mutate stuck payload each poll (old bug: this changed the focus key).
                    cls._tick += 1
                    stuck = dict(cls._stuck)
                    stuck["tried"] = [f"click A{cls._tick}", f"click B{cls._tick}"]
                    stuck["seeing"] = [f"menu-{cls._tick}"]
                    stuck["thread"] = list(cls._stuck.get("thread") or [])
                    job = dict(cls._job)
                    job["stuck"] = stuck
                    job["out"] = ""
                    job["len"] = 0
                    return self._json(job)
                if path == "/api/runs":
                    return self._json([])
                if path.startswith("/api/runs/"):
                    return self._json({
                        "summary": {"task": "1", "env": "staging", "status": "running",
                                    "cases": 1, "done": 0, "counts": {}, "last": "stuck"},
                        "plan": {"cases": [{"id": "C1", "title": "Wallet", "steps": []}]},
                        "results": {}, "shots": [], "reports": [], "action_log": [],
                        "mode": {}, "phase": None, "now_doing": {}, "attempt": 1,
                    })
                return super().do_GET()

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                _ = self.rfile.read(n) if n else b""
                return self._json({"ok": True})

            def _json(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls._port = cls._httpd.server_address[1]
        cls._thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls._thread.start()
        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])

    @classmethod
    def tearDownClass(cls):
        try:
            cls._browser.close()
        finally:
            cls._pw.stop()
            cls._httpd.shutdown()

    def _page(self):
        page = self._browser.new_page(viewport={"width": 1366, "height": 900})
        page.goto(f"http://127.0.0.1:{self._port}/", wait_until="domcontentloaded")
        page.wait_for_function("() => !!document.getElementById('guideIn')")
        # Open the demo run so guidance is “live” and refresh() has a cur.
        page.evaluate("() => { cur = 'demo-run'; guideShow(); }")
        page.wait_for_function("() => !!document.getElementById('stuckGuide')", timeout=5000)
        return page

    def test_stuck_and_guide_survive_poll_ticks(self):
        page = self._page()
        try:
            page.fill("#guideIn", "guide draft — phone is under Contact")
            page.fill("#stuckGuide", "stuck draft — try the Search icon")
            page.focus("#guideIn")
            # Several poll ticks with changing stuck JSON (1s interval + force renderPrompt).
            for i in range(4):
                page.evaluate(
                    """(n) => {
                      const j = Object.assign({}, job || {}, {
                        waiting: true,
                        prompt: job && job.prompt,
                        ask_id: job && job.ask_id,
                        waiting_since: job && job.waiting_since,
                        prompt_kind: 'stuck_chat',
                        stuck: Object.assign({}, (job && job.stuck) || {}, {
                          tried: ['click-'+n], seeing: ['see-'+n]
                        })
                      });
                      job = j;
                      renderPrompt(j);
                    }""",
                    i + 1,
                )
                page.wait_for_timeout(50)
            guide = page.input_value("#guideIn")
            stuck = page.input_value("#stuckGuide")
            focused = page.evaluate("() => document.activeElement && document.activeElement.id")
            self.assertEqual(guide, "guide draft — phone is under Contact")
            self.assertEqual(stuck, "stuck draft — try the Search icon")
            self.assertEqual(focused, "guideIn")
        finally:
            page.close()

    def test_ans_survives_poll_when_generic_ask(self):
        page = self._browser.new_page(viewport={"width": 1366, "height": 768})
        try:
            page.goto(f"http://127.0.0.1:{self._port}/", wait_until="domcontentloaded")
            page.wait_for_selector("#guideIn")
            page.evaluate(
                """() => {
                  lastPromptKey = '';
                  job = {
                    id: 'job-q', running: true, waiting: true, run_dir: 'demo-run',
                    prompt: 'Which admin user? [Enter = admin]', ask_id: 'ask-q',
                    secret: false, prompt_kind: 'question', prompt_title: 'Account',
                    prompt_timeout_sec: 120, waiting_since: Date.now()/1000 - 2, stuck: null
                  };
                  renderPrompt(job);
                }"""
            )
            page.wait_for_selector("#ans")
            page.fill("#ans", "admin@example.test")
            page.focus("#ans")
            for i in range(3):
                page.evaluate(
                    """(n) => {
                      job = Object.assign({}, job, {waiting: true, stuck: null});
                      renderPrompt(job);
                    }""",
                    i,
                )
            self.assertEqual(page.input_value("#ans"), "admin@example.test")
            self.assertEqual(
                page.evaluate("() => document.activeElement && document.activeElement.id"), "ans")
        finally:
            page.close()

    def test_new_ask_does_not_steal_from_guideIn(self):
        page = self._page()
        try:
            page.fill("#guideIn", "still typing guidance")
            page.focus("#guideIn")
            page.evaluate(
                """() => {
                  lastPromptKey = 'old';
                  job = Object.assign({}, job, {
                    ask_id: 'ask-new-2',
                    prompt: 'Stuck on C1 — Options: (1) send a chat (2) skip (3) stop',
                    waiting_since: Date.now()/1000,
                    stuck: Object.assign({}, job.stuck || {}, {tried: ['fresh']})
                  });
                  renderPrompt(job);
                }"""
            )
            page.wait_for_timeout(100)
            focused = page.evaluate("() => document.activeElement && document.activeElement.id")
            self.assertEqual(focused, "guideIn")
            self.assertEqual(page.input_value("#guideIn"), "still typing guidance")
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
