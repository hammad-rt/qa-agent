"""Parallel-safe partitioning and planner batch sizing / plan cache."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tester.agents.planner import size_batches
from agent_tester.models import PlannedCase, Step, TestPlan
from agent_tester.parallel import (
    case_is_parallel_safe, group_ids, max_parallel, pack_safe_ids, parallel_enabled,
    partition_parallel_batches, shared_placeholders,
)
from agent_tester.plan_cache import load_cached_plan, pack_version_hash, plan_fingerprint, save_plan_cache


def _case(cid, texts, *, changes=False, parallel_safe=None):
    steps = [Step(text=t, app="crm", changes_data=changes) for t in texts]
    c = PlannedCase(id=cid, title=cid, area="rewards", steps=steps, expected="ok")
    if parallel_safe is not None:
        c.parallel_safe = parallel_safe
    return c


class ParallelSafe(unittest.TestCase):
    def test_default_off(self):
        import os
        os.environ.pop("PARALLEL_READONLY", None)
        self.assertFalse(parallel_enabled())

    def test_pack_list_and_rewards_session_blocked(self):
        pack = {"parallel_safe_cases": ["E0", "B9", "C1", "C5", "C7"]}
        self.assertEqual(pack_safe_ids(pack), {"E0", "B9", "C1", "C5", "C7"})
        e0 = _case("E0", ["read Current Rewards on contact"])
        self.assertTrue(case_is_parallel_safe(e0, pack))
        b2 = _case("B2", ["apply savo rewards on ORDER-MULTI", "clear applied rewards"])
        self.assertFalse(case_is_parallel_safe(b2, pack))
        # Heuristic: mutating steps
        mut = _case("X1", ["create order"], changes=True)
        self.assertFalse(case_is_parallel_safe(mut, pack))

    def test_shared_placeholder_not_batched(self):
        import os
        os.environ["PARALLEL_READONLY"] = "1"
        try:
            pack = {"parallel_safe_cases": ["E0", "B9"],
                    "placeholders": {"token_pattern": r"(?i)\b(?:ORDER-[A-Z][A-Z0-9_-]*|CUSTOMER)\b"}}
            a = _case("E0", ["read wallet for CUSTOMER"])
            b = _case("B9", ["read ORDER-MULTI invoice"])  # different token — ok together
            c = _case("C1", ["read CUSTOMER contact"])     # shares CUSTOMER with E0
            batches = partition_parallel_batches([a, b, c], pack)
            # E0+B9 may share a batch; C1 must not sit with E0 (CUSTOMER).
            flat = [[x.id for x in batch] for batch in batches]
            for batch in flat:
                if "E0" in batch and "C1" in batch:
                    self.fail(f"CUSTOMER shared in {batch}")
        finally:
            os.environ.pop("PARALLEL_READONLY", None)

    def test_group_ids(self):
        import os
        os.environ["PARALLEL_READONLY"] = "1"
        try:
            pack = {"parallel_safe_cases": ["E0", "B9"]}
            cases = [_case("E0", ["read"]), _case("B9", ["read"]), _case("B2", ["apply rewards"])]
            plan = TestPlan(task_id=1, env="staging", cases=cases)
            g = group_ids(plan.cases, ["E0", "B9", "B2"], pack)
            self.assertTrue(any(set(b) == {"B2"} or b == ["B2"] for b in g))
        finally:
            os.environ.pop("PARALLEL_READONLY", None)


class PlanBatches(unittest.TestCase):
    def test_token_budget_sizing(self):
        ids = [f"T{i}" for i in range(12)]
        text = "\n".join(f"### T{i}\n" + ("step text " * 200) for i in range(12))
        batches = size_batches(text, ids, token_budget=500, max_count=10)
        self.assertGreater(len(batches), 1)
        self.assertEqual([x for b in batches for x in b], ids)

    def test_plan_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pack = SimpleNamespace(root=root, config={"slug": "demo", "name": "Demo",
                                                      "preapprovable_actions": [], "data_prep": []})
            (root / "pack.yaml").write_text("slug: demo\n")
            (root / "facts.md").write_text("# f\n")
            fp = plan_fingerprint("ticket body", ["E0", "B9"], pack)
            plan = TestPlan(task_id=9, env="staging",
                            cases=[_case("E0", ["read"])])
            save_plan_cache(root / "run1", fp, plan)
            loaded = load_cached_plan(root / "run1", fp)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.cases[0].id, "E0")
            self.assertIsNone(load_cached_plan(root / "run1", "wrong"))
            self.assertTrue(pack_version_hash(pack))


if __name__ == "__main__":
    unittest.main()
