"""Accounts are asked every run (with the last user as the default), passwords are kept per user and
environment, and the environment can be staging, pre-prod or production."""
from agent_tester import testing_hermetic as hermetic  # noqa: F401
import os
import unittest
from types import SimpleNamespace

from agent_tester import gate
from agent_tester.helpers import HelperContext
from agent_tester.models import Requirement, Step, PlannedCase, TestPlan
from agent_tester.preflight import gap_resolver as G
from agent_tester.tools.policy import Policy, PolicyViolation


class FakeMemory:
    def __init__(self):
        self.rows = {}

    def get(self, scope, key):
        return self.rows.get(key)

    def put(self, scope, key, value, source, *, secret_env=None):
        self.rows[key] = {"value": value, "source": source, "secret_env": secret_env}


class FakePack:
    slug = "savostore"
    config = {}
    apps = {
        "portal": {"label": "Customer portal", "envs": {"staging": "https://s.example:4225/", "prod": "https://app.example/"},
                   "accounts": {"customer": {"username": "sidney@savostore.com", "password_env": "SAVO_STAGING_PORTAL_PASSWORD"}}},
        "crm": {"label": "CRM (SuiteCRM)", "envs": {"staging": "https://s.example:4220/"},
                "accounts": {"admin": {"username": "maria.tariq", "password_env": "SAVOSTORE_CRM_PASSWORD",
                                       "role_markers": ["Admin"]}}},
    }

    def accounts(self, app):
        return self.apps.get(app, {}).get("accounts", {})

    def url(self, app, env):
        return self.apps.get(app, {}).get("envs", {}).get(env)


def ctx(env="staging", mem=None):
    return G.GapContext(pack=FakePack(), env=env, memory=mem or FakeMemory(), wiki_texts={}, task_text="")


