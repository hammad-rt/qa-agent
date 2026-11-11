"""Stale-ref vs find/raw snapshots, approval-gate tightening, snapshot loops,
inferred login stripping, and already-logged-in step completion."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_tester.agents.tester import TesterSession, UNCHANGED_SNAP_LOOP
from agent_tester.agents.prompts import PLANNER
from agent_tester.helpers.core import login_form as LF
from agent_tester.models import PlannedCase, Step, TestPlan
from agent_tester.plan_checks import (
    autofix, case_is_about_auth, is_auth_step, is_login_step, is_logout_step,
    strip_run_handled_auth_steps,
)
from agent_tester.preflight.packs import load_pack
from agent_tester.run_state import RunState
from agent_tester.stuck_summary import resolve_step
from agent_tester.tools.policy import from_pack, label_matches_action_type
from agent_tester.tools.screenshots import Evidence

PACK = Path(__file__).resolve().parents[1] / "packs" / "savostore"

RAW = (
    "Page URL: https://example.test/admin\n"
    "Page Title: Administration\n"
    "- main:\n"
    + "\n".join(f'  - listitem "Pad {i}" [ref=p{i}]' for i in range(80))
    + "\n"
    '  - link "Repair" [ref=f2e267]\n'
)

TRIMMED = (
    "Page URL: https://example.test/admin\n"
    "Page Title: Administration\n"
    "- main:\n"
    '  - heading "Administration" [ref=e4]\n'
    '[... page text cut: 20000 more characters. Use browser_find for the rest.]\n'
)


def _sess(tmp: Path, steps=None, *, current_step=1, auto=True) -> TesterSession:
    st = RunState(tmp / "run")
    pol = from_pack(load_pack(PACK), "staging", auto=auto, approver=lambda *a: True)
    brief = {
        "case_id": "TC1",
        "title": "Quick Repair and Rebuild",
        "steps": steps or [
            {"n": 1, "text": "Log in to CRM as admin", "app": "crm"},
            {"n": 2, "text": "Open Repair", "app": "crm"},
            {"n": 3, "text": "Run Quick Repair and Rebuild", "app": "crm",
             "action_type": "execute_repair_sql"},
        ],
    }
    s = TesterSession("crm", brief, st, pol, Evidence(st.dir), max_calls=40, max_minutes=5)
    s.current_step = current_step
    s.max_calls = 100
    s._last_act = 0.0
    return s


class StaleRefUsesRawAndFind(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _sess(Path(self.tmp.name), current_step=2)

    def tearDown(self):
        self.tmp.cleanup()

    def t(self, name):
        return SimpleNamespace(name=name)

    def test_ref_only_in_raw_snapshot_is_fresh(self):
        self.s.last_snapshot = TRIMMED
        self.s._page_state = TRIMMED
        self.s._raw_snapshot = RAW
        self.assertFalse(self.s._ref_in_snapshot("f2e267", TRIMMED))
        self.assertTrue(self.s._ref_in_snapshot("f2e267"))
        out = self.s.before_tool(self.t("browser_click"),
                                 {"element": "Repair", "target": "f2e267"}, None)
        self.assertIsNone(out, out)

    def test_find_ref_not_in_trimmed_page_is_fresh(self):
        self.s.last_snapshot = TRIMMED
        self.s._page_state = TRIMMED
        self.s._raw_snapshot = RAW
        self.s.after_tool(self.t("browser_find"), {"text": "Repair"}, None,
                          {"result": 'link "Repair" [ref=f2e267] on the page'})
        self.assertIn("f2e267", self.s._find_refs)
        out = self.s.before_tool(self.t("browser_click"),
                                 {"element": "Repair", "target": "f2e267"}, None)
        self.assertIsNone(out, out)

    def test_unknown_ref_still_stale(self):
        self.s.last_snapshot = TRIMMED
        self.s._raw_snapshot = RAW
        out = self.s.before_tool(self.t("browser_click"),
                                 {"element": "Missing", "target": "e99999"}, None)
        self.assertIsInstance(out, dict)
        self.assertIn("stale", out["error"].lower())

    def test_find_ref_goes_stale_when_raw_page_changes(self):
        self.s._find_refs = {"f2e267"}
        self.s._remember_page(
            "Page URL: https://example.test/other\n- main:\n  - heading \"Home\" [ref=e1]\n"
        )
        self.assertNotIn("f2e267", self.s._find_refs)
        out = self.s.before_tool(self.t("browser_click"),
                                 {"element": "Repair", "target": "f2e267"}, None)
        self.assertIsInstance(out, dict)
        self.assertIn("stale", out["error"].lower())


class UnchangedSnapshotLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _sess(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def t(self, name):
        return SimpleNamespace(name=name)

    def test_fresh_snapshot_clears_need_fresh_and_repeat_unchanged_is_a_loop(self):
        self.s._remember_page(RAW)
        self.assertFalse(self.s._need_fresh_snap)
        self.s._need_fresh_snap = True
        self.s._page_fresh = True
        # A real snapshot is taken (short-circuit skipped) and the flag clears.
        self.assertIsNone(self.s.before_tool(self.t("browser_snapshot"), {}, None))
        self.s.after_tool(self.t("browser_snapshot"), {}, None, {"text": RAW})
        self.assertFalse(self.s._need_fresh_snap)

        self.s._page_fresh = True
        first = self.s.before_tool(self.t("browser_snapshot"), {}, None)
        self.assertIn("unchanged", first["result"].lower())
        self.assertNotIn("error", first)
        out = None
        for _ in range(UNCHANGED_SNAP_LOOP):
            out = self.s.before_tool(self.t("browser_snapshot"), {}, None)
        self.assertIn("error", out)
        self.assertIn("unchanged", out["error"].lower())
        self.assertTrue(self.s.result.loop)


class BrowserGateLabelMatch(unittest.TestCase):
    def test_label_matches_action_type(self):
        self.assertTrue(label_matches_action_type("execute_repair_sql", "Execute"))
        self.assertTrue(label_matches_action_type("change_settings", "Save"))
        self.assertFalse(label_matches_action_type("execute_repair_sql", "Administrator"))
        self.assertFalse(label_matches_action_type("execute_repair_sql", "link Repair"))

    def test_user_menu_click_does_not_inherit_later_execute_type(self):
        tmp = tempfile.TemporaryDirectory()
        asked = []
        try:
            s = _sess(Path(tmp.name), current_step=1, auto=False)
            s.policy.approver = lambda a, r, d: asked.append(a) or True
            s.last_snapshot = '- button "Administrator" [ref=e31]\n'
            s._raw_snapshot = s.last_snapshot
            s._page_fresh = True
            out = s.before_tool(SimpleNamespace(name="browser_click"),
                                {"element": "Administrator", "target": "e31"}, None)
            self.assertIsNone(out, out)
            self.assertEqual(asked, [])
            self.assertNotIn("execute_repair_sql", s._action_gated)
            log = s.state.data.get("action_log") or getattr(s.state, "action_log", None)
            # log_action writes to state; whatever the store, the click must not be gated.
            blob = str(s.state.data)
            self.assertNotIn("execute_repair_sql", blob)
        finally:
            tmp.cleanup()

    def test_execute_button_on_later_step_still_gates(self):
        tmp = tempfile.TemporaryDirectory()
        asked = []
        try:
            s = _sess(Path(tmp.name), current_step=1, auto=False)
            s.policy.approver = lambda a, r, d: asked.append(a) or False
            s.policy.preapproved.clear()
            s.last_snapshot = '- button "Execute" [ref=e100]\n'
            s._raw_snapshot = s.last_snapshot
            out = s.before_tool(SimpleNamespace(name="browser_click"),
                                {"element": "Execute", "target": "e100"}, None)
            self.assertIsInstance(out, dict)
            self.assertTrue(asked or "Blocked" in str(out) or "approval" in str(out).lower()
                            or "Execute" in str(out), out)
        finally:
            tmp.cleanup()


class InferredLoginSteps(unittest.TestCase):
    def test_classifiers(self):
        self.assertTrue(is_login_step("Log in to CRM as admin"))
        self.assertTrue(is_login_step("Sign in as the customer"))
        self.assertTrue(is_logout_step("Log out of the portal"))
        self.assertTrue(is_auth_step("Log in to CRM as admin"))
        self.assertFalse(is_login_step("Open Admin from the user menu, then Repair"))
        self.assertFalse(is_login_step("After login, open the order"))

    def test_strip_inferred_login_on_repair_case(self):
        plan = TestPlan(task_id=1, env="staging", cases=[
            PlannedCase(
                id="TC1", title="Quick Repair and Rebuild",
                expected="Repair completes without error",
                steps=[
                    Step(text="Log in to CRM as admin", app="crm", inferred=True,
                         inferred_reason="Admin access is required", inferred_source="facts.md"),
                    Step(text="Open Repair", app="crm"),
                    Step(text="Run Quick Repair and Rebuild", app="crm",
                         action_type="execute_repair_sql"),
                ],
            ),
        ])
        self.assertFalse(case_is_about_auth(plan.cases[0]))
        out = strip_run_handled_auth_steps(plan)
        texts = [s.text for s in out.cases[0].steps]
        self.assertNotIn("Log in to CRM as admin", texts)
        self.assertEqual(texts, ["Open Repair", "Run Quick Repair and Rebuild"])

    def test_explicit_login_kept_when_case_is_about_login(self):
        plan = TestPlan(task_id=1, env="staging", cases=[
            PlannedCase(
                id="TC1", title="Admin can log in with valid credentials",
                expected="The login succeeds and the home dashboard is shown",
                steps=[
                    Step(text="Log in to CRM as admin", app="crm"),
                    Step(text="Check the home dashboard is visible", app="crm"),
                ],
            ),
        ])
        self.assertTrue(case_is_about_auth(plan.cases[0]))
        out = strip_run_handled_auth_steps(plan)
        self.assertEqual([s.text for s in out.cases[0].steps],
                         ["Log in to CRM as admin", "Check the home dashboard is visible"])

    def test_inferred_login_stripped_even_on_login_case(self):
        plan = TestPlan(task_id=1, env="staging", cases=[
            PlannedCase(
                id="TC1", title="Login fails with a wrong password",
                expected="An error is shown and the form stays",
                steps=[
                    Step(text="Log in to CRM as admin", app="crm", inferred=True,
                         inferred_reason="needed", inferred_source="facts.md"),
                    Step(text="Enter an incorrect password and submit", app="crm"),
                ],
            ),
        ])
        out = strip_run_handled_auth_steps(plan)
        self.assertFalse(any(is_login_step(s.text) and s.inferred for s in out.cases[0].steps))

    def test_autofix_strips_setup_login(self):
        plan = TestPlan(task_id=1, env="staging", cases=[
            PlannedCase(
                id="TC1", title="Open an order",
                expected="The order is visible",
                steps=[Step(text="Log in as the customer", app="portal", inferred=True,
                            inferred_reason="needed", inferred_source="facts.md"),
                       Step(text="Open the order", app="portal")],
            ),
        ])
        out = autofix(plan)
        self.assertEqual([s.text for s in out.cases[0].steps], ["Open the order"])

    def test_planner_prompt_forbids_inferred_login(self):
        self.assertIn("Do not add inferred login or logout steps", PLANNER)
        # The worked example must not teach the model to insert a login setup step.
        self.assertNotIn("Log in to CRM as admin", PLANNER)


class AlreadyLoggedInCompletesStep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _sess(Path(self.tmp.name), current_step=1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_skip_helper_marks_login_step_done(self):
        self.s.state.values["login_form.logged_in"] = {
            "value": "yes", "source": "login_form/crm"}
        self.s._raw_snapshot = (
            "Page URL: https://example.test/index.php?module=Administration&action=index\n"
            "Page Title: Administration\n"
            '- button "Administrator" [ref=e33]\n'
        )
        out = self.s._skip_login_if_already_in()
        self.assertIsNotNone(out)
        self.assertEqual(out["values"]["logged_in"], "yes")
        self.assertIn(1, self.s.result.steps_done)
        self.assertEqual(self.s.current_step, 2)

    def test_helper_no_form_completes_login_step(self):
        self.s._raw_snapshot = (
            "Page URL: https://example.test/index.php?module=Home&action=index\n"
            "Page Title: Home\n"
            '- button "Administrator" [ref=e33]\n'
        )
        out = self.s._after_login_helper({
            "error": "no login form on this page (no visible password field); "
                     "if you are already logged in, skip the login step",
        })
        self.assertNotIn("error", out)
        self.assertEqual(out["values"]["logged_in"], "yes")
        self.assertIn(1, self.s.result.steps_done)
        self.assertEqual(self.s.current_step, 2)

    def test_stuck_outcome_skips_completed_login_step(self):
        brief = {
            "case_id": "TC1",
            "title": "Quick Repair and Rebuild",
            "steps": [
                {"n": 1, "text": "Log in to CRM as admin"},
                {"n": 2, "text": "Open Repair"},
            ],
            "steps_done": [1],
        }
        r = resolve_step(brief, 1)
        self.assertEqual(r.get("step_n") or r.get("n"), 2)
        text = " ".join(str(v) for v in r.values())
        self.assertNotIn("Log in to CRM as admin", text)
        self.assertIn("Open Repair", text)


class RoleMarkerPrefix(unittest.TestCase):
    def test_prefix_allowed_only_when_already_logged_in_this_run(self):
        page = (
            "Page URL: https://example.test/index.php?module=Administration&action=index\n"
            "Page Title: Administration\n"
            '- button "Administrator" [ref=e33]\n'
        )
        # Signed-out: Admin must not match Administrator (PR #26 protection).
        self.assertIsNone(LF._signed_in_evidence(page, ["Admin"]))
        self.assertIsNone(LF._signed_in_evidence(page, ["Admin"], allow_role_prefix=False))
        # Already logged in this run: prefix is allowed off the login path.
        self.assertEqual(
            LF._signed_in_evidence(page, ["Admin"], allow_role_prefix=True),
            "role marker prefix",
        )

    def test_session_valid_with_prefix_when_flag_set(self):
        class FakeBrowser:
            async def snapshot(self):
                return (
                    "Page URL: https://example.test/index.php?module=Home&action=index\n"
                    '- button "Administrator" [ref=e33]\n'
                )

        class Ctx:
            app = "crm"
            state = SimpleNamespace(values={
                "login_form.logged_in": {"value": "yes", "source": "login_form/crm"}})
            config = {"path": "index.php?module=Users&action=Login"}
            def account(self, _a):
                return {"username": "u", "password_env": "PW", "role_markers": ["Admin"]}

        ctx = Ctx()
        with mock.patch.object(LF, "analyze", return_value={"has_password": False}):
            already = asyncio.run(LF._session_already_valid(
                FakeBrowser(), ctx, "admin", ctx.account("admin")))
        self.assertIsNotNone(already)
        self.assertEqual(already["values"]["logged_in"], "yes")

    def test_prefix_does_not_skip_on_login_path(self):
        class FakeBrowser:
            async def snapshot(self):
                return (
                    "Page URL: https://example.test/index.php?module=Users&action=Login\n"
                    "Page Title: Login\n"
                    '- button "Administrator" [ref=e33]\n'
                )

        class Ctx:
            app = "crm"
            state = SimpleNamespace(values={
                "login_form.logged_in": {"value": "yes", "source": "login_form/crm"}})
            config = {"path": "index.php?module=Users&action=Login"}
            def account(self, _a):
                return {"username": "u", "password_env": "PW", "role_markers": ["Admin"]}

        ctx = Ctx()
        with mock.patch.object(LF, "analyze", return_value={"has_password": False}):
            already = asyncio.run(LF._session_already_valid(
                FakeBrowser(), ctx, "admin", ctx.account("admin")))
        self.assertIsNone(already)

    def test_free_text_admin_on_signed_out_page_still_refuses(self):
        class FakeBrowser:
            async def snapshot(self):
                return ('Page Title: Login\n- heading "Administrator" [ref=e1]\n'
                        "- text: Admin login panel")

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


if __name__ == "__main__":
    unittest.main()


class SessionSwitchKeptTests(unittest.TestCase):
    def test_switch_and_logout_steps_kept(self):
        from agent_tester.plan_checks import is_session_switch_step, strip_run_handled_auth_steps
        self.assertTrue(is_session_switch_step("Log in as another client"))
        self.assertTrue(is_session_switch_step("Log in again as admin"))
        self.assertFalse(is_session_switch_step("Log in to the CRM as admin"))
        self.assertFalse(is_session_switch_step("Open the Invoices list"))
        from agent_tester.models import TestPlan
        plan = TestPlan.model_validate({"task_id": 1, "cases": [{"id": "C1", "title": "Wallet balance", "steps": [
            {"n": 1, "text": "Log in to the portal as the client"},
            {"n": 2, "text": "Open Wallet"},
            {"n": 6, "text": "Read the balance"},
            {"n": 7, "text": "Open Orders"},
            {"n": 8, "text": "Open Profile"},
            {"n": 3, "text": "Log out"},
            {"n": 4, "text": "Log in as another client"},
            {"n": 5, "text": "Log in as admin", "inferred": True}]}]})
        texts = [s.text for s in strip_run_handled_auth_steps(plan).cases[0].steps]
        self.assertEqual(texts, ["Open Wallet", "Read the balance", "Open Orders", "Open Profile", "Log out", "Log in as another client"])
