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


class GuidanceForAnotherCase(unittest.TestCase):
    def _s(self, d, case_id):
        s = _session(d)
        s.case_id = case_id
        plan = SimpleNamespace(cases=[SimpleNamespace(id=i) for i in ("TC0", "E1", "E10", "C7")])
        s.state = SimpleNamespace(dir=Path(d), plan=plan)
        return s

    def test_note_naming_a_case_waits_for_it(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "guidance.jsonl").write_text(
                json.dumps({"text": "before earning rewards in E1 set the earn % to 80"}) + "\n")
            self.assertIsNone(self._s(d, "TC0")._new_guidance())
            self.assertIsNone(self._s(d, "E10")._new_guidance())
            self.assertIn("80", self._s(d, "E1")._new_guidance())

    def test_general_note_goes_to_current_case(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "guidance.jsonl").write_text(json.dumps({"text": "you seem stuck"}) + "\n")
            self.assertIn("stuck", self._s(d, "TC0")._new_guidance())

    def test_unfitted_note_is_not_marked_seen(self):
        """A note that names another case stays pending on the same session."""
        with tempfile.TemporaryDirectory() as d:
            s = self._s(d, "TC3")
            s.state.plan.cases = [SimpleNamespace(id=i) for i in ("TC2", "TC3", "TC0", "E1")]
            (Path(d) / "guidance.jsonl").write_text(
                json.dumps({"text": "test TC2 again"}) + "\n")
            self.assertIsNone(s._new_guidance())
            self.assertNotIn(0, getattr(s, "_guide_delivered", set()))
            # A later general note is delivered without swallowing the TC2 note.
            with open(Path(d) / "guidance.jsonl", "a") as h:
                h.write(json.dumps({"text": "you seem stuck"}) + "\n")
            second = s._new_guidance()
            self.assertIn("stuck", second)
            self.assertNotIn("TC2", second)
            self.assertNotIn(0, s._guide_delivered)
            self.assertIn(1, s._guide_delivered)
            s.case_id = "TC2"
            retry = s._new_guidance()
            self.assertIn("TC2", retry)
            self.assertIn(0, s._guide_delivered)
