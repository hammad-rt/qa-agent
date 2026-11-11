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
    pack = load_pack(PACK)
    pol = from_pack(pack, "staging", approver=lambda *a: False)
    brief = {
        "case_id": case_id,
        "title": f"discover UI for admin on {app}",
        "steps": [{"n": 1, "text": "map screens", "app": app}],
        "area": "admin",
        "discovery": discovery,
        "read_only": discovery,
    }
    state = RunState(run_dir)
    state.data["pack_cfg"] = pack.config or {}
    s = TesterSession(app, brief, state, pol, Evidence(run_dir),
                      max_calls=40, max_minutes=5, pack=pack)
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
    """Discovery blocks real submits; Create / Apply / Search stay allowed for mapping."""

    def setUp(self):
        self.pack_cfg = (load_pack(PACK).config or {})

    def test_request_quote_blocked_via_pack_labels(self):
        # Product copy lives in pack.yaml; core alone does not know "purchase quote".
        self.assertFalse(is_discovery_submit_label("Request a purchase quote"))
        self.assertTrue(is_discovery_submit_label("Request a purchase quote", self.pack_cfg))
        why = discovery_blocks_click("Request a purchase quote", pack_cfg=self.pack_cfg)
        self.assertIsNotNone(why)
        self.assertIn("read-only", why.lower())

    def test_core_submits_blocked_create_apply_search_allowed(self):
        for lab in ("Save", "Submit", "Pay now", "Place order", "Checkout",
                    "Confirm payment", "Request quote"):
            self.assertIsNotNone(discovery_blocks_click(lab), lab)
        # Open-form / filter controls must not be blocked (email Advanced Filter needs Apply).
        for lab in ("Create", "Create order", "Create New Order", "Apply", "Apply filter",
                    "Search", "Advanced Filter", "Send", "Confirm"):
            self.assertIsNone(discovery_blocks_click(lab, pack_cfg=self.pack_cfg), lab)

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
        from agent_tester.stuck_summary import suggestion_text
        guess, sug = guess_and_suggestions(
            "submit failed", {}, {"validation": ["content"], "required": [], "disabled": []}, [])
        blob = " ".join(suggestion_text(s) for s in sug)
        self.assertNotIn("Fix: content", blob)
        self.assertFalse(any(suggestion_text(s).lower().startswith("fix:") for s in sug))

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


class RecordIdNotPhone(unittest.TestCase):
    """Items 9/13: record= ids survive redact; CRM UUID required; never float."""

    def test_redact_keeps_record_param(self):
        from agent_tester.redact import redact
        url = "https://crm.example/index.php?module=Contacts&action=DetailView&record=c717b126-aaaa-bbbb-cccc-ddddeeeeffff"
        out = redact(url)
        self.assertIn("c717b126-aaaa-bbbb-cccc-ddddeeeeffff", out)
        self.assertNotIn("[phone]", out)

    def test_record_value_rejects_phone_marker_and_digits_on_crm(self):
        from agent_tester.tools.value_kind import validate_value
        bad = validate_value("crm_contact_id", "[phone]", source="URL record id",
                             platform="suitecrm7")
        self.assertFalse(bad["ok"])
        digits = validate_value("crm_contact_id", "67071257880822", source="URL record id",
                                platform="suitecrm7")
        self.assertFalse(digits["ok"], "suitecrm7 platform.yaml requires uuid")
        ok = validate_value(
            "crm_contact_id",
            "https://crm.example/index.php?module=Contacts&action=DetailView&record=c717b126-aaaa-bbbb-cccc-ddddeeeeffff",
            source="URL record id", platform="suitecrm7")
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["value"], "c717b126-aaaa-bbbb-cccc-ddddeeeeffff")
        # No platform rule → any non-empty string (e.g. numeric) is accepted.
        numeric = validate_value("crm_contact_id", "67071257880822", platform=None)
        self.assertTrue(numeric["ok"])
        self.assertEqual(numeric["value"], "67071257880822")

    def test_record_value_keeps_string_not_float(self):
        with tempfile.TemporaryDirectory() as d:
            s = _sess(Path(d) / "run", case_id="PREP-CUSTOMER-find", app="crm", discovery=False)
            s.state.data["detected_platform"] = "suitecrm7"
            rv = next(t for t in s.tools() if getattr(t, "__name__", "") == "record_value")
            out = rv("crm_contact_id",
                     "index.php?module=Contacts&action=DetailView&record=c717b126-aaaa-bbbb-cccc-ddddeeeeffff",
                     "URL record id")
            self.assertEqual(out.get("recorded"), "crm_contact_id")
            self.assertIsInstance(out["value"], str)
            self.assertEqual(out["value"], "c717b126-aaaa-bbbb-cccc-ddddeeeeffff")


