"""Route↔checklist matching (pack + core aliases) and memory-pollution cleanup."""
from __future__ import annotations

from agent_tester import testing_hermetic as hermetic  # noqa: F401

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from agent_tester.coordinator import ensure_discovery
from agent_tester.discovery_map import (
    DiscoveryMapStore, backfill_from_learned, load_route_synonym_groups,
    match_item_to_rows, route_hints_from_rows, route_match_score,
)
from agent_tester.learned import add as learned_add, parse_routes_section, record_stuck
from agent_tester.memory.pollution import (
    clean_polluted_memory, format_clean_report, is_ephemeral_learned_line,
    is_infra_error_text, is_product_bug_failure, is_stuck_loop_trail,
    is_tester_or_agent_attribution,
)
from agent_tester.memory.retrieve import learned_hints_for
from agent_tester.memory.store import MemoryStore, dump_json
from agent_tester.models import CaseType, PlannedCase, Requirement, Step, StepResult, TestPlan
from agent_tester.run_state import RunState

REPO = Path(__file__).resolve().parents[1]
SAVO_PACK = REPO / "packs" / "savostore"

# Quoted ## Routes lines from the live pack (committed learned.md may omit this
# section). Paths are menu labels only — no hosts, emails, or record ids.
LIVE_ROUTES_MD = """# What the agent learned

## Routes
- route: Invoices | app: crm | path: Orders > Shipping Only Orders
- route: Quotes | app: crm | path: Orders > Buy and Ship Orders
- route: Order status to Order Delivered | app: crm | path: Orders > Shipping Only Orders > Detail > Status
- route: Contact wallet | app: crm | path: Contacts > Wallet
- route: Portal rewards wallet | app: portal | path: Account > Rewards
- route: Receive package | app: crm | path: Orders > Shipping Only Orders > Receive
- route: Find client by email | app: crm | path: Contacts > Search
- route: Buy & Ship create | app: portal | path: Buy & Ship > Request a purchase quote
- route: CRM login | app: crm | path: Home > Login
- route: Portal login | app: portal | path: Home > Login

## parcel_value:portal
- Route: click 'Continue' (legacy area:app line)
"""

# Checklist names observed as "discovered live" when matching failed.
CRM_LIVE_ITEMS = [
    "login",
    "move status",
    "open wallet / rewards",
    "Receive package",
    "Find client by email",
]
PORTAL_LIVE_ITEMS = [
    "login",
    "buy & ship / quote",
]

CRM_EXPECTED = {
    "login": "CRM login",
    "move status": "Order status to Order Delivered",
    "open wallet / rewards": "Contact wallet",
    "Receive package": "Receive package",
    "Find client by email": "Find client by email",
}
PORTAL_EXPECTED = {
    "login": "Portal login",
    "buy & ship / quote": "Buy & Ship create",
}


class FakeMemory:
    def __init__(self, store: MemoryStore, slug: str = "demo"):
        self.store = store
        self.slug = slug
        self.scopes = [f"project:{slug}", "core"]

    def propose(self, kind, text, area=None, evidence=None, applies_when=None, linked_issue=None):
        return self.store.add(kind, self.scopes[0], text, "agent", area=area, status="proposed")

    def set_platforms(self, platforms):
        pass

    def preload(self, areas=None, char_cap=6000):
        return []

    def recall(self, query, k=5, **kw):
        return []


def _savo_groups():
    cfg = yaml.safe_load((SAVO_PACK / "pack.yaml").read_text()) or {}
    return load_route_synonym_groups(cfg, pack_root=SAVO_PACK)


