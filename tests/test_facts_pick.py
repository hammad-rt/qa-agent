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

    def test_shipment_protection_case_gets_mandatory_threshold_rule(self):
        out = relevant(
            FACTS.read_text(),
            "TC2 Shipment Value read-only shipment protection mandatory threshold "
            "declared value Admin Shipment Protection",
        )
        self.assertIn("Shipment Protection behaviour", out)
        self.assertIn("Admin > Shipment Protection", out)
        self.assertIn("Mandatory Shipment Protection Threshold (USD)", out)
        self.assertIn("above that threshold", out)
        self.assertIn("At or below the threshold", out)
        self.assertIn("do not assume an amount", out)
        # The rule lives in the area-selected Shipment Protection section (not only
        # the always-on Business rules block), so a lean shipment-protection case
        # still sees it under the default facts budget.
        section = next(body for head, body in sections(FACTS.read_text())
                       if head.startswith("Shipment Protection behaviour"))
        self.assertIn("Admin > Shipment Protection", section)
        self.assertIn(section, out)
        self.assertLessEqual(len(out), 9000)


if __name__ == "__main__":
    unittest.main()
