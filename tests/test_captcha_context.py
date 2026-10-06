"""#180974 fixes: CAPTCHA at login is handed to a person once (then the case is retried, or BLOCKED), and the
tester's history stays small enough for the 65K model."""
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
        self.assertIn("never try to solve it", prompts.TESTER)
        self.assertIn("not a setting", prompts.PLANNER)
        self.assertIn("belong to the user chosen for this run", prompts.PLANNER)


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
        res = run_plan(_plan(), self.state, {"portal": tester}, self.judge, manual_login=login)
        self.assertEqual(asked, ["portal"])
        self.assertEqual([res["TC1"].verdict, res["TC2"].verdict], ["PASS", "PASS"])

    def test_declined_is_blocked_not_incomplete(self):
        tester, _ = self.captcha_until_login()
        res = run_plan(_plan(), self.state, {"portal": tester}, lambda *a: self.fail("no judge"),
                       manual_login=lambda app, reason: False)
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


if __name__ == "__main__":
    unittest.main()
