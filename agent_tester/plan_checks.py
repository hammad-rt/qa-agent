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
        if c.type == CaseType.ADMIN_CONFIG and not _restored_later(plan, c):
            issues.append(f"{c.id}: admin case has no restore step (in it or in a later case)")
        if c.generated and not c.skip_reason and not c.expected.strip() and not c.open_question:
            issues.append(f"{c.id}: generated case has no expected result and no question for the user")
        if c.generated and c.expected.strip() and not c.expected_source:
            issues.append(f"{c.id}: generated case does not say where its expected result comes from")
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


def _restored_later(plan: TestPlan, case) -> bool:
    try:
        order = execution_order(plan)
    except RecursionError:
        order = [c.id for c in plan.cases]
    by_id = {c.id: c for c in plan.cases}
    later = order[order.index(case.id):] if case.id in order else [case.id]
    return any(s.restores for cid in later for s in by_id[cid].steps)


def autofix(plan: TestPlan, preapprovable: list[str] | None = None) -> TestPlan:
    """Fixed-code repairs the planner often misses:
    - settings changed but never restored: one labelled restore step after the last case that changes settings;
    - pre-approvals limited to what the pack allows (always-ask actions are asked each time)."""
    from .models import Step
    plan = plan.model_copy(deep=True)
    if preapprovable is not None:
        plan.preapprovable_actions = [a for a in plan.preapprovable_actions if a in preapprovable]
    order = execution_order(plan)
    by_id = {c.id: c for c in plan.cases}
    changers = [cid for cid in order
                if by_id[cid].type == CaseType.ADMIN_CONFIG or any(s.action_type == "change_settings" for s in by_id[cid].steps)]
    if changers and not any(s.restores for cid in order[order.index(changers[0]):] for s in by_id[cid].steps):
        last = by_id[changers[-1]]
        apps = [s.app for cid in changers for s in by_id[cid].steps if s.changes_data and s.app not in ("none", "redmine")]
        app = apps[-1] if apps else (last.steps[-1].app if last.steps else "none")
        last.steps.append(Step(
            text="Put every setting changed in this run back to its recorded original value, re-read it and call record_restored",
            app=app, changes_data=True, action_type="change_settings", restores=True, inferred=True,
            inferred_reason="settings changed by the run must be restored", inferred_source="core rule"))
    return plan


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
