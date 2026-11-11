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


class StuckKB(unittest.TestCase):
    def test_stuck_line_keeps_tried_and_route(self):
        with tempfile.TemporaryDirectory() as d:
            acts = [
                {"app": "crm", "tool": "browser_click", "args": {"element": "CREATE"}},
                {"app": "crm", "tool": "browser_select_option", "approval": "approved",
                 "args": {"element": "status", "values": ["Ready to Ship"]}},
                {"app": "crm", "tool": "browser_click", "approval": "approved", "args": {"element": "Save"}},
                {"app": "crm", "tool": "explore",
                 "args": {"why": "browser_click was repeated 4 times",
                          "tried": ["select:e554:ready to ship", "click:e114"]}},
            ]
            line = learned.record_stuck(Path(d), "rewards", "crm", "E1",
                                        "kept repeating the same action without progress",
                                        "stuck at Ready to Ship", acts)
            self.assertIsNotNone(line)
            self.assertIn("Stuck (loop)", line)
            self.assertIn("already tried", line)
            self.assertIn("reached", line)
            self.assertTrue(any("Stuck" in x for x in learned.read(Path(d), "rewards", "crm")))


class ConfirmedRoutesSection(unittest.TestCase):
    """## Routes lines from any pack learned.md → verified pack routes (env lines skipped)."""

    SAMPLE = """# What the agent learned

## Routes
- route: login | app: crm | path: Home > Login
- route: Buy & Ship quote | app: portal | path: Buy & Ship > Request a purchase quote | notes: open form only
- route: wallet | app: crm | path: https://crm.example/index.php?module=Users | notes: bad
- route: contact | app: crm | path: Contacts > List | notes: email me@example.com
- route: order A1158 | app: portal | path: Shipping Only > Open A1158
- route: Repair | app: crm | path: Admin > Repair > Quick Repair and Rebuild
- not a route line at all

## parcel_value:portal
- Route: click 'Continue' (legacy shape — ignored by ## Routes parser)
"""

    def test_parse_routes_section_sample_lines(self):
        warnings = []
        routes = learned.parse_routes_section(self.SAMPLE, warn=warnings.append)
        screens = {r["screen"] for r in routes}
        self.assertEqual(screens, {"login", "Buy & Ship quote", "Repair"})
        by_screen = {r["screen"]: r for r in routes}
        self.assertEqual(by_screen["login"]["app"], "crm")
        self.assertEqual(by_screen["login"]["path"], "Home > Login")
        self.assertEqual(by_screen["login"]["source"], "pack")
        self.assertEqual(by_screen["login"]["status"], "verified")
        self.assertEqual(by_screen["Buy & Ship quote"]["notes"], "open form only")
        self.assertEqual(by_screen["Repair"]["path"],
                         "Admin > Repair > Quick Repair and Rebuild")
        # Env-bound lines skipped with warnings (host/url, email, record id).
        blob = " ".join(warnings).lower()
        self.assertIn("url", blob)
        self.assertIn("email", blob)
        self.assertTrue(any("a1158" in w.lower() or "record id" in w.lower() for w in warnings))
        self.assertTrue(any("bad format" in w.lower() for w in warnings))
        for r in routes:
            self.assertNotIn("https://", r["path"])
            self.assertNotIn("@", r.get("notes", ""))
            self.assertNotRegex(r["screen"] + r["path"], r"\bA\d{3,}\b")

    def test_backfill_writes_verified_pack_source(self):
        from agent_tester.discovery_map import DiscoveryMapStore, backfill_from_learned
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "learned.md").write_text(self.SAMPLE)
            store = DiscoveryMapStore(root / "map.db")
            warnings = []
            written = backfill_from_learned(
                root, store, scope="project:demo", env_fingerprint="preprod",
                warn=warnings.append,
            )
            self.assertEqual(len(written), 3)
            for row in written:
                self.assertEqual(row["status"], "verified")
                self.assertEqual(row["source"], "pack")
                self.assertEqual(row["env_fingerprint"], "preprod")
            login = store.get("project:demo", "general", "crm", "login", "preprod")
            self.assertIsNotNone(login)
            self.assertIn("Login", login["route_text"])
            store.close()