class SavostoreRouteMatch(unittest.TestCase):
    """Load the quoted savostore ## Routes against live-run checklist names."""

    def setUp(self):
        self.groups = _savo_groups()
        self.routes = parse_routes_section(LIVE_ROUTES_MD)
        self.assertGreaterEqual(len(self.routes), 10)
        self.crm_rows = [
            {"checklist_item": r["screen"], "item_key": f"c{i}", "route_text": r["path"]}
            for i, r in enumerate(self.routes) if r["app"] == "crm"
        ]
        self.portal_rows = [
            {"checklist_item": r["screen"], "item_key": f"p{i}", "route_text": r["path"]}
            for i, r in enumerate(self.routes) if r["app"] == "portal"
        ]

    def test_crm_checklist_matches_quoted_routes(self):
        for item, want in CRM_EXPECTED.items():
            hit = match_item_to_rows(self.crm_rows, item, synonym_groups=self.groups)
            self.assertIsNotNone(hit, f"no CRM row for {item!r}")
            self.assertEqual(hit["checklist_item"], want, f"{item!r} matched {hit['checklist_item']!r}")

    def test_portal_checklist_matches_quoted_routes(self):
        for item, want in PORTAL_EXPECTED.items():
            hit = match_item_to_rows(self.portal_rows, item, synonym_groups=self.groups)
            self.assertIsNotNone(hit, f"no portal row for {item!r}")
            self.assertEqual(hit["checklist_item"], want, f"{item!r} matched {hit['checklist_item']!r}")

    def test_core_aliases_match_without_pack_table(self):
        """App-agnostic core aliases are enough for login / move status / wallet."""
        groups = load_route_synonym_groups({})  # core only
        pairs = [
            ("CRM login", "login"),
            ("Order status to Order Delivered", "move status"),
            ("Contact wallet", "open wallet / rewards"),
        ]
        for screen, item in pairs:
            sc = route_match_score(screen, item, groups)
            self.assertGreaterEqual(sc, 0.5, f"{screen!r} vs {item!r} score={sc}")

    def test_stripped_pack_cfg_falls_back_to_pack_yaml(self):
        groups = load_route_synonym_groups(
            {"placeholders": {}, "data_prep": []},  # live-run trimmed pack_cfg
            pack_root=SAVO_PACK,
        )
        self.assertTrue(any("move status" in g for g in groups))
        hit = match_item_to_rows(self.crm_rows, "move status", synonym_groups=groups)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["checklist_item"], "Order status to Order Delivered")

    def test_backfill_then_match_from_temp_learned(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # Committed file may have no ## Routes; the quoted section is what the laptop has.
            (root / "learned.md").write_text(LIVE_ROUTES_MD)
            store = DiscoveryMapStore(root / "map.db")
            written = backfill_from_learned(
                root, store, scope="project:demo", env_fingerprint="preprod",
            )
            self.assertGreaterEqual(len(written), 10)
            rows = store.list_for("project:demo", "general", "crm", "preprod")
            hit = match_item_to_rows(rows, "move status", synonym_groups=self.groups)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["checklist_item"], "Order status to Order Delivered")
            self.assertIn("Shipping Only Orders", hit["route_text"])
            store.close()

    def test_invoices_path_is_offered_as_a_hint(self):
        hints = route_hints_from_rows(self.crm_rows)
        blob = "\n".join(hints)
        self.assertIn("Orders > Shipping Only Orders", blob)
        self.assertIn("Invoices", blob)


