"""Phase 8: test cases written from the task text when a task has no "Test Cases" child.

The planner writes the cases (judgment). Fixed code here: marks them generated, checks that every expected
result quotes a real source, flags uncovered limits, asks the user what the task does not say (one batch),
lets the user drop or add cases at the plan gate, and posts the cases back to Redmine only after approval."""
from __future__ import annotations

import re

from .models import PlannedCase, Step, TestPlan
from .redact import redact


def needs_generation(bundle, prefix: str = "Test Cases - ") -> bool:
    return bundle.test_case_task is None and not bundle.issue.get("subject", "").startswith(prefix.strip())


def mark_generated(plan: TestPlan) -> TestPlan:
    cases = [c.model_copy(update={"generated": True}) for c in plan.cases]
    return plan.model_copy(update={"cases": cases, "cases_source": "generated"})


def task_sources(bundle, facts: str = "") -> dict[str, str]:
    """Named texts an expected result may quote."""
    src = {"description": (bundle.issue.get("subject", "") + "\n" + (bundle.issue.get("description") or ""))}
    if bundle.parent:
        src["parent"] = bundle.parent.get("description") or ""
    for i, c in enumerate(bundle.comments, 1):
        src[f"comment {i}"] = c
    if facts:
        src["facts.md"] = facts
    return src


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s%.]", " ", (t or "").lower())).strip()


_QUOTE = re.compile(r"[\"\u201c\u201d']([^\"\u201c\u201d]{6,})[\"\u201c\u201d']")


def check_generated(plan: TestPlan, sources: dict[str, str]) -> list[str]:
    """Every generated expected result must quote text that really is in the task or facts (or be the user's)."""
    issues = []
    all_text = _norm(" ".join(sources.values()))
    for c in plan.cases:
        if not c.generated or c.skip_reason or not c.expected.strip():
            continue
        src = (c.expected_source or "").strip()
        if not src:
            continue                                   # check_plan already reports it
        if src.lower().startswith("user"):
            continue
        name = src.split(":", 1)[0].strip().lower()
        m = _QUOTE.search(src)
        if not m:
            issues.append(f"{c.id}: expected source has no quote from the task ({src[:60]})")
            continue
        quote = _norm(m.group(1))
        text = _norm(sources.get(name, "")) if name in sources else all_text
        if quote not in text:
            issues.append(f"{c.id}: quoted source not found in {name if name in sources else 'the task'}: \"{m.group(1)[:60]}\"")
    return issues


_LIMIT = re.compile(r"(?:more than|less than|over|under|above|below|at least|at most|minimum|maximum|min|max|up to|"
                    r"limit|threshold|exceeds?|greater than|fewer than)\s+(?:of\s+)?[$€£]?(\d+(?:\.\d+)?)", re.I)


def coverage_notes(plan: TestPlan, task_text: str) -> list[str]:
    """Soft hints shown at the gate: limits in the task with no case touching the value or its neighbour."""
    case_text = " ".join(c.title + " " + c.expected + " " + " ".join(s.text for s in c.steps) for c in plan.cases)
    nums = set(re.findall(r"\d+(?:\.\d+)?", case_text))
    notes = []
    for v in dict.fromkeys(_LIMIT.findall(task_text or "")):
        n = float(v)
        near = {v, f"{n + 1:g}", f"{n - 1:g}", f"{n + 0.01:g}", f"{n - 0.01:g}"}
        if not (near & nums):
            notes.append(f"the task mentions a limit of {v}, but no case tests {v} or the value next to it")
    if plan.cases and not any(re.search(r"invalid|wrong|empty|missing|negative|error|not allowed|reject", c.title + c.expected, re.I)
                              for c in plan.cases):
        notes.append("no case checks invalid or missing input")
    return notes


def ask_case_questions(plan: TestPlan, input_fn=input, out=print) -> list[dict]:
    """One batch: what should happen, for generated cases the task is silent on. Blank answer skips the case."""
    open_cases = [c for c in plan.cases if c.open_question and not c.skip_reason]
    if not open_cases:
        return []
    out("\nThe task doesn't say what should happen in these cases (blank skips the case):")
    answers = []
    for c in open_cases:
        q = c.open_question
        out(f"  {c.id} {c.title}: {q}")
        a = input_fn("     > ").strip()
        if a:
            c.expected, c.expected_source, c.open_question = a, "user answer at the plan gate", None
        else:
            c.skip_reason = "expected result not stated in the task and not answered"
        answers.append({"case": c.id, "question": q, "answer": a})
    return answers


def edit_cases(plan: TestPlan, input_fn=input, out=print) -> TestPlan:
    """Drop cases (and cases that depend on them) or add one-line cases, before the run is approved.
    New ids never reuse a dropped id."""
    n = 1 + max([int(c.id[1:]) for c in plan.cases if re.fullmatch(r"G\d+", c.id)] or [0])
    drop = {x.strip() for x in input_fn("\nDrop any cases? (ids, comma separated; blank for none) ").replace(" ", ",").split(",") if x.strip()}
    if drop:
        unknown = drop - {c.id for c in plan.cases}
        if unknown:
            out("  Not in the plan, ignored: " + ", ".join(sorted(unknown)))
        changed = True
        while changed:
            changed = False
            for c in plan.cases:
                if c.id not in drop and set(c.depends_on) & drop:
                    drop.add(c.id)
                    changed = True
                    out(f"  {c.id} depends on a dropped case, so it is dropped too.")
        plan = plan.model_copy(update={"cases": [c for c in plan.cases if c.id not in drop]})
    while True:
        what = input_fn("Add a case? Describe it in one line (blank to finish) ").strip()
        if not what:
            break
        exp = input_fn("  Expected result? ").strip()
        plan.cases.append(PlannedCase(id=f"G{n}", title=what, steps=[Step(text=what, app="none")], expected=exp,
                                      generated=True, expected_source="user at the plan gate" if exp else None,
                                      skip_reason=None if exp else "no expected result given"))
        out(f"  Added G{n}.")
        n += 1
    return plan


def to_textile(plan: TestPlan, subject: str = "") -> str:
    """Redmine body for a "Test Cases" child task. No values from the run, no secrets."""
    out = [f"Test cases written by qa-agent from the task description{f' ({subject})' if subject else ''} "
           "and reviewed at the plan gate.", ""]
    for c in plan.cases:
        if c.skip_reason and not c.expected:
            continue
        out += [f"h3. {c.id}: {c.title}", ""]
        if c.preconditions:
            out += ["*Preconditions:* " + "; ".join(c.preconditions), ""]
        out += [f"# {s.text}" for s in c.steps if not s.inferred] or ["# (see title)"]
        out += ["", f"*Expected:* {c.expected}", f"_Source: {c.expected_source or 'n/a'}_", ""]
    return redact("\n".join(out))


def post_cases(state, rm, policy, project_id: int, subject: str) -> int | None:
    """Create the "Test Cases - <subject>" child task. `redmine_post` is always-ask, so this prompts every time."""
    plan = state.plan
    if plan is None or plan.cases_source != "generated":
        raise ValueError("this run's cases came from the task, nothing to post")
    if state.data.get("cases_posted"):
        return state.data["cases_posted"]
    policy.gate(f"post {len(plan.cases)} generated test case(s) to Redmine under #{plan.task_id}",
                "generated test cases", action_type="redmine_post")
    new_id = rm.create_issue(project_id, f"Test Cases - {subject}"[:255], to_textile(plan, subject), parent_id=plan.task_id)
    state.data["cases_posted"] = new_id
    state.save()
    return new_id
