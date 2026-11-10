from agent_tester import testing_hermetic as hermetic  # noqa: F401
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.tools.policy import Policy, PolicyViolation
from agent_tester import gate
from agent_tester.config import load_dotenv
from agent_tester.models import Requirement
from agent_tester.preflight import gap_resolver as G


def pol(env="staging"):
    return Policy(allow_hosts=["x"], risky_patterns=[], preapprovable=[], always_ask=["redmine_post", "send_email"],
                  env=env, auto=True, approver=lambda *a: (_ for _ in ()).throw(AssertionError("asked")))


class AutoMode(unittest.TestCase):
    def test_staging_actions_never_ask(self):
        p = pol()
        self.assertEqual(p.gate("pay", "risky", risky=True), "approved")
        self.assertEqual(p.gate("set", "s", action_type="change_settings"), "approved")
        self.assertEqual(p.log[-1]["approval"], "auto")

    def test_prod_writes_skipped_not_asked(self):
        with self.assertRaises(PolicyViolation):
            pol("prod").gate("pay", "risky", risky=True)

    def test_posting_still_goes_to_approver(self):
        with self.assertRaises(AssertionError):
            pol().gate("post", "p", action_type="redmine_post")

    def test_plan_gate_and_env_need_no_input(self):
        with mock.patch.dict(os.environ, {"QA_INTERACTIVE": ""}), mock.patch("builtins.input", side_effect=AssertionError):
            plan = mock.Mock(preapprovable_actions=[])
            self.assertEqual(gate.plan_gate(plan, out=lambda *a: None), (True, []))
            self.assertEqual(gate.choose_env(mock.Mock(apps={}), out=lambda *a: None), "staging")

    def test_ask_batch_honors_injected_input_fn_in_auto(self):
        """Product bug: auto mode used to call real input() for accounts and ignore input_fn."""
        from types import SimpleNamespace

        class FakePack:
            slug = "demo"
            config = {"ask_accounts_each_run": True}
            apps = {"crm": {"label": "CRM", "accounts": {"admin": {"username": "admin",
                                                                   "password_env": "DEMO_CRM_PASSWORD"}}}}

            def accounts(self, app):
                return self.apps[app]["accounts"]

            def url(self, app, env):
                return None

        with mock.patch.dict(os.environ, {"QA_INTERACTIVE": ""}), \
                mock.patch("builtins.input", side_effect=AssertionError("must not call input")):
            with tempfile.TemporaryDirectory() as d:
                os.environ["MEMORY_PREFS_PATH"] = str(Path(d) / "prefs.json")
                mem = SimpleNamespace(get=lambda *a, **k: None, put=lambda *a, **k: None)
                ctx = G.GapContext(pack=FakePack(), env="staging", memory=mem, wiki_texts={}, task_text="")
                r = G.resolve([Requirement(key="account.crm.admin")], ctx)[0]
                written = {}
                gate.ask_batch([r], ctx, input_fn=lambda p: "alice",
                               secret_fn=lambda p: "pw",
                               write_secret=lambda k, v: written.update({k: v}) or os.environ.update({k: v}),
                               out=lambda *a: None)
                self.assertEqual(r.value, "alice")
                self.assertEqual(r.status, "resolved")
                self.assertTrue(written)

    def test_load_dotenv_skipped_under_qa_test(self):
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            env_path.write_text("SHOULD_NOT_LOAD=1\n")
            with mock.patch.dict(os.environ, {"QA_TEST": "1"}, clear=False):
                os.environ.pop("SHOULD_NOT_LOAD", None)
                load_dotenv(env_path)
                self.assertNotIn("SHOULD_NOT_LOAD", os.environ)


if __name__ == "__main__":
    unittest.main()
