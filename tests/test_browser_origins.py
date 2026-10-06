import unittest
from types import SimpleNamespace

from agent_tester.agents.tester import browser_origins, run_urls


class Origins(unittest.TestCase):
    def test_ports_are_included(self):
        o = browser_origins(["staging5.rolustech.com"], ["https://staging5.rolustech.com:4225/",
                                                         "https://staging5.rolustech.com:4220/"])
        self.assertIn("https://staging5.rolustech.com:4225", o)
        self.assertIn("https://staging5.rolustech.com:4220", o)
        self.assertIn("https://staging5.rolustech.com", o)

    def test_urls_outside_allowlist_are_not_added(self):
        self.assertEqual(browser_origins(["a.example"], ["https://evil.example:8443/"]), ["https://a.example"])

    def test_run_url_from_plan_gate_wins(self):
        pack = SimpleNamespace(apps={"crm": {}}, url=lambda a, e: None)
        state = SimpleNamespace(data={"plan": {"requirements": [{"key": "env.preprod.crm", "value": "https://pre.example:8080/"}]}})
        self.assertEqual(run_urls(pack, "preprod", state), {"crm": "https://pre.example:8080/"})


if __name__ == "__main__":
    unittest.main()