class FindMissNavigateThrash(unittest.TestCase):
    """Items 10/14: after find miss, refuse navigate; consecutive navigates stall."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _sess(Path(self.tmp.name) / "run", case_id="PREP-wallet-find", app="crm",
                       discovery=False)

    def tearDown(self):
        self.tmp.cleanup()

    def _tool(self, name):
        return SimpleNamespace(name=name)

    def test_find_miss_blocks_navigate_until_snapshot(self):
        from agent_tester.agents.tester import FIND_MISS_READ_MSG
        self.s.after_tool(self._tool("browser_find"), {"query": "Current Rewards"}, None,
                          "Error: not found")
        self.assertTrue(self.s._find_miss_need_read)
        denied = self.s.before_tool(self._tool("browser_navigate"),
                                    {"url": "https://crm.example/index.php?module=Contacts"}, None)
        self.assertIsNotNone(denied)
        self.assertIn("Do not navigate", denied["error"])
        # Snapshot clears the gate.
        self.s.after_tool(self._tool("browser_snapshot"), {}, None,
                          "Page URL: https://crm.example/\n- heading \"Contacts\"")
        self.assertFalse(self.s._find_miss_need_read)

    def test_navigate_thrash_explores(self):
        from agent_tester.agents.tester import NAV_THRASH_MAX
        self.s._explored = False
        # Alternating list ↔ detail (the PREP-wallet-find pattern) without a read.
        list_u = "https://staging5.rolustech.com:4220/index.php?module=Contacts&action=index"
        detail_u = "https://staging5.rolustech.com:4220/index.php?module=Contacts&action=DetailView&record=x"
        outs = []
        for i in range(NAV_THRASH_MAX * 3):
            outs.append(self.s.before_tool(
                self._tool("browser_navigate"),
                {"url": list_u if i % 2 == 0 else detail_u}, None))
        self.assertTrue(any(o and "error" in o for o in outs if o))


class NoChangeClickRepeat(unittest.TestCase):
    """Item 15: identical click after no page change is refused with modal/tab guidance."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _sess(Path(self.tmp.name) / "run", case_id="PREP-wallet-create-0", app="portal",
                       discovery=False)
        self.s.last_snapshot = (
            "Page URL: https://staging5.rolustech.com:4225/ship\nPage Title: Shipping\n"
            '- button "Create a New Shipment" [ref=f1e59]\n'
            '- button "Shipping Only Order" [ref=f1e9]\n'
        )
        self.s._remember_page(self.s.last_snapshot)

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_identical_no_change_click_refused(self):
        from agent_tester.agents.tester import NO_CHANGE_CLICK_MSG
        tool = SimpleNamespace(name="browser_click")
        args = {"element": "Create a New Shipment", "target": "f1e59"}
        # Simulate click that left the page hash unchanged.
        before = self.s._page_hash
        self.s.after_tool(tool, args, None, self.s.last_snapshot)
        self.assertEqual(self.s._page_hash, before)
        self.assertIsNotNone(self.s._no_change_click)
        self.assertTrue(self.s._need_fresh_snap)
        # Without a fresh snapshot, the same click is refused.
        denied = self.s.before_tool(tool, args, None)
        self.assertIsNotNone(denied)
        self.assertIn("did not change the page", denied["error"])
        self.assertIn("browser_tabs", denied["error"])


