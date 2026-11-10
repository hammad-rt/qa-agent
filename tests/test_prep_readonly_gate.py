"""Data-creating PREP on a read-only case must hit the approval gate (never auto-approve)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace as N
from unittest import mock

from agent_tester import data_prep as D
from agent_tester.models import PlannedCase, Step

WALLET = {
    "id": "wallet_topup", "value": "wallet_ready", "app": "crm", "find": "read wallet",
    "create": [{"app": "portal", "text": "earn/pay to fill wallet"},
               {"app": "crm", "text": "move to Order Delivered"}],
    "test_client": "H", "min_value": 0.01, "when": {"any": ["wallet", "max"]},
}


class State:
    def __init__(self):
        self.data, self.values, self.points = {"action_log": []}, {}, []

    def save(self):
        pass

    def checkpoint(self, m, **k):
        self.points.append(m)

    def log_action(self, **kw):
        self.data["action_log"].append(dict(kw))
        return N(seq=len(self.data["action_log"]))


class Fake:
    def __init__(self, script):
        self.script, self.calls = script, []

    def __call__(self, app, brief, state):
        kind = "create" if "-create" in brief["case_id"] else brief["case_id"].rsplit("-", 1)[-1]
        self.calls.append((kind, brief.get("read_only"), app, brief["case_id"]))
        v = self.script[kind].pop(0)
        return N(values=v, notes="", stuck=False, limit_hit=False)


class PrepReadonlyGate(unittest.TestCase):
    def test_case_is_read_only_detects_title(self):
        c = PlannedCase(id="C1", title="Final checks (read-only)", area="rewards",
                        steps=[Step(text="Read wallet balance", app="portal")])
        self.assertTrue(D.case_is_read_only(c))
        c2 = PlannedCase(id="E1", title="Earn on a paid order", area="rewards",
                         steps=[Step(text="Pay with card", app="portal", changes_data=True)])
        self.assertFalse(D.case_is_read_only(c2))

    def test_readonly_create_denied_without_approval(self):
        f = Fake({"find": [{"wallet_ready": "none"}],
                  "create": [{"wallet_ready": "9"}],
                  "check": [{"wallet_ready_ok": "yes"}]})
        st = State()
        case = PlannedCase(id="C1", title="Final checks (read-only)", area="rewards",
                           steps=[Step(text="Read Current Rewards / wallet", app="crm")])
        plan = N(env="local", requirements=[], task_id=1)
        with mock.patch("agent_tester.coordinator.brief_for",
                        side_effect=lambda p, c, s, st: {
                            "case_id": c.id, "steps": [{"text": c.steps[0].text}]}), \
                mock.patch.object(D, "_approve_prep_create", return_value=False):
            r = D._ensure_one(plan, case, st, {"crm": f, "portal": f}, WALLET)
        self.assertIsNotNone(r)
        self.assertEqual(r.verdict, "BLOCKED")
        self.assertIn("not approved", r.note.lower())
        # Create turns must not have run.
        self.assertFalse(any(c[0] == "create" for c in f.calls), f.calls)

    def test_readonly_create_runs_only_after_yes(self):
        f = Fake({"find": [{"wallet_ready": "0.0"}],
                  "create": [{"wallet_ready": "tmp"}, {"wallet_ready": "12"}],
                  "check": []})
        # min_value 0.01 → 0.0 is not usable → create path
        st = State()
        case = PlannedCase(id="C1", title="Final checks (read-only)", area="rewards",
                           steps=[Step(text="Confirm wallet is readable", app="portal")])
        plan = N(env="local", requirements=[], task_id=1)
        with mock.patch("agent_tester.coordinator.brief_for",
                        side_effect=lambda p, c, s, st: {
                            "case_id": c.id, "steps": [{"text": c.steps[0].text}]}), \
                mock.patch.object(D, "_approve_prep_create", return_value=True) as appr, \
                mock.patch.object(D, "_verify", return_value=("yes", "")):
            r = D._ensure_one(plan, case, st, {"crm": f, "portal": f}, WALLET)
        self.assertIsNone(r)
        appr.assert_called_once()
        self.assertTrue(any(c[0] == "create" for c in f.calls), f.calls)
        self.assertTrue(any(a.get("args", {}).get("action_type") == "prep_create"
                            for a in st.data["action_log"]))

    def test_approve_helper_never_uses_auto_default(self):
        """ask_line is used (person/web); auto prompter must not answer 'y' silently."""
        case = PlannedCase(id="C1", title="Final checks (read-only)",
                           steps=[Step(text="read", app="crm")])
        with mock.patch("agent_tester.auto.ask_line", return_value="") as ask, \
                mock.patch("agent_tester.auto.is_auto", return_value=True):
            self.assertFalse(D._approve_prep_create(case, "wallet_topup"))
        ask.assert_called_once()
        with mock.patch("agent_tester.auto.ask_line", return_value="y") as ask2, \
                mock.patch("agent_tester.auto.is_auto", return_value=True):
            self.assertTrue(D._approve_prep_create(case, "wallet_topup"))
        ask2.assert_called_once()


if __name__ == "__main__":
    unittest.main()
