"""Console plan gate and approvals. One batch of questions, then approve / pre-approve / stop."""
from __future__ import annotations

import getpass

from .models import TestPlan
from .preflight import gap_resolver as G


def show_plan(plan: TestPlan, issues: list[str], out=print) -> None:
    out(f"\nPlan for #{plan.task_id} on {plan.env}: {len(plan.cases)} case(s)")
    if plan.cases_source == "generated":
        out("  The task has no test cases, so I wrote these from its description. Check them before approving.")
    for c in plan.cases:
        out(f"  {c.id} [{c.type.value}]{' [generated]' if c.generated else ''} {c.title}" + (f"  (skip: {c.skip_reason})" if c.skip_reason else ""))
        for i, s in enumerate(c.steps, 1):
            tags = [t for t, on in (("inferred", s.inferred), ("changes data", s.changes_data),
                                    ("pays", s.costs_money), ("irreversible", s.irreversible)) if on]
            out(f"     {i}. [{s.app}] {s.text}" + (f"  <{', '.join(tags)}>" if tags else ""))
            if s.inferred:
                out(f"        why: {s.inferred_reason} (source: {s.inferred_source})")
        out(f"     expected: {c.expected or '(not stated in the task)'}" + (f"  [source: {c.expected_source}]" if c.expected_source else ""))
        if c.open_question:
            out(f"     question: {c.open_question}")
    if issues:
        out("\nPlan check problems:")
        for i in issues:
            out(f"  - {i}")


def ask_batch(reqs, ctx, input_fn=None, secret_fn=getpass.getpass, write_secret=None, out=print) -> None:
    from .config import write_env_secret
    from .auto import prompter
    input_fn = input_fn or prompter("")
    qs = G.open_questions(reqs)
    if not qs:
        return
    out("\nI need a few things before I start:")
    answers = {}
    for n, r in enumerate(qs, 1):
        out(f"  {n}. {r.question}")
        if r.kind == "account" and r.value and r.default is None:
            answers[r.key] = ""          # only the masked password is needed
            continue
        answers[r.key] = input_fn(f"     > ")
    G.apply_answers(reqs, answers, ctx, secret_prompt=secret_fn, write_secret=write_secret or write_env_secret)


def choose_env(pack, input_fn=None, out=print) -> str | None:
    """Ask which environment to test on. Enter = staging. Production needs an explicit yes."""
    from .auto import prompter
    input_fn = input_fn or prompter("")
    from .preflight.gap_resolver import normalize_env
    known = {e for a in pack.apps.values() for e in a.get("envs", {})}
    out("\nWhich environment should I test on?")
    for n, (e, name) in enumerate([("staging", "staging"), ("preprod", "pre-prod"), ("prod", "production")], 1):
        out(f"  {n}. {name}" + ("" if e in known else "  (no URLs on record yet: I'll ask for them and save them)"))
    while True:
        raw = input_fn("  > [Enter = staging] ").strip()
        env = normalize_env(raw) if raw else "staging"
        if env:
            break
        out("  Please answer 1, 2, 3, staging, pre-prod or production.")
    if env == "prod" and not confirm_prod(input_fn, out):
        return None
    return env


def confirm_prod(input_fn=None, out=print) -> bool:
    from .auto import prompter
    input_fn = input_fn or prompter("n")
    out("Production: I'll only read, and I'll ask you before every action that changes data. Nothing can be"
        " pre-approved, no real payments unless you say so, and any setting I change I put back.")
    return input_fn("  Test on production? [y/N] ").strip().lower() in ("y", "yes")


def console_approver(input_fn=input, out=print):
    def approve(action: str, reason: str, details: dict) -> bool:
        out(f"\nAPPROVAL NEEDED ({reason}): {action}")
        return input_fn("  Allow? [y/N] ").strip().lower() in ("y", "yes")
    return approve


def plan_gate(plan: TestPlan, input_fn=None, out=print, policy=None, issues: list[str] | None = None) -> tuple[bool, list[str]]:
    """Returns (approved, action types pre-approved for this run). Always-ask actions (posting,
    email, prod writes) are never offered for pre-approval; they are asked each time.
    change_settings may be in the pre-approve batch when the pack lists it and env != prod."""
    from .auto import prompter
    input_fn = input_fn or prompter("y")
    pre = []
    allowed = list(plan.preapprovable_actions)
    ask_each = []
    if policy is not None:
        ask_each = [a for a in allowed if not policy.can_preapprove(a)]
        allowed = [a for a in allowed if a not in ask_each]
    if allowed:
        out("\nThis run repeats these actions: " + ", ".join(allowed))
        a = input_fn("  Pre-approve them for this run? [y/N] ").strip().lower()
        if a in ("y", "yes"):
            pre = allowed
    if ask_each:
        out("I'll ask you before each of these: " + ", ".join(ask_each) + ".")
    blocking = [i for i in (issues or []) if not i.startswith("requirement ")]
    if blocking:
        out("\nThe plan still has problems (listed above).")
        ok = input_fn("Start the run anyway? [y/N] ").strip().lower() in ("y", "yes")
    else:
        ok = input_fn("\nStart the run? [y/N] ").strip().lower() in ("y", "yes")
    return ok, pre
