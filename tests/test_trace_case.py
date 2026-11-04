import json, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tester.agents.llm import TRACE_CASE, _trace


def _ev(text):
    return SimpleNamespace(author="tester_crm", content=SimpleNamespace(parts=[SimpleNamespace(text=text)]))


class TraceCase(unittest.TestCase):
    def test_case_id_recorded_while_set(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "trace.jsonl"
            _trace(p, _ev("before"))
            tok = TRACE_CASE.set("184815.TC2")
            try:
                _trace(p, _ev("inside"))
            finally:
                TRACE_CASE.reset(tok)
            _trace(p, _ev("after"))
            rows = [json.loads(l) for l in p.read_text().splitlines()]
            self.assertEqual([r.get("case_id") for r in rows], [None, "184815.TC2", None])


if __name__ == "__main__":
    unittest.main()
