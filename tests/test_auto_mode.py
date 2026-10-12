import os
import unittest
from unittest import mock

from agent_tester.tools.policy import Policy, PolicyViolation
from agent_tester import gate


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


if __name__ == "__main__":
    unittest.main()
