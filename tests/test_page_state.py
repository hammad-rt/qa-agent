"""Auto page state, trim, unchanged short-circuit, history prune."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_tester.agents import tester as T
from agent_tester.page_state import (
    UNCHANGED, is_state_changing, page_hash, result_already_has_page,
    reveal_hint, revealed_choice_items, trim_page,
)
from agent_tester.preflight.packs import load_pack
from agent_tester.run_state import RunState
from agent_tester.tools.policy import from_pack
from agent_tester.tools.screenshots import Evidence

PACK = Path(__file__).resolve().parents[1] / "packs" / "savostore"


def _snap(n=20):
    lines = ["Page URL: https://example/crm", "Page Title: Contacts"]
    lines += [f'- link "Item {i}" [ref=e{i}]' for i in range(n)]
    lines += ['- main:', '  - heading "Detail"', '  - textbox "Email" [ref=e99]: user@x.com']
    return "\n".join(lines)


class TrimPage(unittest.TestCase):
    def test_under_cap_keeps_nav_and_main(self):
        raw = (
            "Page URL: https://example.test/\n"
            '- navigation:\n  - link "Home" [ref=e1]\n  - link "Admin" [ref=e2]\n'
            '- main:\n  - heading "Order" [ref=e3]\n  - table [ref=e4]\n'
            '    - row:\n      - cell "A1" [ref=e5]\n'
            '- contentinfo:\n  - text "footer secret"\n'
        )
        out = trim_page(raw, limit=5000)
        self.assertIn("Order", out)
        self.assertIn('link "Admin"', out)
        self.assertNotIn("chrome omitted", out.lower())

    def test_over_cap_keeps_nav_may_drop_footer(self):
        pad = "\n".join(['  - listitem "Row %d" [ref=e%d]' % (i, 100 + i) for i in range(200)])
        raw = (
            "Page URL: https://example.test/\n"
            '- navigation:\n  - link "Home" [ref=e1]\n  - link "Admin" [ref=e2]\n'
            '- main:\n  - heading "Order" [ref=e3]\n' + pad + "\n"
            '- contentinfo:\n  - text "footer secret"\n'
        )
        out = trim_page(raw, limit=400)
        self.assertIn("Admin", out)
        self.assertIn("Order", out)
        self.assertNotIn("footer secret", out)

    def test_hash_ignores_refs(self):
        a = page_hash('- button "Save" [ref=e1]')
        b = page_hash('- button "Save" [ref=e99]')
        self.assertEqual(a, b)


    def test_keeps_expanded_menu_items_inside_nav(self):
        raw = (
            "Page URL: https://example.test/app\n"
            "Page Title: Home\n"
            '- navigation:\n'
            '  - menubar:\n'
            '    - menuitem "Home" [ref=e1]\n'
            '    - menuitem "Orders" [expanded] [ref=f1e11]:\n'
            '      - menu:\n'
            '        - menuitem "Shipping Only Orders" [ref=f1e20]\n'
            '        - menuitem "Buy and Ship Orders" [ref=f1e21]\n'
            '    - menuitem "Contacts" [ref=e3]\n'
            '- main:\n'
            '  - heading "Dashboard" [ref=e4]\n'
            '  - table [ref=e5]\n'
            '    - row:\n'
            '      - cell "A1" [ref=e6]\n'
        )
        out = trim_page(raw, limit=5000)
        self.assertIn("Dashboard", out)
        self.assertIn("Shipping Only Orders", out)
        self.assertIn("f1e20", out)
        self.assertIn("Buy and Ship Orders", out)
        # Under the cap the full navigation (including siblings) is kept.
        self.assertIn('menuitem "Contacts"', out)

    def test_keeps_sibling_open_menu_role(self):
        raw = (
            "Page URL: https://example.test/app\n"
            "Page Title: Home\n"
            '- navigation:\n'
            '  - menubar:\n'
            '    - menuitem "Orders" [expanded] [ref=f1e11]\n'
            '- menu:\n'
            '  - menuitem "Export" [ref=f1e20]\n'
            '  - menuitem "Print" [ref=f1e21]\n'
            '- main:\n'
            '  - heading "Dashboard" [ref=e4]\n'
        )
        out = trim_page(raw, limit=5000)
        self.assertIn("Export", out)
        self.assertIn("Print", out)
        self.assertIn("f1e20", out)

    def test_hash_changes_when_menu_opens(self):
        closed = (
            "Page URL: https://example.test/app\n"
            "Page Title: Home\n"
            '- navigation:\n'
            '  - menubar:\n'
            '    - menuitem "Orders" [ref=f1e11]\n'
            '- main:\n'
            '  - heading "Dashboard" [ref=e4]\n'
        )
        opened = (
            "Page URL: https://example.test/app\n"
            "Page Title: Home\n"
            '- navigation:\n'
            '  - menubar:\n'
            '    - menuitem "Orders" [expanded] [ref=f1e11]:\n'
            '      - menu:\n'
            '        - menuitem "Export" [ref=f1e20]\n'
            '        - menuitem "Print" [ref=f1e21]\n'
            '- main:\n'
            '  - heading "Dashboard" [ref=e4]\n'
        )
        self.assertNotEqual(page_hash(trim_page(closed)), page_hash(trim_page(opened)))
        hint = reveal_hint(closed, opened)
        self.assertIsNotNone(hint)
        self.assertIn("the menu opened; pick one of:", hint)
        self.assertIn("Export", hint)
        self.assertIn("Print", hint)
        self.assertEqual(revealed_choice_items(closed), [])
        self.assertEqual(revealed_choice_items(opened), ["Export", "Print"])

    def test_revealed_items_survive_length_cap(self):
        main = "\n".join([f'  - listitem "Row {i}" [ref=e{100+i}]' for i in range(400)])
        raw = (
            "Page URL: https://example.test/app\n"
            "Page Title: Home\n"
            '- navigation:\n'
            '  - menuitem "Orders" [expanded] [ref=f1e11]:\n'
            '    - menu:\n'
            '      - menuitem "Export" [ref=f1e20]\n'
            '- main:\n'
            '  - heading "Dashboard" [ref=e4]\n'
            + main
        )
        closed = (
            "Page URL: https://example.test/app\n"
            "Page Title: Home\n"
            '- navigation:\n'
            '  - menuitem "Orders" [ref=f1e11]\n'
            '- main:\n'
            '  - heading "Dashboard" [ref=e4]\n'
        )
        out = trim_page(raw, limit=800, before=closed)
        self.assertIn("Export", out)
        self.assertIn("f1e20", out)

    def test_already_open_user_menu_is_not_hoisted_over_main(self):
        """An already-expanded user menu at the end must not be hoisted over main content."""
        menu = "\n".join([f'  - menuitem "Recent {i}" [ref=u{i}]' for i in range(80)])
        raw = (
            "Page URL: https://example.test/admin\n"
            "Page Title: Administration\n"
            "- main:\n"
            '  - heading "Administration" [ref=e4]\n'
            '  - link "Repair" [ref=f2e267]\n'
            '- button "Administrator" [expanded] [ref=e33]:\n'
            + menu + "\n"
        )
        # Already open: do not hoist the huge menu in front of Repair.
        out = trim_page(raw, limit=600, before=raw)
        self.assertIn("f2e267", out)
        self.assertIn('link "Repair"', out)
        self.assertLess(out.find("Repair"), out.find("Recent 0") if "Recent 0" in out else len(out))
        closed = (
            "Page URL: https://example.test/admin\n"
            "Page Title: Administration\n"
            "- main:\n"
            '  - heading "Administration" [ref=e4]\n'
            '  - link "Repair" [ref=f2e267]\n'
            '- button "Administrator" [ref=e33]\n'
        )
        # A huge newly opened menu is hoisted first so the cap cannot cut it.
        huge = trim_page(raw, limit=600, before=closed)
        self.assertIn("Recent 0", huge)
        self.assertLess(huge.find("Recent 0"), huge.find("Repair") if "Repair" in huge else len(huge))
        # Over the cap, a modest newly opened menu is hoisted in front of Repair.
        modest_menu = "\n".join([f'  - menuitem "Recent {i}" [ref=u{i}]' for i in range(8)])
        pad = "\n".join([f'  - listitem "Pad {i}" [ref=p{i}]' for i in range(40)])
        modest = (
            "Page URL: https://example.test/admin\n"
            "Page Title: Administration\n"
            "- main:\n"
            '  - heading "Administration" [ref=e4]\n'
            '  - link "Repair" [ref=f2e267]\n'
            + pad + "\n"
            '- button "Administrator" [expanded] [ref=e33]:\n'
            + modest_menu + "\n"
        )
        self.assertGreater(len(modest), 600)
        hoisted = trim_page(modest, limit=600, before=closed)
        self.assertIn("Repair", hoisted)
        self.assertIn("Recent 0", hoisted)
        self.assertLess(hoisted.find("Recent 0"), hoisted.find("Repair"))

    def test_reveal_hint_ignores_already_open_menu_and_empty_before(self):
        opened = (
            "Page URL: https://example.test/app\n"
            '- menuitem "Orders" [expanded] [ref=e1]:\n'
            '  - menu:\n'
            '    - menuitem "Export" [ref=e2]\n'
        )
        self.assertIsNone(reveal_hint("", opened))
        self.assertIsNone(reveal_hint(opened, opened))



class AutoPageState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = RunState(Path(self.tmp.name) / "run")
        pack = load_pack(PACK)
        self.policy = from_pack(pack, "staging", auto=True, approver=lambda *a: True)
        brief = {"case_id": "TC1", "requirements": {}, "steps": [{"n": 1, "text": "look"}]}
        self.s = T.TesterSession("crm", brief, self.state, self.policy, Evidence(self.state.dir),
                                 max_calls=40, max_minutes=5)
        self.s.max_calls = 100

    def tearDown(self):
        self.tmp.cleanup()

    def t(self, name):
        return SimpleNamespace(name=name)

    def test_unchanged_snapshot_short_circuits_without_loop(self):
        self.s._page_hash = "abc"
        self.s._page_fresh = True
        self.s._page_state = "cached"
        before_calls = self.s.calls
        r = self.s.before_tool(self.t("browser_snapshot"), {}, None)
        self.assertIn("unchanged", r["result"].lower())
        self.assertEqual(self.s.calls, before_calls)  # not counted
        self.assertEqual(getattr(self.s, "_reads", 0), 0)

    def test_click_result_with_page_marks_fresh(self):
        page = _snap()
        out = self.s.after_tool(self.t("browser_click"), {"element": "Save"}, None, {"text": page})
        self.assertTrue(self.s._page_fresh)
        self.assertTrue(self.s._page_hash)
        self.assertIn("page state", str(out).lower())

    def test_record_value_ok_after_auto_page(self):
        self.s.before_tool(self.t("browser_click"), {"element": "x", "target": "e1"}, None)
        self.s.after_tool(self.t("browser_click"), {"element": "x"}, None, {"text": _snap()})
        # last_act set, but _page_fresh means record_value is allowed
        r = self.s.before_tool(self.t("record_value"), {}, None)
        self.assertIsNone(r)

    def test_state_changing_detect(self):
        self.assertTrue(is_state_changing("browser_click"))
        self.assertTrue(is_state_changing("browser_navigate"))
        self.assertFalse(is_state_changing("browser_snapshot"))
        self.assertTrue(result_already_has_page(_snap()))
        # A short tree (few refs) after a click is still a page.
        short = (
            "Page URL: https://example.test/app\n"
            "Page Title: Home\n"
            '- menuitem "Orders" [expanded] [ref=f1e11]\n'
            '- menu:\n'
            '  - menuitem "Export" [ref=f1e20]\n'
        )
        self.assertTrue(result_already_has_page(short))
        self.assertFalse(result_already_has_page("clicked"))


def _page(n):
    return (
        f"Page URL: https://example.test/p{n}\n"
        f"Page Title: T{n}\n"
        f'- main:\n  - heading "H{n}" [ref=e{n}]\n'
    )


class PruneLatestOnly(unittest.TestCase):
    def test_default_keep_is_two(self):
        self.assertEqual(T.KEEP_PAGE_READS, 2)

    def test_keep_one(self):
        class FR:
            def __init__(self, name, response):
                self.name, self.response = name, response
        class Part:
            def __init__(self, fr): self.function_response = fr
        class C:
            def __init__(self, fr): self.parts = [Part(fr)]
        frs = [FR("browser_click", {"result": _page(i)}) for i in range(3)]
        contents = [C(f) for f in frs]
        n = T.prune_history(contents, 1)
        self.assertEqual(n, 2)
        self.assertIn("example.test/p2", str(frs[-1].response))
        self.assertEqual(frs[0].response, {"result": T.DROPPED})

    def test_does_not_stub_non_page_browser_results(self):
        class FR:
            def __init__(self, name, response):
                self.name, self.response = name, response
        class Part:
            def __init__(self, fr): self.function_response = fr
        class C:
            def __init__(self, fr): self.parts = [Part(fr)]
        tabs = FR("browser_tabs", {"result": "2 tabs open"})
        old = FR("browser_snapshot", {"result": _page(1)})
        new = FR("browser_snapshot", {"result": _page(2)})
        contents = [C(tabs), C(old), C(new)]
        n = T.prune_history(contents, 1)
        self.assertEqual(n, 1)
        self.assertEqual(tabs.response, {"result": "2 tabs open"})
        self.assertEqual(old.response, {"result": T.DROPPED})
        self.assertIn("example.test/p2", str(new.response))


if __name__ == "__main__":
    unittest.main()
