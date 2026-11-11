import os
import tempfile
import unittest
from pathlib import Path

from agent_tester.config import ROOT
from agent_tester.memory import promotion
from agent_tester.memory.service import Memory
from agent_tester.memory.store import MemoryStore, dump_json
from agent_tester.models import CaseResult, Requirement, TestPlan, PlannedCase
from agent_tester.preflight import gap_resolver as G
from agent_tester.preflight.pack_loader import load_pack
from agent_tester.run_state import RunState
from agent_tester.end_of_run import finish_memory

PACK = ROOT / "packs" / "savostore"


class FakeEmbedder:
    """Bag-of-words vectors so the embedding path is exercised offline."""
    VOCAB = ["apply", "button", "rewards", "reload", "cap", "shipping", "stale", "login", "wallet", "checkout", "stripe", "burn"]

    def embed(self, text):
        t = text.lower()
        v = [float(t.count(w)) for w in self.VOCAB]
        return v if any(v) else None


class Base(unittest.TestCase):
    embedder = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.db", self.embedder)
        self.mem = Memory(self.store, "savostore")
        self.pack = load_pack(PACK)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class StoreTests(Base):
    def test_scope_isolation(self):
        self.store.add("lesson", "project:other", "other project lesson about rewards", "user_correction")
        self.store.add("lesson", "project:savostore", "reload the page before reading rewards", "user_correction")
        self.store.add("lesson", "core", "always screenshot the final total", "user_correction")
        texts = [r["text"] for r in self.mem.preload()]
        self.assertEqual(len(texts), 2)
        self.assertFalse(any("other project" in t for t in texts))
        self.assertTrue(texts[0].startswith("reload"))      # project lessons before core

    def test_redaction_on_write(self):
        os.environ["SAVOSTORE_CRM_PASSWORD"] = "Sup3rSecret!"
        mid = self.store.add("fact", "project:savostore", "login with Sup3rSecret! and card 4242 4242 4242 4242", "user_answer")
        t = self.store.get(mid)["text"]
        self.assertNotIn("Sup3rSecret!", t)
        self.assertNotIn("4242 4242 4242 4242", t)
        os.environ.pop("SAVOSTORE_CRM_PASSWORD")

    def test_keyword_search(self):
        self.store.add("lesson", "project:savostore", "The Apply button needs a reload before the wallet updates", "user_correction")
        self.store.add("lesson", "project:savostore", "Parcels tab is under Orders", "user_correction")
        hits = self.mem.recall("wallet apply")
        self.assertEqual(len(hits), 1)
        self.assertIn("Apply button", hits[0]["text"])

    def test_preload_cap(self):
        for i in range(50):
            self.store.add("lesson", "project:savostore", f"lesson {i} " + "x" * 200, "user_correction")
        rows = self.mem.preload(char_cap=1000)
        self.assertLess(len(rows), 10)
        from agent_tester.memory.retrieve import tokens_of
        self.assertLessEqual(tokens_of(self.mem.as_prompt(rows)), 1000 // 4 + 50)

    def test_export(self):
        self.store.add("lesson", "core", "a", "user_correction")
        p = Path(self.tmp.name) / "export.json"
        dump_json(self.store, p)
        self.assertIn('"kind": "lesson"', p.read_text())
        self.assertNotIn("_vec", p.read_text())


class EmbeddingSearch(Base):
    embedder = FakeEmbedder()

    def test_vector_search(self):
        self.store.add("lesson", "project:savostore", "rewards apply button needs a reload", "user_correction")
        self.store.add("lesson", "project:savostore", "login uses the staging account", "user_correction")
        hits = self.mem.recall("apply rewards then reload")
        self.assertIn("reload", hits[0]["text"])
        self.assertIn("score", hits[0])


class LearningTests(Base):
    def test_correction_is_active_immediately(self):
        mid = self.mem.correction("TC4", "FAIL", "PASS", "Cap uses new unpaid shipping only; intended", area="rewards")
        row = self.store.get(mid)
        self.assertEqual((row["kind"], row["status"], row["source"]), ("lesson", "active", "user_correction"))
        self.assertIn("verdict should be PASS", row["text"])

    def test_proposals_need_approval_and_dedupe(self):
        a = self.mem.propose("recipe", "When Apply does nothing, reload the order page first", area="rewards")
        b = self.mem.propose("recipe", "When Apply does nothing, reload the order page first.", area="rewards")
        self.assertEqual(a, b)
        self.assertEqual(self.mem.preload(), [])
        self.mem.approve([a])
        self.assertEqual(len(self.mem.preload()), 1)
        with self.assertRaises(ValueError):
            self.mem.propose("known_bug", "x")

    def test_recipe_fails_twice(self):
        mid = self.store.add("recipe", "project:savostore", "use the X button", "agent_proposal")
        self.assertEqual(self.mem.recipe_result(mid, False)["status"], "active")
        self.assertEqual(self.mem.recipe_result(mid, False)["status"], "needs_recheck")
        self.assertEqual(self.mem.preload(), [])

    def test_linked_bug_closed(self):
        mid = self.store.add("known_bug", "project:savostore", "rewards burn on checkout open", "run_end", linked_issue=777)
        self.store.add("known_bug", "project:savostore", "another", "run_end", linked_issue=778)
        self.assertEqual(self.mem.recheck_linked(lambda i: i == 777), [mid])
        self.assertEqual(self.store.get(mid)["status"], "needs_recheck")

    def test_open_bugs_saved(self):
        res = [CaseResult(case_id="TC1", verdict="FAIL", expected="$40", actual="$34"),
               CaseResult(case_id="TC2", verdict="FAIL", known_bug=2, actual="x"),
               CaseResult(case_id="TC3", verdict="PASS")]
        ids = self.mem.record_open_bugs(res, "run1", 180966)
        self.assertEqual(len(ids), 1)
        self.assertIn("#180966", self.store.get(ids[0])["text"])

    def test_open_bugs_skip_tester_mistakes(self):
        res = [
            CaseResult(case_id="TC1", verdict="FAIL",
                       expected="paste 25.50",
                       actual="The agent used `browser_type` instead of pasting",
                       note="test not executed properly"),
            CaseResult(case_id="TC2", verdict="FAIL",
                       expected="007 stays 7",
                       actual="The tester typed 007 then typed 0 without clearing (0070)"),
            CaseResult(case_id="TC3", verdict="FAIL",
                       expected="threshold saves", actual="value rejected",
                       note="app rejected the whole number"),
        ]
        ids = self.mem.record_open_bugs(res, "run1", 180966)
        self.assertEqual(len(ids), 1)
        self.assertIn("TC3", self.store.get(ids[0])["text"])
        self.assertNotIn("browser_type", self.store.get(ids[0])["text"])

    def test_known_bug_candidates(self):
        c = self.mem.known_bug_candidates("Rewards committed and wallet deducted when Stripe checkout opens; abandoning burns rewards",
                                          self.pack.known_bugs)
        self.assertEqual(c[0]["n"], 1)
        self.assertEqual(self.mem.known_bug_candidates("zzz qqq", self.pack.known_bugs), [])


class ResourceTests(Base):
    def test_gap_urls_not_lasting_memory(self):
        """URLs are confirmed for this run only — never written as lasting memory values."""
        import tempfile
        from agent_tester.memory.prefs import RunPrefs
        prefs_dir = Path(self.tmp.name)
        # Patch prefs path used by gap_resolver via env-free temp file: monkey by writing default after.
        ctx = G.GapContext(pack=self.pack, env="uat", memory=self.mem, wiki_texts={}, task_text="",
                           url_checker=lambda u: True, allow_hosts=["uat.example.com"])
        r = G.resolve([Requirement(key="env.uat.portal")], ctx)[0]
        G.apply_answers([r], {"env.uat.portal": "https://uat.example.com/"}, ctx, None, lambda *a: None)
        self.assertEqual(r.status, "resolved")
        self.assertEqual(r.value, "https://uat.example.com/")
        # No lasting URL value in SQLite
        row = self.mem.get("project:savostore", "env.uat.portal")
        self.assertTrue(row is None or row.get("value") in (None, ""))

    def test_secret_env_name_only(self):
        self.mem.put("project:savostore", "account.crm.admin", "maria.tariq", "user", secret_env="SAVOSTORE_CRM_PASSWORD")
        g = self.mem.get("project:savostore", "account.crm.admin")
        self.assertEqual(g["secret_env"], "SAVOSTORE_CRM_PASSWORD")
        self.assertIsNone(g["value"])

    def test_import_json_drops_values(self):
        import json
        p = Path(self.tmp.name) / "resources.json"
        p.write_text(json.dumps([{"scope": "project:savostore", "key": "env.uat.crm", "value": "https://u/",
                                  "source": "user", "secret_env": "X"}]))
        self.assertEqual(self.mem.import_resources_json(p), 1)
        self.assertFalse(p.exists())
        g = self.mem.get("project:savostore", "env.uat.crm")
        self.assertIsNone(g["value"])
        self.assertEqual(g["secret_env"], "X")


class PromotionTests(Base):
    def test_generalize(self):
        t = promotion.generalize("On #180966 order A1158 with $400 Savostore cap was wrong (verdict should be PASS, not FAIL; case TC4)", ["Savostore"])
        self.assertNotIn("180966", t)
        self.assertNotIn("A1158", t)
        self.assertNotIn("$400", t)
        self.assertNotIn("Savostore", t)
        self.assertNotIn("verdict should be", t)

    def test_candidates(self):
        a = self.mem.correction("TC1", "FAIL", "PASS", "Reload the page before reading totals after apply", general=True)
        self.store.add("lesson", "project:savostore", "Some savostore-only rule", "user_correction")
        self.store.add("lesson", "project:other", "after apply reload the page before reading totals", "user_correction")
        self.store.add("lesson", "project:savostore", "after apply reload the page before reading totals again", "user_correction")
        cands = promotion.candidates(self.store, "savostore", ["savostore"])
        ids = {c["id"] for c in cands}
        self.assertIn(a, ids)
        self.assertEqual(len(cands), 2)
        cid = promotion.promote(self.store, a, cands[0]["core_text"])
        self.assertEqual(self.store.get(cid)["scope"], "core")
        with self.assertRaises(ValueError):
            promotion.promote(self.store, self.store.add("fact", "project:savostore", "cap is 10%", "user"), "x")


class EndOfRun(Base):
    def test_finish(self):
        st = RunState(Path(self.tmp.name) / "run")
        st.set_plan(TestPlan(task_id=1, cases=[PlannedCase(id="TC1", title="t")]))
        st.set_result(CaseResult(case_id="TC1", verdict="FAIL", expected="a", actual="b"))
        p1 = self.mem.propose("lesson", "good lesson")
        p2 = self.mem.propose("lesson", "bad lesson entirely different")
        answers = iter(["y", "n"])
        out = finish_memory(self.mem, self.pack, st, input_fn=lambda _: next(answers), out=lambda *a: None)
        self.assertEqual(out["approved"], [p1])
        self.assertEqual(out["rejected"], [p2])
        self.assertEqual(len(out["open_bugs_saved"]), 1)
        self.assertEqual(st.data["learned"]["approved"], [p1])


if __name__ == "__main__":
    unittest.main()
