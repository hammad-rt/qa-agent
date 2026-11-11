"""Regression tests replaying laptop run 165177-20261111-103004 evidence (items 1–7)."""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_tester.agents.tester import TesterSession, LOOP_STOP, STUCK_MAX_ATTEMPTS
from agent_tester.coordinator import _log_discovery_reuse, _run_discovery
from agent_tester.discovery_guard import discovery_blocks_click, is_discovery_submit_label
from agent_tester import logsetup
from agent_tester.models import CaseType, PlannedCase, Step, TestPlan
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.stuck_summary import (
    build_stuck_summary, guess_and_suggestions, latest_shot, load_thread, persist_thread,
)
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence

PACK = Path(__file__).resolve().parents[1] / "packs/savostore"


def _sess(run_dir: Path, *, case_id="DISCO-admin-portal", app="portal", discovery=True):
    pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
    brief = {
        "case_id": case_id,
        "title": f"discover UI for admin on {app}",
        "steps": [{"n": 1, "text": "map screens", "app": app}],
        "area": "admin",
        "discovery": discovery,
        "read_only": discovery,
    }
    s = TesterSession(app, brief, RunState(run_dir), pol, Evidence(run_dir),
                      max_calls=40, max_minutes=5)
    s.current_step = 1
    s._explored = True
    s.last_snapshot = (
        "Page URL: https://portal.example/quote\nPage Title: Request a quote\n"
        '- textbox "Content" [ref=e10]\n'
        '- button "Request a purchase quote" [ref=e67]\n'
        '- alert "Please fill out this field" [ref=e99]\n'
    )
    return s


