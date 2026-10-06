"""The browser may use only this run's sites; a new site the app sends it to (e.g. a payment page) needs the
user's OK, and is then allowed for the rest of the run."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tester.agents.tester import TesterSession, run_urls
from agent_tester.config import ROOT
from agent_tester.main import run_sites
from agent_tester.models import Requirement, TestPlan
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence

PACK = ROOT / "packs" / "savostore"


def page(url):
    return f"### Page state\n- Page URL: {url}\n- Page Title: x"


class BrowserSites(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = RunState(Path(self.tmp.name) / "run")
        self.answer = False
        self.asked = []
        self.policy = from_pack(load_pack(PACK), "staging",
                                approver=lambda a, r, d: self.asked.append(a) or self.answer)
        plan = TestPlan(task_id=1, requirements=[
            Requirement(key="env.preprod.portal", kind="url", value="https://pre.client-site.io:8443/", status="resolved"),
            Requirement(key="env.preprod.crm", kind="url", value="https://crm.client-site.io/", status="resolved")])
        self.sites = run_sites(self.policy, plan)
        self.s = TesterSession("portal", {"case_id": "TC1", "requirements": {}}, self.state, self.policy,
                               Evidence(self.state.dir), max_calls=50, max_minutes=5)

    def tearDown(self):
        self.tmp.cleanup()

    def t(self, n):
        return SimpleNamespace(name=n)

    def test_sites_come_from_the_run_not_the_pack(self):
        self.assertEqual(self.sites, ["pre.client-site.io", "crm.client-site.io"])
        self.assertNotIn("staging5.rolustech.com", self.policy.allow_hosts)

    def test_any_port_on_a_run_site_is_fine(self):
        self.assertIsNone(self.s.before_tool(self.t("browser_navigate"), {"url": "https://pre.client-site.io:8443/login"}, None))
        out = self.s.after_tool(self.t("browser_click"), {}, None, page("https://pre.client-site.io:8443/home"))
        self.assertNotIn("error", out if isinstance(out, dict) else {})
        self.assertEqual(self.asked, [])

    def test_navigate_elsewhere_is_blocked(self):
        out = self.s.before_tool(self.t("browser_navigate"), {"url": "https://other.example/"}, None)
        self.assertIn("Blocked", out["error"])

    def test_payment_redirect_asks_once_then_allowed(self):
        self.answer = True
        out = self.s.after_tool(self.t("browser_click"), {"element": "Pay"}, None, page("https://checkout.stripe.com/c/pay/x"))
        self.assertNotIn("error", out if isinstance(out, dict) else {})
        self.assertEqual(len(self.asked), 1)
        self.assertIn("checkout.stripe.com", self.policy.allow_hosts)
        self.s.after_tool(self.t("browser_type"), {}, None, page("https://checkout.stripe.com/c/pay/x#card"))
        self.assertEqual(len(self.asked), 1)

    def test_denied_site_blocks_actions_until_back(self):
        out = self.s.after_tool(self.t("browser_click"), {}, None, page("https://unknown.example/"))
        self.assertIn("unknown.example", out["error"])
        self.assertIn("Blocked", self.s.before_tool(self.t("browser_click"), {"element": "x"}, None)["error"])
        self.assertIsNone(self.s.before_tool(self.t("browser_navigate_back"), {}, None))
        self.s.after_tool(self.t("browser_navigate_back"), {}, None, page("https://pre.client-site.io:8443/home"))
        self.assertIsNone(self.s.off_site)

    def test_run_url_from_plan_gate_wins(self):
        pack = SimpleNamespace(apps={"crm": {}}, url=lambda a, e: None)
        state = SimpleNamespace(data={"plan": {"requirements": [{"key": "env.preprod.crm", "value": "https://pre.example:8080/"}]}})
        self.assertEqual(run_urls(pack, "preprod", state), {"crm": "https://pre.example:8080/"})


if __name__ == "__main__":
    unittest.main()
