"""Discovery map reuse, deploy-marker spot-check, mid-stop flush, and admin deny-list."""
from __future__ import annotations

from agent_tester import testing_hermetic as hermetic  # noqa: F401

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_tester.coordinator import (
    ensure_discovery, discovery_checklist, inject_discovery, mark_discovery_stale,
)
from agent_tester.discovery_guard import (
    admin_action_env_allowed, case_allows_admin_action, discovery_blocks_click,
    discovery_blocks_tool, filter_discovery_checklist, is_admin_action_label,
)
from agent_tester.discovery_map import (
    DiscoveryMapStore, compute_deploy_marker, extract_asset_urls, has_change_signal,
    is_reusable, item_key, labels_from_route, parse_routes_from_discovery_notes,
    spot_check_route, flush_pending,
)
from agent_tester.memory.store import MemoryStore
from agent_tester.models import CaseType, PlannedCase, Requirement, Step, StepResult, TestPlan
from agent_tester.run_state import RunState
from agent_tester.agents.tester import TesterSession
from agent_tester.tools.policy import Policy
from agent_tester.tools.screenshots import Evidence


class FakeMemory:
    def __init__(self, store: MemoryStore, slug: str = "demo"):
        self.store = store
        self.slug = slug
        self.scopes = [f"project:{slug}", "core"]
        self.proposed = []

    def propose(self, kind, text, area=None, evidence=None, applies_when=None, linked_issue=None):
        mid = self.store.add(kind, self.scopes[0], text, "agent", area=area, status="proposed")
        self.proposed.append((kind, text, area))
        return mid

    def set_platforms(self, platforms):
        pass

    def preload(self, areas=None, char_cap=6000):
        return []

    def recall(self, query, k=5, **kw):
        return []


def _plan_case(area="orders", app="portal"):
    case = PlannedCase(
        id="TC2", title="Create portal order", area=area, type=CaseType.UI,
        steps=[Step(text="open orders", app=app), Step(text="create order", app=app)],
        expected="created")
    plan = TestPlan(task_id=1, env="staging", cases=[case], requirements=[
        Requirement(key="env.staging.portal", kind="url", value="https://portal.example:1/"),
        Requirement(key="env.staging.crm", kind="url", value="https://crm.example:2/"),
    ])
    return plan, case


class DeployMarker(unittest.TestCase):
    def test_hashes_cache_busted_assets_and_version(self):
        html = (
            '<script src="/static/app.a1b2c3d4.js"></script>'
            '<link rel="stylesheet" href="/static/app.css?v=99">'
            '<meta name="generator" content="SuiteCRM 7.14.2">'
            '<footer>Version 7.14.2</footer>'
        )
        m1 = compute_deploy_marker(url="https://crm.example/", html=html,
                                   platform_version="7.14.2", fetch=False)
        m2 = compute_deploy_marker(url="https://crm.example/", html=html.replace("a1b2c3d4", "ffff1111"),
                                   platform_version="7.14.2", fetch=False)
        self.assertTrue(m1)
        self.assertNotEqual(m1, m2)
        self.assertTrue(extract_asset_urls(html))

    def test_change_signal_near_item_words(self):
        text = "The Orders screen was redesigned; the New Order button moved under Sales."
        self.assertTrue(has_change_signal(text, "create order"))
        self.assertFalse(has_change_signal("No UI changes this sprint.", "create order"))


class DiscoveryMapStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = MemoryStore(Path(self.tmp.name) / "m.db")
        self.ms = DiscoveryMapStore(self.db.db)
        self.scope = "project:demo"

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_upsert_and_verify(self):
        row = self.ms.upsert_route(
            scope=self.scope, area="orders", app="portal",
            checklist_item="create order", env_fingerprint="env=staging|portal=p",
            route_text="click 'Orders' -> click 'New Order'",
            key_labels=["Orders", "New Order"], deploy_marker="abc",
            status="proposed",
        )
        self.assertEqual(row["status"], "proposed")
        self.assertEqual(row["success_count"], 0)
        v = self.ms.mark_verified(row["id"], deploy_marker="abc")
        self.assertEqual(v["status"], "verified")
        self.assertGreaterEqual(v["success_count"], 1)
        self.assertTrue(v["last_success_at"])

    def test_refuses_env_bound_route(self):
        with self.assertRaises(Exception):
            self.ms.upsert_route(
                scope=self.scope, area="orders", app="portal",
                checklist_item="create order", env_fingerprint="fp",
                route_text="open https://portal.example/orders/A1158 for client@x.com",
            )

    def test_reuse_rules_and_spot_check(self):
        row = self.ms.upsert_route(
            scope=self.scope, area="orders", app="portal",
            checklist_item="create order", env_fingerprint="fp",
            route_text="click 'Orders' -> click 'New'",
            key_labels=["Orders", "New"], deploy_marker="old",
            status="proposed",
        )
        self.assertEqual(is_reusable(row, current_marker="old"), "reuse")
        self.assertEqual(is_reusable(row, current_marker="new"), "spot_check")
        self.ms.mark_verified(row["id"], deploy_marker="old")
        row = self.ms.get_by_id(row["id"])
        self.assertEqual(is_reusable(row, current_marker="old"), "reuse")
        self.ms.mark_stale(row["id"], "boom")
        row = self.ms.get_by_id(row["id"])
        self.assertEqual(is_reusable(row, current_marker="old"), "discover")

    def test_verified_without_success_date_is_not_reusable_forever(self):
        """Audit LOW: missing last_success_at/verified_at → discover (keep 14-day window)."""
        row = {
            "status": "verified", "success_count": 1, "fail_count": 0,
            "route_text": "click 'Orders' -> click 'New'",
            "key_labels": ["Orders", "New"], "deploy_marker": "m",
            "checklist_item": "create order",
            "last_success_at": None, "verified_at": None,
        }
        self.assertEqual(is_reusable(row, current_marker="m"), "discover")
        # Stale beyond DEFAULT_REUSE_MAX_DAYS also rediscovers.
        row["last_success_at"] = "2020-01-01T00:00:00+00:00"
        self.assertEqual(is_reusable(row, current_marker="m"), "discover")

    def test_spot_check_missing_label(self):
        ok, missing, _ = spot_check_route(
            route_text="click 'Orders'", key_labels=["Orders", "New Order"],
            snapshot_text='button "Orders"',
        )
        self.assertFalse(ok)
        self.assertIn("New Order", missing)
        ok2, missing2, _ = spot_check_route(
            route_text="click 'Orders'", key_labels=["Orders"],
            snapshot_text='button "Orders"',
        )
        self.assertTrue(ok2)
        self.assertEqual(missing2, [])


