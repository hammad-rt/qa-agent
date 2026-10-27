import json, tempfile, unittest
from pathlib import Path

from agent_tester.modes import draft_from


def _run(root, name, status, task=5):
    d = Path(root) / name; d.mkdir()
    plan = {"task_id": task, "env": "staging", "cases": [{"id": "TC1", "title": "t", "expected": "e"}]}
    (d / "state.json").write_text(json.dumps({"status": status, "plan": plan}))
    return d


class DraftFrom(unittest.TestCase):
    def test_picks_the_chosen_draft(self):
        with tempfile.TemporaryDirectory() as root:
            _run(root, "5-20261027-100000", "dry_run")
            _run(root, "5-20261027-110000", "done")     # a later run does not hide the draft
            plan, src, _ = draft_from(root, "5-20261027-100000", 5)
            self.assertEqual(plan.cases[0].id, "TC1"); self.assertEqual(src.name, "5-20261027-100000")

    def test_rejects_non_draft_other_task_and_paths(self):
        with tempfile.TemporaryDirectory() as root:
            _run(root, "5-20261027-110000", "done")
            _run(root, "6-20261027-100000", "dry_run", task=6)
            self.assertIsNone(draft_from(root, "5-20261027-110000", 5)[0])
            self.assertIsNone(draft_from(root, "6-20261027-100000", 5)[0])
            self.assertIsNone(draft_from(root, "../etc", 5)[0])


if __name__ == "__main__":
    unittest.main()