class DiscoveryPathHints(unittest.TestCase):
    """Live discovery receives saved menu paths even when the item is not reused."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = RunState(Path(self.tmp.name) / "run")
        self.state.data["_skip_deploy_fetch"] = True
        self.db = MemoryStore(Path(self.tmp.name) / "m.db")
        self.mem = FakeMemory(self.db)
        self.ms = DiscoveryMapStore(self.db.db)
        self.case = PlannedCase(
            id="TC1", title="wallet", area="rewards", type=CaseType.UI,
            steps=[Step(text="open wallet", app="crm")],
        )
        self.plan = TestPlan(task_id=1, env="staging", cases=[self.case], requirements=[
            Requirement(key="env.staging.crm", kind="url", value="https://crm.example:2/"),
        ])
        self.fp = "env=staging|crm=crm.example:2"

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_unmatched_invoices_path_is_in_live_brief(self):
        # A pack route whose screen name does not match the checklist still
        # must be given as a path hint so discovery does not click 'Orders' blindly.
        self.ms.upsert_route(
            scope="project:demo", area="general", app="crm",
            checklist_item="Invoices", env_fingerprint=self.fp,
            route_text="Orders > Shipping Only Orders",
            key_labels=["Orders", "Shipping Only Orders"],
            status="verified", source="pack", last_success_at=None, success_count=0,
        )
        seen = []

        def tester(app, brief, state):
            seen.append(brief)
            return StepResult(case_id=brief["case_id"], app=app, notes="mapped")

        ensure_discovery(
            self.plan, self.case, "crm", self.state, {"crm": tester}, self.mem,
            checklist=["move status"],
        )
        self.assertEqual(len(seen), 1)
        blob = " ".join(seen[0].get("recipes") or []) + " " + " ".join(seen[0].get("discovery_hints") or [])
        self.assertIn("Orders > Shipping Only Orders", blob)


class PollutionFilters(unittest.TestCase):
    def test_infra_and_stuck_and_tester_patterns(self):
        self.assertTrue(is_infra_error_text(
            "Discovery: discovery failed: LLMOutputError: LLM call hit the 180s per-call timeout"))
        self.assertTrue(is_stuck_loop_trail(
            "Stuck (loop): kept repeating the same action without progress. "
            "reached click 'Administrator menu' -> click 'Admin link' -> click 'Repair'. "
            "already tried: click:e31"))
        self.assertTrue(is_tester_or_agent_attribution(
            "The agent used `browser_type` instead of pasting"))
        self.assertTrue(is_tester_or_agent_attribution(
            "The tester typed 007 then typed 0 without clearing (0070)"))
        self.assertTrue(is_ephemeral_learned_line(
            "Discovery: discovery failed: LLMOutputError: LLM call hit the 180s per-call timeout"))
        self.assertFalse(is_infra_error_text("Threshold save was rejected and the old value returned"))
        self.assertTrue(is_product_bug_failure(SimpleNamespace(
            verdict="FAIL", known_bug=None, note="value not saved",
            expected="200", actual="kept 12.5")))
        self.assertFalse(is_product_bug_failure(SimpleNamespace(
            verdict="FAIL", known_bug=None,
            note="test not executed properly",
            expected="paste", actual="The agent used `browser_type` instead of pasting")))

    def test_learned_add_and_hints_drop_infra_and_trails(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "learned.md").write_text("# What the agent learned\n")
            self.assertFalse(learned_add(
                root, "admin", "crm",
                "Discovery: discovery failed: LLMOutputError: LLM call hit the 180s per-call timeout (2026-11-11)",
            ))
            self.assertIsNone(record_stuck(
                root, "admin", "crm", "TC0",
                "kept repeating the same action without progress",
                "stuck in admin",
                [{"app": "crm", "tool": "browser_click", "args": {"element": "Administrator menu"}, "outcome": "ok"}],
            ))
            # Pre-existing polluted lines must not be injected.
            (root / "learned.md").write_text(
                "# What the agent learned\n\n## admin:crm\n"
                "- Stuck (loop): kept repeating. reached click 'Administrator menu' -> click 'Repair'. "
                "already tried: click:e1 (2026-11-05, TC0)\n"
                "- Discovery: discovery failed: LLMOutputError: LLM call hit the 180s per-call timeout (2026-11-11)\n"
                "- Route: click 'Orders' -> click 'Shipping Only Orders' (2026-11-05, TC1 reached)\n"
            )
            hints = learned_hints_for(root, "admin", "crm", "")
            blob = " ".join(hints)
            self.assertIn("Shipping Only Orders", blob)
            self.assertNotIn("LLMOutputError", blob)
            self.assertNotIn("Administrator menu", blob)

    def test_cleanup_dry_run_and_apply_are_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "memory.db"
            export = Path(d) / "memory-export.json"
            store = MemoryStore(db)
            bad_bug = store.add(
                "open_bug_found", "project:demo",
                "TC2: The agent used `browser_type` instead of pasting",
                "run_end",
            )
            bad_recipe = store.add(
                "recipe", "project:demo",
                "Discovery: discovery failed: LLMOutputError: LLM call hit the 180s per-call timeout",
                "run_end",
            )
            good = store.add(
                "fact", "project:demo",
                "Shipment Value fields are read-only after a repair and reload",
                "user",
            )
            dump_json(store, export)
            # dump_json should already omit polluted rows; seed a dirty export too.
            dirty = json.loads(export.read_text())
            dirty.append({
                "id": 99, "kind": "recipe", "scope": "project:demo",
                "text": "Stuck (loop): kept repeating. reached click 'Admin' -> click 'Repair'. already tried: click:e1",
                "status": "active", "source": "run_end",
            })
            export.write_text(json.dumps(dirty, indent=2))

            preview = clean_polluted_memory(store, export_path=export, dry_run=True)
            self.assertTrue(preview["dry_run"])
            self.assertGreaterEqual(len(preview["db_retired"]), 2)
            ids = {e["id"] for e in preview["db_retired"]}
            self.assertIn(bad_bug, ids)
            self.assertIn(bad_recipe, ids)
            self.assertNotIn(good, ids)
            self.assertTrue(preview["export_removed"])
            # Dry-run must not change anything.
            self.assertEqual(store.get(bad_bug)["status"], "active")
            self.assertTrue(any(r.get("id") == 99 for r in json.loads(export.read_text())))
            self.assertIn("Would remove", format_clean_report(preview))

            applied = clean_polluted_memory(store, export_path=export, dry_run=False)
            self.assertEqual(store.get(bad_bug)["status"], "retired")
            self.assertEqual(store.get(bad_recipe)["status"], "retired")
            self.assertEqual(store.get(good)["status"], "active")
            kept_ids = {r.get("id") for r in json.loads(export.read_text())}
            self.assertNotIn(99, kept_ids)
            self.assertIn(good, kept_ids)

            again = clean_polluted_memory(store, export_path=export, dry_run=False)
            self.assertEqual(again["db_retired"], [])
            self.assertEqual(again["export_removed"], [])
            store.close()


if __name__ == "__main__":
    unittest.main()


class ProductCancelNotInfra(unittest.TestCase):
    def test_order_cancelled_is_not_infra(self):
        from agent_tester.memory.pollution import is_infra_error_text
        self.assertFalse(is_infra_error_text("The order was cancelled and the wallet refunded"))
        self.assertTrue(is_infra_error_text("Root node tester_crm was cancelled by an external cancellation"))