class AccountsAskedEachRun(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.saved = dict(os.environ)
        hermetic.clear_pack_secrets()
        for k in list(os.environ):
            if k.startswith(("SAVO", "SAVOSTORE")):
                del os.environ[k]
        self._prefs_tmp = tempfile.TemporaryDirectory()
        os.environ["MEMORY_PREFS_PATH"] = str(Path(self._prefs_tmp.name) / "prefs.json")
        # Keep suite hermetic even after clear/restore cycles in tearDown.
        os.environ["QA_INTERACTIVE"] = "1"
        os.environ["QA_TEST"] = "1"

    def tearDown(self):
        self._prefs_tmp.cleanup()
        os.environ.clear()
        os.environ.update(self.saved)
        os.environ["QA_INTERACTIVE"] = "1"
        os.environ["QA_TEST"] = "1"
        hermetic.clear_pack_secrets()

    def test_asked_even_when_pack_has_user_and_password(self):
        os.environ["SAVOSTORE_CRM_PASSWORD"] = "x"
        r = G.resolve_account(Requirement(key="account.crm.admin"), "crm", "admin", ctx())
        self.assertEqual(r.status, "missing")
        self.assertEqual(r.default, "maria.tariq")  # FakePack staging username
        self.assertIn("[Enter = maria.tariq]", r.question)

    def test_enter_keeps_default_and_reuses_existing_password(self):
        os.environ["SAVOSTORE_CRM_PASSWORD"] = "x"
        c = ctx()
        r = G.resolve(([Requirement(key="account.crm.admin")]), c)[0]
        asked = []
        gate.ask_batch([r], c, input_fn=lambda p: "", secret_fn=lambda p: asked.append(p) or "",
                       write_secret=lambda k, v: None, out=lambda *a: None)
        self.assertEqual((r.value, r.secret_env, r.status), ("maria.tariq", "SAVOSTORE_CRM_PASSWORD", "resolved"))
        self.assertEqual(asked, [])

    def test_other_user_gets_own_password_and_becomes_next_default(self):
        os.environ["SAVOSTORE_CRM_PASSWORD"] = "x"
        mem = FakeMemory()
        c = ctx(mem=mem)
        written = {}
        r = G.resolve([Requirement(key="account.crm.admin")], c)[0]
        gate.ask_batch([r], c, input_fn=lambda p: "hammad.iqbal", secret_fn=lambda p: "pw",
                       write_secret=lambda k, v: written.update({k: v}) or os.environ.update({k: v}), out=lambda *a: None)
        self.assertEqual(r.value, "hammad.iqbal")
        self.assertEqual(r.secret_env, "SAVOSTORE_CRM_PASSWORD__HAMMAD_IQBAL")
        self.assertEqual(list(written), ["SAVOSTORE_CRM_PASSWORD__HAMMAD_IQBAL"])
        self.assertEqual(r.must_have, ["Admin"])
        # last-used lives in local prefs (not lasting SQLite memory)
        r2 = G.resolve_account(Requirement(key="account.crm.admin"), "crm", "admin", ctx(mem=mem))
        self.assertEqual(r2.default, "hammad.iqbal")

    def test_resolved_user_answer_not_reasked_on_replan(self):
        r = Requirement(key="account.crm.admin", kind="account", value="u", status="resolved", source="user")
        G.resolve([r], ctx())
        self.assertEqual(r.status, "resolved")

    def test_pack_switch_off(self):
        os.environ["SAVOSTORE_CRM_PASSWORD"] = "x"
        c = ctx()
        c.pack.config = {"ask_accounts_each_run": False}
        try:
            r = G.resolve_account(Requirement(key="account.crm.admin"), "crm", "admin", c)
            self.assertEqual(r.status, "resolved")
        finally:
            c.pack.config = {}

    def test_missing_accounts_and_urls_added(self):
        plan = TestPlan(task_id=1, env="preprod", cases=[PlannedCase(id="TC1", title="t", steps=[
            Step(text="Log in", app="crm"), Step(text="Open order", app="portal")])])
        added = G.ensure_account_requirements(plan, FakePack(), "preprod")
        self.assertEqual(set(added), {"env.preprod.crm", "env.preprod.portal", "account.crm.admin", "account.portal.customer"})

    def test_helper_uses_run_user(self):
        state = SimpleNamespace(data={"plan": {"requirements": [
            {"key": "account.crm.admin", "value": "hammad.iqbal", "secret_env": "SAVOSTORE_CRM_PASSWORD__HAMMAD_IQBAL"},
            {"key": "env.preprod.crm", "value": "https://pre.example/"}]}})
        hc = HelperContext(pack=FakePack(), env="preprod", app="crm", case_id="TC1", state=state)
        acc = hc.account("admin")
        self.assertEqual(acc["username"], "hammad.iqbal")
        self.assertEqual(acc["password_env"], "SAVOSTORE_CRM_PASSWORD__HAMMAD_IQBAL")
        self.assertEqual(acc["role_markers"], ["Admin"])
        self.assertEqual(hc.url("index.php"), "https://pre.example/index.php")


class Environments(unittest.TestCase):
    def test_normalize(self):
        for t, e in [("pre-prod", "preprod"), ("Production", "prod"), ("2", "preprod"), ("staging", "staging")]:
            self.assertEqual(G.normalize_env(t), e)
        self.assertIsNone(G.normalize_env("qa9"))

    def test_choose_env(self):
        self.assertEqual(gate.choose_env(FakePack(), input_fn=lambda p: "", out=lambda *a: None), "staging")
        self.assertEqual(gate.choose_env(FakePack(), input_fn=lambda p: "pre-prod", out=lambda *a: None), "preprod")
        answers = iter(["production", "n"])
        self.assertIsNone(gate.choose_env(FakePack(), input_fn=lambda p: next(answers), out=lambda *a: None))

    def test_password_env_per_environment(self):
        self.assertEqual(G.password_env_for("CRM_PW", "maria.tariq", "prod"), "CRM_PW__PROD__MARIA_TARIQ")
        self.assertEqual(G.password_env_for("CRM_PW", "maria.tariq"), "CRM_PW__MARIA_TARIQ")

    def test_prod_default_user_not_taken_from_staging_pack(self):
        r = G.resolve_account(Requirement(key="account.crm.admin"), "crm", "admin", ctx("prod"))
        self.assertIsNone(r.default)

    def _policy(self, env):
        return Policy(allow_hosts=[], risky_patterns=[], env=env,
                      preapprovable=["create_test_order", "pay_staging_test_card", "change_settings"], always_ask=["redmine_post", "send_email", "prod_write"])

    def test_prod_never_preapproves(self):
        p = self._policy("prod")
        with self.assertRaises(PolicyViolation):
            p.preapprove(["create_test_order"])

    def test_staging_card_only_on_staging(self):
        p = self._policy("preprod")
        p.preapprove(["create_test_order"])
        with self.assertRaises(PolicyViolation):
            p.preapprove(["pay_staging_test_card"])

    def test_plan_gate_on_prod_offers_nothing(self):
        plan = TestPlan(task_id=1, env="prod", preapprovable_actions=["create_test_order"])
        lines = []
        ok, pre, _corr = gate.plan_gate(plan, input_fn=lambda p: "y", out=lines.append, policy=self._policy("prod"))
        self.assertEqual(pre, [])
        self.assertTrue(any("ask you before each" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
