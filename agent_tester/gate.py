"""Console plan gate and approvals. One batch of questions, then approve / pre-approve / stop."""
from __future__ import annotations

import getpass

from .models import TestPlan
from .preflight import gap_resolver as G


def show_plan(plan: TestPlan, issues: list[str], out=print) -> None:
    out(f"\nPlan for #{plan.task_id} on {plan.env}: {len(plan.cases)} case(s)")
    for c in plan.cases:
        out(f"  {c.id} [{c.type.value}] {c.title}" + (f"  (skip: {c.skip_reason})" if c.skip_reason else ""))
        for i, s in enumerate(c.steps, 1):
            tags = [t for t, on in (("inferred", s.inferred), ("changes data", s.changes_data),
                                    ("pays", s.costs_money), ("irreversible", s.irreversible)) if on]
            out(f"     {i}. [{s.app}] {s.text}" + (f"  <{', '.join(tags)}>" if tags else ""))
            if s.inferred:
                out(f"        why: {s.inferred_reason} (source: {s.inferred_source})")
        out(f"     expected: {c.expected}")
    if issues:
        out("\nPlan check problems:")
        for i in issues:
            out(f"  - {i}")


def ask_batch(reqs, ctx, input_fn=input, secret_fn=getpass.getpass, write_secret=None, out=print) -> None:
    from .config import write_env_secret
    qs = G.open_questions(reqs)
    if not qs:
        return
    out("\nI need a few things before I start:")
    answers = {}
    for n, r in enumerate(qs, 1):
        out(f"  {n}. {r.question}")
        if r.kind == "account" and r.value:
            answers[r.key] = ""          # only the masked password is needed
            continue
        answers[r.key] = input_fn(f"     > ")
    G.apply_answers(reqs, answers, ctx, secret_prompt=secret_fn, write_secret=write_secret or write_env_secret)


def console_approver(input_fn=input, out=print):
    def approve(action: str, reason: str, details: dict) -> bool:
        out(f"\nAPPROVAL NEEDED ({reason}): {action}")
        return input_fn("  Allow? [y/N] ").strip().lower() in ("y", "yes")
    return approve


def plan_gate(plan: TestPlan, input_fn=input, out=print) -> tuple[bool, list[str]]:
    """Returns (approved, action types pre-approved for this run)."""
    pre = []
    if plan.preapprovable_actions:
        out("\nThis run repeats these actions: " + ", ".join(plan.preapprovable_actions))
        a = input_fn("  Pre-approve them for this run? [y/N] ").strip().lower()
        if a in ("y", "yes"):
            pre = list(plan.preapprovable_actions)
    ok = input_fn("\nStart the run? [y/N] ").strip().lower() in ("y", "yes")
    return ok, pre
