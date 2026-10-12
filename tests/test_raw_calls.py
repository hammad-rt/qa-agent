import unittest
from types import SimpleNamespace
from google.genai import types
from agent_tester.agents import raw_calls

RAW = '<|tool_call>call:run_helper({name:<|"|>login_form<|"|>,params_json:<|"|>{"account": "customer"}<|"|>})<tool_call|>'


class RawCalls(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(raw_calls.parse(RAW), [("run_helper", {"name": "login_form", "params_json": '{"account": "customer"}'})])

    def test_parse_scalars(self):
        self.assertEqual(raw_calls.parse('call:mark_step({n:8})'), [("mark_step", {"n": 8})])

    def test_after_model_converts(self):
        r = SimpleNamespace(content=types.Content(role="model", parts=[types.Part(text=RAW)]))
        out = raw_calls.after_model(None, r)
        fc = out.content.parts[0].function_call
        self.assertEqual((fc.name, fc.args["name"]), ("run_helper", "login_form"))

    def test_plain_text_untouched(self):
        r = SimpleNamespace(content=types.Content(role="model", parts=[types.Part(text="done")]))
        self.assertIsNone(raw_calls.after_model(None, r))


if __name__ == "__main__":
    unittest.main()
