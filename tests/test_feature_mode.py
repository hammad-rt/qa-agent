import unittest
from types import SimpleNamespace

from agent_tester import feature as F
from agent_tester.casegen import add_repair_case
from agent_tester.models import PlannedCase, Step, TestPlan
from agent_tester.preflight.redmine import Redmine


def _case(i, title, deps=(), steps=("open order",), gen=False):
    return PlannedCase(id=i, title=title, expected="shows it", depends_on=list(deps), generated=gen,
                       steps=[Step(text=s, app="crm") for s in steps])


class Merge(unittest.TestCase):
    def test_prefix_dedupe_and_one_repair(self):
        a = TestPlan(task_id=11, cases=[_case("TC1", "Card shows price"), _case("TC2", "Save keeps value", ["TC1"])])
        b = TestPlan(task_id=12, cases=[_case("TC1", "card shows PRICE."), _case("TC2", "Other", gen=True)])
        m = F.merge_plans(10, "staging", [(11, a), (12, b)])
        self.assertEqual([c.id for c in m.cases], ["11.TC1", "11.TC2", "12.TC2"])
        self.assertEqual(m.cases[0].covers, [11, 12])
        self.assertEqual(m.cases[1].depends_on, ["11.TC1"])
        self.assertTrue(m.cases[2].generated)
        r = add_repair_case(m, SimpleNamespace(config={"stack": {"crm": {"name": "SuiteCRM 7"}}}))
        self.assertEqual(r.cases[0].id, "TC0")
        self.assertEqual(sum("Quick Repair" in c.title for c in r.cases), 1)
        self.assertTrue(all("TC0" in c.depends_on for c in r.cases[1:]))


class FakeRM(Redmine):
    def __init__(self, issues):
        self.issues = issues

    def issue(self, iid):
        return self.issues[iid]


def _iss(i, subj, children=(), relations=()):
    return {"id": i, "subject": subj, "project": {"id": 1}, "children": [{"id": c, "subject": s} for c, s in children],
            "relations": list(relations), "journals": []}


class Walk(unittest.TestCase):
    def test_children_grandchildren_related_and_containers(self):
        rm = FakeRM({
            10: _iss(10, "Feature", [(11, "Sub A"), (12, "Group B")], [{"issue_id": 10, "issue_to_id": 30, "relation_type": "relates"}]),
            11: _iss(11, "Sub A", [(21, "Test Cases - Sub A")]),
            21: _iss(21, "Test Cases - Sub A"),
            12: _iss(12, "Group B", [(13, "Sub B1")]),
            13: _iss(13, "Sub B1", [(23, "Test Cases - Sub B1")]),
            23: _iss(23, "Test Cases - Sub B1"),
            30: _iss(30, "Related C"),
        })
        got = {b.issue["id"]: b for b in rm.feature_tasks(10)}
        self.assertEqual(sorted(got), [11, 13, 30])           # feature and the grouping subtask aren't planned
        self.assertEqual(got[13].test_case_task["id"], 23)
        self.assertEqual(got[30].via, "related")
        self.assertIsNone(got[30].test_case_task)              # no Test Cases: its cases get written
        self.assertTrue(F.is_feature(list(got.values()), 10))
        self.assertFalse(F.is_feature([], 10))
