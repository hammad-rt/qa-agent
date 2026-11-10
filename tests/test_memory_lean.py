"""Lean memory: env guard, budgeted retrieval, seeds, retrospective, resource scrub."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tester.memory.env_guard import EnvDetailError, assert_safe_lasting, contains_env_detail, scrub_env_details
from agent_tester.memory.prefs import RunPrefs
from agent_tester.memory.retrieve import DEFAULT_TOKEN_BUDGET, TurnRetriever, tokens_of
from agent_tester.memory.review import build_retrospective, candidate_lessons, propose_from_review
from agent_tester.memory.seeds import CORE_LESSONS, ensure_seeds
from agent_tester.memory.service import Memory
from agent_tester.memory.store import MemoryStore
from agent_tester.models import CaseResult
from agent_tester.run_state import RunState


class EnvGuardTests(unittest.TestCase):
    def test_detects_env_details(self):
        self.assertTrue(contains_env_detail("use sidney@savostore.com for login"))
        self.assertTrue(contains_env_detail("open https://staging5.rolustech.com:4220/"))
        self.assertTrue(contains_env_detail("order A1158 failed"))
        self.assertTrue(contains_env_detail("password: hunter2"))
        self.assertFalse(contains_env_detail("Reload the page before reading totals after apply"))

    def test_assert_refuses_env_only(self):
        with self.assertRaises(EnvDetailError):
            assert_safe_lasting("https://staging.example.com/orders/A1158")
        with self.assertRaises(EnvDetailError):
            assert_safe_lasting("user: admin@example.com")

    def test_assert_allows_scrubbed_lesson(self):
        t = assert_safe_lasting("Reload after apply; never trust a greyed-out control alone")
        self.assertIn("Reload", t)

    def test_write_paths_guard(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "m.db")
            mem = Memory(store, "demo")
            # Env-only noise is refused; mixed text is scrubbed so no raw env details remain.
            with self.assertRaises(EnvDetailError):
                mem.propose("lesson", "https://crm.example.com/orders/A9999")
            mid = mem.propose("lesson", "Prefer Advanced Search for email; never hard-code https://crm.example.com")
            text = store.get(mid)["text"]
            self.assertNotIn("https://", text)
            # Pure env noise refused; mixed text is stored scrubbed (no raw record ids/URLs).
            with self.assertRaises(EnvDetailError):
                mem.propose("lesson", "A9999")
            cid = mem.correction("TC1", "FAIL", "PASS", "reload after apply on order A9999")
            self.assertNotIn("A9999", store.get(cid)["text"])
            store.close()


class ResourceNoEnvValue(unittest.TestCase):
    def test_put_drops_value(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "m.db")
            mem = Memory(store, "demo")
            mid = mem.put("project:demo", "account.crm.admin", "maria.tariq", "user",
                          secret_env="DEMO_CRM_PASSWORD")
            row = store.get(mid)
            self.assertIsNone(row["value_or_ref"])
            self.assertEqual(row["secret_env"], "DEMO_CRM_PASSWORD")
            self.assertIsNone(mem.get("project:demo", "account.crm.admin")["value"])
            store.close()

    def test_prefs_hold_last_used(self):
        with tempfile.TemporaryDirectory() as d:
            p = RunPrefs(Path(d) / "prefs.json")
            p.put("project:demo", "account.crm.admin.staging.last_used", "admin")
            self.assertEqual(p.get("project:demo", "account.crm.admin.staging.last_used"), "admin")


class SeedTests(unittest.TestCase):
    def test_core_seeds_idempotent_and_agnostic(self):
        from agent_tester.memory.seeds import DOMAIN_LESSONS
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "m.db")
            a = ensure_seeds(store, "savostore")
            self.assertEqual(a["core"], len(CORE_LESSONS))
            self.assertEqual(a["domain"], sum(len(v) for v in DOMAIN_LESSONS.values()))
            self.assertEqual(a["project"], 1)
            b = ensure_seeds(store, "savostore")
            self.assertEqual(b, {"core": 0, "domain": 0, "platform": 0, "project": 0})
            texts = [r["text"] for r in store.query(["core"], ["lesson"])]
            self.assertTrue(all("SuiteCRM" not in t for t in texts))
            domain = [r["text"] for r in store.query(["domain:crm"], ["lesson"])]
            self.assertTrue(any("Advanced Search" in t for t in domain))
            self.assertTrue(all("SuiteCRM" not in t and "Savostore" not in t for t in domain))
            store.close()


class RetrievalBudget(unittest.TestCase):
    def test_injected_stays_under_budget_when_store_and_learned_are_large(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = MemoryStore(root / "m.db")
            mem = Memory(store, "demo")
            for i in range(80):
                store.add("lesson", "project:demo", f"lesson about rewards apply reload number {i} " + ("word " * 40),
                          "user_correction", area="rewards", status="active")
                store.add("recipe", "project:demo", f"recipe click Orders then New for case {i} " + ("step " * 30),
                          "agent_proposal", area="rewards", status="active")
            pack = root / "pack"
            pack.mkdir()
            learned = pack / "learned.md"
            lines = ["# What the agent learned\n", "## rewards:crm\n"]
            for i in range(60):
                lines.append(f"- Stuck (stuck): dead end {i} on the rewards panel (" + ("x" * 100) + f") (2026-11-0{i % 9}, TC{i})\n")
                lines.append(f"- Route: click 'Orders' -> click 'Rewards' -> click 'Apply' ({i})\n")
            learned.write_text("".join(lines))

            state = RunState(root / "run")
            tr = TurnRetriever(mem, token_budget=DEFAULT_TOKEN_BUDGET, state=state)
            res = tr.retrieve(area="rewards", app="crm", query="apply rewards button reload",
                              title="Apply rewards", pack_root=pack, include_learned=True)
            self.assertLessEqual(res.tokens, DEFAULT_TOKEN_BUDGET)
            self.assertLessEqual(res.chars, DEFAULT_TOKEN_BUDGET * 4)
            self.assertLess(len(res.rows), 40)
            self.assertLessEqual(len(res.learned_hints), 4)
            # Second turn same area:app should cache / skip re-inject of same ids
            res2 = tr.retrieve(area="rewards", app="crm", query="apply rewards button reload",
                               title="Apply rewards", pack_root=pack, include_learned=True)
            self.assertTrue(res2.cache_hit)
            self.assertEqual(res2.tokens, 0)  # already in context — no re-send
            stats = state.data["memory_injection_log"]
            self.assertGreaterEqual(len(stats), 2)
            self.assertLessEqual(max(e["tokens"] for e in stats), DEFAULT_TOKEN_BUDGET)
            store.close()

    def test_tokens_of(self):
        self.assertEqual(tokens_of(""), 0)
        self.assertEqual(tokens_of("abcd"), 1)


class ConfirmAndReview(unittest.TestCase):
    def test_lesson_activates_after_two_confirmations(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "m.db")
            mem = Memory(store, "demo")
            mid = mem.propose("lesson", "Always re-read the threshold after Save", evidence="run-a/TC1")
            self.assertEqual(store.get(mid)["status"], "proposed")
            # Same run: dedupe only
            mem.propose("lesson", "Always re-read the threshold after Save.", evidence="run-a/TC2")
            self.assertEqual(store.get(mid)["status"], "proposed")
            # Different run: confirmation bump → lasting
            mem.propose("lesson", "Always re-read the threshold after Save", evidence="run-b/TC1")
            self.assertEqual(store.get(mid)["status"], "active")
            self.assertGreaterEqual(store.get(mid)["confirm_count"], 2)
            store.close()

    def test_fact_stays_proposed_until_user_approves(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d) / "m.db")
            mem = Memory(store, "demo")
            mid = mem.propose("fact", "Threshold must be a whole number")
            mem.propose("fact", "Threshold must be a whole number")
            self.assertEqual(store.get(mid)["status"], "proposed")
            mem.approve([mid])
            self.assertEqual(store.get(mid)["status"], "active")
            store.close()

    def test_retrospective_code_only(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            results = [
                CaseResult(case_id="TC1", verdict="FAIL", note="value wrong", expected="1", actual="2"),
                CaseResult(case_id="TC2", verdict="AGENT INCOMPLETE", note="stuck in loop"),
                CaseResult(case_id="TC3", verdict="PASS"),
            ]
            review = build_retrospective(st, results)
            self.assertIn("TC1", review["summary"])
            self.assertEqual(len(review["failed"]), 2)
            cands = candidate_lessons(review)
            self.assertTrue(cands)
            store = MemoryStore(Path(d) / "m.db")
            mem = Memory(store, "demo")
            ids = propose_from_review(mem, review)
            self.assertTrue(ids)
            store.close()


class LearnedScrub(unittest.TestCase):
    def test_add_strips_env(self):
        from agent_tester import learned
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ok = learned.add(root, "crm", "crm",
                             "Behaviour: search for sidney@savostore.com on https://crm.example.com order A1158")
            # Either refused or scrubbed without raw env details
            if ok:
                text = (root / "learned.md").read_text()
                self.assertNotIn("sidney@savostore.com", text)
                self.assertNotIn("https://crm.example.com", text)
                self.assertNotIn("A1158", text)
            else:
                self.assertFalse((root / "learned.md").exists() or "sidney@" in (root / "learned.md").read_text()
                                 if (root / "learned.md").exists() else True)


if __name__ == "__main__":
    unittest.main()
