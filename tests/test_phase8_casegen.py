"""Phase 8: cases written from the task description when there is no Test Cases task."""
import tempfile
import unittest
from pathlib import Path

from agent_tester import casegen
from agent_tester.config import ROOT
from agent_tester.models import CaseResult, PlannedCase, Step, TestPlan
from agent_tester.plan_checks import check_plan
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.preflight.redmine import TaskBundle
from agent_tester.report import bug_drafts
from agent_tester.run_state import RunState
from agent_tester.tools.policy import PolicyViolation, from_pack

PACK = ROOT / "packs" / "savostore"
DESC = "Customers earn rewards. Orders over 150 get 5% points. Points are credited after the order is delivered."


def bundle(tc=None, subject="Rewards on delivered orders"):
    return TaskBundle(issue={"id": 500, "subject": subject, "description": DESC, "project": {"id": 1}},
                      test_case_task=tc, comments=["Cancelled orders must not get points."])


def gen_plan():
    return casegen.mark_generated(TestPlan(task_id=500, cases=[
        PlannedCase(id="G1", title="Points credited on delivery", steps=[Step(text="Deliver order of 200")],
                    expected="Points are credited after the order is delivered",
                    expected_source='description: "Points are credited after the order is delivered"'),
        PlannedCase(id="G2", title="Boundary 150", steps=[Step(text="Order of exactly 150")],
                    open_question="What should happen at exactly 150?"),
        PlannedCase(id="G3", title="Cancelled order", steps=[Step(text="Cancel order")], depends_on=["G1"],
                    expected="No points", expected_source='comment 1: "Cancelled orders must not get points."'),
    ]))


class Detect(unittest.TestCase):
    def test_no_tc_child_means_generate(self):
        self.assertTrue(casegen.needs_generation(bundle()))
        self.assertFalse(casegen.needs_generation(bundle(tc={"id": 501, "subject": "Test Cases - x"})))
        self.assertFalse(casegen.needs_generation(bundle(subject="Test Cases - Rewards")))

    def test_mark_generated_is_done_by_code(self):
        p = casegen.mark_generated(TestPlan(task_id=1, cases=[PlannedCase(id="G1", title="t")]))
        self.assertEqual(p.cases_source, "generated")
        self.assertTrue(all(c.generated for c in p.cases))


class Checks(unittest.TestCase):
    def test_good_plan_passes(self):
        p = gen_plan()
        self.assertEqual(casegen.check_generated(p, casegen.task_sources(bundle())), [])
        self.assertEqual([i for i in check_plan(p) if i.startswith("G")], [])

    def test_made_up_quote_is_caught(self):
        p = gen_plan()
        p.cases[0].expected_source = 'description: "Points are credited instantly"'
        issues = casegen.check_generated(p, casegen.task_sources(bundle()))
        self.assertTrue(any("G1" in i and "not found" in i for i in issues))

    def test_missing_source_or_expected_flagged(self):
        p = gen_plan()
        p.cases[0].expected_source = None
        p.cases[1].open_question = None
        issues = check_plan(p)
        self.assertTrue(any("G1" in i and "comes from" in i for i in issues))
        self.assertTrue(any("G2" in i and "no question" in i for i in issues))

    def test_task_cases_are_not_held_to_generated_rules(self):
        p = TestPlan(task_id=1, cases=[PlannedCase(id="TC1", title="t", expected="x")])
        self.assertEqual(check_plan(p), [])

    def test_coverage_notes(self):
        p = gen_plan()
        p.cases[1].steps = [Step(text="Order of 100")]
        p.cases[1].title = "Small order"
        notes = casegen.coverage_notes(p, DESC)
        self.assertTrue(any("150" in n for n in notes))
        self.assertTrue(any("invalid" in n for n in notes))
        self.assertFalse(any("150" in n for n in casegen.coverage_notes(gen_plan(), DESC)))


class Gate(unittest.TestCase):
    def test_question_answer_sets_expected_with_user_source(self):
        p = gen_plan()
        ans = casegen.ask_case_questions(p, input_fn=lambda _: "No points (not over 150)", out=lambda *_: None)
        self.assertEqual(ans[0]["question"], "What should happen at exactly 150?")
        self.assertEqual(p.cases[1].expected, "No points (not over 150)")
        self.assertTrue(p.cases[1].expected_source.startswith("user"))
        self.assertIsNone(p.cases[1].open_question)

    def test_blank_answer_skips_case(self):
        p = gen_plan()
        casegen.ask_case_questions(p, input_fn=lambda _: "", out=lambda *_: None)
        self.assertTrue(p.cases[1].skip_reason)

    def test_drop_cascades_and_add(self):
        inputs = iter(["G1", "Refund removes points", "Points go back to 0", ""])
        out = []
        p = casegen.edit_cases(gen_plan(), input_fn=lambda _: next(inputs), out=out.append)
        ids = [c.id for c in p.cases]
        self.assertNotIn("G1", ids)
        self.assertNotIn("G3", ids)            # depended on G1
        self.assertIn("G4", ids)
        g4 = p.cases[-1]
        self.assertTrue(g4.generated and g4.expected_source.startswith("user"))


class PostBack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = RunState(Path(self.tmp.name) / "500-20261006-120000")
        self.st.set_plan(gen_plan())
        self.pack = load_pack(PACK)

    def tearDown(self):
        self.tmp.cleanup()

    def test_textile_has_cases_and_sources_no_inferred(self):
        p = gen_plan()
        p.cases[0].steps.insert(0, Step(text="Log in", inferred=True, inferred_reason="r", inferred_source="facts.md"))
        t = casegen.to_textile(p, "Rewards")
        self.assertIn("h3. G1", t)
        self.assertIn("_Source: description:", t)
        self.assertNotIn("Log in", t)

    def test_post_needs_approval_and_happens_once(self):
        calls = []

        class RM:
            def create_issue(self, project, subject, body, parent_id=None, relates_to=None):
                calls.append((project, subject, parent_id))
                return 777

        denied = from_pack(self.pack, "staging", approver=lambda *a: False)
        with self.assertRaises(PolicyViolation):
            casegen.post_cases(self.st, RM(), denied, 1, "Rewards")
        self.assertEqual(calls, [])
        ok = from_pack(self.pack, "staging", approver=lambda *a: True)
        self.assertEqual(casegen.post_cases(self.st, RM(), ok, 1, "Rewards"), 777)
        self.assertEqual(calls, [(1, "Test Cases - Rewards", 500)])
        self.assertEqual(casegen.post_cases(self.st, RM(), ok, 1, "Rewards"), 777)
        self.assertEqual(len(calls), 1)

    def test_bug_draft_from_generated_case_is_labelled(self):
        self.st.set_result(CaseResult(case_id="G1", verdict="FAIL", expected="credited", actual="not credited",
                                      note="Generated case: app looks wrong"))
        out = bug_drafts.build_drafts(self.st, self.pack)
        self.assertTrue(out["drafts"][0]["generated_case"])
        self.assertIn("wrote from the task description", bug_drafts.draft_text(out["drafts"][0]))

    def test_report_says_generated(self):
        from agent_tester.report.builder import build_report
        self.st.set_result(CaseResult(case_id="G1", verdict="PASS"))
        html = build_report(self.st, self.pack, pdf=False).read_text()
        self.assertIn("generated", html)
        self.assertIn("The task had no test cases", html)
        self.assertIn("source: description:", html)


if __name__ == "__main__":
    unittest.main()
