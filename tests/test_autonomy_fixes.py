"""Autonomy plan: staging change_settings preapprove, action_type browser gate, tool budget, discovery-first."""
from __future__ import annotations

from agent_tester import testing_hermetic as hermetic  # noqa: F401
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_tester.agents.tester import TesterSession, tool_budget, _is_xsrf_page, _url_nav_key
from agent_tester.config import ROOT
from agent_tester.coordinator import (
    ensure_discovery, ensure_case_discovery, inject_discovery, recipes_covering, run_plan,
    discovery_key, env_fingerprint, mark_discovery_stale, apps_needing_discovery,
)
from agent_tester.models import Requirement
from agent_tester.models import CaseResult, CaseType, PlannedCase, Step, StepResult, TestPlan
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import Policy, from_pack
from agent_tester.tools.screenshots import Evidence

PACK = ROOT / "packs" / "savostore"


class ToolBudget(unittest.TestCase):
    def test_four_steps_get_at_least_eighty(self):
        self.assertGreaterEqual(tool_budget(4, floor=40), 80)
        self.assertEqual(tool_budget(4, floor=40), 80)

    def test_floor_and_cap(self):
        self.assertEqual(tool_budget(1, floor=40), 40)
        self.assertEqual(tool_budget(10, floor=40, cap=120), 120)


class ActionTypeBrowserGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = RunState(Path(self.tmp.name) / "run")
        self.asked = []
        pack = load_pack(PACK)
        self.policy = from_pack(pack, "staging", auto=False,
                                approver=lambda a, r, d: self.asked.append(a) or False)
        brief = {
            "case_id": "TC5",
            "area": "rewards",
            "steps": [{"n": 1, "text": "Save threshold", "app": "crm", "action_type": "change_settings"}],
            "requirements": {},
        }
        self.s = TesterSession("crm", brief, self.state, self.policy, Evidence(self.state.dir),
                               max_calls=20, max_minutes=5)
        self.s.current_step = 1

    def tearDown(self):
        self.tmp.cleanup()

    def t(self, name):
        return SimpleNamespace(name=name)

    def test_click_on_change_settings_step_invokes_gate(self):
        r = self.s.before_tool(self.t("browser_click"), {"element": "Save", "ref": "e1"}, None)
        self.assertIn("Blocked", r["error"])
        self.assertTrue(any("change_settings" in a or "Save" in a for a in self.asked) or self.asked)
        self.assertEqual(self.asked[0], "browser_click: Save")

    def test_preapproved_change_settings_click_is_silent(self):
        self.policy.preapprove(["change_settings"])
        asked = []
        self.policy.approver = lambda a, r, d: asked.append(a) or True
        r = self.s.before_tool(self.t("browser_click"), {"element": "Save", "ref": "e1"}, None)
        self.assertIsNone(r)
        self.assertEqual(asked, [])  # pre_approved, no console prompt
        self.assertIn("change_settings", self.s._action_gated)


class FakeMemory:
    def __init__(self, recipes=None):
        self._recipes = list(recipes or [])
        self.scopes = ["core", "project:savostore"]
        self.store = self
        self.proposed = []

    def preload(self, areas=None, char_cap=6000):
        rows = [{"kind": "recipe", "text": t, "id": i, "area": "rewards"} for i, t in enumerate(self._recipes)]
        if areas:
            return rows  # tests use matching area
        return rows

    def recall(self, query, k=5):
        return self.preload()

    def query(self, scopes, kinds=None, statuses=("active",), areas=None, key=None):
        if "recipe" in (kinds or []):
            return [{"kind": "recipe", "text": t, "id": i} for i, t in enumerate(self._recipes + [p[1] for p in self.proposed])]
        return []

    def propose(self, kind, text, area=None, evidence=None, applies_when=None, linked_issue=None):
        self.proposed.append((kind, text, area))
        return len(self.proposed)


