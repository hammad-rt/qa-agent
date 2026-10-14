import asyncio, tempfile, unittest
from pathlib import Path
from agent_tester import gen_tools
from agent_tester.helpers import _load_module

JS = "async (page, args) => { const el = page.locator('aria-ref=' + args.target); await el.press('Control+A'); return JSON.stringify({ok: true}); }"


class GenToolsTest(unittest.TestCase):
    def test_good_script_passes_and_loads(self):
        self.assertEqual(gen_tools.check("select_all_field", JS, set()), [])
        src = gen_tools.render("select_all_field", "select all text", {"target": "ref"}, JS, False, None, "TC1")
        p = gen_tools.save(tempfile.mkdtemp(), "select_all_field", src, True)
        mod = _load_module(p, "t")
        self.assertEqual(mod.HELPER["name"], "select_all_field")
        self.assertFalse(mod.HELPER["changes_data"])
        class B:
            async def _do(self, tool, args):
                self.args = (tool, args); return '{"ok":true}'
        b = B()
        out = asyncio.run(mod.run(b, None, target="e15"))
        self.assertEqual(b.args[0], "browser_run_code_unsafe")
        self.assertIn('"target": "e15"', b.args[1]["code"])
        self.assertIn("ok", out["notes"])

    def test_bad_scripts_rejected(self):
        for bad in ["async (page, args) => { return document.cookie }",
                    "async (page, args) => { await page.goto('https://evil') }",
                    "async (page, args) => { await fetch('/x') }",
                    "async (page, args) => { page.locator('#password').fill('x') }",
                    "async (page, args) => { click('Logout') }",
                    "() => 1"]:
            self.assertTrue(gen_tools.check("some_tool", bad, set()), bad)
        self.assertTrue(gen_tools.check("login_form", JS, {"login_form"}))
        self.assertTrue(gen_tools.check("Bad-Name", JS, set()))

    def test_unapproved_goes_to_proposed(self):
        d = tempfile.mkdtemp()
        p = gen_tools.save(d, "x_tool", "x", False)
        self.assertEqual(p.parent, Path(d) / "helpers" / "proposed")
