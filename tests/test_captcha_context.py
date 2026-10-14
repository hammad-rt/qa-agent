"""#180974 fixes: CAPTCHA at login is handed to a person once (then the case is retried, or BLOCKED), and the
tester's history stays small enough for the 65K model."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from google.genai import types

from agent_tester.agents import prompts
from agent_tester.agents import tester as T
from agent_tester.config import ROOT
from agent_tester.coordinator import run_plan
from agent_tester.main import login_first, make_manual_login
from agent_tester.models import CaseResult, Requirement, Step, StepResult, PlannedCase, TestPlan
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence

PACK = ROOT / "packs" / "savostore"


def fr(name, text):
    return types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        name=name, response={"result": text}))])


class History(unittest.TestCase):
    def test_prune_keeps_last_two_browser_reads(self):
        contents = [types.Content(role="user", parts=[types.Part(text="do TC5")]),
                    fr("browser_snapshot", "page1"), fr("record_value", "ok"),
                    fr("browser_snapshot", "page2"), fr("browser_click", "page3"), fr("browser_snapshot", "page4")]
        self.assertEqual(T.prune_history(contents, 2), 2)
        got = [c.parts[0].function_response.response["result"] for c in contents[1:]]
        self.assertEqual(got, [T.DROPPED, "ok", T.DROPPED, "page3", "page4"])
        self.assertEqual(T.prune_history(contents, 2), 0)          # already pruned
        self.assertEqual(contents[0].parts[0].text, "do TC5")

    def test_long_page_cut(self):
        out = T._cap_obj({"content": [{"type": "text", "text": "x" * 50}]}, 20)
        text = out["content"][0]["text"]
        self.assertTrue(text.startswith("x" * 20) and "30 more characters" in text)
        self.assertEqual(T._cap_obj("short", 20), "short")

    def test_context_error_detected(self):
        self.assertTrue(T._is_context_error(Exception("This model's maximum context length is 65536 tokens")))
        self.assertFalse(T._is_context_error(ConnectionError("refused")))

    def test_tester_reply_limit_smaller(self):
        from agent_tester.agents import llm
        self.assertLessEqual(T.TESTER_MAX_TOKENS, 4096)
        with mock.patch.object(llm, "LiteLlm", create=True) as L:
            try:
                llm.model(mock.MagicMock(llm_base_url="http://x/v1", llm_model="gemma4", llm_api_key=""), max_tokens=2048)
            except Exception:  # noqa: BLE001  settings shape differs; only the kwarg matters
                pass
        if L.call_args:
            self.assertIn(2048, str(L.call_args))

    def test_persistent_profile(self):
        with tempfile.TemporaryDirectory() as d:
            ts = T.playwright_toolset(Path(d), "portal")
            args = ts._connection_params.server_params.args
            self.assertIn("--user-data-dir", args)
            self.assertNotIn("--isolated", args)
            self.assertTrue(str(args[args.index("--user-data-dir") + 1]).endswith("profiles/portal"))

    def test_prompts(self):
        self.assertIn("needs_manual_login", prompts.TESTER)
        self.assertIn("ticks a reCAPTCHA", prompts.TESTER)
        self.assertIn("ONLY when you can see a CAPTCHA", prompts.TESTER)
        self.assertIn("not a setting", prompts.PLANNER)
        self.assertIn("belong to the user chosen for this run", prompts.PLANNER)

    def test_tester_prompt_formats(self):
        # Literal JSON in the prompt must be double-braced so .format does not KeyError.
        text = prompts.TESTER.format(app_label="Customer portal", base_url="https://example/", env="staging")
        self.assertIn('run_helper("login_form", \'{"account": "<pack account>"}\')', text)


class LoginTool(unittest.TestCase):
    def test_needs_manual_login_tool(self):
        with tempfile.TemporaryDirectory() as d:
            state = RunState(Path(d) / "run")
            pol = from_pack(load_pack(PACK), "staging", approver=lambda *a: False)
            s = T.TesterSession("portal", {"case_id": "TC1", "requirements": {}}, state, pol,
                                Evidence(state.dir), max_calls=50, max_minutes=5)
            tool = next(t for t in s.tools() if t.__name__ == "needs_manual_login")
            tool("reCAPTCHA image challenge")
            self.assertTrue(s.finished and s.result.stuck)
            self.assertIn("reCAPTCHA", s.result.needs_login)


def _plan():
    return TestPlan(task_id=180974, env="staging", cases=[
        PlannedCase(id="TC1", title="t1", expected="x", steps=[Step(text="login", app="portal")]),
        PlannedCase(id="TC2", title="t2", expected="y", steps=[Step(text="see", app="portal")])],
        requirements=[Requirement(key="account.portal.customer", kind="account", value="hammad.iqbal@rolustech.com",
                                  status="resolved")])


class ManualLogin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = RunState(Path(self.tmp.name) / "run")
        self.judge = lambda b, a, r, s: CaseResult(case_id=b["case_id"], verdict="PASS")

    def tearDown(self):
        self.tmp.cleanup()

    def captcha_until_login(self):
        logged = {"in": False}

        def tester(app, brief, state):
            if not logged["in"]:
                return StepResult(case_id=brief["case_id"], app=app, stuck=True, needs_login="reCAPTCHA")
            return StepResult(case_id=brief["case_id"], app=app)
        return tester, logged

    def test_login_then_retry_once_for_whole_run(self):
        tester, logged = self.captcha_until_login()
        asked = []

        def login(app, reason):
            asked.append(app)
            logged["in"] = True
            return True
        res = run_plan(_plan(), self.state, {"portal": tester}, self.judge, manual_login=login,
                       manual_login_apps=["portal"])
        self.assertEqual(asked, ["portal"])
        self.assertEqual([res["TC1"].verdict, res["TC2"].verdict], ["PASS", "PASS"])

    def test_declined_is_blocked_not_incomplete(self):
        tester, _ = self.captcha_until_login()
        res = run_plan(_plan(), self.state, {"portal": tester}, lambda *a: self.fail("no judge"),
                       manual_login=lambda app, reason: False, manual_login_apps=["portal"])
        self.assertEqual(res["TC1"].verdict, "BLOCKED")
        self.assertIn("needs a person", res["TC1"].note)

    def test_no_callback_is_blocked(self):
        tester, _ = self.captcha_until_login()
        res = run_plan(_plan(), self.state, {"portal": tester}, lambda *a: self.fail("no judge"))
        self.assertEqual(res["TC1"].verdict, "BLOCKED")

    def test_login_first_from_pack(self):
        pack = load_pack(PACK)
        self.assertIn("portal", pack.config["manual_login_apps"])
        seen = []
        opener = lambda run_dir, app, url, user, input_fn=input: seen.append((app, url, user)) or True
        login = make_manual_login(pack, _plan(), self.state, opener=opener)
        with mock.patch.dict(os.environ, {"CAPTCHA_AUTO": "1"}):
            self.assertEqual(login_first(pack, _plan(), self.state, login, input_fn=lambda q: ""), [])  # auto tick first
        with mock.patch.dict(os.environ, {"CAPTCHA_AUTO": "0"}):
            did = login_first(pack, _plan(), self.state, login, input_fn=lambda q: "")
        self.assertEqual(did, ["portal"])
        self.assertEqual(seen[0][0], "portal")
        self.assertEqual(seen[0][2], "hammad.iqbal@rolustech.com")
        self.assertTrue(seen[0][1].startswith("http"))
        # already done: not asked again (resume)
        self.assertEqual(login_first(pack, _plan(), self.state, login, input_fn=lambda q: self.fail("asked")), [])

    def test_login_first_skipped_by_user(self):
        pack = load_pack(PACK)
        login = make_manual_login(pack, _plan(), self.state, opener=lambda *a, **k: self.fail("opened"))
        self.assertEqual(login_first(pack, _plan(), self.state, login, input_fn=lambda q: "n"), [])

    def test_session_lost_reoffers_manual_login_once(self):
        """If login_first marked portal done but CAPTCHA hits again, ask once more then retry."""
        attempts = {"n": 0}

        def tester(app, brief, state):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return StepResult(case_id=brief["case_id"], app=app, stuck=True, needs_login="reCAPTCHA")
            return StepResult(case_id=brief["case_id"], app=app)

        asked = []

        def login(app, reason):
            asked.append((app, reason))
            return True

        self.state.data["manual_logins"] = {"portal": True}
        res = run_plan(_plan(), self.state, {"portal": tester}, self.judge, manual_login=login,
                       manual_login_apps=["portal"])
        self.assertEqual(asked, [("portal", "reCAPTCHA")])
        self.assertEqual(res["TC1"].verdict, "PASS")
        self.assertTrue(self.state.data["manual_logins"]["portal"])
        self.assertEqual(self.state.data["manual_login_retries"]["portal"], 1)

    def test_second_session_loss_is_blocked(self):
        """Only one mid-run re-login per app; a further CAPTCHA is BLOCKED without another prompt."""
        def tester(app, brief, state):
            return StepResult(case_id=brief["case_id"], app=app, stuck=True, needs_login="reCAPTCHA again")

        asked = []

        def login(app, reason):
            asked.append((app, reason))
            return True

        self.state.data["manual_logins"] = {"portal": True}
        self.state.data["manual_login_retries"] = {"portal": 1}
        res = run_plan(_plan(), self.state, {"portal": tester}, lambda *a: self.fail("no judge"),
                       manual_login=login, manual_login_apps=["portal"])
        self.assertEqual(asked, [])
        self.assertEqual(res["TC1"].verdict, "BLOCKED")


    def test_needs_login_on_non_manual_app_is_blocked_without_offer(self):
        """CRM is not in manual_login_apps; coordinator must not offer a person login."""
        asked = []

        def tester(app, brief, state):
            return StepResult(case_id=brief["case_id"], app="crm", stuck=True,
                              needs_login="maybe CAPTCHA or session issue")

        def login(app, reason):
            asked.append(app)
            return True

        plan = TestPlan(task_id=1, env="staging", cases=[
            PlannedCase(id="TC1", title="c", expected="e",
                        steps=[Step(text="login", app="crm")])],
                        requirements=[])
        res = run_plan(plan, self.state, {"crm": tester}, lambda *a: self.fail("no judge"),
                       manual_login=login, manual_login_apps=["portal"])
        self.assertEqual(asked, [])
        self.assertEqual(res["TC1"].verdict, "BLOCKED")


class VisibleBrowser(unittest.TestCase):
    def test_display_passed_to_browser(self):
        with mock.patch.dict("os.environ", {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}), \
                tempfile.TemporaryDirectory() as d:
            ts = T.playwright_toolset(Path(d), "portal")
            env = ts._connection_params.server_params.env
            self.assertEqual(env.get("DISPLAY"), ":0")
            self.assertIn("--headless", ts._connection_params.server_params.args)

    def test_headed_flag(self):
        with mock.patch.dict("os.environ", {"QA_HEADED": "1"}), tempfile.TemporaryDirectory() as d:
            self.assertNotIn("--headless", T.playwright_toolset(Path(d), "portal")._connection_params.server_params.args)

    def test_no_display_says_so(self):
        msgs = []
        with mock.patch.dict("os.environ", {}, clear=True), tempfile.TemporaryDirectory() as d:
            self.assertFalse(T.manual_login(Path(d), "portal", "https://x/login", "u", out=msgs.append))
        self.assertIn("DISPLAY", msgs[0])

    def test_still_on_login(self):
        start = "https://staging5.rolustech.com:4225/login"
        self.assertTrue(T.still_on_login("https://staging5.rolustech.com:4225/login?next=/", start))
        self.assertFalse(T.still_on_login("https://staging5.rolustech.com:4225/home", start))
        self.assertFalse(T.still_on_login("https://staging5.rolustech.com:4225/", "https://staging5.rolustech.com:4225/"))

    def test_playwright_toolset_uses_storage_state_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            storage = T.storage_state_path(root, "portal")
            storage.parent.mkdir(parents=True)
            storage.write_text('{"cookies":[],"origins":[]}')
            with mock.patch.dict("os.environ", {"QA_HEADED": "1"}):
                args = T.playwright_toolset(root, "portal")._connection_params.server_params.args
            self.assertIn("--isolated", args)
            self.assertIn("--storage-state", args)
            self.assertIn(str(storage.resolve()), args)
            self.assertNotIn("--user-data-dir", args)
            self.assertNotIn("--headless", args)

    def test_stale_profile_locks_cleared(self):
        with tempfile.TemporaryDirectory() as d:
            profile = Path(d) / "profiles" / "portal"
            profile.mkdir(parents=True)
            lock = profile / "SingletonLock"
            lock.symlink_to("host-99999999")  # pid that is not running
            (profile / "SingletonCookie").symlink_to("x")
            self.assertFalse(T.profile_in_use(profile))
            self.assertTrue(T.wait_profile_free(profile, timeout=1.0))
            self.assertFalse(lock.exists())
            self.assertFalse((profile / "SingletonCookie").exists())

    def test_playwright_toolset_waits_for_free_profile(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            profile = T.profile_path(root, "portal")
            profile.mkdir(parents=True)
            (profile / "SingletonLock").symlink_to("host-99999999")
            with mock.patch.dict("os.environ", {"QA_HEADED": "1"}):
                ts = T.playwright_toolset(root, "portal")
            self.assertFalse((profile / "SingletonLock").exists())
            self.assertNotIn("--headless", ts._connection_params.server_params.args)

    def test_release_profile_does_not_kill_live_chrome(self):
        with tempfile.TemporaryDirectory() as d:
            profile = Path(d) / "profiles" / "portal"
            profile.mkdir(parents=True)
            (profile / "SingletonLock").symlink_to("host-424242")
            (profile / "SingletonCookie").symlink_to("x")
            signals = []

            def fake_kill(pid, sig):
                if pid != 424242:
                    raise ProcessLookupError()
                signals.append(sig)
                if sig == 0:
                    return None  # process still alive
                raise AssertionError(f"must not send {sig}")

            with mock.patch("os.kill", side_effect=fake_kill):
                self.assertTrue(T.profile_in_use(profile))
                self.assertFalse(T.release_profile(profile, timeout=0.2))
            self.assertTrue(signals)  # waited and probed
            self.assertTrue(all(s == 0 for s in signals))  # never SIGTERM/SIGKILL
            self.assertTrue((profile / "SingletonLock").is_symlink())  # dangling target; must not unlink

    def test_manual_login_saves_storage_then_closes(self):
        """After Enter: browser_storage_state then browser_close; no process kill."""
        calls = []

        class FakeRes:
            isError = False

        class FakeSess:
            async def initialize(self):
                return None

            async def call_tool(self, name, args=None):
                args = args or {}
                calls.append((name, args))
                if name == "browser_snapshot":
                    class R:
                        isError = False
                        content = [type("P", (), {"text": "Page URL: https://x/home\n"})()]
                    return R()
                if name == "browser_storage_state":
                    Path(args["filename"]).parent.mkdir(parents=True, exist_ok=True)
                    Path(args["filename"]).write_text('{"cookies":[],"origins":[]}')
                return FakeRes()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class FakeStdio:
            async def __aenter__(self):
                return (None, None)

            async def __aexit__(self, *a):
                return False

        msgs = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict("os.environ", {"DISPLAY": ":0"}), \
                mock.patch("mcp.client.stdio.stdio_client", return_value=FakeStdio()), \
                mock.patch("mcp.ClientSession", return_value=FakeSess()), \
                mock.patch.object(T, "release_profile", return_value=True) as rel:
            ok = T.manual_login(Path(d), "portal", "https://x/login", "u",
                                input_fn=lambda q: "", out=msgs.append)
        self.assertTrue(ok)
        names = [c[0] for c in calls]
        self.assertEqual(names[-2:], ["browser_storage_state", "browser_close"])
        self.assertTrue(any("Login saved" in m for m in msgs))
        rel.assert_called()


if __name__ == "__main__":
    unittest.main()


class AutoCheckbox(unittest.TestCase):
    def _run(self, reply):
        import asyncio
        from agent_tester.helpers.core import login_form as L
        b = mock.Mock()
        async def do(name, args): return reply
        b._do = do
        return asyncio.run(L.solve_checkbox(b))

    def test_passed(self):
        r = self._run('### Result\n"{\\"present\\": true, \\"passed\\": true, \\"challenge\\": false}"')
        self.assertTrue(r["passed"])

    def test_puzzle(self):
        r = self._run('### Result\n"{\\"present\\": true, \\"passed\\": false, \\"challenge\\": true}"')
        self.assertTrue(r["challenge"])


def _isolate_browser_home():
    """Each test gets its own shared-browser folder (runs/.browser by default)."""
    import shutil, unittest as _u
    for cls in [v for v in globals().values() if isinstance(v, type) and issubclass(v, _u.TestCase)]:
        orig = cls.setUp

        def setUp(self, _orig=orig):
            d = tempfile.mkdtemp()
            p = mock.patch.dict("os.environ", {"QA_BROWSER_DIR": d})
            p.start()
            self.addCleanup(p.stop)
            self.addCleanup(shutil.rmtree, d, True)
            _orig(self)
        cls.setUp = setUp


_isolate_browser_home()
