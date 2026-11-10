"""Feature mode: plan every subtask (and related ticket) of a feature, merge the plans deterministically,
remove exact duplicate checks, and start with one Quick Repair and Rebuild."""
from __future__ import annotations

import re

from .models import PlannedCase, TestPlan


def sub_id(task_id: int, case_id: str) -> str:
    return f"{task_id}.{case_id}"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _sig(c: PlannedCase) -> tuple:
    """Two cases are the same check when title, steps and expected result match (ignoring case/punctuation)."""
    from .field_text import field_text
    return (
        _norm(field_text(c.title, sep=" ")),
        tuple(_norm(field_text(s, sep=" ")) for s in c.steps),
        _norm(field_text(c.expected, sep=" ")),
    )


def is_feature(bundles, feature_id: int) -> bool:
    """Feature mode when the walk found any work item besides the ticket itself."""
    return any(b.issue["id"] != feature_id for b in bundles)


def merge_plans(feature_id: int, env: str, plans: list[tuple[int, TestPlan]]) -> TestPlan:
    cases: list[PlannedCase] = []
    by_sig: dict[tuple, PlannedCase] = {}
    alias: dict[str, str] = {}                    # merged-away id -> kept id
    reqs, seen_req = [], set()
    conflicts, data, pre, base = [], [], [], []
    for tid, p in plans:
        local = {c.id for c in p.cases}
        for c in p.cases:
            new_id = sub_id(tid, c.id)
            deps = [sub_id(tid, d) for d in c.depends_on if d in local]
            nc = c.model_copy(update={"id": new_id, "depends_on": deps, "source_task": tid, "covers": [tid]})
            k = _sig(nc)
            if k in by_sig and not nc.depends_on and not by_sig[k].depends_on:
                kept = by_sig[k]
                if tid not in kept.covers:
                    kept.covers.append(tid)
                alias[new_id] = kept.id
                continue
            by_sig[k] = nc
            cases.append(nc)
        for r in p.requirements:
            if r.key not in seen_req:
                seen_req.add(r.key)
                reqs.append(r)
        conflicts += [f"#{tid}: {x}" for x in p.global_conflicts]
        data += [f"#{tid}: {x}" for x in p.data_to_create]
        pre += [a for a in p.preapprovable_actions if a not in pre]
        base += p.baseline_steps
    for c in cases:
        c.depends_on = list(dict.fromkeys(alias.get(d, d) for d in c.depends_on))
    return TestPlan(task_id=feature_id, env=env, cases_source="task", cases=cases, requirements=reqs,
                    global_conflicts=conflicts, data_to_create=data, preapprovable_actions=pre, baseline_steps=base)


def summary(bundles, plan: TestPlan) -> list[str]:
    lines = []
    for b in bundles:
        tid = b.issue["id"]
        n = sum(1 for c in plan.cases if c.source_task == tid)
        shared = sum(1 for c in plan.cases if tid in c.covers and c.source_task != tid)
        src = "its Test Cases task" if b.test_case_task else "written from its description (generated)"
        lines.append(f"#{tid} ({b.via}) {b.issue.get('subject', '')[:70]}: {n} case(s) from {src}"
                     + (f", plus {shared} shared with another subtask" if shared else ""))
    return lines


async def plan_feature(settings, pack, bundles, feature_id: int, env: str, prefix: str, **kw) -> TestPlan:
    """Plan each work item with the normal planner (its own Test Cases, or generated from its text), then merge."""
    from .agents.planner import plan_task
    from .casegen import add_repair_case, needs_generation
    plans = []
    for b in bundles:
        tid = b.issue["id"]
        gen = needs_generation(b, prefix)
        print(f"Planning #{tid} ({b.via}): " + ("writing cases from its description" if gen else "from its Test Cases task"))
        p = await plan_task(settings, pack, b.all_text(), tid, env, generate=gen, repair=False, **kw)
        plans.append((tid, p))
    return add_repair_case(merge_plans(feature_id, env, plans), pack)
