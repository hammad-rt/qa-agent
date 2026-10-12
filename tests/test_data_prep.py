import unittest
from types import SimpleNamespace
from agent_tester import data_prep
from agent_tester.models import PlannedCase, Step
from agent_tester.tools.policy import Policy, PolicyViolation

RULES = [{"id": "o", "value": "test_order", "app": "crm", "find": "f", "create": "c",
          "when": {"areas": ["rewards"], "any": ["stripe"], "none": ["buy & ship"]}, "consumed_by": ["complete payment"]}]


def case(title, area="rewards"):
    return PlannedCase(id="B7", title=title, area=area, steps=[Step(text="go to stripe", app="portal")])


class State:
    def __init__(self): self.data, self.values = {}, {}
    def save(self): pass
    def checkpoint(self, *a, **k): pass


class DataPrep(unittest.TestCase):
    def test_match_and_exclusion(self):
        self.assertIsNotNone(data_prep.matching_rule(case("abandon"), RULES))
        self.assertIsNone(data_prep.matching_rule(case("Buy & Ship flow"), RULES))
        self.assertIsNone(data_prep.matching_rule(case("x", area="orders"), RULES))

    def test_reuse_until_consumed(self):
        st = State(); st.data["prep"] = {"o": {"value": "A1142", "consumed": False}}
        self.assertIsNone(data_prep.ensure_data(SimpleNamespace(env="prod"), case("a"), st, {}, RULES))
        self.assertEqual(st.values["test_order"]["value"], "A1142")
        data_prep.after_case(case("then complete payment"), st, RULES)
        self.assertTrue(st.data["prep"]["o"]["consumed"])

    def test_no_client_for_env_blocks(self):
        r = data_prep.ensure_data(SimpleNamespace(env="prod"), case("a"), State(), {}, [{**RULES[0], "test_client": {"staging": "H"}}])
        self.assertEqual(r.verdict, "BLOCKED")

    def test_env_limited_rule_blocks_with_note(self):
        rule = {**RULES[0], "id": "w", "envs": ["staging"], "blocked_note": "top up by hand"}
        r = data_prep.ensure_data(SimpleNamespace(env="prod", requirements=[]), case("a"), State(), {}, [rule])
        self.assertEqual((r.verdict, "top up by hand" in r.note), ("BLOCKED", True))

    def test_prod_auto_except_payment(self):
        p = Policy(allow_hosts=[], risky_patterns=[], preapprovable=[], always_ask=[], env="prod", auto=True)
        self.assertEqual(p.gate("Save order", "r", risky=True), "approved")
        for a in ("Pay by Card", "checkout.stripe.com"):
            with self.assertRaises(PolicyViolation):
                p.gate(a, "r", risky=True)


if __name__ == "__main__":
    unittest.main()
