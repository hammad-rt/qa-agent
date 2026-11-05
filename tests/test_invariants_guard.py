import unittest
from types import SimpleNamespace as N
import yaml
from agent_tester.invariants import computed_checks
from agent_tester.agents import tester as T


class Invariants(unittest.TestCase):
    def setUp(self):
        self.inv = yaml.safe_load(open("packs/savostore/pack.yaml"))["invariants"]

    def test_a12_math_done_in_code(self):
        out = computed_checks(self.inv, {"L": 1.25, "W": -0.22, "U": 1.47})
        self.assertTrue(any("W = L - U" in o and "-> holds" in o for o in out))
        self.assertTrue(any("W >= 0" in o and "does NOT hold" in o for o in out))

    def test_ambiguous_names_skip(self):
        self.assertEqual(computed_checks(self.inv, {"W0": 1, "W1": 2, "L": 3, "U": 1}), [])


class Guards(unittest.TestCase):
    def test_other_app(self):
        urls = {"crm": "http://localhost/savostorea/", "portal": "http://localhost:3000/"}
        self.assertEqual(T._other_app("http://localhost/savostorea/index.php", urls, "portal"), "crm")
        self.assertIsNone(T._other_app("http://localhost:3000/shipping-only", urls, "portal"))
        self.assertIsNone(T._other_app("https://checkout.stripe.com/x", urls, "portal"))

    def test_not_found(self):
        self.assertTrue(T._is_not_found('- heading "404" [level=1]\n- heading "This page could not be found."'))
        self.assertFalse(T._is_not_found("Order A1404 total 404.00"))


class GuidanceInTurn(unittest.TestCase):
    def test_note_reaches_the_next_model_call_and_stays(self):
        import json, tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        logged = []
        st = N(dir=d, log_action=lambda **k: logged.append(k), checkpoint=lambda *a, **k: None)
        sess = N(state=st, case_id="B1", app="portal", current_step=2)
        (d / "guidance.jsonl").write_text(json.dumps({"text": "use the X button"}) + "\n")
        req = N(contents=[])
        T.TesterSession.before_model(sess, None, req)
        self.assertIn("use the X button", req.contents[-1].parts[0].text)
        req2 = N(contents=[])                           # ADK rebuilds the request: the note is still there
        T.TesterSession.before_model(sess, None, req2)
        self.assertIn("use the X button", req2.contents[-1].parts[0].text)
        self.assertEqual(len([l for l in logged if l.get("tool") == "guidance"]), 1)


if __name__ == "__main__":
    unittest.main()