class EnsureDiscoveryReuse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = RunState(Path(self.tmp.name) / "run")
        self.state.data["_skip_deploy_fetch"] = True
        self.state.data["deploy_markers"] = {"portal": "marker1"}
        self.db = MemoryStore(Path(self.tmp.name) / "m.db")
        self.mem = FakeMemory(self.db)
        self.ms = DiscoveryMapStore(self.db.db)
        self.plan, self.case = _plan_case()
        self.fp = "env=staging|portal=portal.example:1"

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _seed(self, item="create order", marker="marker1", status="verified", success=1):
        row = self.ms.upsert_route(
            scope="project:demo", area="orders", app="portal",
            checklist_item=item, env_fingerprint=self.fp,
            route_text="click 'Orders' -> click 'New Order'",
            key_labels=["Orders", "New Order"], deploy_marker=marker,
            status="proposed",
        )
        if status == "verified":
            for _ in range(success):
                self.ms.mark_verified(row["id"], deploy_marker=marker)
        return self.ms.get_by_id(row["id"])

    def test_reuses_verified_map_no_llm(self):
        self._seed()
        seen = []

        def tester(app, brief, state):
            seen.append(brief)
            return StepResult(case_id=brief["case_id"], app=app, notes="should not run")

        entry = ensure_discovery(self.plan, self.case, "portal", self.state,
                                 {"portal": tester}, self.mem, checklist=["create order"])
        self.assertEqual(seen, [])
        self.assertEqual(entry["source"], "memory")
        self.assertTrue(any("Orders" in r for r in entry.get("recipes") or []))
        log = (self.state.dir / "progress.log").read_text()
        self.assertIn("reused from memory", log)

    def test_marker_change_spot_check_ok_reuses(self):
        self._seed(marker="old")
        self.state.data["deploy_markers"] = {"portal": "new"}
        self.state.data["_spot_check_snap"] = {"portal": 'heading "Orders" button "New Order"'}
        seen = []

        def tester(app, brief, state):
            seen.append(1)
            return StepResult(case_id=brief["case_id"], app=app, notes="live")

        entry = ensure_discovery(self.plan, self.case, "portal", self.state,
                                 {"portal": tester}, self.mem, checklist=["create order"])
        self.assertEqual(seen, [])
        self.assertEqual(entry["source"], "memory")
        log = (self.state.dir / "progress.log").read_text()
        self.assertIn("deploy changed, spot-checked", log)

    def test_marker_change_missing_label_rediscovers(self):
        self._seed(marker="old")
        self.state.data["deploy_markers"] = {"portal": "new"}
        self.state.data["_spot_check_snap"] = {"portal": 'heading "Home"'}  # missing Orders/New
        seen = []

        def tester(app, brief, state):
            seen.append(list(brief.get("discovery_checklist") or []))
            return StepResult(case_id=brief["case_id"], app=app,
                              notes="Menu: click 'Orders' then click 'New Order'")

        entry = ensure_discovery(self.plan, self.case, "portal", self.state,
                                 {"portal": tester}, self.mem, checklist=["create order"])
        self.assertEqual(len(seen), 1)
        # First discovery for this run key after a failed spot-check (no prior cache) is a full pass.
        self.assertIn("discovery", entry["source"])

    def test_change_signal_forces_live(self):
        self._seed()
        self.case.steps[0].text = "The create order button was renamed and moved; open the new layout"
        seen = []

        def tester(app, brief, state):
            seen.append(1)
            return StepResult(case_id=brief["case_id"], app=app, notes="mapped click 'Sales' -> click 'Orders'")

        ensure_discovery(self.plan, self.case, "portal", self.state,
                         {"portal": tester}, self.mem, checklist=["create order"])
        self.assertEqual(len(seen), 1)

    def test_rediscover_flag_forces_live(self):
        self._seed()
        self.state.data["rediscover"] = True
        seen = []

        def tester(app, brief, state):
            seen.append(1)
            return StepResult(case_id=brief["case_id"], app=app, notes="fresh map click 'Orders'")

        ensure_discovery(self.plan, self.case, "portal", self.state,
                         {"portal": tester}, self.mem, checklist=["create order"])
        self.assertEqual(len(seen), 1)

    def test_mid_discovery_stop_saves_covered_next_run_incremental(self):
        """Stop mid-discovery → covered routes saved; next run reuses them and discovers only the rest."""
        seen = []

        def tester(app, brief, state):
            seen.append(list(brief.get("discovery_checklist") or []))
            # Only map the first checklist item, then "stop" (partial notes).
            return StepResult(
                case_id=brief["case_id"], app=app,
                notes="create order: click 'Orders' -> click 'New Order'\n(stopped before payment)",
            )

        ensure_discovery(self.plan, self.case, "portal", self.state,
                         {"portal": tester}, self.mem,
                         checklist=["create order", "record payment"])
        self.assertEqual(len(seen), 1)
        rows = self.ms.list_for("project:demo", "orders", "portal", self.fp)
        self.assertTrue(any("create order" in (r.get("checklist_item") or "") for r in rows))
        # Simulate stop flush.
        flush_pending(self.state, self.mem)

        # New run state, same memory DB.
        state2 = RunState(Path(self.tmp.name) / "run2")
        state2.data["_skip_deploy_fetch"] = True
        state2.data["deploy_markers"] = {"portal": "marker1"}
        seen2 = []

        def tester2(app, brief, state):
            seen2.append(list(brief.get("discovery_checklist") or []))
            return StepResult(case_id=brief["case_id"], app=app,
                              notes="record payment: click 'Payments' -> click 'Add'")

        entry = ensure_discovery(self.plan, self.case, "portal", state2,
                                 {"portal": tester2}, self.mem,
                                 checklist=["create order", "record payment"])
        self.assertEqual(len(seen2), 1)
        # Live pass only for the missing item.
        self.assertTrue(all("payment" in x.lower() or "record" in x.lower() for x in seen2[0]))
        self.assertNotIn("create order", [x.lower() for x in seen2[0]])
        self.assertIn("inc", entry.get("source", ""))

    def test_inject_stays_within_budget(self):
        recipes = [f"click 'Menu{i}' -> click 'Item{i}' " + ("x" * 200) for i in range(20)]
        brief = inject_discovery({"case_id": "TC2"}, {"recipes": recipes, "notes": "n" * 5000})
        from agent_tester.memory.retrieve import DEFAULT_TOKEN_BUDGET, tokens_of
        used = sum(tokens_of(r) for r in brief["recipes"]) + tokens_of(brief.get("discovery_notes") or "")
        self.assertLessEqual(used, DEFAULT_TOKEN_BUDGET)


