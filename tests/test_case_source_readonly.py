"""read_only must come from the cases-file section heading, not the LLM title.

Smoke: planner renamed C1 to 'Portal and CRM Wallet Parity' and dropped
'Final checks (read-only)', so PREP-wallet-create ran without asking.
"""
from __future__ import annotations

import unittest

from agent_tester.case_source import apply_source_case_markers, source_case_markers
from agent_tester import data_prep as D
from agent_tester.models import PlannedCase, Step, TestPlan

# Shape mirrors packs' real test-cases.md sectioning (## section, ### C1. title).
CASES_MD = """# Savo Rewards test cases

## Setup

### TC0. Quick Repair and Rebuild
Run Admin → Repair → Quick Repair and Rebuild.

## Earn paths

### E1. Earn on delivered order
Place and deliver an order; wallet rises.

## Final checks (read-only)

### C1. Portal and CRM wallet parity
Compare portal wallet Current Rewards to CRM Contact Current Rewards.
Expected: portal and CRM balances match.

### C2. Thresholds visible
Read thresholds only; change nothing.
"""


class CaseSourceReadonly(unittest.TestCase):
    def test_markers_from_section_heading(self):
        m = source_case_markers(CASES_MD)
        self.assertIn("C1", m)
        self.assertEqual(m["C1"]["source_section"], "Final checks (read-only)")
        self.assertTrue(m["C1"]["read_only"])
        self.assertTrue(m["C2"]["read_only"])
        self.assertFalse(m["E1"]["read_only"])
        self.assertEqual(m["E1"]["source_section"], "Earn paths")

    def test_apply_survives_llm_title_rename(self):
        plan = TestPlan(task_id=165177, env="preprod", cases=[
            PlannedCase(id="C1", title="Portal and CRM Wallet Parity", area="rewards",
                        steps=[Step(text="Read portal wallet", app="portal"),
                               Step(text="Read CRM Current Rewards", app="crm")],
                        expected="Balances match."),
            PlannedCase(id="E1", title="Earn on delivered order", area="rewards",
                        steps=[Step(text="Pay with card", app="portal", changes_data=True)]),
        ])
        # Without markers: renamed title has no read-only language.
        self.assertFalse(D.case_is_read_only(plan.cases[0]))
        apply_source_case_markers(plan, CASES_MD)
        self.assertEqual(plan.cases[0].source_section, "Final checks (read-only)")
        self.assertTrue(plan.cases[0].read_only)
        self.assertTrue(D.case_is_read_only(plan.cases[0]))
        self.assertFalse(plan.cases[1].read_only)
        self.assertFalse(D.case_is_read_only(plan.cases[1]))

    def test_case_heading_read_only_without_section(self):
        text = "### C9 (read-only). Snapshot values\nRead only.\n"
        m = source_case_markers(text)
        self.assertTrue(m["C9"]["read_only"])


if __name__ == "__main__":
    unittest.main()
