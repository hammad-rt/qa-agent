"""Deterministic plan validation before the approval gate (tech spec section 5)."""
from __future__ import annotations

from .models import CaseType, TestPlan


def check_plan(plan: TestPlan) -> list[str]:
    issues: list[str] = []
    ids = {c.id for c in plan.cases}

    # dependencies exist and have no cycles
    graph = {c.id: [d for d in c.depends_on] for c in plan.cases}
    for c in plan.cases:
        for d in c.depends_on:
            if d not in ids:
                issues.append(f"{c.id}: depends on unknown case {d}")
    state: dict[str, int] = {}

    def visit(n, path):
        if state.get(n) == 1:
            issues.append(f"dependency cycle: {' -> '.join(path + [n])}")
            return
        if state.get(n) == 2:
            return
        state[n] = 1
        for m in graph.get(n, []):
            if m in graph:
                visit(m, path + [n])
        state[n] = 2

    for n in graph:
        visit(n, [])

    for c in plan.cases:
        for i, s in enumerate(c.steps):
            if plan.env == "prod" and s.changes_data and not s.needs_approval:
                issues.append(f"{c.id} step {i+1}: changes prod data without approval")
            if (s.changes_data or s.costs_money) and not (s.needs_approval or s.action_type):
                issues.append(f"{c.id} step {i+1}: changes data/money but has no approval or action type")
            if s.inferred and not (s.inferred_reason and s.inferred_source):
                issues.append(f"{c.id} step {i+1}: inferred step without reason and source")
        if c.type == CaseType.ADMIN_CONFIG and not any(s.restores for s in c.steps):
            issues.append(f"{c.id}: admin case has no restore step")
        if c.type == CaseType.NOT_UI and not c.skip_reason:
            issues.append(f"{c.id}: non-UI case needs a skip_reason")

    # irreversible steps last on each record, across the whole run order
    order = execution_order(plan) if not any("cycle" in i for i in issues) else [c.id for c in plan.cases]
    by_id = {c.id: c for c in plan.cases}
    seq = [(cid, i, s) for cid in order for i, s in enumerate(by_id[cid].steps)]
    for k, (cid, i, s) in enumerate(seq):
        if s.irreversible and s.record:
            later = [(c2, j) for c2, j, s2 in seq[k + 1:] if s2.record == s.record]
            if later:
                c2, j = later[0]
                issues.append(f"{cid} step {i+1}: irreversible on {s.record} but {c2} step {j+1} uses it later (use a fresh record or reorder)")

    for r in plan.requirements:
        if r.status != "resolved" and not r.question:
            issues.append(f"requirement {r.key}: not resolved and no question for the user")
    return issues


def execution_order(plan: TestPlan) -> list[str]:
    """Topological order that keeps the planner's order where possible."""
    done, out = set(), []
    by_id = {c.id: c for c in plan.cases}

    def add(cid, guard=()):
        if cid in done or cid in guard or cid not in by_id:
            return
        for d in by_id[cid].depends_on:
            add(d, guard + (cid,))
        done.add(cid)
        out.append(cid)

    for c in plan.cases:
        add(c.id)
    return out
