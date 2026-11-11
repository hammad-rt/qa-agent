"""Queued guidance, headed start flag, and always-visible UI chrome."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.web import server as W


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "agent_tester" / "web" / "index.html").read_text()


class SaveGuide(unittest.TestCase):
    def test_idle_run_queues_instead_of_409(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "1-x"
            d.mkdir()
            (d / "state.json").write_text(json.dumps({"status": "paused", "plan": {"task_id": 1}}))
            with mock.patch.object(W, "_live", return_value=False):
                r = W.save_guide(d, "the phone field is under Contact details")
            self.assertEqual(r, {"ok": True, "queued": True})
            rows = [json.loads(l) for l in (d / "guidance.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["when"], "queued")
            self.assertIn("phone field", rows[0]["text"])

    def test_live_run_is_not_queued(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "1-x"
            d.mkdir()
            with mock.patch.object(W, "_live", return_value=True):
                r = W.save_guide(d, "click Apply")
            self.assertEqual(r, {"ok": True, "queued": False})
            rows = [json.loads(l) for l in (d / "guidance.jsonl").read_text().splitlines()]
            self.assertEqual(rows[0]["when"], "live")

    def test_empty_text_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "1-x"
            d.mkdir()
            r = W.save_guide(d, "   ")
            self.assertIn("error", r)
            self.assertEqual(r.get("status"), 400)
            self.assertFalse((d / "guidance.jsonl").exists())


class HeadedFlag(unittest.TestCase):
    def test_apply_headed_appends_once(self):
        self.assertEqual(W.apply_headed(["test", "1", "--env", "staging"], True),
                         ["test", "1", "--env", "staging", "--headed"])
        self.assertEqual(W.apply_headed(["test", "1", "--headed"], True),
                         ["test", "1", "--headed"])
        self.assertEqual(W.apply_headed(["test", "1"], False), ["test", "1"])

    def test_job_sets_qa_headed_from_flag(self):
        captured = {}

        def fake_popen(cmd, **kw):
            captured["cmd"] = cmd
            captured["env"] = kw.get("env") or {}
            proc = mock.Mock()
            proc.stdout.fileno.return_value = 0
            proc.poll.return_value = 0
            return proc

        with mock.patch("agent_tester.web.server.subprocess.Popen", side_effect=fake_popen), \
             mock.patch("agent_tester.web.server.threading.Thread"), \
             mock.patch("agent_tester.web.server.os.read", return_value=b""):
            W.Job(["test", "1", "--headed"])
        self.assertEqual(captured["env"].get("QA_HEADED"), "1")
        self.assertIn("--headed", captured["cmd"])


class UiChrome(unittest.TestCase):
    def test_tabs_then_guidance_outside_live_pane(self):
        head = INDEX.index('class="head"')
        tabs = INDEX.index('id="tabBar"')
        guide = INDEX.index('id="guideRow"')
        live = INDEX.index('id="live"')
        self.assertLess(head, tabs)
        self.assertLess(tabs, guide)
        self.assertLess(guide, live)
        # Guidance is not hidden and lives outside the live pane.
        self.assertNotIn('id="guideRow" class="row rbar" style="display:none', INDEX)
        self.assertIn('id="guideHint"', INDEX)
        self.assertIn("guidance will be applied on the next rerun/resume", INDEX)

    def test_headed_toggle_and_kebab_delete_gate(self):
        self.assertIn('id="headed"', INDEX)
        self.assertIn("Show browser", INDEX)
        self.assertIn("headed:headedOpt()", INDEX)
        self.assertIn("rmore", INDEX)
        self.assertIn("Can't delete while this run is running", INDEX)
        self.assertIn("runActive", INDEX)


if __name__ == "__main__":
    unittest.main()