class StuckGuidanceAfterGo(unittest.TestCase):
    """Item 1: after go, guidance injects and the identical click is forbidden → ladder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _sess(Path(self.tmp.name) / "run")
        self.s._recent = ["click:button request a purchase quote"] * 3
        self.s._sigs = {"click:button request a purchase quote": 3}

    def tearDown(self):
        self.tmp.cleanup()

    def test_go_forbids_identical_click_then_ladder(self):
        self.s._capture_stuck_ask_shot = lambda: None
        with mock.patch.object(self.s, "_can_stuck_ask", return_value=True), \
             mock.patch.object(self.s, "_ask_stuck_line",
                               side_effect=["Try a different control or route to the same goal", "go"]):
            out = self.s._maybe_stuck_ask(
                why="browser_click was repeated 3 times", name="browser_click", repeats=3)
        self.assertIn("GUIDANCE", out["error"])
        self.assertIn("different control", out["error"].lower())
        self.assertTrue(self.s._forbidden_after_stuck)
        self.assertTrue(any("GUIDANCE" in n for n in self.s._guidance_notes))

        args = {"element": "Request a purchase quote", "ref": "e67"}
        # First forbidden repeats are hard-denied (do not execute / no explore yet).
        for i in range(LOOP_STOP - 1):
            denied = self.s._deny_forbidden_after_stuck("browser_click", args)
            self.assertIsNotNone(denied)
            self.assertIn("Blocked", denied["error"])
            self.assertIn(f"Forbidden repeat {i + 1}/{LOOP_STOP}", denied["error"])

        # At STUCK_MAX → lookup ladder (explore), not another identical click.
        with mock.patch.object(self.s, "_start_exploring",
                               return_value={"error": "ladder"}) as explore:
            out = self.s._deny_forbidden_after_stuck("browser_click", args)
            self.assertEqual(out, {"error": "ladder"})
            explore.assert_called_once()


class DiscoverySubmitBlock(unittest.TestCase):
    """Item 2: discovery must not click submit/create/request/pay."""

    def test_request_quote_blocked(self):
        self.assertTrue(is_discovery_submit_label("Request a purchase quote"))
        why = discovery_blocks_click("Request a purchase quote")
        self.assertIsNotNone(why)
        self.assertIn("read-only", why.lower())

    def test_save_submit_pay_create_blocked(self):
        for lab in ("Save", "Submit", "Pay now", "Create order", "Request quote"):
            self.assertIsNotNone(discovery_blocks_click(lab), lab)

    def test_open_form_link_allowed(self):
        self.assertIsNone(discovery_blocks_click("Orders"))
        self.assertIsNone(discovery_blocks_click("New Quote form"))


class DiscoCaseIdPerApp(unittest.TestCase):
    """Item 3: each app gets its own DISCO id/title; stuck threads stay per case."""

    def test_disco_ids_differ_by_app(self):
        with tempfile.TemporaryDirectory() as d:
            state = RunState(Path(d) / "run")
            plan = TestPlan(task_id=1, env="staging", cases=[])
            case = PlannedCase(
                id="TC0", title="admin", area="admin", type=CaseType.UI,
                steps=[Step(text="map", app="crm")])
            seen = []

            def make_tester(app):
                def tester(a, brief, st):
                    seen.append((brief["case_id"], brief["title"], a))
                    return SimpleNamespace(
                        case_id=brief["case_id"], app=a, notes="ok",
                        screenshots=[], snapshots=[], values={}, steps_done=[],
                        stuck=False, limit_hit=False, loop=False)
                return tester

            # Drive _run_discovery twice with different apps.
            from agent_tester.models import StepResult

            def wrap(app):
                def tester(a, brief, st):
                    seen.append((brief.get("case_id"), brief.get("title")))
                    return StepResult(case_id=brief["case_id"], app=a, notes="mapped")
                return {app: tester}

            _run_discovery(plan, case, "crm", state, wrap("crm"), None, "fp", "admin:crm",
                           {}, want=["record payment"], incremental=True, prior=None)
            _run_discovery(plan, case, "portal", state, wrap("portal"), None, "fp",
                           "admin:portal", {}, want=["quote"], incremental=True, prior=None)
            ids = [c[0] for c in seen]
            self.assertIn("DISCO-admin-crm-inc", ids)
            self.assertIn("DISCO-admin-portal-inc", ids)
            self.assertTrue(any("on crm" in (c[1] or "") for c in seen))
            self.assertTrue(any("on portal" in (c[1] or "") for c in seen))

    def test_stuck_threads_isolated_per_case(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            persist_thread(run, "DISCO-admin-crm-inc",
                           [{"role": "user", "text": "record payment parcel"}], {})
            persist_thread(run, "DISCO-admin-portal-inc",
                           [{"role": "user", "text": "portal quote help"}], {})
            crm = load_thread(run, "DISCO-admin-crm-inc")
            portal = load_thread(run, "DISCO-admin-portal-inc")
            self.assertEqual(crm[0]["text"], "record payment parcel")
            self.assertEqual(portal[0]["text"], "portal quote help")
            self.assertNotEqual(crm, portal)


class AskTimeScreenshot(unittest.TestCase):
    """Item 4: stuck card uses fresh ask shot + real page title/path; never stale login png."""

    def test_fresh_shot_preferred_over_login(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            shots = run / "shots"
            shots.mkdir()
            (shots / "login-crm-admin.png").write_bytes(b"old")
            (shots / "disco-admin-portal-01-stuck-ask.png").write_bytes(b"new")
            snap = (
                "Page URL: https://portal.example/quote/new\n"
                "Page Title: Purchase quote\n"
                '- button "Request a purchase quote" [ref=e67]\n'
            )
            summary = build_stuck_summary(
                brief={"case_id": "DISCO-admin-portal", "title": "discover UI for admin on portal",
                       "steps": [{"n": 1, "text": "map"}]},
                case_id="DISCO-admin-portal", app="portal", current_step=1,
                why="repeated click", recent=["click:button request a purchase quote"] * 3,
                snapshot=snap, run_dir=run,
                screenshots=["login-crm-admin.png", "disco-admin-portal-01-stuck-ask.png"],
                fresh_shot="disco-admin-portal-01-stuck-ask.png",
            )
            self.assertEqual(summary["screenshot"], "disco-admin-portal-01-stuck-ask.png")
            self.assertEqual(summary["page_title"], "Purchase quote")
            self.assertIn("/quote/new", summary["page_path"])
            self.assertNotEqual(summary["page_title"], "(untitled)")
            self.assertNotEqual(summary["page_path"], "(unknown page)")

    def test_latest_shot_skips_other_app_login(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "shots").mkdir()
            (run / "shots" / "login-crm-admin.png").write_bytes(b"x")
            got = latest_shot(run, ["login-crm-admin.png"], app="portal")
            self.assertIsNone(got)


class JunkValidationGuess(unittest.TestCase):
    """Item 5: no 'Fix: content' junk; real validation text or no suggestion."""

    def test_bare_content_not_suggested(self):
        guess, sug = guess_and_suggestions(
            "submit failed", {}, {"validation": ["content"], "required": [], "disabled": []}, [])
        self.assertNotIn("Fix: content", " ".join(sug))
        self.assertFalse(any(s.lower().startswith("fix:") for s in sug))

    def test_real_validation_used(self):
        guess, sug = guess_and_suggestions(
            "submit failed", {},
            {"validation": ["Please fill out this field"], "required": [], "disabled": []}, [])
        self.assertIn("Please fill out this field", guess)


class ReuseLogDedupe(unittest.TestCase):
    """Item 6: dedupe reuse entries, screen names, no PREP blobs, honest savings."""

    def test_dedupe_and_honest_saved(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            reused = [
                {"checklist_item": "open wallet / rewards", "item_key": "wallet",
                 "last_success_at": "2026-11-10T00:00:00+00:00"},
                {"checklist_item": "open wallet / rewards", "item_key": "wallet",
                 "last_success_at": "2026-11-10T00:00:00+00:00"},
                {"checklist_item": 'WALLET create: Move that paid order to "Done" and mark',
                 "item_key": "prep-blob", "last_success_at": "2026-11-10T00:00:00+00:00"},
            ] * 3
            _log_discovery_reuse(
                st, "admin:portal", reused,
                live=["login", "quote form"], spot_checked=0, calls_saved=60)
            log = (st.dir / "progress.log").read_text()
            self.assertEqual(log.count("open wallet"), 1)
            self.assertNotIn("Move that paid order", log)
            self.assertIn("discovered live", log)
            # Must not claim the full ~60 when live discovery still runs.
            self.assertNotRegex(log, r"~6[0-9] tool calls saved")
            m = __import__("re").search(r"~(\d+) tool calls saved", log)
            self.assertIsNotNone(m)
            self.assertLessEqual(int(m.group(1)), 24)


class OtelDetachNoise(unittest.TestCase):
    """Item 7: OTEL detach-context noise stays out of errors.log."""

    def test_detach_filtered(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"AGENT_DEBUG": ""}):
            run = Path(d) / "run"
            run.mkdir()
            with logsetup.RunErrors(run):
                logging.getLogger("opentelemetry.context").error(
                    "Failed to detach context",
                    exc_info=RuntimeError("Failed to detach context"))
                logging.getLogger("agent_tester").error("real failure here")
            text = (run / "errors.log").read_text()
            self.assertNotIn("Failed to detach context", text)
            self.assertIn("real failure here", text)


if __name__ == "__main__":
    unittest.main()
