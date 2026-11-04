import tempfile, unittest
from pathlib import Path

from agent_tester import casegen
from agent_tester.main import read_cases_file, with_cases_file
from agent_tester.preflight.redmine import TaskBundle


class CasesFile(unittest.TestCase):
    def test_file_stands_in_for_test_cases_task(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "test-cases.md"
            f.write_text("## TC1 Earn points\nSteps: place an order\nExpected: points added\n")
            text = read_cases_file(f)
            b = TaskBundle(issue={"id": 165177, "subject": "Savo Rewards", "project": {"id": 7}}, test_case_task=None)
            self.assertTrue(casegen.needs_generation(b))
            b2 = with_cases_file(b, text, f.name)
            self.assertFalse(casegen.needs_generation(b2))
            self.assertIn("Earn points", b2.all_text())
            self.assertIsNone(b.test_case_task)        # the original bundle is untouched

    def test_rejects_wrong_type_missing_or_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(read_cases_file(Path(d) / "nope.md"))
            (Path(d) / "x.pdf").write_text("a")
            self.assertIsNone(read_cases_file(Path(d) / "x.pdf"))
            (Path(d) / "e.md").write_text("  \n")
            self.assertIsNone(read_cases_file(Path(d) / "e.md"))


if __name__ == "__main__":
    unittest.main()
