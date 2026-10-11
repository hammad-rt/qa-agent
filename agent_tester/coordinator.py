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


def discovery_key(area: str | None, app: str) -> str:
    return f"{area or 'general'}:{app}"


def recipes_covering(memory, area: str | None, app: str) -> list[str]:
    """Active/proposed recipe texts that cover this case area (and a light app recall fallback)."""
    if memory is None:
        return []
    areas = [area] if area else None
    rows = memory.preload(areas)
    texts = [r["text"] for r in rows if r.get("kind") == "recipe" and r.get("text")]
    if texts:
        return texts
    q = " ".join(x for x in (area, app) if x).strip()
    if not q:
        return []
    return [r["text"] for r in memory.recall(q, k=5) if r.get("kind") == "recipe" and r.get("text")]


def ensure_discovery(plan, case, app: str, state, testers: dict, memory) -> dict:
    """If memory has no recipe for this area/app, run a bounded discovery pass once per area:app this run.
    Returns {source, recipes, notes} stored under state.data['discovery'][key]."""
    key = discovery_key(case.area, app)
    store = state.data.setdefault("discovery", {})
    if key in store:
        return store[key]
    if memory is None:
        entry = {"source": "no_memory", "recipes": [], "notes": ""}
        store[key] = entry
        state.save()
        return entry
    recipes = recipes_covering(memory, case.area, app)
    if recipes:
        entry = {"source": "memory", "recipes": recipes, "notes": ""}
        store[key] = entry
        state.save()
        state.checkpoint(f"discovery skipped for {key}: memory has {len(recipes)} recipe(s)")
        return entry
    tester = testers.get(app)
    if tester is None:
        entry = {"source": "skipped", "recipes": [], "notes": f"no tester for {app}"}
        store[key] = entry
        state.save()
        return entry
    from .models import Step
    goal = case.title or case.id
    disco_case_id = f"DISCO-{case.area or app}"
    steps = [Step(
        text=(f"Map how to reach the screens/actions needed for: {goal}. "
              "Note menu labels, URLs and search paths. Do not change data, settings, or pay."),
        app=app)]
    pseudo = PlannedCase(id=disco_case_id, title=f"discover UI for {case.area or app}",
                         area=case.area, steps=steps, type=case.type)
    brief = brief_for(plan, pseudo, [0], state)
    brief["discovery"] = True
    brief["read_only"] = True
    brief["max_tool_calls"] = 30
    brief["discovery_goal"] = goal
    notes, blocked = "", False
    try:
        sr = tester(app, brief, state)
        notes = (sr.notes or "").strip()
        if sr.needs_login:
            blocked = True
            notes = (notes + f"\nBLOCKED: login needs a person ({sr.needs_login})").strip()
    except Exception as e:  # noqa: BLE001
        notes = f"discovery failed: {type(e).__name__}: {e}"
        state.data.setdefault("notes", []).append(notes)
    # Collect recipes proposed during this run for the area (status proposed)
    proposed = []
    if memory is not None:
        try:
            for r in memory.store.query(memory.scopes, ["recipe"], statuses=("proposed", "active"),
                                        areas=[case.area] if case.area else None):
                if r.get("text"):
                    proposed.append(r["text"])
        except Exception:  # noqa: BLE001
            pass
    entry = {"source": "discovery", "recipes": proposed, "notes": notes, "blocked": blocked}
    store[key] = entry
    state.save()
    ddir = state.dir / "discovery"
    ddir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (case.area or app))
    (ddir / f"{safe}.md").write_text(
        f"# Discovery {key}\n\nsource: discovery\n\n## Notes\n{notes}\n\n## Recipes\n"
        + "\n".join(f"- {r}" for r in proposed) + "\n")
    state.checkpoint(f"discovery done for {key}" + (" (blocked)" if blocked else ""))
    return entry


_ADMIN_DEEPLINK = "module=Administration&action="
_ADMIN_DEEPLINK_WARN = (
    "WARNING: do not browser_navigate to SuiteCRM module=Administration&action=… URLs "
    "(XSRF / missing referer); use the menu labels in the recipe instead."
)


def _has_admin_deeplink(texts) -> bool:
    return any(_ADMIN_DEEPLINK in (t or "") for t in texts)


def inject_discovery(brief: dict, entry: dict) -> dict:
    brief = dict(brief)
    recipes = list(entry.get("recipes") or [])
    notes = entry.get("notes") or ""
    brief["recipes"] = recipes
    brief["discovery_notes"] = notes
    # Soft-sanitize: keep recipe text intact for memory diffs; warn this run's brief only.
    if _has_admin_deeplink(recipes) or _ADMIN_DEEPLINK in notes:
        if _ADMIN_DEEPLINK_WARN not in notes:
            brief["discovery_notes"] = (notes + "\n" if notes else "") + _ADMIN_DEEPLINK_WARN
    return brief


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


