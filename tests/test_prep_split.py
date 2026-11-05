import unittest
from types import SimpleNamespace as N
from unittest import mock
from agent_tester import data_prep as D
from agent_tester.models import PlannedCase, Step

RULE = {"id": "o", "value": "test_order", "app": "crm", "find": "find for {client}", "create": "make for {client}",
        "test_client": "H", "when": {"any": ["invoice"]},
        "verify": {"app": "portal", "text": "check {value} {record}"}}
WALLET = {"id": "w", "value": "wallet_ready", "app": "crm", "find": "f", "create": "c", "test_client": "H",
          "min_value": 0.01, "when": {"any": ["max"]}}


class State:
    def __init__(self):
        self.data, self.values, self.points = {"action_log": []}, {}, []
    def save(self): pass
    def checkpoint(self, m, **k): self.points.append(m)


def case(title="Open the invoice and apply rewards"):
    return PlannedCase(id="B1", title=title, area="portal", steps=[Step(text=title, app="portal")])


class Fake:
    """Answers each prep turn from a script keyed by the turn kind (find / create / check)."""
    def __init__(self, script):
        self.script, self.calls = script, []
    def __call__(self, app, brief, state):
        kind = brief["case_id"].rsplit("-", 1)[-1]
        self.calls.append((kind, brief["read_only"], brief["steps"][0]["text"]))
        v = self.script[kind].pop(0)
        if isinstance(v, Exception):
            raise v
        return N(values=v, notes="", stuck=False, limit_hit=False)


def run(rule, script, title=None):
    f = Fake(script)
    st = State()
    plan = N(env="local", requirements=[], task_id=1)
    with mock.patch("agent_tester.coordinator.brief_for",
                    side_effect=lambda p, c, s, st: {"case_id": c.id, "steps": [{"text": c.steps[0].text}]}):
        r = D._ensure_one(plan, case(title) if title else case(), st, {"crm": f, "portal": f}, rule)
    return r, st, f


class PrepSplit(unittest.TestCase):
    def test_find_is_read_only_and_exact(self):
        r, st, f = run(RULE, {"find": [{"test_order": "A1", "test_order_record": "r1"}], "check": [{"test_order_ok": "yes"}]})
        self.assertIsNone(r)
        self.assertEqual(f.calls[0][:2], ("find", True))
        self.assertIn("Never edit", f.calls[0][2])
        self.assertEqual(st.values["test_order_record"]["value"], "r1")

    def test_none_found_creates(self):
        r, st, f = run(RULE, {"find": [{"test_order": "none"}], "create": [{"test_order": "A9", "test_order_record": "r9"}],
                              "check": [{"test_order_ok": "yes"}]})
        self.assertIsNone(r)
        self.assertEqual([c[:2] for c in f.calls], [("find", True), ("create", False), ("check", True)])
        self.assertEqual(st.data["prep"]["o"]["value"], "A9")

    def test_rejected_is_remembered_and_replaced(self):
        r, st, f = run(RULE, {"find": [{"test_order": "A1"}], "create": [{"test_order": "A2"}],
                              "check": [{"test_order_ok": "no: balance 0"}, {"test_order_ok": "yes"}]})
        self.assertIsNone(r)
        self.assertEqual(st.data["prep_rejected"]["o"][0]["value"], "A1")
        self.assertIn("A1", f.calls[1][2])               # create is told not to reuse A1

    def test_unclear_answer_is_asked_again(self):
        r, st, f = run(RULE, {"find": [{"test_order": "A1"}], "check": [{"test_order_ok": "0.0"}, {"test_order_ok": "yes"}]})
        self.assertIsNone(r)
        self.assertIn("not a yes or no", f.calls[-1][2])

    def test_empty_wallet_keeps_the_order(self):
        r, st, f = run(RULE, {"find": [{"test_order": "A1"}],
                              "check": [{"test_order_ok": "no: you have no savo rewards at the moment"}]})
        self.assertEqual(r.verdict, "BLOCKED")
        self.assertIn("wallet", r.note)
        self.assertFalse(st.data["prep"]["o"]["consumed"])

    def test_negative_wallet_is_not_ready(self):
        r, st, f = run(WALLET, {"find": [{"wallet_ready": "-0.22"}], "create": [{"wallet_ready": "12.5"}]})
        self.assertIsNone(r)
        self.assertEqual(st.data["prep"]["w"]["value"], "12.5")

    def test_model_outage_waits_then_retries(self):
        with mock.patch("agent_tester.reliability.wait_for_model", return_value=True):
            r, st, f = run(RULE, {"find": [ConnectionError("Connection error."), {"test_order": "A1"}],
                                  "check": [{"test_order_ok": "yes"}]})
        self.assertIsNone(r)
        self.assertTrue(any("waiting" in p for p in st.points))

    def test_portal_case_with_placeholder_matches(self):
        import yaml
        rules = yaml.safe_load(open("packs/savostore/pack.yaml"))["data_prep"]
        c = PlannedCase(id="B12", title="Apply / remove does not stack", area="portal",
                        steps=[Step(text="Open the invoice for ORDER-MULTI", app="portal")])
        self.assertIn("shipping_only_order", [r["id"] for r in D.matching_rules(c, rules)])
        e1 = PlannedCase(id="E1", title="Earn on a paid order", area="rewards",
                         steps=[Step(text="Open the ORDER-EARN invoice on the portal", app="portal")])
        self.assertNotIn("paid_order_new_parcel", [r["id"] for r in D.matching_rules(e1, rules)])


if __name__ == "__main__":
    unittest.main()
