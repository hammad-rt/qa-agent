import tempfile, unittest
from pathlib import Path
from agent_tester.run_state import RunState


class T(unittest.TestCase):
    def test_reopen_keeps_passes_and_resets_failed(self):
        d = Path(tempfile.mkdtemp())
        st = RunState.new(d, 1)
        st.data["results"] = {"TC1": {"case_id": "TC1", "verdict": "PASS"}, "TC2": {"case_id": "TC2", "verdict": "FAIL"}}
        st.data["action_log"] = [{"seq": 1, "case_id": "TC2"}, {"seq": 2, "case_id": "TC1"}]
        st.data["entered_values"] = {"TC2": {"x": "1"}}
        st.reopen_cases(["TC2"])
        self.assertIn("TC1", st.data["results"])
        self.assertNotIn("TC2", st.data["results"])
        self.assertEqual(st.data["previous_attempts"]["TC2"][0]["verdict"], "FAIL")
        self.assertEqual(st.actions_for("TC2"), [])
        self.assertEqual(len(st.actions_for("TC1")), 1)
        self.assertNotIn("TC2", st.data["entered_values"])
        st.data["action_log"].append({"seq": 3, "case_id": "TC2"})
        self.assertEqual(len(st.actions_for("TC2")), 1)


if __name__ == "__main__":
    unittest.main()
