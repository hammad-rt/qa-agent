import json, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tester.agents import tester as T


def _session(d):
    cls = next(c for c in vars(T).values() if isinstance(c, type) and hasattr(c, "_new_guidance"))
    s = cls.__new__(cls)
    s.state = SimpleNamespace(dir=Path(d))
    return s


class Guidance(unittest.TestCase):
    def test_each_note_delivered_once(self):
        with tempfile.TemporaryDirectory() as d:
            s = _session(d)
            self.assertIsNone(s._new_guidance())
            f = Path(d) / "guidance.jsonl"
            f.write_text(json.dumps({"text": "use the Contact details tab"}) + "\n")
            first = s._new_guidance()
            self.assertIn("use the Contact details tab", first)
            self.assertIn("does not lift approvals", first)
            self.assertIsNone(s._new_guidance())
            with open(f, "a") as h:
                h.write(json.dumps({"text": "the save button is at the bottom"}) + "\n")
            second = s._new_guidance()
            self.assertIn("save button", second)
            self.assertNotIn("Contact details", second)

    def test_new_session_sees_earlier_notes_with_caveat(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "guidance.jsonl").write_text(json.dumps({"text": "x"}) + "\n")
            self.assertIn("earlier case", _session(d)._new_guidance())


if __name__ == "__main__":
    unittest.main()


class HeldIsLoggable(unittest.TestCase):
    def test_action_log_accepts_held(self):
        from agent_tester.models import ActionLogEntry
        e = ActionLogEntry(seq=1, case_id="TC1", app="portal", tool="browser_click", approval="held",
                           outcome="user guidance delivered")
        self.assertEqual(e.approval, "held")
