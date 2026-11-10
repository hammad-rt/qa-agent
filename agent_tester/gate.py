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


def ask_batch(reqs, ctx, input_fn=None, secret_fn=None, write_secret=None, out=print) -> None:
    """Ask open requirements in one batch.

    When the caller passes an explicit ``input_fn`` (tests, web adapters), that function is
    always used — including for account questions. Real automatic runs leave ``input_fn`` as
    None so account questions still go to a person via ``ask_line`` (stdin / web UI).
    """
    from .config import write_env_secret
    from .auto import prompter, is_auto
    from .auto import secret_input, ask_line
    injected = input_fn is not None
    input_fn = input_fn or prompter("")
    secret_fn = secret_fn or secret_input
    qs = G.open_questions(reqs)
    if not qs:
        return
    out("\nI need a few things before I start:")
    answers = {}
    for n, r in enumerate(qs, 1):
        if r.kind == "account" and r.value and r.default is None:
            out(f"  {n}. {r.question}")
            answers[r.key] = ""          # only the masked password is needed
            continue
        # Real auto runs: always ask a person for the test user (stdin/web). Injected
        # input_fn (unit tests / adapters) must not be bypassed.
        ask = ask_line if (r.kind == "account" and is_auto() and not injected) else input_fn
        answers[r.key] = ask(f"  {n}. {r.question}\n     > ")
    G.apply_answers(reqs, answers, ctx, secret_prompt=secret_fn, write_secret=write_secret or write_env_secret)


def choose_env(pack, input_fn=None, out=print) -> str | None:
    """Ask which environment to test on. Enter = staging. Production needs an explicit yes."""
    from .auto import prompter
    input_fn = input_fn or prompter("")
    from .preflight.gap_resolver import normalize_env
    known = {e for a in pack.apps.values() for e in a.get("envs", {})}
    out("\nWhich environment should I test on?")
    for n, (e, name) in enumerate([("staging", "staging"), ("preprod", "pre-prod"), ("prod", "production"), ("local", "local (your own computer)")], 1):
        out(f"  {n}. {name}" + ("" if e in known else "  (no URLs on record yet: I'll ask for them and save them)"))
    while True:
        raw = input_fn("  > [Enter = staging] ").strip()
        env = normalize_env(raw) if raw else "staging"
        if env:
            break
        out("  Please answer 1-4, staging, pre-prod, production or local.")
    if env == "prod" and not confirm_prod(input_fn, out):
        return None
    return env


def confirm_prod(input_fn=None, out=print) -> bool:
    from .auto import prompter
    input_fn = input_fn or prompter("y")
    out("Production: I'll only read, and I'll ask you before every action that changes data. Nothing can be"
        " pre-approved, no real payments unless you say so, and any setting I change I put back.")
    return input_fn("  Test on production? [y/N] ").strip().lower() in ("y", "yes")


def console_approver(input_fn=None, out=print):
    from .auto import ask_line
    input_fn = input_fn or ask_line
    def approve(action: str, reason: str, details: dict) -> bool:
        out(f"\nAPPROVAL NEEDED ({reason}): {action}")
        return input_fn("  Allow? [y/N] ").strip().lower() in ("y", "yes")
    return approve


def show_data_plan(rows: list[dict] | None, out=print) -> None:
    """Short table of placeholders: find-or-create, and each create part's app + what it does."""
    from .placeholders import format_data_plan
    if not rows:
        return
    out("\n" + format_data_plan(rows))


def correct_data_plan(rows: list[dict], input_fn=None, out=print) -> dict:
    """Optional interactive corrections to create parts for this run only.

    Returns {TOKEN: {"create": [{"app", "text"}, ...]}} overlays. Empty in automatic mode.
    """
    from .auto import is_auto, prompter
    if not rows or is_auto():
        return {}
    input_fn = input_fn or prompter("")
    out("\nCorrect any create part? Enter a token (e.g. ORDER-MULTI), or press Enter to keep the plan.")
    corrections: dict = {}
    while True:
        tok = input_fn("  Token to edit (Enter = done): ").strip()
        if not tok:
            break
        row = next((r for r in rows if str(r.get("token") or "").upper() == tok.upper()), None)
        if row is None or row.get("missing"):
            out(f"  No placeholder {tok} in this plan.")
            continue
        parts = list(row.get("create") or [])
        out(f"  Current create parts for {row['token']}:")
        for i, p in enumerate(parts, 1):
            out(f"    {i}. [{p.get('app')}] {(p.get('text') or '')[:120]}")
        raw = input_fn("  New parts as app|text lines (blank line ends; Enter alone keeps current):\n  > ").rstrip()
        if not raw:
            continue
        new_parts = []
        line = raw
        while line:
            if "|" in line:
                app, text = line.split("|", 1)
                new_parts.append({"app": app.strip(), "text": text.strip()})
            else:
                out("  Use app|text (example: portal|Create a shipping-only order...)")
            line = input_fn("  > ").rstrip()
        if new_parts:
            corrections[row["token"].upper()] = {"create": new_parts}
            row["create"] = new_parts
            out(f"  Updated {row['token']} for this run.")
    return corrections


def plan_gate(plan: TestPlan, input_fn=None, out=print, policy=None, issues: list[str] | None = None,
              data_plan: list[dict] | None = None) -> tuple[bool, list[str], dict]:
    """Returns (approved, action types pre-approved for this run, data_plan_corrections).

    Always-ask actions (posting, email, prod writes) are never offered for pre-approval; they are
    asked each time. change_settings may be in the pre-approve batch when the pack lists it and
    env != prod. Data plan is shown (and optionally corrected) before the start prompt.
    """
    from .auto import prompter, is_auto
    input_fn = input_fn or prompter("y")
    show_data_plan(data_plan, out=out)
    corrections = {}
    if data_plan and not is_auto():
        corrections = correct_data_plan(data_plan, input_fn=input_fn, out=out)
    elif data_plan and is_auto():
        out("  (automatic: data plan logged; no corrections)")
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
    return ok, pre, corrections