def restore_pass(plan: TestPlan, state: RunState, testers: dict[str, Tester], unrestored: list[dict]) -> list[dict]:
    """After the cases: put back settings that were changed but not restored and verified (even when a case
    failed or stopped early). Each change still goes through the approval gate. Returns what is still unrestored."""
    from .ledger import Ledger
    from .models import Step
    steps = [Step(text=f"Set '{u['setting']}' back to {u['original']!r}, re-read it, then call "
                       f"record_restored('{u['setting']}', <value read>, verified=true)",
                  app=u["app"], changes_data=True, action_type="change_settings", restores=True)
             for u in unrestored]
    pseudo = PlannedCase(id="RESTORE", title="restore settings changed by this run", steps=steps)
    for app, idx in app_turns(pseudo):
        if app not in testers:
            state.data["notes"].append(f"restore on {app}: no tester for this app")
            continue
        try:
            testers[app](app, brief_for(plan, pseudo, idx, state), state)
        except Exception as e:  # noqa: BLE001
            state.data["notes"].append(f"restore on {app} failed: {type(e).__name__}: {e}")
    state.checkpoint("restore pass done")
    return Ledger(state).unrestored()


def run_plan(plan: TestPlan, state: RunState, testers: dict[str, Tester], judge: Judge,
             on_case_done: Callable[[CaseResult], None] | None = None, max_retries: int = 1,
             manual_login: Callable[[str, str], bool] | None = None,
             manual_login_apps: list[str] | None = None, memory=None) -> dict[str, CaseResult]:
    state.checkpoint("run started", status="running")
    read_snapshot(plan, state, testers, "baseline")
    by_id = {c.id: c for c in plan.cases}
    for cid in execution_order(plan):
        if state.result(cid):
            continue                                  # resume: already done
        case = by_id[cid]
        result = _run_case(plan, case, state, testers, judge, max_retries, manual_login,
                           manual_login_apps, memory=memory)
        state.set_result(result)
        if on_case_done:
            on_case_done(result)
    read_snapshot(plan, state, testers, "final")
    state.checkpoint("run finished", status="finished")
    return {cid: state.result(cid) for cid in execution_order(plan)}


def _run_case(plan, case, state, testers, judge, max_retries, manual_login=None,
              manual_login_apps=None, memory=None) -> CaseResult:
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
        disco = ensure_discovery(plan, case, app, state, testers, memory)
        brief = inject_discovery(brief_for(plan, case, steps, state), disco)
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
        if sr.needs_login:
            done = state.data.setdefault("manual_logins", {})
            retries = state.data.setdefault("manual_login_retries", {})
            # Mid-run offer only for pack-listed CAPTCHA apps (e.g. portal), not CRM password failures.
            allowed = set(manual_login_apps or [])
            if manual_login is not None and app in allowed:
                # One mid-run re-offer per app if an earlier login did not stick; then BLOCKED.
                offer = False
                if not done.get(app):
                    offer = True
                elif retries.get(app, 0) < 1:
                    print(f"\nThe {app} session did not stick after the earlier login "
                          f"(CAPTCHA or login page again). Please log in once more "
                          f"(only one retry allowed per app this run).")
                    retries[app] = retries.get(app, 0) + 1
                    done[app] = False
                    state.save()
                    offer = True
                if offer and manual_login(app, sr.needs_login):
                    done[app] = True
                    state.save()
                    state.checkpoint(f"manual login on {app} done; retrying {case.id}")
                    try:
                        sr = tester(app, brief, state)
                    except RunPaused:
                        raise
                    except Exception as e:                # noqa: BLE001
                        sr.notes = f"retry after manual login failed: {type(e).__name__}: {e}"
            if sr.needs_login:
                return CaseResult(case_id=case.id, verdict="BLOCKED", expected=case.expected,
                                  values={k: v for r in results for k, v in r.values.items()},
                                  evidence=[s for r in results for s in r.screenshots],
                                  note=f"login on {app} needs a person (CAPTCHA or robot check): {sr.needs_login}")
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
        result = judge(full_brief, state.actions_for(case.id), results, state)
    except Exception as e:                        # noqa: BLE001  judge output unusable; keep the run going
        return CaseResult(case_id=case.id, verdict="AGENT INCOMPLETE", expected=case.expected,
                          values={k: v for r in results for k, v in r.values.items()},
                          evidence=[s for r in results for s in r.screenshots],
                          note=f"the judge could not decide: {type(e).__name__}: {str(e)[:300]}")
    if memory is not None and result.verdict in ("PASS", "PARTIAL"):
        _propose_success_recipe(memory, case, state)
    return result


def _propose_success_recipe(memory, case, state) -> None:
    """If discovery found a path and nothing was proposed yet, propose a recipe from discovery notes."""
    apps = {s.app for s in case.steps if s.app not in ("none", "redmine")}
    for app in apps:
        entry = (state.data.get("discovery") or {}).get(discovery_key(case.area, app)) or {}
        if entry.get("source") != "discovery":
            continue
        if entry.get("recipes"):
            continue
        notes = (entry.get("notes") or "").strip()
        if not notes or notes.startswith("discovery failed") or entry.get("blocked"):
            continue
        text = f"when working on {case.area or app} ({case.title}): {notes[:400]}"
        try:
            memory.propose("recipe", text, area=case.area, evidence=f"{state.dir.name}/{case.id}")
            state.data.setdefault("notes", []).append(f"proposed recipe from discovery for {case.area or app}")
            state.save()
        except Exception:  # noqa: BLE001
            pass
