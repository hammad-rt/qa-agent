"""Fixed-code coordinator: runs one case at a time, hands app turns to testers, sends evidence to the judge.

Testers and the judge are injected callables so this file has no LLM dependency:
  tester(app, brief: dict, state: RunState) -> StepResult
  judge(brief: dict, actions: list[dict], results: list[StepResult], state: RunState) -> CaseResult
"""
from __future__ import annotations

from typing import Callable

from .models import CaseResult, PlannedCase, StepResult, TestPlan
from .plan_checks import execution_order
from .run_state import RunState

Tester = Callable[[str, dict, RunState], StepResult]
Judge = Callable[[dict, list, list, RunState], CaseResult]


class RunPaused(RuntimeError):
    """LLM or browser kept failing; state is checkpointed and `resume` continues from here."""


def app_turns(case: PlannedCase) -> list[tuple[str, list[int]]]:
    """Group consecutive steps by app: [('portal',[0,1]), ('crm',[2]), ('portal',[3])]."""
    turns: list[tuple[str, list[int]]] = []
    for i, s in enumerate(case.steps):
        app = s.app or "none"
        if turns and turns[-1][0] == app:
            turns[-1][1].append(i)
        else:
            turns.append((app, [i]))
    return turns


def brief_for(plan: TestPlan, case: PlannedCase, steps: list[int], state: RunState) -> dict:
    """What a tester or the judge sees: the case, its steps, values so far. Never another agent's reasoning."""
    return {
        "task_id": plan.task_id, "env": plan.env, "case_id": case.id, "title": case.title,
        "type": case.type.value, "area": case.area, "expected": case.expected, "preconditions": case.preconditions,
        "generated": case.generated, "expected_source": case.expected_source,
        "evidence_required": case.evidence_required, "values_to_record": case.values_to_record,
        "steps": [{"n": i + 1, **case.steps[i].model_dump(mode="json")} for i in steps],
        "values_so_far": {k: v["value"] if isinstance(v, dict) else v for k, v in state.values.items()},
        "requirements": {r.key: {"value": r.value, "secret_env": r.secret_env} for r in plan.requirements},
    }


def read_snapshot(plan: TestPlan, state: RunState, testers: dict[str, Tester], label: str) -> dict:
    """Baseline (before the first case) or final (after the last) read-only values (tech spec section 8)."""
    if not plan.baseline_steps or state.data.get(label):
        return state.data.get(label, {})
    pseudo = PlannedCase(id=label.upper(), title=f"{label} values", steps=plan.baseline_steps)
    got: dict = {}
    for app, steps in app_turns(pseudo):
        if app in ("none", "redmine") or app not in testers:
            continue
        brief = brief_for(plan, pseudo, steps, state)
        brief["read_only"] = True
        try:
            sr = testers[app](app, brief, state)
            got.update(sr.values)
        except Exception as e:  # noqa: BLE001
            state.data["notes"].append(f"{label} read on {app} failed: {e}")
    state.data[label] = got
    state.checkpoint(f"{label} values recorded ({len(got)})")
    return got


def run_plan(plan: TestPlan, state: RunState, testers: dict[str, Tester], judge: Judge,
             on_case_done: Callable[[CaseResult], None] | None = None, max_retries: int = 1) -> dict[str, CaseResult]:
    state.checkpoint("run started", status="running")
    read_snapshot(plan, state, testers, "baseline")
    by_id = {c.id: c for c in plan.cases}
    for cid in execution_order(plan):
        if state.result(cid):
            continue                                  # resume: already done
        case = by_id[cid]
        result = _run_case(plan, case, state, testers, judge, max_retries)
        state.set_result(result)
        if on_case_done:
            on_case_done(result)
    read_snapshot(plan, state, testers, "final")
    state.checkpoint("run finished", status="finished")
    return {cid: state.result(cid) for cid in execution_order(plan)}


def _run_case(plan, case, state, testers, judge, max_retries) -> CaseResult:
    if case.skip_reason:
        return CaseResult(case_id=case.id, verdict="SKIPPED", expected=case.expected, note=case.skip_reason)
    for dep in case.depends_on:
        r = state.result(dep)
        if r is None or r.verdict not in ("PASS", "PARTIAL"):
            return CaseResult(case_id=case.id, verdict="BLOCKED", expected=case.expected,
                              note=f"depends on {dep} ({r.verdict if r else 'not run'})")
    results: list[StepResult] = []
    for app, steps in app_turns(case):
        if app in ("none", "redmine"):
            continue
        tester = testers.get(app)
        if tester is None:
            return CaseResult(case_id=case.id, verdict="BLOCKED", expected=case.expected, note=f"no tester for app '{app}'")
        brief = brief_for(plan, case, steps, state)
        attempt = 0
        while True:
            log_mark = len(state.data["action_log"])
            try:
                sr = tester(app, brief, state)
                break
            except RunPaused:
                raise
            except Exception as e:                    # noqa: BLE001  model/browser hiccup
                attempt += 1
                changed = [a for a in state.data["action_log"][log_mark:] if a.get("approval") in ("approved", "pre_approved")]
                if changed:                           # never replay a turn that already changed data
                    state.checkpoint(f"paused in {case.id} ({app}) after a data-changing step: {type(e).__name__}: {e}", status="paused")
                    raise RunPaused(f"{case.id}: failed after a data-changing step; check the record before resuming") from e
                if attempt > max_retries:
                    state.checkpoint(f"paused in {case.id} ({app}): {type(e).__name__}: {e}", status="paused")
                    raise RunPaused(f"{case.id}: {e}") from e
        results.append(sr)
        for k, v in sr.values.items():
            state.values.setdefault(k, {"value": v, "source": f"{case.id}/{app}"})
        if sr.stuck or sr.limit_hit:
            why = "hit the step/time limit" if sr.limit_hit else "got stuck"
            return CaseResult(case_id=case.id, verdict="AGENT INCOMPLETE", expected=case.expected,
                              values={k: v for r in results for k, v in r.values.items()},
                              evidence=[s for r in results for s in r.screenshots],
                              note=f"tester on {app} {why}: {sr.notes}".strip())
    full_brief = brief_for(plan, case, list(range(len(case.steps))), state)
    try:
        return judge(full_brief, state.actions_for(case.id), results, state)
    except Exception as e:                        # noqa: BLE001  judge output unusable; keep the run going
        return CaseResult(case_id=case.id, verdict="AGENT INCOMPLETE", expected=case.expected,
                          values={k: v for r in results for k, v in r.values.items()},
                          evidence=[s for r in results for s in r.screenshots],
                          note=f"the judge could not decide: {type(e).__name__}: {str(e)[:300]}")
