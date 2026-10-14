import tempfile, unittest
from pathlib import Path
from agent_tester import learned


class Learned(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_route_from_actions_scrubs_ids_and_stops_at_change(self):
        acts = [{"app": "portal", "tool": "browser_navigate", "args": {"url": "https://x.com:4225/orders/1174?record=99"}},
                {"app": "portal", "tool": "browser_click", "args": {"element": "Link to order A1174", "target": "e2"}},
                {"app": "portal", "tool": "browser_click", "args": {"element": "Edit Order Details"}},
                {"app": "crm", "tool": "browser_click", "args": {"element": "Other app"}},
                {"app": "portal", "tool": "browser_click", "args": {"element": "Save"}, "approval": "approved"},
                {"app": "portal", "tool": "browser_click", "args": {"element": "After"}}]
        hops = learned.route_from_actions(acts, "portal")
        self.assertEqual(hops, ["open /orders/<id>?record=<id>", "click 'Link to order <a record>'",
                                "click 'Edit Order Details'"])

    def test_add_read_dedupe(self):
        self.assertTrue(learned.add(self.root, "parcel_value", "portal", "Route: a -> b (2026-10-14, TC1 reached)"))
        self.assertFalse(learned.add(self.root, "parcel_value", "portal", "Route: a -> b (2026-10-15, TC3 reached)"))
        self.assertEqual(len(learned.read(self.root, "parcel_value", "portal")), 1)
        self.assertEqual(learned.read(self.root, "parcel_value", "crm"), [])
        # a differently named area still matches by words
        self.assertEqual(len(learned.read(self.root, "parcel_value_edit", "portal")), 1)

    def test_seeded_pack_file_parses(self):
        root = Path(__file__).resolve().parents[1] / "packs" / "savostore"
        self.assertTrue(learned.read(root, "parcel_value", "portal"))
        self.assertTrue(any("XSRF" in x for x in learned.read(root, "shipment_protection_threshold", "crm")))
