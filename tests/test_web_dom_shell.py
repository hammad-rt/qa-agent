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
        # Classic tabs stay at the top of main (outside panes), always available on a stuck live run.
        for tab in ("cases", "shots", "report", "know", "mem"):
            self.assertIn(f'data-p="{tab}"', markup)
        self.assertRegex(markup, r'data-p="cases"[^>]*>\s*Cases')
        self.assertRegex(markup, r'data-p="shots"[^>]*>\s*Screenshots')
        self.assertRegex(markup, r'data-p="report"[^>]*>\s*Report')
        self.assertRegex(markup, r'data-p="mem"[^>]*>\s*Memory')
        self.assertRegex(markup, r'data-p="know"[^>]*>\s*Knowledge')
        # Classic tabs sit directly under the run title (first chrome in the pane).
        # Guidance stays always visible (outside panes), below phase/now/wait chrome.
        main = re.search(r"<main>([\s\S]*)</main>", markup)
        self.assertIsNotNone(main)
        body = main.group(1)
        head_i = body.find('class="head"')
        tabs_i = body.find('role="tablist"')
        phase_i = body.find('id="phaseBar"')
        guide_i = body.find('id="guideRow"')
        live_i = body.find('id="live"')
        self.assertGreater(head_i, -1)
        self.assertGreater(tabs_i, head_i)
        self.assertGreater(phase_i, tabs_i)
        self.assertGreater(guide_i, tabs_i)
        self.assertGreater(live_i, guide_i)
        # Not nested inside a pane
        before_live = body[:live_i]
        self.assertNotIn('class="pane', before_live.split('id="guideRow"')[0][-80:])
        self.assertIn('id="headed"', markup)
        self.assertIn('id="nowDoing"', markup)
        self.assertIn("100dvh", self.html)
        self.assertIn("min-height:0", self.html.replace(" ", ""))
        # Guidance input is always present (not gated by a pane).
        self.assertIn('id="guideIn"', markup)
        self.assertIn('id="resumeBtn"', markup)
        self.assertIn("stuckAct('go')", self.html)
        self.assertIn(" Continue</button>", self.html)

    def test_css_shell_overflow_rules(self):
        css = self.html.split("</style>", 1)[0]
        self.assertIn("overflow:hidden", css)
        self.assertIn("100dvh", css)
        self.assertIn("prompt-pin", css)
        self.assertIn("max-height:min(30vh", css.replace(" ", ""))

    def test_run_card_actions_stay_compact(self):
        """Sidebar run cards: title row truncates, actions are a kebab, no Delete while active."""
        css, _, rest = self.html.partition("</style>")
        script = rest
        self.assertIn(".run .tid", css)
        self.assertIn("text-overflow:ellipsis", css)
        self.assertIn(".run .tpills", css)
        self.assertIn(".racts.open", css)
        self.assertIn(".rmenu", css)
        compact = css.replace(" ", "")
        self.assertIn("run:hover.racts", compact)
        self.assertIn("run.sel.racts", compact)
        self.assertIn("run.is-run.racts", compact)
        self.assertIn("function runCardHtml", script)
        self.assertIn('aria-haspopup="menu"', script)
        self.assertIn("Can't delete while this run is active", script)
        self.assertIn("['new','planning','running','stuck']", script.replace(" ", ""))
        self.assertIn("is-run", script)
        # Two icon buttons in the title row would fight the pills; actions live in the menu.
        self.assertNotRegex(
            script,
            r'class="racts"><button class="sm icon[^"]*" title="Rename"',
        )


if __name__ == "__main__":
    unittest.main()
