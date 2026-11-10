"""Auto page state, trim, unchanged short-circuit, history prune."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_tester.agents import tester as T
from agent_tester.page_state import (
    UNCHANGED, is_state_changing, page_hash, result_already_has_page, trim_page,
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
    def test_keeps_main_drops_nav(self):
        raw = (
            "Page URL: https://x/\n"
            '- navigation:\n  - link "Home" [ref=e1]\n  - link "Admin" [ref=e2]\n'
            '- main:\n  - heading "Order" [ref=e3]\n  - table [ref=e4]\n'
            '    - row:\n      - cell "A1" [ref=e5]\n'
            '- contentinfo:\n  - text "footer"\n'
        )
        out = trim_page(raw, limit=5000)
        self.assertIn("Order", out)
        self.assertIn("chrome omitted", out.lower())
        self.assertNotIn('link "Admin"', out)

    def test_hash_ignores_refs(self):
        a = page_hash('- button "Save" [ref=e1]')
        b = page_hash('- button "Save" [ref=e99]')
        self.assertEqual(a, b)


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


class PruneLatestOnly(unittest.TestCase):
    def test_keep_one(self):
        class FR:
            def __init__(self, name, response):
                self.name, self.response = name, response
        class Part:
            def __init__(self, fr): self.function_response = fr
        class C:
            def __init__(self, fr): self.parts = [Part(fr)]
        frs = [FR("browser_click", {"t": i}) for i in range(3)]
        contents = [C(f) for f in frs]
        n = T.prune_history(contents, 1)
        self.assertEqual(n, 2)
        self.assertEqual(frs[-1].response, {"t": 2})
        self.assertEqual(frs[0].response, {"result": T.DROPPED})


if __name__ == "__main__":
    unittest.main()
