"""The login helper must click the real submit button, not a password show/hide toggle."""
import json
import unittest

from agent_tester.helpers.core.login_form import ANALYZE_JS

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

PAGES = {
    '<form><input type=email name=u><input type=password id=p><button type=button aria-label="Show password">o</button>'
    '<button type=submit>Log in</button></form>': "Log in",
    '<form><input name=u><input type=password id=p><button class="toggle-eye">Show password</button>'
    '<button>Sign in</button></form>': "Sign in",
    '<form><input name=u><input type=password id=p><button type=button>Show password</button></form>': None,
    '<form><input name=user_name><input type=password name=pw><input type=submit value="Log In"></form>': "Log In",
}


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class SubmitPick(unittest.TestCase):
    def test_skips_password_toggles(self):
        try:
            with sync_playwright() as p:
                b = p.chromium.launch()
                pg = b.new_page()
                for html, want in PAGES.items():
                    pg.set_content(html)
                    got = (json.loads(pg.evaluate(ANALYZE_JS)).get("submit") or {}).get("label")
                    self.assertEqual(got, want, html)
                b.close()
        except Exception as e:  # no browser binary on this machine
            if "Executable doesn't exist" in str(e):
                self.skipTest("no chromium")
            raise
