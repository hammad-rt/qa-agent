import tempfile
import unittest
from pathlib import Path

from agent_tester.agents.tester import _tool_error
from agent_tester.run_state import RunState


class ToolOutcome(unittest.TestCase):
    def test_detects_playwright_errors(self):
        t = ("### Error\nTimeoutError: locator.fill: Timeout 5000ms exceeded.\nCall log:\n"
             "  - waiting for element to be visible, enabled and editable")
        self.assertTrue(_tool_error(t).startswith("TimeoutError"))
        self.assertIsNone(_tool_error("### Ran Playwright code\nawait page.fill('x')\n### Page\n- Page URL: https://a"))
        self.assertIsNone(_tool_error(""))

    def test_set_outcome(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState.new(Path(d), 1)
            e = st.log_action(case_id="TC1", app="crm", tool="browser_type", args={})
            st.set_outcome(e.seq, "error: TimeoutError")
            self.assertEqual(st.data["action_log"][-1]["outcome"], "error: TimeoutError")
