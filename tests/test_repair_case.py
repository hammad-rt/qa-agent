import unittest
from types import SimpleNamespace

from agent_tester.casegen import add_repair_case, crm_app, to_textile
from agent_tester.models import PlannedCase, Step, TestPlan
from agent_tester.plan_checks import check_plan


def _pack(stack):
    return SimpleNamespace(config={"stack": stack})


def _plan():
    return TestPlan(task_id=1, cases_source="generated", cases=[
        PlannedCase(id="TC1", title="Shipping only invoice", generated=True, expected="x", expected_source="user",
                    steps=[Step(text="open invoice", app="crm")]),
        PlannedCase(id="TC2", title="Portal shows it", generated=True, depends_on=["TC1"], expected="y",
                    expected_source="user", steps=[Step(text="open order", app="portal")])])


class RepairCase(unittest.TestCase):
    def test_crm_detection(self):
        self.assertEqual(crm_app(_pack({"crm": {"name": "SuiteCRM 7"}, "portal": {"name": "Next.js"}})), "crm")
        self.assertEqual(crm_app(_pack({"app": "SugarCRM 13"})), "app")
        self.assertIsNone(crm_app(_pack({"portal": {"name": "Next.js"}})))

    def test_prepends_and_renumbers(self):
        p = add_repair_case(_plan(), _pack({"crm": {"name": "SuiteCRM 7"}}))
        self.assertEqual([c.id for c in p.cases], ["TC1", "TC2", "TC3"])
        self.assertIn("Quick Repair and Rebuild", p.cases[0].title)
        self.assertEqual(p.cases[1].depends_on, ["TC1"])
        self.assertEqual(p.cases[2].depends_on, ["TC1", "TC2"])
        self.assertTrue(any(s.needs_approval for s in p.cases[0].steps))
        self.assertTrue(all(c.preconditions[0].startswith("Hard-reload") for c in p.cases[1:]))
        self.assertIn("Hard-reload", p.cases[0].steps[-1].text)
        self.assertTrue(all(s.app == "crm" for s in p.cases[0].steps))
        self.assertIn("h3. TC1: Run Quick Repair and Rebuild", to_textile(p))
        self.assertFalse([i for i in check_plan(p) if "TC1" in i and "requirement" not in i], check_plan(p))

    def test_idempotent_and_non_crm(self):
        pack = _pack({"crm": {"name": "SuiteCRM 7"}})
        once = add_repair_case(_plan(), pack)
        self.assertEqual(len(add_repair_case(once, pack).cases), 3)
        self.assertEqual(len(add_repair_case(_plan(), _pack({"portal": "Next.js"})).cases), 2)


if __name__ == "__main__":
    unittest.main()
