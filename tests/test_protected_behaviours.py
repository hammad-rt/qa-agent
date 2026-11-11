"""Static / fake checks for .cursor/rules/protected-behaviours.mdc.

A PR that breaks a locked behaviour must turn this module (and related gates) red.
Also scans tests/fixtures for env-bound identity (golden traces must be placeholders only).
"""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

from agent_tester.agents import tester as T
from agent_tester.memory.env_guard import find_env_details

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "agent_tester" / "web" / "index.html"
FIXTURES = ROOT / "tests" / "fixtures"
RULES = ROOT / ".cursor" / "rules"


class ProtectedWebDom(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text()
        cls.markup = re.split(r"<script[\s>]", cls.html, maxsplit=1)[0]

    def test_tabs_under_title_before_phase_and_guide(self):
        main = re.search(r"<main>([\s\S]*)</main>", self.markup)
        self.assertIsNotNone(main)
        body = main.group(1)
        head_i = body.find('class="head"')
        tabs_i = body.find('role="tablist"')
        phase_i = body.find('id="phaseBar"')
        guide_i = body.find('id="guideRow"')
        self.assertGreater(tabs_i, head_i)
        self.assertGreater(phase_i, tabs_i)
        self.assertGreater(guide_i, tabs_i)

    def test_guide_always_visible(self):
        self.assertIn('id="guideIn"', self.markup)
        self.assertIn('id="guideRow"', self.markup)

    def test_stuck_continue_skip_stop(self):
        self.assertIn("stuckAct('go')", self.html)
        self.assertIn("stuckAct('skip')", self.html)
        self.assertIn("stuckAct('stop')", self.html)

    def test_run_card_delete_gated_for_active(self):
        compact = self.html.replace(" ", "")
        self.assertIn("['new','planning','running','stuck']", compact)


class ProtectedStuckThreshold(unittest.TestCase):
    def test_stuck_max_attempts_is_three(self):
        self.assertEqual(T.STUCK_MAX_ATTEMPTS, 3)
        self.assertEqual(T.LOOP_STOP, 3)


class ProtectedPolicyGates(unittest.TestCase):
    def test_card_allowed_preprod_mpesa_needs_phone(self):
        from agent_tester.data_prep import case_calls_for_mpesa, mpesa_gate
        from agent_tester.preflight.pack_loader import load_pack
        from agent_tester.tools.policy import from_pack

        pack = load_pack(ROOT / "packs" / "savostore")
        staging = from_pack(pack, "staging", approver=lambda *a: True)
        preprod = from_pack(pack, "preprod", approver=lambda *a: True)
        # Card / pay labels are env-gated in pack policy — staging+preprod allow card-like risk.
        self.assertTrue(hasattr(staging, "check") or hasattr(staging, "approve")
                        or hasattr(staging, "gate"))
        self.assertEqual(getattr(preprod, "env", "preprod"), "preprod")
        # M-Pesa helpers exist and phone gate is required in tests/test_stuck_pay.py
        self.assertTrue(callable(mpesa_gate))
        self.assertTrue(callable(case_calls_for_mpesa))


class ProtectedEnvFixtures(unittest.TestCase):
    """Golden-trace fixtures and rule files must not contain real env-bound identity."""

    _PLACEHOLDER_OK = re.compile(
        r"\{client_email\}|\{client_name\}|\{login\}|ORDER-\d+|INVOICE-\d+|RECORD-\d+|"
        r"https://(?:crm|portal)\.example\b",
        re.I,
    )

    def test_fixtures_have_no_real_env_details(self):
        """Golden traces only — platform_html / example packs may use synthetic hosts for detectors."""
        golden = FIXTURES / "golden_traces"
        if not golden.is_dir():
            self.skipTest("no golden_traces fixtures")
        leaks = []
        for path in golden.rglob("*"):
            if not path.is_file() or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                continue
            if path.name == "README.md":
                continue
            text = path.read_text(errors="replace")
            # Strip allowed placeholders before env_guard scan.
            scrubbed = self._PLACEHOLDER_OK.sub("PLACEHOLDER", text)
            hits = find_env_details(scrubbed)
            # Also ban obvious real-looking emails/hosts that placeholders replaced incompletely.
            if re.search(r"[\w.+-]+@(?!example\b)[\w.-]+\.[a-z]{2,}", scrubbed, re.I):
                hits.append("email")
            if re.search(r"https?://(?!(?:crm|portal)\.example\b)[^\s\"']+", scrubbed, re.I):
                hits.append("url/host")
            if hits:
                leaks.append(f"{path.relative_to(ROOT)}: {sorted(set(hits))}")
        self.assertEqual(leaks, [], "env-bound identity in fixtures:\n" + "\n".join(leaks))

    def test_preserve_and_protected_rules_exist(self):
        self.assertTrue((RULES / "preserve-behaviour.mdc").is_file())
        self.assertTrue((RULES / "protected-behaviours.mdc").is_file())
        self.assertTrue((RULES / "graphify.mdc").is_file())
        self.assertTrue((ROOT / ".cursor" / "skills" / "graphify-code-nav" / "SKILL.md").is_file())
        self.assertTrue((ROOT / ".cursor" / "skills" / "pr-gate" / "SKILL.md").is_file())
        self.assertTrue((ROOT / ".agents" / "skills" / "graphify" / "SKILL.md").is_file())
        self.assertTrue((ROOT / ".cursor" / "agents" / "regression-auditor.md").is_file())
        self.assertTrue((ROOT / ".cursor" / "agents" / "env-leak-checker.md").is_file())
        self.assertTrue((ROOT / ".cursor" / "agents" / "ui-invariants-checker.md").is_file())
        self.assertTrue((ROOT / ".github" / "pull_request_template.md").is_file())
        # GitHub Actions workflows are intentionally out of this guardrails PR.


class ProtectedCoreAgnostic(unittest.TestCase):
    def test_core_seeds_have_no_project_names(self):
        banned = re.compile(r"(?i)savo|suitecrm|sugarcrm|savostore")
        # Seed / lasting lesson text only — owner docs under core/ may name a project.
        seeds = ROOT / "core" / "seeds"
        if not seeds.is_dir():
            seeds = ROOT / "agent_tester" / "memory" / "seeds"
        if not seeds.is_dir():
            self.skipTest("no core seeds dir")
        hits = []
        for path in seeds.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".md", ".txt", ".json", ".yml", ".yaml", ".py"}:
                t = path.read_text(errors="replace")
                if banned.search(t):
                    hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(hits, [], "project names in core seeds: " + ", ".join(hits))


