"""Regression gates: behaviours that older fixes must not break again."""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_tester.agents import planner as P
from agent_tester.agents.llm import LLMOutputError
from agent_tester.models import Requirement, TestPlan
from agent_tester.preflight import gap_resolver as G
from agent_tester.run_state import RunState

HTML = Path(__file__).resolve().parents[1] / "agent_tester/web/index.html"
PACK = Path(__file__).resolve().parents[1] / "packs/savostore"


class AskAccountsEachRun(unittest.TestCase):
    def test_ask_accounts_each_run_forces_missing_every_time(self):
        """When ask_accounts_each_run is set, the plan-gate still asks which user each run."""
        from agent_tester.preflight.pack_loader import load_pack

        pack = load_pack(PACK)
        self.assertTrue(G.ask_accounts_each_run(pack))
        prefs = {}
        mem = SimpleNamespace(get=lambda *a, **k: None, put=lambda *a, **k: None)
        ctx = G.GapContext(pack=pack, env="staging", memory=mem, wiki_texts={}, task_text="")
        with mock.patch.object(G, "_prefs", return_value=SimpleNamespace(
                get=lambda *a, **k: prefs.get(a[-1]) if a else None,
                put=lambda *a, **k: prefs.update({a[-2]: a[-1]}) if len(a) >= 2 else None)):
            r = G.resolve_account(Requirement(key="account.crm.admin"), "crm", "admin", ctx)
        self.assertEqual(r.status, "missing")
        self.assertIsNone(r.value)
        self.assertIn("Which admin user", r.question or "")
        self.assertIn("asked each run", r.sources_checked)

    def test_rerun_path_resets_accounts_when_flag_on(self):
        """main.rerun clears account values so gate.ask_batch runs again."""
        src = Path(__file__).resolve().parents[1] / "agent_tester/main.py"
        text = src.read_text()
        self.assertIn("ask_accounts_each_run(pack)", text)
        self.assertIn('r.status, r.source, r.value = "missing", None, None', text)
        self.assertIn("gate.ask_batch(accts, ctx)", text)


class ResumeStillWorks(unittest.TestCase):
    def test_web_resume_endpoint_and_button(self):
        html = HTML.read_text()
        self.assertIn('id="resumeBtn"', html)
        self.assertIn("async function resume()", html)
        self.assertIn("{resume:cur", html)
        from agent_tester.web import server as W
        src = Path(W.__file__).read_text()
        self.assertIn('elif b.get("resume")', src)
        self.assertIn('args = ["resume", str(d)]', src)

    def test_cli_resume_command_registered(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn('sub.add_parser("resume")', (root / "agent_tester/main.py").read_text())
        self.assertIn('args = ["resume", str(d)]', (root / "agent_tester/web/server.py").read_text())


class StuckAskContinueGate(unittest.TestCase):
    def test_ui_stuck_continue_gate_present(self):
        html = HTML.read_text()
        self.assertIn("stuckAct('go')", html)
        self.assertIn(" Continue</button>", html)
        self.assertIn("stuckAct('skip')", html)
        self.assertIn("stuckAct('stop')", html)
        self.assertIn("stuckAct('chat')", html)
        # Continue sends 'go' so the tester gate advances.
        self.assertIn("kind==='go'){text='go'}", html.replace(" ", ""))

    def test_tester_continue_after_guidance_go(self):
        from agent_tester.agents.tester import TesterSession
        from agent_tester.preflight.pack_loader import load_pack
        from agent_tester.tools.policy import from_pack
        from agent_tester.tools.screenshots import Evidence

        with tempfile.TemporaryDirectory() as t:
            st = RunState(Path(t) / "run")
            pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
            s = TesterSession(
                "crm",
                {"case_id": "E1", "title": "Earn", "steps": [{"n": 1, "text": "pay", "app": "crm"}],
                 "area": "rewards"},
                st, pol, Evidence(st.dir), max_calls=30, max_minutes=5)
            s.current_step = 1
            s._explored = True
            s._recent = ["click:Save", "click:Save"]
            s.last_snapshot = "Page URL: https://example.test/\nSave"
            s._capture_stuck_ask_shot = lambda: None
            with mock.patch.object(s, "_can_stuck_ask", return_value=True), \
                 mock.patch.object(s, "_ask_stuck_line", side_effect=["try Payments", "go"]):
                out = s._maybe_stuck_ask(why="looped", name="browser_click", repeats=4)
            self.assertIn("GUIDANCE", out["error"])
            self.assertIn("Payments", out["error"])
            self.assertFalse(s.result.limit_hit)


class PlannerRealShapedFailure(unittest.TestCase):
    def test_empty_object_and_short_plan_are_failures(self):
        self.assertIsNotNone(P._empty_plan_reason(None, ["A1"]))
        empty = TestPlan.model_validate({"task_id": 1, "env": "staging", "cases": []})
        self.assertEqual(P._empty_plan_reason(empty, ["A1"]), "empty cases array")
        short = TestPlan.model_validate({
            "task_id": 1, "env": "staging",
            "cases": [{"id": "A1", "title": "t", "type": "ui", "expected": "ok",
                       "steps": [{"text": "x"}]}],
        })
        self.assertIn("missing cases", P._empty_plan_reason(short, ["A1", "A2"]) or "")


class UiShellLayout(unittest.TestCase):
    def test_aside_main_same_height_shell(self):
        css = HTML.read_text().split("</style>", 1)[0]
        compact = css.replace(" ", "").replace("\n", "")
        self.assertIn("aside{width:330px", compact)
        self.assertIn("height:100%", compact)
        self.assertIn("100dvh", compact)
        # Flex row body so aside and main share the viewport height.
        self.assertIn("body{margin:0", compact)
        self.assertIn("display:flex", compact)
        self.assertIn("main{flex:110", compact)

    def test_run_card_actions_inside_card_contract(self):
        html = HTML.read_text()
        self.assertIn("function runCardHtml", html)
        self.assertIn("rmore", html)
        self.assertIn("Can't delete while this run is active", html)
        self.assertIn("['new','planning','running','stuck']", html.replace(" ", ""))
        self.assertIn("Rename", html)


if __name__ == "__main__":
    unittest.main()