class LoginOnlyWhenSignedOut(unittest.TestCase):
    """Item 16 / audit: skip login only with positive session evidence."""

    def test_session_valid_skips_login(self):
        from agent_tester.helpers.core import login_form as LF
        import asyncio

        class FakeBrowser:
            def __init__(self):
                self.navigated = []

            async def navigate(self, url):
                self.navigated.append(url)

            async def snapshot(self):
                return 'Page Title: Home\n- button "Log out" [ref=e1]\nAdmin Dashboard'

        class Ctx:
            app = "portal"
            state = SimpleNamespace(values={"login_form.logged_in": {"value": "yes", "source": "x/portal"}})
            def account(self, _a):
                return {"username": "u", "password_env": "P", "role_markers": ["Admin Dashboard"]}
            config = {}
            def url(self, p=""):
                return f"https://portal.example/{p}"

        browser = FakeBrowser()
        with mock.patch.dict("os.environ", {"P": "longsecret1"}):
            with mock.patch.object(LF, "analyze", return_value={"has_password": False}):
                out = asyncio.run(LF.run(browser, Ctx(), "customer"))
        self.assertEqual(out["values"]["logged_in"], "yes")
        self.assertIn("skipped login_form", out["notes"].lower())
        self.assertEqual(browser.navigated, [])

    def test_signed_out_public_page_must_login(self):
        """Public home with no password field and no session evidence → navigate to login."""
        from agent_tester.helpers.core import login_form as LF
        import asyncio

        class FakeBrowser:
            def __init__(self):
                self.navigated = []

            async def navigate(self, url):
                self.navigated.append(url)

            async def snapshot(self):
                return 'Page Title: Welcome\n- link "About" [ref=e1]\n- text: Public portal home'

        class Ctx:
            app = "portal"
            state = SimpleNamespace(values={})  # no prior login this run
            def account(self, _a):
                # No role_markers → empty markers must NOT count as signed in.
                return {"username": "u", "password_env": "P", "role_markers": []}
            config = {"path": "login"}
            def url(self, p=""):
                return f"https://portal.example/{p or ''}"

        browser = FakeBrowser()
        ctx = Ctx()
        with mock.patch.object(LF, "analyze", return_value={"has_password": False}):
            already = asyncio.run(LF._session_already_valid(browser, ctx, "customer", ctx.account("c")))
        self.assertIsNone(already, "no markers / prev_ok / logout → must not skip login")

        async def fake_analyze(_b):
            if browser.navigated:
                return {"has_password": True,
                        "user": {"idx": 0, "name": "user", "label": "Username"},
                        "password": {"idx": 1, "name": "pass", "label": "Password"},
                        "submit": {"id": "go", "label": "Log In"}, "error": ""}
            return {"has_password": False}

        with mock.patch.dict("os.environ", {"P": "longsecret1", "LOGIN_WAIT": "0"}):
            with mock.patch.object(LF, "analyze", side_effect=fake_analyze):
                with mock.patch.object(LF, "parse_snapshot", return_value=[]):
                    try:
                        asyncio.run(LF._attempt(browser, ctx, "customer", path="login"))
                    except Exception:
                        pass
        self.assertTrue(browser.navigated, "signed-out public page must navigate to login path")
        self.assertTrue(any("login" in u for u in browser.navigated))

    def test_already_signed_in_skips(self):
        """Role markers (or logout control) on a page without a password field → skip login."""
        from agent_tester.helpers.core import login_form as LF
        import asyncio

        class FakeBrowser:
            def __init__(self):
                self.navigated = []

            async def navigate(self, url):
                self.navigated.append(url)

            async def snapshot(self):
                return 'Admin Dashboard\n- button "Sign out" [ref=e9]'

        class Ctx:
            app = "crm"
            state = SimpleNamespace(values={})
            def account(self, _a):
                return {"username": "u", "password_env": "PW", "role_markers": ["Admin Dashboard"]}
            config = {"path": "index.php?module=Users&action=Login"}
            def url(self, p=""):
                return f"https://crm.example/{p}"

        browser = FakeBrowser()
        with mock.patch.dict("os.environ", {"PW": "longsecret1"}):
            with mock.patch.object(LF, "analyze", return_value={"has_password": False}):
                out = asyncio.run(LF.run(browser, Ctx(), "admin"))
        self.assertEqual(browser.navigated, [])
        self.assertEqual(out["values"]["logged_in"], "yes")
        self.assertIn("skipped", out["notes"].lower())

    def test_admin_free_text_does_not_skip_login(self):
        """'Admin' as a page-text substring (e.g. Administrator) is not signed-in evidence."""
        from agent_tester.helpers.core import login_form as LF
        import asyncio

        class FakeBrowser:
            async def snapshot(self):
                return ('Page Title: Login\n- heading "Administrator" [ref=e1]\n'
                        '- text: Admin login panel')

        class Ctx:
            app = "crm"
            state = SimpleNamespace(values={})
            def account(self, _a):
                return {"username": "u", "password_env": "PW", "role_markers": ["Admin"]}

        ctx = Ctx()
        with mock.patch.object(LF, "analyze", return_value={"has_password": False}):
            already = asyncio.run(LF._session_already_valid(
                FakeBrowser(), ctx, "admin", ctx.account("admin")))
        self.assertIsNone(already)

    def test_admin_marker_does_not_match_administrator_control(self):
        from agent_tester.helpers.core import login_form as LF
        import asyncio

        class FakeBrowser:
            async def snapshot(self):
                return '- link "Administrator" [ref=e1]\n- link "About" [ref=e2]'

        class Ctx:
            app = "crm"
            state = SimpleNamespace(values={})
            def account(self, _a):
                return {"username": "u", "password_env": "PW", "role_markers": ["Admin"]}

        ctx = Ctx()
        with mock.patch.object(LF, "analyze", return_value={"has_password": False}):
            already = asyncio.run(LF._session_already_valid(
                FakeBrowser(), ctx, "admin", ctx.account("admin")))
        self.assertIsNone(already)

    def test_admin_control_skips_login(self):
        from agent_tester.helpers.core import login_form as LF
        import asyncio

        class FakeBrowser:
            def __init__(self):
                self.navigated = []
            async def navigate(self, url):
                self.navigated.append(url)
            async def snapshot(self):
                return '- link "Admin" [ref=e1]\n- button "Log out" [ref=e2]'

        class Ctx:
            app = "crm"
            state = SimpleNamespace(values={})
            config = {}
            def account(self, _a):
                return {"username": "u", "password_env": "PW", "role_markers": ["Admin"]}
            def url(self, p=""):
                return f"https://crm.example/{p}"

        browser = FakeBrowser()
        with mock.patch.dict("os.environ", {"PW": "longsecret1"}):
            with mock.patch.object(LF, "analyze", return_value={"has_password": False}):
                out = asyncio.run(LF.run(browser, Ctx(), "admin"))
        self.assertEqual(browser.navigated, [])
        self.assertEqual(out["values"]["logged_in"], "yes")
        self.assertIn("skipped", out["notes"].lower())

    def test_prior_flag_without_signed_in_control_must_navigate(self):
        """A prior logged_in flag on a public page with no signed-in control is not enough."""
        from agent_tester.helpers.core import login_form as LF
        import asyncio

        class FakeBrowser:
            def __init__(self):
                self.navigated = []
            async def navigate(self, url):
                self.navigated.append(url)
            async def snapshot(self):
                return 'Page Title: Welcome\n- link "About" [ref=e1]\n- text: Public home'

        class Ctx:
            app = "crm"
            state = SimpleNamespace(values={
                "login_form.logged_in": {"value": "yes", "source": "x/crm"}})
            def account(self, _a):
                return {"username": "u", "password_env": "P", "role_markers": ["Admin"]}
            config = {"path": "index.php?module=Users&action=Login"}
            def url(self, p=""):
                return f"https://crm.example/{p or ''}"

        browser = FakeBrowser()
        ctx = Ctx()
        with mock.patch.object(LF, "analyze", return_value={"has_password": False}):
            already = asyncio.run(LF._session_already_valid(
                browser, ctx, "admin", ctx.account("a")))
        self.assertIsNone(already)

        async def fake_analyze(_b):
            if browser.navigated:
                return {"has_password": True,
                        "user": {"idx": 0, "name": "user", "label": "Username"},
                        "password": {"idx": 1, "name": "pass", "label": "Password"},
                        "submit": {"id": "go", "label": "Log In"}, "error": ""}
            return {"has_password": False}

        with mock.patch.dict("os.environ", {"P": "longsecret1", "LOGIN_WAIT": "0"}):
            with mock.patch.object(LF, "analyze", side_effect=fake_analyze):
                with mock.patch.object(LF, "parse_snapshot", return_value=[]):
                    try:
                        asyncio.run(LF._attempt(browser, ctx, "admin", path="login"))
                    except Exception:
                        pass
        self.assertTrue(browser.navigated, "prior flag alone must still navigate to login")
        self.assertTrue(any("login" in u for u in browser.navigated))

    def test_login_shot_uses_role_key_not_email(self):
        from types import SimpleNamespace
        from agent_tester.helpers.core import login_form as LF

        class Pack:
            def accounts(self, app):
                return {"customer": {"username": "user@example.com"},
                        "admin": {"username": "admin"}}

        ctx = SimpleNamespace(app="portal", pack=Pack(), state=SimpleNamespace(data={}))
        self.assertEqual(LF._account_role_key(ctx, "customer"), "customer")
        self.assertEqual(LF._account_role_key(ctx, "user@example.com"), "customer")
        self.assertEqual(LF._account_role_key(ctx, "admin"), "admin")
        self.assertNotIn("@", LF._account_role_key(ctx, "user@example.com"))
        # Unmapped identity still must not appear in a shot name.
        self.assertEqual(LF._account_role_key(ctx, "other@example.com"), "account")
        self.assertEqual(
            f"login-{ctx.app}-{LF._account_role_key(ctx, 'user@example.com')}.png",
            "login-portal-customer.png")