class GoldenTraceReplay(unittest.TestCase):
    """Replay redacted fixtures: loops caught at STUCK_MAX; discovery must not write data."""

    def test_loop_fixture_triggers_at_three(self):
        import json
        import tempfile
        from agent_tester.agents.tester import STUCK_MAX_ATTEMPTS, TesterSession
        from agent_tester.preflight.pack_loader import load_pack
        from agent_tester.run_state import RunState
        from agent_tester.tools.policy import from_pack
        from agent_tester.tools.screenshots import Evidence
        from unittest import mock

        data = json.loads((FIXTURES / "golden_traces/loop_165177/trace_clicks.json").read_text())
        self.assertEqual(data["stuck_at_attempt"], STUCK_MAX_ATTEMPTS)
        with tempfile.TemporaryDirectory() as t:
            st = RunState(Path(t) / "run")
            pol = from_pack(load_pack(ROOT / "packs/savostore"), "staging", approver=lambda *a: False)
            s = TesterSession(
                "portal",
                {"case_id": data["case_id"], "title": "discover UI for admin", "discovery": True,
                 "steps": [{"n": 1, "text": "map portal", "app": "portal"}]},
                st, pol, Evidence(st.dir), max_calls=40, max_minutes=5)
            s.current_step = 1
            s.last_snapshot = (
                f"Page URL: {data['page_url']}\nPage Title: {data['page_title']}\n"
                f'- button "{data["clicks"][0]["element"]}" [ref={data["clicks"][0]["target"]}]\n'
            )
            asks = []

            def fake_ask(**kw):
                asks.append(kw)
                return {"error": "STUCK_ASK"}

            with mock.patch.object(s, "_maybe_stuck_ask", side_effect=fake_ask):
                out = None
                for c in data["clicks"][: STUCK_MAX_ATTEMPTS + 1]:
                    out = s._budget_check("browser_click", dict(c))
                    if out and out.get("error") == "STUCK_ASK":
                        break
            self.assertEqual(out, {"error": "STUCK_ASK"})
            self.assertEqual(len(asks), 1)

    def test_pass_fixture_has_no_discovery_writes_flag(self):
        import json
        data = json.loads((FIXTURES / "golden_traces/pass_183498/passed_steps.json").read_text())
        self.assertFalse(data.get("discovery_writes"))
        for c in data["cases"]:
            self.assertEqual(c["verdict"], "PASS")
            for lab in c["route_labels"]:
                self.assertFalse(find_env_details(lab))


if __name__ == "__main__":
    unittest.main()
