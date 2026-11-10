"""DOM/structure: tabs + guidance stay outside conditionally hidden containers."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "agent_tester/web/index.html"


class WebDomShell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML.read_text()

    def test_tabs_and_guide_in_main_not_inside_pane(self):
        # Strip script so string searches stay on markup.
        markup = re.split(r"<script[\s>]", self.html, maxsplit=1)[0]
        self.assertIn('id="guideRow"', markup)
        self.assertIn('role="tablist"', markup)
        self.assertIn('data-p="live"', markup)
        self.assertIn('data-p="know"', markup)
        self.assertIn('data-p="mem"', markup)
        # Tabs and guide must appear before any pane.on / pane blocks as siblings under main.
        main = re.search(r"<main>([\s\S]*)</main>", markup)
        self.assertIsNotNone(main)
        body = main.group(1)
        guide_i = body.find('id="guideRow"')
        tabs_i = body.find('role="tablist"')
        live_i = body.find('id="live"')
        self.assertGreater(guide_i, 0)
        self.assertGreater(tabs_i, guide_i)
        self.assertGreater(live_i, tabs_i)
        # Not nested inside a pane
        before_live = body[:live_i]
        self.assertNotIn('class="pane', before_live.split('id="guideRow"')[0][-80:])
        self.assertIn('id="headed"', markup)
        self.assertIn('id="nowDoing"', markup)
        self.assertIn("100dvh", self.html)
        self.assertIn("min-height:0", self.html.replace(" ", ""))

    def test_css_shell_overflow_rules(self):
        css = self.html.split("</style>", 1)[0]
        self.assertIn("overflow:hidden", css)
        self.assertIn("100dvh", css)
        self.assertIn("prompt-pin", css)
        self.assertIn("max-height:min(30vh", css.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
