import unittest
from pathlib import Path

from agent_tester.facts_pick import relevant, sections

FACTS = Path(__file__).resolve().parents[1] / "packs/savostore/facts.md"


class FactsPick(unittest.TestCase):
    def test_earn_case_gets_rewards_rules(self):
        f = FACTS.read_text()
        out = relevant(f, "E1 earn rewards Order Delivered wallet Savo Rewards Current Rewards Total Rewards")
        self.assertIn("FULLY paid", out)
        self.assertIn("## Business rules", out)
        self.assertLessEqual(len(out), 9000)

    def test_unrelated_case_skips_rewards_detail(self):
        out = relevant(FACTS.read_text(), "Shipment Protection threshold parcel value")
        self.assertIn("Shipment Protection behaviour", out)

    def test_sections_and_limit(self):
        f = "# T\n\n## Business rules\n- a\n\n## Other zebra\n" + "zebra " * 3000
        self.assertEqual([h for h, _ in sections(f)], ["Business rules", "Other zebra"])
        out = relevant(f, "zebra zebra", limit=500)
        self.assertIn("Business rules", out)
        self.assertNotIn("Other zebra", out)

    def test_empty(self):
        self.assertEqual(relevant("", "x"), "")


if __name__ == "__main__":
    unittest.main()