class DiscoveryFirst(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = RunState(Path(self.tmp.name) / "run")
        self.case = PlannedCase(
            id="TC2", title="Create portal order", area="orders", type=CaseType.UI,
            steps=[Step(text="open orders", app="portal"), Step(text="create", app="portal")],
            expected="created")
        self.plan = TestPlan(task_id=1, cases=[self.case])

    def tearDown(self):
        self.tmp.cleanup()

    def test_runs_discovery_with_memory_recipes_as_hints(self):
        """Recipes in memory/learned are hints only — discovery still runs a live pass."""
        mem = FakeMemory(recipes=["when creating an order, go to Orders then New"])
        seen = []

        def tester(app, brief, state):
            seen.append(brief)
            self.assertTrue(brief.get("discovery"))
            self.assertTrue(brief.get("read_only"))
            self.assertIn("Orders then New", (brief.get("recipes") or [""])[0])
            self.assertTrue(brief.get("discovery_hints"))
            return StepResult(case_id=brief["case_id"], app=app, notes="Menu: Orders > New Order (verified)")

        entry = ensure_discovery(self.plan, self.case, "portal", self.state, {"portal": tester}, mem)
        self.assertEqual(entry["source"], "discovery")
        self.assertEqual(len(seen), 1)
        self.assertTrue(any("Orders then New" in h for h in entry["hints"]))
        self.assertIn("verified", entry["notes"])
        brief = inject_discovery({"case_id": "TC2", "steps": []}, entry)
        self.assertTrue(brief["recipes"])

    def test_learned_pack_routes_do_not_skip_discovery(self):
        """Stale Routes/Stuck/Behaviour lines in learned.md must not skip a fresh discovery pass."""
        pack = Path(self.tmp.name) / "pack"
        pack.mkdir()
        (pack / "learned.md").write_text(
            "# What the agent learned\n\n## orders:portal\n"
            "- Stuck (loop): old dead end\n"
            "- Behaviour: something old\n"
            "- Route: Home > Orders (2020-01-01)\n")
        self.state.data["pack_root"] = str(pack)
        mem = FakeMemory(recipes=[])
        seen = []

        def tester(app, brief, state):
            seen.append(brief.get("case_id"))
            self.assertTrue(brief.get("discovery_hints"))
            return StepResult(case_id=brief["case_id"], app=app, notes="live map: Orders list")

        entry = ensure_discovery(self.plan, self.case, "portal", self.state, {"portal": tester}, mem)
        self.assertEqual(entry["source"], "discovery")
        self.assertEqual(seen, [f"DISCO-{self.case.area}-portal"])
        self.assertTrue(any("Stuck" in h or "Route" in h or "Behaviour" in h for h in entry["hints"]))

    def test_runs_discovery_once_when_memory_empty(self):
        mem = FakeMemory(recipes=[])
        seen = []

        def tester(app, brief, state):
            seen.append((brief.get("case_id"), brief.get("discovery"), brief.get("max_tool_calls")))
            self.assertTrue(brief.get("discovery"))
            self.assertTrue(brief.get("read_only"))
            return StepResult(case_id=brief["case_id"], app=app, notes="Menu: Orders > New Order")

        entry = ensure_discovery(self.plan, self.case, "portal", self.state, {"portal": tester}, mem)
        self.assertEqual(entry["source"], "discovery")
        self.assertEqual(len(seen), 1)
        self.assertGreaterEqual(seen[0][2], 30)
        self.assertLessEqual(seen[0][2], 60)
        self.assertIn("Orders", entry["notes"])
        self.assertTrue(entry.get("checklist"))
        # second call uses cache when checklist covered
        entry2 = ensure_discovery(self.plan, self.case, "portal", self.state, {"portal": tester}, mem)
        self.assertEqual(len(seen), 1)
        self.assertEqual(entry2, entry)
        self.assertTrue((self.state.dir / "discovery" / "orders.md").is_file())

    def test_reruns_discovery_when_env_host_changes(self):
        mem = FakeMemory(recipes=[])
        seen = []

        def tester(app, brief, state):
            seen.append(brief.get("case_id"))
            return StepResult(case_id=brief["case_id"], app=app, notes="mapped path A")

        self.plan.requirements = [
            Requirement(key="env.staging.portal", kind="url", value="https://old.example:4225/")]
        ensure_discovery(self.plan, self.case, "portal", self.state, {"portal": tester}, mem)
        self.assertEqual(len(seen), 1)
        self.plan.requirements = [
            Requirement(key="env.staging.portal", kind="url", value="https://new.example:4225/")]
        ensure_discovery(self.plan, self.case, "portal", self.state, {"portal": tester}, mem)
        self.assertEqual(len(seen), 2)

    def test_reruns_discovery_when_recipe_marked_stale(self):
        mem = FakeMemory(recipes=["stale route"])
        seen = []

        def tester(app, brief, state):
            seen.append(1)
            return StepResult(case_id=brief["case_id"], app=app, notes="fresh map")

        ensure_discovery(self.plan, self.case, "portal", self.state, {"portal": tester}, mem)
        self.assertEqual(len(seen), 1)
        mark_discovery_stale(self.state, self.case.area, "portal", "got stuck")
        ensure_discovery(self.plan, self.case, "portal", self.state, {"portal": tester}, mem)
        self.assertEqual(len(seen), 2)

    def test_run_plan_discovers_even_when_memory_has_recipes(self):
        mem = FakeMemory(recipes=["when X, click Y"])
        briefs = []

        def tester(app, brief, state):
            briefs.append(dict(brief))
            if brief.get("discovery"):
                return StepResult(case_id=brief["case_id"], app=app, notes="live: click Y")
            return StepResult(case_id=brief["case_id"], app=app)

        judge = lambda b, a, r, s: CaseResult(case_id=b["case_id"], verdict="PASS")
        run_plan(self.plan, self.state, {"portal": tester}, judge, memory=mem)
        self.assertGreaterEqual(len(briefs), 2)
        self.assertTrue(briefs[0].get("discovery"))
        self.assertIn("click Y", (briefs[0].get("recipes") or [""])[0])
        case_brief = next(b for b in briefs if b["case_id"] == "TC2")
        self.assertFalse(case_brief.get("discovery"))
        self.assertTrue(case_brief.get("recipes"))

    def test_run_plan_discovers_then_runs_case(self):
        mem = FakeMemory(recipes=[])
        briefs = []

        def tester(app, brief, state):
            briefs.append((brief["case_id"], bool(brief.get("discovery")), brief.get("discovery_notes", "")))
            if brief.get("discovery"):
                return StepResult(case_id=brief["case_id"], app=app, notes="path: /orders/new")
            return StepResult(case_id=brief["case_id"], app=app)

        judge = lambda b, a, r, s: CaseResult(case_id=b["case_id"], verdict="PASS")
        run_plan(self.plan, self.state, {"portal": tester}, judge, memory=mem)
        self.assertEqual(briefs[0][0].startswith("DISCO"), True)
        self.assertTrue(briefs[0][1])
        self.assertEqual(briefs[1][0], "TC2")
        self.assertFalse(briefs[1][1])
        self.assertIn("/orders/new", briefs[1][2])

    def test_discovery_runs_before_mutating_prep(self):
        """Self-discovery must complete before ensure_data create/edit turns."""
        mem = FakeMemory(recipes=["old recipe"])
        order = []
        rule = {"id": "o", "value": "test_order", "app": "portal", "area": "orders",
                "find": "find for {client}", "create": "make for {client}",
                "client_login_app": "portal",
                "when": {"areas": ["orders"], "any": ["create"]}}

        def tester(app, brief, state):
            if brief.get("discovery"):
                order.append("discovery")
                return StepResult(case_id=brief["case_id"], app=app, notes="mapped create order")
            if brief.get("contact_lookup"):
                order.append("contact")
                return StepResult(case_id=brief["case_id"], app=app,
                                  values={"crm_contact_id": "99", "crm_contact_email": "user@example.com"})
            if brief.get("data_prep"):
                order.append("prep:" + ("ro" if brief.get("read_only") else "mutate"))
                return StepResult(case_id=brief["case_id"], app=app,
                                  values={"test_order": "A1", "test_order_record": "r1"})
            order.append("case")
            return StepResult(case_id=brief["case_id"], app=app)

        self.plan.requirements = [
            Requirement(key="account.portal.customer", kind="account", value="user@example.com")]
        judge = lambda b, a, r, s: CaseResult(case_id=b["case_id"], verdict="PASS")
        run_plan(self.plan, self.state, {"portal": tester}, judge, memory=mem, data_prep=[rule])
        self.assertEqual(order[0], "discovery")
        self.assertIn("contact", order)
        self.assertIn("prep:ro", order)
        self.assertLess(order.index("discovery"), order.index("contact"))
        self.assertLess(order.index("contact"), order.index("prep:ro"))
        self.assertLess(order.index("discovery"), order.index("case"))
        self.assertEqual(self.state.data["contact"]["record_id"], "99")

    def test_env_fingerprint_tracks_hosts(self):
        plan = TestPlan(task_id=1, env="staging", cases=[], requirements=[
            Requirement(key="env.staging.portal", kind="url", value="https://a.example:1/"),
            Requirement(key="env.staging.crm", kind="url", value="https://b.example:2/"),
        ])
        self.assertIn("portal=a.example:1", env_fingerprint(plan))
        self.assertIn("crm=b.example:2", env_fingerprint(plan, ["crm"]))
        self.assertNotIn("portal=", env_fingerprint(plan, ["crm"]))

    def test_incremental_discovery_for_missing_checklist(self):
        mem = FakeMemory(recipes=[])
        seen = []

        def tester(app, brief, state):
            seen.append(list(brief.get("discovery_checklist") or []))
            return StepResult(case_id=brief["case_id"], app=app,
                              notes="mapped: " + ", ".join(brief.get("discovery_checklist") or []))

        ensure_discovery(self.plan, self.case, "portal", self.state, {"portal": tester}, mem,
                         checklist=["create order"])
        self.assertEqual(len(seen), 1)
        # Later need not covered by cache → incremental pass
        ensure_discovery(self.plan, self.case, "portal", self.state, {"portal": tester}, mem,
                         checklist=["create order", "record payment", "move status"])
        self.assertEqual(len(seen), 2)
        self.assertTrue(any("payment" in x or "status" in x for x in seen[1]))
        entry = self.state.data["discovery"][discovery_key(self.case.area, "portal")]
        self.assertIn("inc", entry["source"])

class XsrfDeepLinkGuard(unittest.TestCase):
    """Stop SuiteCRM Administration deep-link / XSRF loops (run 180966-…182438)."""

    XSRF_SNIP = (
        'heading "Possible Cross Site Request Forgery (XSRF) Attack Detected"\n'
        'heading "If you think this is a mistake please ask your administrator to add the following '
        'site to the acceptable referer list"'
    )
    ADMIN_URL = "https://staging5.rolustech.com:4220/index.php?module=Administration&action=saveStorageFeeUsd"

    def test_is_xsrf_page_matches_live_wording(self):
        self.assertTrue(_is_xsrf_page(self.XSRF_SNIP))
        self.assertTrue(_is_xsrf_page("CROSS SITE REQUEST FORGERY"))
        self.assertTrue(_is_xsrf_page("add to the acceptable referer list"))
        self.assertFalse(_is_xsrf_page("Shipping Only Storage Fee"))
        self.assertFalse(_is_xsrf_page(""))

    def test_url_nav_key_normalizes_path_query(self):
        a = _url_nav_key(self.ADMIN_URL)
        b = _url_nav_key("index.php?module=Administration&action=saveStorageFeeUsd")
        self.assertEqual(a, b)
        self.assertIn("module=Administration", a)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = RunState(Path(self.tmp.name) / "run")
        pack = load_pack(PACK)
        self.policy = from_pack(pack, "staging", approver=lambda *a, **k: True)
        brief = {"case_id": "TC1", "area": "crm", "steps": [{"n": 1, "text": "open fee", "app": "crm"}],
                 "requirements": {}}
        self.s = TesterSession("crm", brief, self.state, self.policy, Evidence(self.state.dir),
                               max_calls=40, max_minutes=5)

    def tearDown(self):
        self.tmp.cleanup()

    def t(self, name):
        return SimpleNamespace(name=name)

    def test_after_xsrf_denies_same_navigate(self):
        page = f"Page URL: {self.ADMIN_URL}\n" + self.XSRF_SNIP
        # Simulate navigate then XSRF response on snapshot (common after deep-link)
        self.s.before_tool(self.t("browser_navigate"), {"url": self.ADMIN_URL}, None)
        out = self.s.after_tool(self.t("browser_snapshot"), {}, None, {"text": page})
        self.assertIn("error", out)
        self.assertIn("XSRF", out["error"])
        self.assertTrue(self.s._xsrf_urls)

        denied = self.s.before_tool(self.t("browser_navigate"), {"url": self.ADMIN_URL}, None)
        self.assertIn("error", denied)
        self.assertIn("XSRF", denied["error"])

        other = "https://staging5.rolustech.com:4220/index.php?module=Home&action=index"
        ok = self.s.before_tool(self.t("browser_navigate"), {"url": other}, None)
        self.assertIsNone(ok)

    def test_inject_discovery_warns_on_admin_deeplink(self):
        recipe = ("when Admin Threshold Storage is needed, go to Administration menu -> Admin -> "
                  "Shipping Only Storage Fee (URL: index.php?module=Administration&action=saveStorageFeeUsd)")
        brief = inject_discovery({"case_id": "TC1", "steps": []},
                                 {"recipes": [recipe], "notes": "Menu path works"})
        self.assertEqual(brief["recipes"], [recipe])  # recipe text unchanged
        self.assertIn("do not browser_navigate", brief["discovery_notes"])
        self.assertIn("module=Administration&action=", brief["discovery_notes"])

    def test_inject_discovery_no_warn_without_deeplink(self):
        brief = inject_discovery(
            {"case_id": "TC1"},
            {"recipes": ["when fee needed, click Administration then Shipping Only Storage Fee"],
             "notes": "ok"})
        self.assertEqual(brief["discovery_notes"], "ok")


if __name__ == "__main__":
    unittest.main()
