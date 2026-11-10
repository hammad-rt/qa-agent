"""Context selector: budgets, always-send safety/case rules, promotion/demotion."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_tester.context import (
    ContextChunk, ContextScores, ContextSelector, StepContext,
    case_tagged_rule_chunks, parse_numbered_rules, safety_chunks_from_pack,
    select_chunks, update_scores_from_run,
)
from agent_tester.run_state import RunState


class AlwaysSend(unittest.TestCase):
    def test_safety_and_case_rules_never_dropped(self):
        safety = safety_chunks_from_pack({
            "safety_rules": [
                {"text": "Stripe 4242 only", "always_send": True},
                {"text": "Never M-Pesa", "always_send": True},
            ]
        })
        numbered = {2: "cap is 10%", 14: "4242 only"}
        case_rules = case_tagged_rule_chunks("E1", "E1 earn (rules 2, 14)", numbered)
        filler = [ContextChunk(source="facts", text=("x" * 2000) + f" filler{i}",
                               step_types=("navigate",)) for i in range(20)]
        # Zero usage scores for everything — learning would demote fillers.
        scores = {c.id: 0.0 for c in safety + case_rules + filler}
        sel = select_chunks(safety + case_rules + filler,
                            StepContext(step_type="navigate", case_id="E1"),
                            scores=scores, budget=50, demote_below=0.15)
        always_sources = {c.source for c in sel.chunks if c.always_send or c.exempt_from_scoring}
        self.assertIn("safety", always_sources)
        self.assertIn("case_rules", always_sources)
        # All safety + case_rules present even with tiny budget.
        for c in safety + case_rules:
            self.assertIn(c.id, [x.id for x in sel.chunks])
            self.assertIn(c.id, sel.always_ids)

    def test_demotion_cannot_drop_exempt(self):
        ch = ContextChunk(source="safety", text="SAFETY: never SQL", always_send=True,
                          exempt_from_scoring=True)
        with tempfile.TemporaryDirectory() as d:
            store = ContextScores(Path(d) / "c.db")
            # Attempt to mark unused across many runs — exempt rows are skipped in decay.
            store.mark_sent(ContextChunk(source="facts", text="optional", id="opt1"), "navigate")
            for _ in range(6):
                store.end_run_decay({"opt1", ch.id}, set(), exempt_ids={ch.id})
            self.assertEqual(store.get(ch.id), 0.5)  # never written / stays default
            self.assertLess(store.get("opt1"), 0.15)
            store.close()


class SelectionBudget(unittest.TestCase):
    def test_omits_low_relevance_within_budget(self):
        always = [ContextChunk(source="case_steps", text="steps here", always_send=True)]
        good = ContextChunk(source="facts", text="rewards wallet Current Rewards",
                            step_types=("read",), keywords=("wallet", "rewards"))
        bad = ContextChunk(source="facts", text="intercom widget styling colors",
                           step_types=("read",), keywords=("intercom",))
        sel = select_chunks(always + [good, bad],
                            StepContext(step_type="read", query="read wallet Current Rewards"),
                            budget=800)
        ids = [c.id for c in sel.chunks]
        self.assertIn(good.id, ids)
        self.assertIn(always[0].id, ids)

    def test_parse_case_rule_refs(self):
        from agent_tester.context.chunks import case_rule_refs
        self.assertEqual(case_rule_refs("E1 (rules 2, 14)"), [2, 14])
        self.assertEqual(parse_numbered_rules("1. aaa\n2. bbb\n"), {1: "aaa", 2: "bbb"})


class LearningSignals(unittest.TestCase):
    def test_promotion_on_stall_fix(self):
        with tempfile.TemporaryDirectory() as d:
            store = ContextScores(Path(d) / "c.db")
            store.mark_stall_fix("chunkA", "read")
            self.assertGreater(store.get("chunkA", "read"), 0.5)
            store.close()

    def test_update_from_run_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            st = RunState(Path(d) / "run")
            st.data["context_injection_log"] = [{
                "step_type": "read", "tokens": 100, "omitted_tokens": 50,
                "chunks": [{"id": "a", "source": "facts", "tokens": 40, "always": False},
                           {"id": "s", "source": "safety", "tokens": 20, "always": True}],
            }]
            st.data["context_usage_log"] = [{"used": ["a"], "resolved_by": "a"}]
            store = ContextScores(Path(d) / "c.db")
            summary = update_scores_from_run(store, st, exempt_ids={"s"})
            self.assertIn("decayed", summary)
            self.assertIn("tokens_omitted", st.data.get("context_stats") or {})
            store.close()

    def test_get_context_promotes(self):
        sel = ContextSelector()
        c = ContextChunk(source="facts", text="Advanced Search by email for contacts",
                         keywords=("email", "contact"))
        sel.register([c])
        found = sel.by_topic("contact email search")
        self.assertTrue(found)
        self.assertEqual(found[0].id, c.id)


class ContextLearningOff(unittest.TestCase):
    def test_flag_disables_demotion(self):
        import os
        ch = ContextChunk(source="facts", text="optional route", id="x1")
        with mock_env("CONTEXT_LEARNING", "0"):
            sel = select_chunks([ch], StepContext(step_type="navigate"),
                                scores={"x1": 0.0}, demote_below=0.15)
            self.assertEqual([c.id for c in sel.chunks], ["x1"])
            self.assertEqual(sel.demoted_ids, [])


class mock_env:
    def __init__(self, k, v):
        self.k, self.v = k, v
    def __enter__(self):
        import os
        self.old = os.environ.get(self.k)
        os.environ[self.k] = self.v
    def __exit__(self, *a):
        import os
        if self.old is None:
            os.environ.pop(self.k, None)
        else:
            os.environ[self.k] = self.old


if __name__ == "__main__":
    unittest.main()
