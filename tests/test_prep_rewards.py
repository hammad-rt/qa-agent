import unittest
from types import SimpleNamespace as N
import yaml
from agent_tester import data_prep as D


def case(title, *steps):
    return N(title=title, preconditions=[], expected="", steps=[N(text=s) for s in steps], area="rewards")


class PrepCondition(unittest.TestCase):
    """The case-dependent part of a prep check comes from the pack (verify.condition), not the code."""
    def setUp(self):
        rules = yaml.safe_load(open("packs/savostore/pack.yaml"))["data_prep"]
        self.so = next(r for r in rules if r["id"] == "shipping_only_order")["verify"]

    def test_earn_case_does_not_need_rewards_box(self):
        c = case("Earn on a paid order", "Pay S_e by card 4242, without rewards.")
        self.assertFalse(D.case_matches(c, self.so["condition"]))
        t = D.verify_text(c, self.so)
        self.assertNotIn("You can use up to", t)
        self.assertIn("pays without rewards", t)
        self.assertNotIn("{condition}", t)

    def test_redeem_case_needs_rewards_box(self):
        c = case("Partial rewards", "Apply $5 of Savo Rewards, then pay the rest by card 4242.")
        self.assertTrue(D.case_matches(c, self.so["condition"]))
        self.assertIn("You can use up to", D.verify_text(c, self.so))

    def test_plain_text_rule_unchanged(self):
        self.assertEqual(D.verify_text(case("x"), {"text": "read it"}), "read it")

    def test_generic_condition(self):
        v = {"text": "ok.{condition}", "condition": {"when": ["coupon"], "then": "{space}Needs a coupon box.",
                                                     "else": ""}}
        self.assertEqual(D.verify_text(case("Use a coupon"), v), "ok. Needs a coupon box.")
        self.assertEqual(D.verify_text(case("Plain buy"), v), "ok.")
        self.assertFalse(D.case_matches(case("x"), None))


class Generic(unittest.TestCase):
    def test_settle_from_pack(self):
        import os
        from agent_tester.agents.tester import _settle_seconds
        os.environ.pop("TESTER_SETTLE_SECONDS_BACKOFFICE", None)
        pack = N(apps={"backoffice": {"settle_seconds": 7}})
        self.assertEqual(_settle_seconds("backoffice", pack), 7.0)
        self.assertEqual(_settle_seconds("web", pack), float(os.environ.get("TESTER_SETTLE_SECONDS", "4")))
        os.environ["TESTER_SETTLE_SECONDS_BACKOFFICE"] = "2"
        try:
            self.assertEqual(_settle_seconds("backoffice", pack), 2.0)
        finally:
            del os.environ["TESTER_SETTLE_SECONDS_BACKOFFICE"]

    def test_no_project_names_in_generic_code(self):
        import pathlib, re
        for f in ["agent_tester/data_prep.py", "agent_tester/agents/prompts.py", "agent_tester/agents/tester.py"]:
            t = pathlib.Path(f).read_text()
            self.assertIsNone(re.search(r"savo|reward|wallet|\bKES\b", t, re.I), f)


if __name__ == "__main__":
    unittest.main()
