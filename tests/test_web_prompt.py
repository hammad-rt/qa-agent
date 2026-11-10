"""Web UI prompt classification and pre-case phase derivation (read-only status fields)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.web import server as W


class ClassifyPrompt(unittest.TestCase):
    def test_stuck_ask_parses_timeout(self):
        p = W.classify_prompt(
            "Stuck on 184815.TC2 (step 3): looped on Save.\n"
            "Tried: browser_click, browser_snapshot.\n"
            "Page: https://example/orders.\n"
            "Options: (1) give guidance (free text)  (2) skip this case  (3) stop the run\n"
            "Reply with 1:<text>, 2:<reason>, or 3:<reason> (wait up to 600s): "
        )
        self.assertIn(p["kind"], ("stuck_ask", "stuck_chat"))
        self.assertEqual(p["timeout_sec"], 600)
        self.assertIn("Stuck", p["title"])

    def test_gates_and_generic(self):
        self.assertEqual(W.classify_prompt("\nStart the run? [y/N] ")["kind"], "start_run")
        self.assertEqual(W.classify_prompt("  Pre-approve them for this run? [y/N] ")["kind"], "preapprove")
        self.assertEqual(W.classify_prompt("  Allow? [y/N] ")["kind"], "approval")
        self.assertEqual(W.classify_prompt("  Test on production? [y/N] ")["kind"], "prod_confirm")
        self.assertEqual(W.classify_prompt("  Token to edit (Enter = done): ")["kind"], "data_plan")
        self.assertEqual(W.classify_prompt("Account email?\n> ")["kind"], "question")
        self.assertEqual(W.classify_prompt("")["kind"], "none")

    def test_tool_approve(self):
        p = W.classify_prompt(
            "The tester asks for a new tool 'foo': purpose\n"
            "----- script -----\ndef foo():\n  pass\n------------------\n"
            "  Approve this tool? [y/N]: "
        )
        self.assertEqual(p["kind"], "tool_approve")


class PhaseAndPreTasks(unittest.TestCase):
    def test_disco_then_prep_active(self):
        st = {
            "status": "running",
            "plan": {"cases": [{"id": "T.TC1"}]},
            "results": {},
            "action_log": [
                {"seq": 1, "case_id": "DISCO-crm", "app": "crm", "tool": "browser_snapshot",
                 "args": {}, "outcome": "ok"},
                {"seq": 2, "case_id": "CONTACT-T.TC1", "app": "crm", "tool": "browser_type",
                 "args": {"text": "a@b.c"}, "outcome": "ok"},
                {"seq": 3, "case_id": "PREP-ORDER-MULTI-find", "app": "portal", "tool": "browser_click",
                 "args": {"element": "Search"}, "outcome": "ok"},
            ],
            "checkpoints": [
                {"at": "2026-03-10T10:00:00", "label": "run started"},
                {"at": "2026-03-10T10:05:00", "label": "discovery done for crm"},
            ],
        }
        pre = W.derive_pre_tasks(st, live=True)
        ids = {r["id"]: r for r in pre}
        self.assertIn("DISCO-crm", ids)
        self.assertEqual(ids["PREP-ORDER-MULTI-find"]["status"], "running")
        self.assertGreaterEqual(ids["PREP-ORDER-MULTI-find"]["actions"], 1)
        self.assertTrue(ids["PREP-ORDER-MULTI-find"]["current_action"])
        phase = W.derive_phase(st, live=True)
        self.assertEqual(phase["active"], "prep")
        self.assertEqual(phase["steps"][0]["state"], "done")  # planning

    def test_summary_exposes_phase_fields(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "9-x"
            d.mkdir()
            (d / "state.json").write_text(json.dumps({
                "status": "running",
                "plan": {"task_id": 9, "env": "staging", "cases": [{"id": "9.TC1"}]},
                "results": {},
                "action_log": [
                    {"seq": 1, "case_id": "DISCO-portal", "app": "portal", "tool": "browser_snapshot",
                     "args": {}, "outcome": "ok"},
                ],
                "checkpoints": [{"at": "2026-03-10T10:00:00", "label": "run started"}],
            }))
            with mock.patch.object(W, "RUNS", Path(t)), mock.patch.object(W, "_live", return_value=True):
                s = W.run_summary(d)
            self.assertEqual(s["phase"], "discovery")
            self.assertTrue(s["pre_tasks"])
            self.assertEqual(s["pre_tasks"][0]["id"], "DISCO-portal")
            self.assertEqual(s["current"], "DISCO-portal")
            detail = None
            with mock.patch.object(W, "RUNS", Path(t)), mock.patch.object(W, "_live", return_value=True):
                detail = W.run_detail(d)
            self.assertIn("phase", detail)
            self.assertEqual(detail["phase"]["active"], "discovery")


class JobPromptMeta(unittest.TestCase):
    def test_info_includes_prompt_kind_when_pending(self):
        j = W.Job.__new__(W.Job)
        j.id = "abcd"
        j.args = ["test", "1"]
        j.started = 0
        j.out = ""
        j.pending = {"prompt": "\nStart the run? [y/N] ", "secret": False}
        j.waiting_since = 1000.0
        j._buf = ""
        j._saved = 0
        j.run_hint = None
        import threading
        j.lock = threading.Lock()

        class P:
            def poll(self):
                return None
        j.proc = P()
        info = j.info()
        self.assertTrue(info["waiting"])
        self.assertEqual(info["prompt_kind"], "start_run")
        self.assertEqual(info["waiting_since"], 1000.0)
        self.assertIn("Start", info["prompt_title"])


if __name__ == "__main__":
    unittest.main()