class DiscoveryAdminDeny(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = RunState(Path(self.tmp.name) / "run")
        pack = SimpleNamespace(config={})
        self.policy = Policy(allow_hosts=["x"], risky_patterns=[], preapprovable=["admin_repair", "execute_repair_sql"],
                             always_ask=["repair_sql_destructive"], env="staging",
                             approver=lambda *a, **k: True)
        self.ev = Evidence(self.state.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def _sess(self, *, discovery=False, env="staging", steps=None, title=""):
        brief = {
            "case_id": "TC0" if not discovery else "DISCO-admin",
            "area": "admin", "title": title or "Run Quick Repair",
            "env": env,
            "steps": steps or [{"n": 1, "text": "Click Quick Repair and Rebuild", "app": "crm",
                                "action_type": "admin_repair"}],
            "discovery": discovery, "read_only": discovery,
        }
        s = TesterSession("crm", brief, self.state, self.policy, self.ev, max_calls=20, max_minutes=5)
        s.env = env
        s.current_step = 1
        return s

    def t(self, name):
        return SimpleNamespace(name=name)

    def test_discovery_blocked_from_clicking_repair(self):
        s = self._sess(discovery=True, steps=[{"n": 1, "text": "map admin", "app": "crm"}])
        out = s.before_tool(self.t("browser_click"),
                            {"element": "Quick Repair and Rebuild", "ref": "e1"}, None)
        self.assertIn("error", out)
        self.assertIn("read-only", out["error"].lower())

    def test_discovery_blocked_request_approval_and_check_sql(self):
        s = self._sess(discovery=True, steps=[{"n": 1, "text": "map", "app": "crm"}])
        tools = {f.__name__: f for f in s.tools() if hasattr(f, "__name__")}
        # request_approval / check_repair_sql are FunctionTools — call via before path helpers.
        from agent_tester.discovery_guard import discovery_blocks_tool
        self.assertIsNotNone(discovery_blocks_tool(
            "request_approval", {"action_type": "admin_repair", "description": "Run QRR"}))
        self.assertIsNotNone(discovery_blocks_tool("check_repair_sql", {}))
        # Session-level: invoke the wrapped tools if present.
        ra = next((x for x in s.tools() if getattr(x, "__name__", "") == "request_approval"
                   or getattr(getattr(x, "func", None), "__name__", "") == "request_approval"), None)
        # Fallback: call discovery_blocks via request_approval logic directly.
        why = discovery_blocks_click("Quick Repair and Rebuild")
        self.assertIsNotNone(why)

    def test_case_asked_repair_goes_through_approval(self):
        asked = []
        self.policy.approver = lambda a, r, d: asked.append(a) or True
        self.policy.auto = False
        s = self._sess(discovery=False, env="staging",
                       steps=[{"n": 1, "text": "Click Quick Repair and Rebuild", "app": "crm",
                               "action_type": "admin_repair"}],
                       title="Run Quick Repair and Rebuild")
        # Click is allowed (case asks); gate will approve via browser_gate / request_approval.
        out = s.before_tool(self.t("browser_click"),
                            {"element": "Quick Repair and Rebuild", "ref": "e1"}, None)
        self.assertIsNone(out)  # not denied
        self.assertTrue(case_allows_admin_action(s.brief, action_type="admin_repair",
                                                 label="Quick Repair and Rebuild"))

    def test_preprod_refusal(self):
        self.assertFalse(admin_action_env_allowed("preprod", {}, "admin_repair"))
        self.assertFalse(admin_action_env_allowed("prod", {}, "execute_repair_sql"))
        self.assertTrue(admin_action_env_allowed("staging", {}, "admin_repair"))
        s = self._sess(discovery=False, env="preprod",
                       steps=[{"n": 1, "text": "Click Quick Repair and Rebuild", "app": "crm",
                               "action_type": "admin_repair"}],
                       title="Run Quick Repair and Rebuild")
        self.state.data["pack_cfg"] = {"admin_repair_envs": ["staging", "local"]}
        out = s.before_tool(self.t("browser_click"),
                            {"element": "Quick Repair and Rebuild", "ref": "e1"}, None)
        self.assertIn("error", out)
        self.assertRegex(out["error"].lower(), r"preprod|not allowed")

    def test_checklist_does_not_seed_admin_unless_case_names_it(self):
        plan, case = _plan_case(area="orders", app="portal")
        items = discovery_checklist(plan, "portal", self.state, area="orders")
        self.assertFalse(any(is_admin_action_label(x) for x in items))
        # Soft filter rewrites admin-ish lines to locate-only.
        soft = filter_discovery_checklist(["run Quick Repair and Rebuild", "create order"])
        self.assertTrue(any("do not run" in x.lower() for x in soft))
        self.assertTrue(any("create order" in x.lower() for x in soft))


class ParseRoutes(unittest.TestCase):
    def test_parse_notes(self):
        notes = "create order: click 'Orders' -> click 'New Order'\nother noise"
        got = parse_routes_from_discovery_notes(notes, ["create order", "record payment"])
        self.assertIn("create order", got)
        self.assertNotIn("record payment", got)
        self.assertTrue(labels_from_route(got["create order"][0]))


if __name__ == "__main__":
    unittest.main()
