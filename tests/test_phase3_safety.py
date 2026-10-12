import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tester.config import ROOT
from agent_tester.coordinator import RunPaused, run_plan
from agent_tester.ledger import Ledger
from agent_tester.models import CaseResult, CaseType, PlannedCase, Step, StepResult, TestPlan
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.redact import scrub_secrets
from agent_tester.reliability import RunLock, RunLocked, apply_retention, is_retryable, with_retries
from agent_tester.run_state import RunState
from agent_tester.tools.policy import PolicyViolation, from_pack

PACK = ROOT / "packs" / "savostore"


class Retries(unittest.TestCase):
    def test_backoff_then_success(self):
        calls, sleeps = {"n": 0}, []

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("connection reset")
            return "ok"

        async def fake_sleep(d):
            sleeps.append(d)
        self.assertEqual(asyncio.run(with_retries(flaky, sleep=fake_sleep)), "ok")
        self.assertEqual(len(sleeps), 2)
        self.assertLess(sleeps[0], sleeps[1])

    def test_no_retry_on_logic_error(self):
        async def bad():
            raise ValueError("bad json")

        async def fake_sleep(d):
            raise AssertionError("should not sleep")
        with self.assertRaises(ValueError):
            asyncio.run(with_retries(bad, sleep=fake_sleep))

    def test_gives_up(self):
        async def down():
            raise TimeoutError("timed out")

        async def fake_sleep(d):
            pass
        with self.assertRaises(TimeoutError):
            asyncio.run(with_retries(down, attempts=3, sleep=fake_sleep))
        self.assertTrue(is_retryable(Exception("HTTP 503 Service Unavailable")))


