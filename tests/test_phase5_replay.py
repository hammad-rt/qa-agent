"""Phase 5: replay set rebuilt from past report PDFs, and scoring against the pass bar."""
import shutil
import tempfile
import unittest
from pathlib import Path

from agent_tester.config import ROOT
from agent_tester.evals.extract import extract, match_known_bug, normalize_verdict
from agent_tester.evals.replay import oracle_judge, replay
from agent_tester.models import CaseResult
from agent_tester.preflight.pack_loader import load_pack

PACK = ROOT / "packs" / "savostore"
BUGS = [{"n": 1, "severity": "Critical", "text": "Rewards committed when checkout opens, not after payment."},
        {"n": 2, "severity": "High", "text": "Remaining 'up to' limit ignores pending rewards."}]
SAMPLE = """Task #111 - Example
Counting: A2 counted Partial (one sub-check blocked).

A1 - Apply limit      FAIL
Steps   Applied 60 with 40 left.
Expected  Rejected with a limit message.
Actual   Accepted; up to shows 40, card 4111 1111 1111 1111.

A2 - Mixed       PASS (flow) / BLOCKED (pay)
Expected  Flow works.
Actual   Flow works; pay step blocked.

A3 - Happy path      PASS
Expected  Wallet drops once.
Actual   Wallet dropped once.

Bug 1: Up to limit ignores pending rewards     High
Affected cases: A1
"""


class Extract(unittest.TestCase):
    def test_cases_verdicts_counting_and_bug_links(self):
        d = extract(SAMPLE, BUGS)
        got = {c["id"]: c for c in d["cases"]}
        self.assertEqual(sorted(got), ["A1", "A2", "A3"])
        self.assertEqual(got["A1"]["verdict"], "FAIL")
        self.assertEqual(got["A2"]["verdict"], "PARTIAL")              # counting override
        self.assertEqual(got["A1"]["known_bugs"], [2])                  # mapped by text, not by report number
        self.assertEqual(got["A3"]["known_bugs"], [])
        self.assertNotIn("4111 1111", got["A1"]["actual"])
        self.assertEqual(got["A1"]["task"], 111)

    def test_normalize_and_match(self):
        self.assertEqual(normalize_verdict("FAIL (CRITICAL)"), "FAIL")
        self.assertEqual(normalize_verdict("PASS (flow) / FAIL (final)"), "PARTIAL")
        self.assertEqual(normalize_verdict(""), "NOT VERIFIED")
        self.assertIsNone(match_known_bug("Login button is blue", BUGS))

    @unittest.skipUnless(shutil.which("pdftotext"), "pdftotext missing")
    def test_real_report_maps_bugs_7_and_8(self):
        from agent_tester.evals.extract import pdf_text
        pdf = next((PACK / "evals" / "source").glob("*.pdf"))
        d = extract(pdf_text(pdf), load_pack(PACK).known_bugs)
        mapping = {b["report_n"]: b["n"] for b in d["report_bugs"]}
        self.assertEqual((mapping[7], mapping[8]), (8, 7))              # report numbering differs from facts.md
        self.assertGreaterEqual(len(d["cases"]), 30)


class Score(unittest.TestCase):
    CASES = [{"key": f"1:{i}", "id": i, "title": i, "steps": "", "expected": "e", "actual": "a",
              "verdict": v, "known_bugs": kb, "figures": []}
             for i, v, kb in [("A1", "FAIL", [2]), ("A2", "PASS", []), ("A3", "PARTIAL", []), ("A4", "SKIPPED", []),
                              ("A5", "PASS", []), ("A6", "PASS", []), ("A7", "PASS", []), ("A8", "PASS", []),
                              ("A9", "PASS", []), ("A10", "PASS", []), ("A11", "FAIL", [1])]]

    def test_oracle_passes_and_skipped_not_scored(self):
        with tempfile.TemporaryDirectory() as d:
            s = replay(self.CASES, oracle_judge(self.CASES), Path(d))
            self.assertEqual((s.total, s.agree, s.passed), (10, 10, True))
            self.assertTrue((Path(d) / "replay_score.json").exists())

    def test_fail_to_pass_and_wrong_bug_fail_the_bar(self):
        def judge(brief, actions, srs, state):
            self.assertEqual(srs[0].values["observed"], "a")             # judge sees the recorded actual
            v = {"A1": "PASS", "A11": "FAIL"}.get(brief["case_id"], next(c["verdict"] for c in self.CASES if c["id"] == brief["case_id"]))
            return CaseResult(case_id=brief["case_id"], verdict=v, known_bug=2 if brief["case_id"] == "A11" else None)
        with tempfile.TemporaryDirectory() as d:
            s = replay(self.CASES, judge, Path(d))
        self.assertEqual(s.agree, 9)
        self.assertEqual(s.fail_to_pass, ["1:A1"])
        self.assertEqual(len(s.bug_mislabels), 1)
        self.assertFalse(s.passed)

    def test_judge_crash_counts_as_disagreement(self):
        def boom(*a):
            raise RuntimeError("llm down")
        with tempfile.TemporaryDirectory() as d:
            s = replay(self.CASES, boom, Path(d), only=["A2"])
        self.assertEqual((s.total, s.agree), (1, 0))
        self.assertEqual(s.rows[0]["got"], "AGENT INCOMPLETE")


class KnownAnswersFile(unittest.TestCase):
    def test_saved_set_has_corrections_and_no_secrets(self):
        import json
        data = json.loads((PACK / "evals" / "known_answers.json").read_text())
        by = {c["id"]: c for c in data["cases"]}
        self.assertEqual(by["A2"]["verdict"], "PASS")
        self.assertIn("intended", by["A2"]["correction"])
        text = json.dumps(data).lower()
        for bad in ("password:", "@rolustech", "4111 1111"):
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
