import unittest
from agent_tester.agents.tester import nudge_text


class Nudge(unittest.TestCase):
    def test_raw_gemma_tool_call_detected(self):
        t = nudge_text('<|tool_call>call:run_helper({name:<|"|>login_form<|"|>})<tool_call|>')
        self.assertIn("wrote a tool call as text", t)

    def test_plain_stop(self):
        self.assertIn("without calling finish", nudge_text("I am done."))


if __name__ == "__main__":
    unittest.main()
