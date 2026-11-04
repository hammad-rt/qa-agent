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


def ask_case_questions(plan: TestPlan, input_fn=None, out=print) -> list[dict]:
    """One batch: what should happen, for generated cases the task is silent on. Blank answer skips the case."""
    from .auto import prompter
    input_fn = input_fn or prompter("")
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


def edit_cases(plan: TestPlan, input_fn=None, out=print) -> TestPlan:
    """Drop cases (and cases that depend on them) or add one-line cases, before the run is approved.
    New ids never reuse a dropped id."""
    from .auto import prompter
    input_fn = input_fn or prompter("")
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


# ---- Quick Repair and Rebuild first (SuiteCRM / SugarCRM) --------------------------------------------
_CRM_STACK = re.compile(r"suite\s*crm|sugar\s*crm", re.I)
_QRR = re.compile(r"quick\s*repair", re.I)
REPAIR_ID = "TC1"
HARD_RELOAD = "Hard-reload each page (bypass the cache) before testing it, after TC1's repair"


def crm_app(pack) -> str | None:
    """The pack app that runs on SuiteCRM/SugarCRM (pack.yaml `stack:`), if any."""
    for app, st in (((getattr(pack, "config", None) or {}).get("stack")) or {}).items():
        name = st.get("name", "") if isinstance(st, dict) else str(st)
        if _CRM_STACK.search(name or ""):
            return app
    return None


def _renumber(cid: str, shift: dict) -> str:
    return shift.get(cid, cid)


def add_repair_case(plan: TestPlan, pack, extensions_changed: bool = False) -> TestPlan:
    """Generated cases on a SuiteCRM/SugarCRM project always start with Admin -> Repair -> Quick Repair and
    Rebuild (same rule as redmine-task-authoring), so stale Extension/layout/vardef caches don't fail the run.
    Generated cases are renumbered TC2.. and depend on TC1. Cases from a Redmine Test Cases task keep their ids;
    the repair is added as TC0 with inferred (labelled) steps and every case depends on it. Idempotent."""
    app = crm_app(pack)
    if not app or not plan.cases or any(_QRR.search(c.title + " " + " ".join(x.text for x in c.steps)) for c in plan.cases):
        return plan
    from_task = plan.cases_source == "task"    # a Redmine Test Cases child: keep its ids, add TC0 in front
    rid = "TC0" if from_task else REPAIR_ID
    shift = {}
    for i, c in enumerate([] if from_task else plan.cases, 2):
        m = re.fullmatch(r"TC(\d+)", c.id)
        if m:
            shift[c.id] = f"TC{int(m.group(1)) + 1}"
    rest = []
    for c in plan.cases:
        deps = [_renumber(d, shift) for d in c.depends_on]
        if rid not in deps:
            deps = [rid] + deps
        pre = list(c.preconditions)
        if HARD_RELOAD not in pre:
            pre = [HARD_RELOAD] + pre
        rest.append(c.model_copy(update={"id": _renumber(c.id, shift), "depends_on": deps, "preconditions": pre}))
    what = "Quick Repair and Rebuild" + (" (and Rebuild Extensions)" if extensions_changed else "")
    repair = PlannedCase(
        id=REPAIR_ID, title=f"Run {what} on the target environment", area="admin", generated=True,
        preconditions=["Logged in to the CRM as an admin user"],
        steps=[Step(text="Open Admin from the user menu, then Repair (find the links on the page, don't type URLs)", app=app),
               Step(text=f"Click {what} and wait until the page shows the repair finished", app=app,
                    action_type="admin_repair", needs_approval=True),
               Step(text="If the page lists database differences (SQL) with an Execute button, these are the "
                         "queries that add the new fields to the database: read the full SQL from the page, pass it "
                         "to check_repair_sql, ask with request_approval using the action_type it returns, and click "
                         "Execute only when approved. Then run Quick Repair and Rebuild again and confirm no "
                         "differences are listed. If nothing is listed, note that and go on", app=app,
                    needs_approval=True),
               Step(text="Hard-reload the screens the later cases use (bypass the cache) so they load the rebuilt "
                         "layouts and vardefs", app=app)],
        expected="The repair completes without errors; every later case assumes this passed",
        expected_source="user: redmine-task-authoring rule, Quick Repair and Rebuild is always TC1",
        values_to_record=["repair_result", "repair_sql"])
    if from_task:                               # not in the task's cases: label it as added by the agent
        why = "the Test Cases task has no Quick Repair and Rebuild step; SuiteCRM/SugarCRM caches go stale after a deploy"
        repair = repair.model_copy(update={
            "id": rid, "generated": False,
            "steps": [st.model_copy(update={"inferred": True, "inferred_reason": why,
                                            "inferred_source": "redmine-task-authoring rule"}) for st in repair.steps]})
    return plan.model_copy(update={"cases": [repair] + rest})
