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

    def test_client_label_run_email_always_wins(self):
        # No login: pack name only (legacy / blocked unless set).
        self.assertEqual(data_prep.client_label("Hammad Iqbal", None), "Hammad Iqbal")
        self.assertIsNone(data_prep.client_label(None, None))
        # Preprod/local/prod: portal email alone — no hard-coded staging name.
        pre = data_prep.client_label(None, "equipmentventures@gmail.com")
        self.assertIn("equipmentventures@gmail.com", pre)
        self.assertNotIn("Hammad", pre)
        # Staging hint present: email still primary; name is example only.
        both = data_prep.client_label("Hammad Iqbal", "equipmentventures@gmail.com")
        self.assertTrue(both.startswith("the client whose login / email is equipmentventures@gmail.com"))
        self.assertIn("often named Hammad Iqbal", both)
        self.assertIn("never by name alone", both)
        # Staging usual login can still mention Hammad as the hint without leading with it.
        stg = data_prep.client_label("Hammad Iqbal", "hammad.iqbal@rolustech.com")
        self.assertTrue(stg.startswith("the client whose login / email is hammad.iqbal@rolustech.com"))

    def test_no_client_for_env_blocks(self):
        r = data_prep.ensure_data(SimpleNamespace(env="prod"), case("a"), State(), {}, [{**RULES[0], "test_client": {"staging": "H"}}])
        self.assertEqual(r.verdict, "BLOCKED")

    def test_env_limited_rule_blocks_with_note(self):
        rule = {**RULES[0], "id": "w", "envs": ["staging"], "blocked_note": "top up by hand"}
        r = data_prep.ensure_data(SimpleNamespace(env="prod", requirements=[]), case("a"), State(), {}, [rule])
        self.assertEqual((r.verdict, "top up by hand" in r.note), ("BLOCKED", True))

    def test_prod_changes_ask_even_in_auto(self):
        asked = []
        p = Policy(allow_hosts=[], risky_patterns=[], preapprovable=[], always_ask=[], env="prod", auto=True,
                   approver=lambda a, r, d: asked.append(a) or True)
        self.assertEqual(p.gate("Save order", "r", risky=True), "approved")
        self.assertEqual(asked, ["[PRODUCTION] Save order"])
        for a in ("Pay by Card", "checkout.stripe.com"):
            with self.assertRaises(PolicyViolation):
                p.gate(a, "r", risky=True)
        self.assertEqual(len(asked), 1)                 # payments are refused, never asked

    def test_prod_denied_or_no_approver(self):
        p = Policy(allow_hosts=[], risky_patterns=[], preapprovable=[], always_ask=[], env="prod", auto=True,
                   approver=lambda a, r, d: False)
        with self.assertRaises(PolicyViolation):
            p.gate("Save order", "r", risky=True)
        p = Policy(allow_hosts=[], risky_patterns=[], preapprovable=[], always_ask=[], env="prod", auto=True)
        with self.assertRaises(PolicyViolation):
            p.gate("Update status", "r", action_type="change_status")

    def test_prod_change_detection(self):
        p = Policy(allow_hosts=[], risky_patterns=[], preapprovable=[], always_ask=[], env="prod")
        yes = [("browser_click", "Save"), ("browser_click", "Delete record"), ("browser_select_option", "Status"),
               ("browser_click", "Mass Update"), ("browser_click", "Quick Repair and Rebuild")]
        yes += [("browser_type", "Shipment Value"), ("browser_click", "Door delivery switch"),
                ("browser_click", "Shipment Protection checkbox")]
        no = [("browser_click", "Orders"), ("browser_click", "Edit"),
              ("browser_click", "Detail View tab")]
        for t, l in yes:
            self.assertTrue(p.prod_change(t, l), (t, l))
        for t, l in no:
            self.assertFalse(p.prod_change(t, l), (t, l))
        self.assertTrue(p.prod_change("browser_press_key", "", {"key": "Enter"}))
        self.assertTrue(p.prod_change("browser_click", "Status cell", {"doubleClick": True}))
        self.assertTrue(p.prod_change("browser_type", "Name", {"submit": True}))
        stg = Policy(allow_hosts=[], risky_patterns=[], preapprovable=[], always_ask=[], env="staging")
        self.assertFalse(stg.prod_change("browser_click", "Save"))

if __name__ == "__main__":
    unittest.main()


class OrderIdsStayText(unittest.TestCase):
    def test_float_and_text_ids_in_do_not_use_list(self):
        self.assertEqual(", ".join(str(u) for u in [1142.0, "B1465"]), "1142.0, B1465")