class Lock(unittest.TestCase):
    def test_one_run_at_a_time(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".run.lock"
            with RunLock(p):
                with self.assertRaises(RunLocked):
                    with RunLock(p):
                        pass
            with RunLock(p):     # released after exit
                pass


class Retention(unittest.TestCase):
    def test_trim_old_runs(self):
        with tempfile.TemporaryDirectory() as d:
            runs = Path(d)
            now = time.time()
            for i in range(7):
                r = runs / f"run{i}"
                (r / "shots").mkdir(parents=True)
                (r / "shots" / "a.png").write_bytes(b"x")
                (r / "state.json").write_text("{}")
                (r / "report.html").write_text("r")
                (r / "trace.jsonl").write_text("t")
                age = now - (i * 3600 if i < 6 else 20 * 86400)
                os.utime(r, (age, age))
            trimmed = apply_retention(runs, keep=5, days=14, now=now)
            self.assertEqual({p.name for p in trimmed}, {"run5", "run6"})
            self.assertEqual(sorted(x.name for x in (runs / "run6").iterdir()), ["report.html", "state.json"])
            self.assertTrue((runs / "run0" / "shots" / "a.png").exists())


class Secrets(unittest.TestCase):
    def test_scrub(self):
        env = {"SAVOSTORE_CRM_PASSWORD": "hunter22", "HOME": "/home/box", "GITHUB_TOKEN": "ghp_abcdef"}
        t = scrub_secrets("pw hunter22 token ghp_abcdef home /home/box", env)
        self.assertEqual(t, "pw [SAVOSTORE_CRM_PASSWORD] token [GITHUB_TOKEN] home /home/box")

    def test_state_file_scrubbed(self):
        os.environ["X_TEST_PASSWORD"] = "Zq9!longsecret"
        try:
            with tempfile.TemporaryDirectory() as d:
                st = RunState(Path(d) / "r")
                st.data["notes"].append("typed Zq9!longsecret into the form")
                st.save()
                self.assertNotIn("Zq9!longsecret", st.path.read_text())
        finally:
            os.environ.pop("X_TEST_PASSWORD")


class ReadOnly(unittest.TestCase):
    def test_read_only_refuses_even_preapproved(self):
        p = from_pack(load_pack(PACK), "staging", approver=lambda *a: True)
        p.preapprove(["create_test_order"])
        p.read_only = True
        with self.assertRaises(PolicyViolation):
            p.gate("create", "baseline", action_type="create_test_order")
        self.assertEqual(p.gate("read", "baseline"), "not_needed")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = RunState(Path(self.tmp.name) / "180966-x")
        self.led = Ledger(self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ledger_and_leftovers(self):
        self.led.add("TC1", "portal", "order A2001", "created", "Pending")
        self.led.add("TC1", "portal", "order A2001", "changed", "Paid")
        self.led.add("TC2", "portal", "order A1158", "used")
        self.assertEqual(len(self.state.data["ledger"]), 2)
        self.assertEqual(self.state.data["ledger"][0]["final_state"], "Paid")
        self.assertEqual([e["record"] for e in self.led.leftovers()], ["order A2001"])
        self.assertEqual(self.led.run_tag, "qa-180966-x")
        self.led.mark_cleaned("order A2001", "portal")
        self.assertEqual(self.led.leftovers(), [])

    def test_restore_tracking(self):
        self.led.record_original("TC9", "crm", "rewards threshold", "150")
        self.assertEqual(len(self.led.unrestored("TC9")), 1)
        self.assertFalse(self.led.record_restored("crm", "rewards threshold", "151", True)["verified"])
        self.assertTrue(self.led.record_restored("crm", "rewards threshold", "150", True)["verified"])
        self.assertEqual(self.led.unrestored(), [])
        with self.assertRaises(KeyError):
            self.led.record_restored("crm", "unknown", "1", True)

    def test_judge_flags_unrestored(self):
        from agent_tester.agents.judge import enforce_rules
        self.led.record_original("TC9", "crm", "threshold", "150")
        r = enforce_rules(CaseResult(case_id="TC9", verdict="PASS", note="ok"), {}, [], [], self.led.unrestored("TC9"))
        self.assertEqual(r.verdict, "PARTIAL")
        self.assertIn("not restored", r.note)


class TesterTools(unittest.TestCase):
    def setUp(self):
        from agent_tester.agents.tester import TesterSession
        from agent_tester.memory.service import Memory
        from agent_tester.memory.store import MemoryStore
        from agent_tester.tools.screenshots import Evidence
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.state = RunState(d / "run")
        self.store = MemoryStore(d / "m.db")
        self.mem = Memory(self.store, "savostore")
        self.store.add("lesson", "project:savostore", "Apply button needs a reload", "user_correction")
        policy = from_pack(load_pack(PACK), "staging")
        self.mk = lambda brief: TesterSession("crm", brief, self.state, policy, Evidence(self.state.dir), 10, 5,
                                               memory=self.mem, ledger=Ledger(self.state))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_memory_and_ledger_tools(self):
        s = self.mk({"case_id": "TC9", "area": "rewards", "requirements": {}})
        t = {f.__name__: f for f in s.tools()}
        self.assertIn("reload", t["recall_lessons"]("apply button")["lessons"][0]["text"])
        mid = t["propose_memory"]("recipe", "Reload after apply", "")["proposed"]
        self.assertEqual(self.store.get(mid)["status"], "proposed")
        self.assertEqual(self.store.get(mid)["area"], "rewards")
        t["record_original"]("threshold", "150")
        self.assertTrue(t["record_restored"]("threshold", "150", True)["restored_ok"])
        t["ledger_record"]("order A1", "created", "Pending")
        self.assertEqual(len(self.state.data["ledger"]), 1)

    def test_read_only_turn_has_no_write_tools(self):
        s = self.mk({"case_id": "BASELINE", "read_only": True, "requirements": {}})
        names = {f.__name__ for f in s.tools()}
        self.assertNotIn("ledger_record", names)
        self.assertNotIn("propose_memory", names)
        self.assertIn("record_value", names)


class CoordinatorSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = RunState(Path(self.tmp.name) / "run")

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_replay_after_data_change(self):
        plan = TestPlan(task_id=1, cases=[PlannedCase(id="TC1", title="pay", steps=[Step(text="pay", app="portal")])])
        calls = {"n": 0}

        def tester(app, brief, state):
            calls["n"] += 1
            state.log_action(case_id="TC1", app=app, tool="request_approval", approval="pre_approved")
            raise ConnectionError("LLM dropped")
        res = run_plan(plan, self.state, {"portal": tester}, lambda *a: None)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(res["TC1"].verdict, "AGENT INCOMPLETE")
        self.assertIn("check the record", res["TC1"].note)

    def test_baseline_and_final(self):
        plan = TestPlan(task_id=1, baseline_steps=[Step(text="read wallet", app="portal")],
                        cases=[PlannedCase(id="TC1", title="t", steps=[Step(text="x", app="portal")])])
        seen = []

        def tester(app, brief, state):
            seen.append((brief["case_id"], brief.get("read_only", False)))
            v = {"wallet": 100.0 if brief["case_id"] == "BASELINE" else 95.0} if brief.get("read_only") else {}
            return StepResult(case_id=brief["case_id"], app=app, values=v)
        run_plan(plan, self.state, {"portal": tester}, lambda b, a, r, s: CaseResult(case_id=b["case_id"], verdict="PASS"))
        self.assertEqual(seen, [("BASELINE", True), ("TC1", False), ("FINAL", True)])
        self.assertEqual(self.state.data["baseline"], {"wallet": 100.0})
        self.assertEqual(self.state.data["final"], {"wallet": 95.0})


class ReportPhase3(unittest.TestCase):
    def test_report_sections(self):
        from agent_tester.report.builder import build_report
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.set_plan(TestPlan(task_id=5, cases=[PlannedCase(id="TC1", title="t")]))
            st.set_result(CaseResult(case_id="TC1", verdict="PASS"))
            st.data.update(baseline={"wallet": 100}, final={"wallet": 95})
            led = Ledger(st)
            led.add("TC1", "portal", "order A1", "created", "Paid")
            led.record_original("TC1", "crm", "threshold", "150")
            html = build_report(st).read_text()
            for s in ("Baseline vs final", "Settings not restored", "order A1", "threshold"):
                self.assertIn(s, html)


if __name__ == "__main__":
    unittest.main()
