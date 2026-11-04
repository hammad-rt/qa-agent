import unittest
from types import SimpleNamespace as N
import yaml
from agent_tester import data_prep as D


def case(title, *steps):
    return N(title=title, preconditions=[], expected="", steps=[N(text=s) for s in steps], area="rewards")


class PrepRewards(unittest.TestCase):
    def setUp(self):
        rules = yaml.safe_load(open("packs/savostore/pack.yaml"))["data_prep"]
        self.so = next(r for r in rules if r["id"] == "shipping_only_order")["verify"]

    def test_earn_case_does_not_need_rewards_box(self):
        c = case("Earn on a paid order", "Pay S_e by card 4242, without rewards.")
        self.assertFalse(D.uses_rewards(c))
        t = D.verify_text(c, self.so)
        self.assertNotIn("You can use up to", t)
        self.assertIn("pays without rewards", t)
        self.assertNotIn("{rewards}", t)

    def test_redeem_case_needs_rewards_box(self):
        c = case("Partial rewards", "Apply $5 of Savo Rewards, then pay the rest by card 4242.")
        self.assertTrue(D.uses_rewards(c))
        self.assertIn("You can use up to", D.verify_text(c, self.so))

    def test_plain_text_rule_unchanged(self):
        self.assertEqual(D.verify_text(case("x"), {"text": "read it"}), "read it")


if __name__ == "__main__":
    unittest.main()