class IdlePausesForHuman(unittest.TestCase):
    """Item 8: human wait pauses LLM idle clock."""

    def test_pause_context(self):
        from agent_tester.agents import llm as L
        self.assertFalse(L.human_wait_active())
        with L.pause_idle_for_human():
            self.assertTrue(L.human_wait_active())
        self.assertFalse(L.human_wait_active())


class PrepStuckEvidence(unittest.TestCase):
    """Item 12: PREP blocked path stores stuck evidence in state."""

    def test_blocked_writes_prep_stuck(self):
        from agent_tester import data_prep as D
        from agent_tester.models import PlannedCase, Step

        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.data["action_log"] = [
                {"case_id": "PREP-wallet-find", "tool": "browser_navigate",
                 "args": {"url": "https://crm.example/"}, "seq": 1},
                {"case_id": "PREP-wallet-find", "tool": "browser_click",
                 "args": {"element": "Search"}, "seq": 2},
            ]
            case = PlannedCase(id="C1", title="t", expected="e",
                               steps=[Step(text="x", app="crm")])
            plan = TestPlan(task_id="1", title="t", env="staging", cases=[case],
                            requirements=[])
            # Call blocked via a tiny wrapper by invoking _ensure_one's path is heavy;
            # exercise the nested blocked by patching create/find to fail early.
            rule = {"id": "wallet", "value": "wallet_ready", "app": "crm",
                    "find": "read", "envs": ["prod"],  # wrong env → blocked immediately
                    "blocked_note": "not on staging", "test_client": "H"}
            plan.env = "staging"
            # envs gate: rule only allows prod
            out = D._ensure_one(plan, case, st, {}, rule)
            self.assertIsNotNone(out)
            self.assertEqual(out.verdict, "BLOCKED")
            self.assertIn("wallet", st.data.get("prep_stuck") or {})


if __name__ == "__main__":
    unittest.main()
