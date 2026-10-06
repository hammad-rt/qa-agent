"""Phase 6: helper registry, MCP browser adapter, core helpers and pack helpers against a fake Playwright MCP."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_tester.config import ROOT
from agent_tester.helpers import (HelperContext, McpBrowser, candidates, load_helpers, parse_snapshot,
                                  read_labelled_values, run_helper)
from agent_tester.helpers.core.read_table import parse_tables
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack

PACK = ROOT / "packs" / "savostore"

LOGIN = '''- generic [ref=e1]:
  - textbox "Username" [ref=e5]
  - textbox "Password" [ref=e6]
  - button "Log In" [ref=e7]'''
HOME = '''- banner [ref=e1]:
  - link "Admin" [ref=e2]
  - heading "Home" [ref=e3]'''
TABLE = '''- table "Orders" [ref=e10]:
  - row "Order Status Total" [ref=e11]:
    - columnheader "Order" [ref=e12]
    - columnheader "Status" [ref=e13]
    - columnheader "Total" [ref=e14]
  - row "A1158 Paid $1,000.00" [ref=e15]:
    - cell "A1158" [ref=e16]
    - cell "Paid" [ref=e17]
    - cell "$1,000.00" [ref=e18]
  - row "A1159 Draft $40.00" [ref=e19]:
    - cell "A1159" [ref=e20]
    - cell "Draft" [ref=e21]
    - cell "$40.00" [ref=e22]'''
WALLET = '''- generic [ref=e1]:
  - text: Current Rewards $597.02
  - text: Total Rewards
  - text: $1,111.00'''


class FakeMcp:
    """Pages change on click; records every call and its args."""
    def __init__(self, pages):
        self.pages, self.page, self.calls = pages, 0, []

    async def __call__(self, tool, args):
        self.calls.append((tool, dict(args)))
        if tool == "browser_snapshot":
            return self.pages[min(self.page, len(self.pages) - 1)]
        if tool == "browser_click":
            self.page += 1
        return "ok"


def ctx(app="crm", state=None, policy=None, pack=None):
    return HelperContext(pack=pack or load_pack(PACK), env="staging", app=app, case_id="TC1", state=state, policy=policy)


class Registry(unittest.TestCase):
    def test_core_ready_pack_helpers_off_until_configured(self):
        hs = load_helpers(load_pack(PACK))
        self.assertTrue(hs["login_form"].enabled and hs["read_table"].enabled)
        for n in ("read_wallet", "portal_apply_rewards", "crm_move_order_status", "crm_add_parcel",
                  "crm_ship_parcel", "portal_create_order"):
            self.assertFalse(hs[n].enabled, n)
            self.assertIn("pack.yaml", hs[n].why_disabled)
        self.assertTrue(hs["portal_apply_rewards"].meta["changes_data"])

    def test_configured_pack_helper_turns_on(self):
        pack = load_pack(PACK)
        pack.config["helpers"]["read_wallet"] = {"path": "rewards", "labels": ["Current Rewards"]}
        self.assertTrue(load_helpers(pack)["read_wallet"].enabled)


class Browser(unittest.TestCase):
    def test_parse_snapshot_and_find(self):
        els = parse_snapshot(LOGIN)
        self.assertEqual([(e.role, e.name, e.ref) for e in els][1:], [("textbox", "Username", "e5"),
                                                                     ("textbox", "Password", "e6"), ("button", "Log In", "e7")])
        b = McpBrowser(FakeMcp([LOGIN]))
        self.assertEqual(asyncio.run(b.find(["Login", "Log in"], ("button",))).ref, "e7")   # case-insensitive, partial

    def test_tables_and_labelled_values(self):
        t = parse_tables(TABLE)[0]
        self.assertEqual(t["header"], ["Order", "Status", "Total"])
        self.assertEqual(t["rows"][1], ["A1159", "Draft", "$40.00"])
        self.assertEqual(read_labelled_values(WALLET, ["Current Rewards", "Total Rewards"]),
                         {"Current Rewards": "$597.02", "Total Rewards": "$1,111.00"})


class LoginHelper(unittest.TestCase):
    def test_password_typed_by_code_never_returned_or_logged(self):
        fake = FakeMcp([LOGIN, HOME])
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"SAVOSTORE_CRM_PASSWORD": "s3cret-pw"}):
            st = RunState(Path(d) / "r")
            pack = load_pack(PACK)
            info = load_helpers(pack)["login_form"]
            b = McpBrowser(fake)
            out = asyncio.run(run_helper(info, b, ctx(state=st, policy=from_pack(pack, "staging"), pack=pack), {"account": "admin"}))
            self.assertEqual(out["values"], {"logged_in": "yes", "role_ok": "yes"})
            self.assertNotIn("s3cret-pw", str(out) + str(b.calls) + (st.dir / "state.json").read_text())
            typed = [a for t, a in fake.calls if t == "browser_type"]
            self.assertEqual(typed[1]["text"], "s3cret-pw")                 # the browser did get it
            self.assertEqual(fake.calls[0], ("browser_navigate", {"url": "https://staging5.rolustech.com:4220/"}))
            self.assertEqual(st.data["action_log"][-1]["tool"], "helper:login_form")

    def test_missing_password_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {"SAVOSTORE_CRM_PASSWORD": ""}):
            info = load_helpers(load_pack(PACK))["login_form"]
            out = asyncio.run(run_helper(info, McpBrowser(FakeMcp([LOGIN])), ctx(), {"account": "admin"}))
        self.assertIn("SAVOSTORE_CRM_PASSWORD", out["error"])


class Safety(unittest.TestCase):
    def _apply(self, approver, read_only=False):
        pack = load_pack(PACK)
        pack.config["helpers"]["portal_apply_rewards"] = {"path": "orders/{order}", "fields": {"amount": "Rewards amount"},
                                                          "submit_label": "Apply", "read_labels": ["up to"]}
        info = load_helpers(pack)["portal_apply_rewards"]
        page = '- textbox "Rewards amount" [ref=e1]\n- button "Apply" [ref=e2]'
        fake = FakeMcp([page, "- text: You can use up to $25.00"])
        policy = from_pack(pack, "staging", approver=approver)
        policy.read_only = read_only
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "r")
            out = asyncio.run(run_helper(info, McpBrowser(fake), ctx("portal", st, policy, pack), {"order": "A1158", "amount": 15}))
            return out, fake, st.data["action_log"]

    def test_data_changing_helper_asks_first(self):
        asked = []
        out, fake, log = self._apply(lambda a, r, d: asked.append(a) or True)
        self.assertEqual(len(asked), 1)
        self.assertEqual(out["values"]["up to"], "$25.00")
        self.assertEqual(fake.calls[0][1]["url"], "https://staging5.rolustech.com:4225/orders/A1158")
        self.assertEqual(log[-1]["approval"], "approved")

    def test_denied_or_read_only_does_nothing(self):
        for kw in ({"approver": lambda *a: False}, {"approver": lambda *a: True, "read_only": True}):
            out, fake, log = self._apply(**kw)
            self.assertIn("Blocked", out["error"])
            self.assertEqual(fake.calls, [])
            self.assertEqual(log[-1]["approval"], "denied")

    def test_helper_cannot_leave_allowlist(self):
        pack = load_pack(PACK)
        info = load_helpers(pack)["read_table"]
        out = asyncio.run(run_helper(info, McpBrowser(FakeMcp([TABLE])), ctx(policy=from_pack(pack, "staging"), pack=pack),
                                     {"path": "https://evil.example.com/x"}))
        self.assertIn("error", out)

    def test_read_only_helper_cannot_press_risky_buttons(self):
        from agent_tester.helpers import HelperError, _guard
        g = _guard(from_pack(load_pack(PACK), "staging"), may_change=False)
        with self.assertRaises(HelperError):
            g("browser_click", {"element": "Pay now"})
        g("browser_click", {"element": "Log In"})                          # fine

    def test_unknown_params_rejected(self):
        info = load_helpers(load_pack(PACK))["read_table"]
        out = asyncio.run(run_helper(info, McpBrowser(FakeMcp([TABLE])), ctx(), {"bogus": 1}))
        self.assertIn("unknown parameter", out["error"])


class Candidates(unittest.TestCase):
    def test_recipes_that_kept_working(self):
        from agent_tester.memory.store import MemoryStore
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "m.db", None)
            ok = store.add("recipe", "project:savostore", "Apply rewards: open order, type amount, click Apply", "test", area="rewards", status="active")
            store.add("recipe", "project:savostore", "Rarely used", "test", status="active")
            for _ in range(3):
                store.bump(ok, True)
            rows = candidates(store, "savostore")
            store.close()
        self.assertEqual([r["id"] for r in rows], [ok])


if __name__ == "__main__":
    unittest.main()
