"""Fixed-code coordinator: runs one case at a time, hands app turns to testers, sends evidence to the judge.

Testers and the judge are injected callables so this file has no LLM dependency:
  tester(app, brief: dict, state: RunState) -> StepResult
  judge(brief: dict, actions: list[dict], results: list[StepResult], state: RunState) -> CaseResult
"""
from __future__ import annotations

from .reliability import is_retryable, wait_for_model

import re
from datetime import date

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


MAX_CONTINUATIONS = 2


def _maybe_continue(app, brief, sr, state, tester, log_mark):
    """A long flow that ran out of budget while still finishing steps gets a continuation turn with a fresh budget,
    starting from the steps it reached. Not after a loop, not without progress, and never after a turn that already
    changed data (that could repeat the change)."""
    n = 0
    while (sr.limit_hit and not sr.needs_login and not getattr(sr, "loop", False) and sr.steps_done
           and n < MAX_CONTINUATIONS):
        total = len(brief.get("steps") or [])
        if total and len(set(sr.steps_done)) >= total:
            break
        changed = [a for a in state.data["action_log"][log_mark:]
                   if a.get("approval") in ("approved", "pre_approved")]
        if changed:
            break
        n += 1
        done_before = set(sr.steps_done)
        nb = dict(brief)
        nb["continue_from"] = {"steps_done": sorted(done_before), "notes": sr.notes}
        state.checkpoint(f"{brief['case_id']} ({app}) ran out of budget after step(s) {sorted(done_before)}; "
                         f"continuing (turn {n + 1})")
        log_mark = len(state.data["action_log"])
        nxt = tester(app, nb, state)
        nxt.steps_done = sorted(done_before | set(nxt.steps_done))
        nxt.values = {**sr.values, **nxt.values}
        nxt.screenshots = sr.screenshots + [x for x in nxt.screenshots if x not in sr.screenshots]
        nxt.snapshots = sr.snapshots + [x for x in nxt.snapshots if x not in sr.snapshots]
        if set(nxt.steps_done) == done_before and nxt.limit_hit:
            return nxt                              # no new progress: stop continuing
        sr = nxt
    return sr


def discovery_key(area: str | None, app: str) -> str:
    return f"{area or 'general'}:{app}"


def recipes_covering(memory, area: str | None, app: str, state=None, title: str = "",
                     pack_root=None) -> list[str]:
    """Few ranked recipe/lesson hints for this area:app — budgeted, never a dump of learned.md."""
    from .memory.retrieve import learned_hints_for
    pack_hints = learned_hints_for(pack_root, area, app, title) if pack_root else []
    if memory is None:
        return pack_hints
    # Tests (and thin fakes) may only implement preload/recall — keep that path working.
    if not hasattr(memory, "retriever"):
        areas = [area] if area else None
        rows = memory.preload(areas) if hasattr(memory, "preload") else []
        texts = [r["text"] for r in rows if r.get("kind") == "recipe" and r.get("text")]
        if not texts:
            q = " ".join(x for x in (area, app, title) if x).strip()
            if q and hasattr(memory, "recall"):
                texts = [r["text"] for r in memory.recall(q, k=5)
                         if r.get("kind") == "recipe" and r.get("text")]
        return list(dict.fromkeys(texts + pack_hints))
    tr = memory.retriever(state=state)
    res = tr.retrieve(area=area, app=app or "", query=title or (area or app or ""),
                      title=title, pack_root=pack_root, include_learned=True)
    texts = [r["text"] for r in res.rows if r.get("kind") == "recipe" and r.get("text")]
    texts += list(res.learned_hints or [])
    if not texts:
        texts = [r["text"] for r in res.rows if r.get("text")]
    return list(dict.fromkeys(texts + pack_hints))


def env_fingerprint(plan, apps: list[str] | None = None) -> str:
    """Stable host fingerprint for the apps in this run (re-discover when CRM/portal host changes)."""
    from urllib.parse import urlparse
    want = set(apps or [])
    hosts: dict[str, str] = {}
    for r in getattr(plan, "requirements", None) or []:
        key = getattr(r, "key", None) or (r.get("key") if isinstance(r, dict) else None)
        val = getattr(r, "value", None) or (r.get("value") if isinstance(r, dict) else None)
        kind = getattr(r, "kind", None) or (r.get("kind") if isinstance(r, dict) else None)
        if not key or not val:
            continue
        parts = str(key).split(".")
        if len(parts) >= 3 and parts[0] == "env" and (kind in (None, "url") or str(val).startswith("http")):
            app = parts[-1]
            if want and app not in want:
                continue
            host = urlparse(str(val)).netloc or str(val)
            if host:
                hosts[app] = host
    env = getattr(plan, "env", "") or ""
    return "|".join([f"env={env}"] + [f"{a}={hosts[a]}" for a in sorted(hosts)])


def mark_discovery_stale(state, area: str | None, app: str, why: str = "") -> None:
    """Invalidate a cached discovery entry so the next ensure_discovery re-runs (recipe failed / env change)."""
    key = discovery_key(area, app)
    stale = state.data.setdefault("discovery_stale", {})
    stale[key] = why or "stale"
    store = state.data.get("discovery") or {}
    if key in store:
        del store[key]
    state.save()
    state.checkpoint(f"discovery stale for {key}" + (f": {why}" if why else ""))


DISCOVERY_TRIES = 2
DISCOVERY_BASE_CALLS = 30
DISCOVERY_CALLS_PER_ITEM = 5
DISCOVERY_CALLS_CAP = 60
_NO_PROGRESS = re.compile(r"maximum repeated|did not respond|no screens were mapped|unable to|could not reach|stuck",
                          re.I)


def discovery_checklist(plan, app: str, state, *, area: str | None = None,
                        data_prep: list[dict] | None = None) -> list[str]:
    """Short deduped checklist for this area:app from ALL selected cases + up-front data-plan parts."""
    from .placeholders import (checklist_from_cases, discovery_items_from_plan, merge_checklist)
    cases = [c for c in plan.cases if (area is None or c.area == area)]
    # Prefer all plan cases that touch this app (not only current area) so later cases are covered.
    cases_for_app = [c for c in plan.cases
                     if any((s.app or "none") == app for s in c.steps)]
    case_items = checklist_from_cases(cases_for_app or cases, app, limit=10)
    data_plan = state.data.get("_data_plan_rules") or state.data.get("data_plan") or []
    plan_items = discovery_items_from_plan(data_plan, app)
    # Also pull create apps from matching data_prep rules (legacy).
    prep_items = []
    if data_prep:
        from .data_prep import matching_rules, _create_parts
        for c in cases_for_app or cases:
            for rule in matching_rules(c, data_prep):
                for p in _create_parts(rule):
                    if p.get("app") == app:
                        prep_items.append(f"prep create on {app}: {(p.get('text') or '')[:60]}")
                if (rule.get("find_app") or rule.get("app")) == app and rule.get("find"):
                    prep_items.append(f"prep find on {app}")
    return merge_checklist(case_items, plan_items, prep_items, limit=10)


def _checklist_covered(entry: dict, checklist: list[str]) -> list[str]:
    """Return checklist items not obviously mentioned in prior discovery notes/recipes."""
    blob = " ".join([
        str(entry.get("notes") or ""),
        " ".join(entry.get("checklist") or []),
        " ".join(entry.get("recipes") or []),
    ]).lower()
    missing = []
    for item in checklist:
        # Token overlap: require at least one distinctive word (>3 chars) from the item.
        words = [w for w in re.findall(r"[a-z0-9]{4,}", item.lower()) if w not in
                 {"from", "with", "their", "this", "that", "open", "list", "and"}]
        if not words or not any(w in blob for w in words):
            missing.append(item)
    return missing


def ensure_discovery(plan, case, app: str, state, testers: dict, memory,
                     data_prep: list[dict] | None = None,
                     checklist: list[str] | None = None) -> dict:
    """Always run a bounded read-only discovery pass for this area/app (once per key this run).

    Prior pack/memory recipes are HINTS only — they never skip discovery. Re-runs when the env
    fingerprint (CRM/portal host) changed or when a recipe used this run was marked failed/stale.
    Checklist is built from ALL selected cases + data-plan parts for this app (not just this case).
    If a later need is missing from the cached map, runs a small incremental pass for those items.
    Returns {source, recipes, notes, hints, env, checklist} stored under state.data['discovery'][key]."""
    key = discovery_key(case.area, app)
    store = state.data.setdefault("discovery", {})
    fp = env_fingerprint(plan, [app])
    stale = (state.data.get("discovery_stale") or {}).get(key)
    cached = store.get(key)
    want = list(checklist) if checklist is not None else discovery_checklist(
        plan, app, state, area=case.area, data_prep=data_prep)
    if cached and not stale and cached.get("env") == fp:
        missing = _checklist_covered(cached, want)
        if not missing:
            return cached
        # Incremental discovery for just the missing items.
        return _run_discovery(plan, case, app, state, testers, memory, fp, key, store,
                              want=missing, incremental=True, prior=cached)
    if cached and (stale or cached.get("env") != fp):
        why = stale or f"env changed ({cached.get('env')!r} -> {fp!r})"
        state.checkpoint(f"discovery re-run for {key}: {why}")
        store.pop(key, None)
        (state.data.get("discovery_stale") or {}).pop(key, None)

    return _run_discovery(plan, case, app, state, testers, memory, fp, key, store,
                          want=want, incremental=False, prior=None)


def _run_discovery(plan, case, app, state, testers, memory, fp, key, store, *,
                   want: list[str], incremental: bool, prior: dict | None) -> dict:
    from . import learned
    hints = recipes_covering(memory, case.area, app, state=state, title=case.title or "",
                             pack_root=state.data.get("pack_root"))
    tester = testers.get(app)
    if tester is None:
        entry = {"source": "skipped", "recipes": list(hints), "hints": list(hints),
                 "notes": f"no tester for {app}", "env": fp, "checklist": want}
        store[key] = entry
        state.save()
        return entry

    from .models import Step
    goal_lines = list(want) if want else [case.title or case.id]
    goal = "; ".join(goal_lines[:10])
    disco_case_id = f"DISCO-{case.area or app}" + ("-inc" if incremental else "")
    hint_note = ""
    if hints:
        hint_note = (
            f" Prior recipes/routes for this area are HINTS only ({len(hints)}): verify each against the live UI; "
            "ignore stale or wrong paths; map the real menus, search and screens you see now.")
    need_detail = any(re.search(r"(?i)detail|subpanel|payment|status|record", x) for x in goal_lines)
    detail_note = ""
    if need_detail:
        detail_note = (
            " When the checklist needs record-level actions, open ONE existing record's DetailView (read-only) "
            "and note its subpanels and their Create/Select controls. Never save.")
    steps = [Step(
        text=(f"Map how to reach these screens/actions (checklist): {goal}. "
              "Note menu labels, search/filter paths and URLs (ID only). Do not change data, settings, or pay."
              + detail_note + hint_note),
        app=app)]
    pseudo = PlannedCase(id=disco_case_id, title=f"discover UI for {case.area or app}",
                         area=case.area, steps=steps, type=case.type)
    brief = brief_for(plan, pseudo, [0], state)
    brief["discovery"] = True
    brief["read_only"] = True
    n_extra = max(0, len(goal_lines) - 1)
    brief["max_tool_calls"] = min(DISCOVERY_CALLS_CAP,
                                  DISCOVERY_BASE_CALLS + DISCOVERY_CALLS_PER_ITEM * n_extra)
    brief["discovery_goal"] = goal
    brief["discovery_checklist"] = goal_lines
    brief["recipes"] = list(hints)
    brief["discovery_hints"] = list(hints)
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
    proposed = []
    if memory is not None:
        try:
            for r in memory.store.query(memory.scopes, ["recipe"], statuses=("proposed", "active"),
                                        areas=[case.area] if case.area else None):
                if r.get("text"):
                    proposed.append(r["text"])
        except Exception:  # noqa: BLE001
            pass
    prior_notes = (prior or {}).get("notes") or ""
    prior_recipes = list((prior or {}).get("recipes") or [])
    prior_checklist = list((prior or {}).get("checklist") or [])
    combined_notes = (prior_notes + ("\n" if prior_notes and notes else "") + notes).strip()
    recipes = list(dict.fromkeys(proposed + prior_recipes + list(hints)))
    checklist = list(dict.fromkeys(prior_checklist + goal_lines))
    entry = {"source": "discovery" + ("-inc" if incremental else ""), "recipes": recipes,
             "hints": list(hints), "notes": combined_notes, "blocked": blocked, "env": fp,
             "checklist": checklist}
    try:
        from .platform_detect import detect, fingerprint_patch
        from .memory.seeds import ensure_seeds
        app_url = ""
        for r in getattr(plan, "requirements", None) or []:
            key_r = getattr(r, "key", None) or (r.get("key") if isinstance(r, dict) else None)
            val = getattr(r, "value", None) or (r.get("value") if isinstance(r, dict) else None)
            if key_r and val and str(key_r).endswith(f".{app}") and str(val).startswith("http"):
                app_url = str(val)
                break
        det = detect(url=app_url, html=notes or "", title="")
        entry.update(fingerprint_patch(det))
        if det.platform and memory is not None:
            memory.set_platforms([det.platform])
            ensure_seeds(memory.store, getattr(memory, "slug", None), platforms=[det.platform])
            state.data["detected_platform"] = det.platform
            state.data["detected_platform_version"] = det.platform_version
    except Exception as e:  # noqa: BLE001
        state.data.setdefault("notes", []).append(f"platform detect skipped: {type(e).__name__}")
    useful = bool(proposed) or bool(notes and not blocked and not notes.startswith("discovery failed")
                                    and not _NO_PROGRESS.search(notes))
    tries = state.data.setdefault("discovery_tries", {})
    tries[key] = tries.get(key, 0) + 1
    store[key] = entry
    if useful and notes:
        learned.add(state.data.get("pack_root"), case.area, app,
                    f"Discovery: {learned.scrub(notes)[:500]} ({date.today().isoformat()})")
    state.save()
    ddir = state.dir / "discovery"
    ddir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (case.area or app))
    plat_line = (
        f"platform: {entry.get('platform')} ({entry.get('platform_confidence')}) "
        f"version: {entry.get('platform_version')} ({entry.get('version_confidence')})\n"
    )
    (ddir / f"{safe}.md").write_text(
        f"# Discovery {key}\n\nsource: {entry['source']}\nenv: {fp}\n{plat_line}"
        f"\n## Checklist\n" + "\n".join(f"- {c}" for c in checklist)
        + f"\n\n## Notes\n{combined_notes}\n\n## Hints\n"
        + "\n".join(f"- {r}" for r in hints) + "\n\n## Recipes\n"
        + "\n".join(f"- {r}" for r in proposed) + "\n")
    state.checkpoint(f"discovery done for {key}" + (" (blocked)" if blocked else "")
                     + (" (incremental)" if incremental else "")
                     + (f" ({len(want)} item(s))" if want else "")
                     + (f" platform={entry.get('platform')}" if entry.get("platform") else ""))
    hard = blocked or notes.startswith("discovery failed")
    if not useful and hard and tries[key] >= DISCOVERY_TRIES:
        raise RunPaused(f"Discovery for {key} found no way to the screen after {tries[key]} tries "
                        f"({notes[:300] or 'no notes'}). Sort out the login, then resume "
                        f"(prior recipes are hints only and do not replace a live discovery pass).")
    return entry


def apps_needing_discovery(case, data_prep: list[dict] | None = None,
                           data_plan: list[dict] | None = None) -> list[str]:
    """Apps that need a discovery pass before mutating prep / the case body (case steps + prep + data plan)."""
    apps: list[str] = []
    for s in case.steps:
        a = s.app or "none"
        if a not in ("none", "redmine") and a not in apps:
            apps.append(a)
    if data_prep:
        from .data_prep import matching_rules, _create_parts
        for rule in matching_rules(case, data_prep):
            for a in (rule.get("find_app"), rule.get("app"), (rule.get("verify") or {}).get("app")):
                if a and a not in ("none", "redmine") and a not in apps:
                    apps.append(a)
            for p in _create_parts(rule):
                a = p.get("app")
                if a and a not in ("none", "redmine") and a not in apps:
                    apps.append(a)
    for row in data_plan or []:
        for a in (row.get("find_app"), row.get("verify_app")):
            if a and a not in ("none", "redmine") and a not in apps:
                apps.append(a)
        for p in row.get("create") or []:
            a = p.get("app") if isinstance(p, dict) else None
            if a and a not in ("none", "redmine") and a not in apps:
                apps.append(a)
    return apps


def ensure_case_discovery(plan, case, state, testers: dict, memory, data_prep: list[dict] | None = None) -> dict:
    """Read-only self-discovery for every app this case (and its prep / data plan) will use, before any mutate."""
    out = {}
    data_plan = state.data.get("_data_plan_rules") or state.data.get("data_plan") or []
    for app in apps_needing_discovery(case, data_prep, data_plan):
        out[app] = ensure_discovery(plan, case, app, state, testers, memory, data_prep=data_prep)
    return out


_ADMIN_DEEPLINK = "module=Administration&action="
_ADMIN_DEEPLINK_WARN = (
    "WARNING: do not browser_navigate to SuiteCRM module=Administration&action=… URLs "
    "(XSRF / missing referer); use the menu labels in the recipe instead."
)


def _has_admin_deeplink(texts) -> bool:
    return any(_ADMIN_DEEPLINK in (t or "") for t in texts)


def inject_discovery(brief: dict, entry: dict, *, max_recipes: int = 6, max_note_chars: int = 800) -> dict:
    """Attach a small slice of discovery hints — never the whole recipe list or learned.md."""
    brief = dict(brief)
    recipes = list(entry.get("recipes") or [])[:max_recipes]
    notes = (entry.get("notes") or "")[:max_note_chars]
    brief["recipes"] = recipes
    brief["discovery_notes"] = notes
    # Soft-sanitize: keep recipe text intact for memory diffs; warn this run's brief only.
    if _has_admin_deeplink(recipes) or _ADMIN_DEEPLINK in notes:
        if _ADMIN_DEEPLINK_WARN not in notes:
            brief["discovery_notes"] = (notes + "\n" if notes else "") + _ADMIN_DEEPLINK_WARN
    return brief


def inject_memory(brief: dict, memory, state, app: str) -> dict:
    """Select and attach a budgeted memory block for this turn; log injected size."""
    if memory is None or not hasattr(memory, "retriever"):
        brief["memory_prompt"] = ""
        brief["memory_stats"] = {"tokens": 0, "chars": 0}
        return brief
    tr = memory.retriever(state=state)
    res = tr.retrieve(
        area=brief.get("area"), app=app, query=brief.get("title") or brief.get("case_id") or "",
        title=brief.get("title") or "", pack_root=state.data.get("pack_root"),
        include_learned=True,
    )
    brief["memory_prompt"] = res.prompt
    brief["memory_stats"] = {"tokens": res.tokens, "chars": res.chars, "cache_hit": res.cache_hit,
                             "rows": len(res.rows), "hints": len(res.learned_hints)}
    # Prefer retrieve's ranked learned hints over a raw dump already in recipes.
    if res.learned_hints and not brief.get("recipes"):
        brief["recipes"] = list(res.learned_hints)[:4]
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


def run_plan(plan: TestPlan, state: RunState, *args, **kwargs) -> dict[str, CaseResult]:
    """Runs the plan with run.pid in the run folder, so the web UI can tell a terminal run is live
    (and offer its guide box)."""
    import os
    pidf = state.dir / "run.pid"
    try:
        pidf.write_text(f"{os.getpid()}\n")
    except OSError:
        pass
    from .logsetup import RunErrors
    try:
        with RunErrors(state.dir):
            return _run_plan(plan, state, *args, **kwargs)
    finally:
        try:
            if pidf.read_text().strip() == str(os.getpid()):
                pidf.unlink()
        except OSError:
            pass


def _resolve_upfront(plan, state, testers, memory, data_prep, pack_cfg) -> None:
    """Discover apps used by the data plan, resolve CRM contact once, then find/create every placeholder."""
    from .placeholders import build_data_plan
    from .data_prep import resolve_data_plan, ensure_contact, contact_needed
    if state.data.get("data_plan_resolved"):
        return
    corrections = state.data.get("data_plan_corrections") or {}
    data_plan = state.data.get("_data_plan_rules") or build_data_plan(plan, pack_cfg, corrections=corrections)
    state.data["_data_plan_rules"] = data_plan
    # Discovery for every app the data plan (and first cases) will touch, before mutating create.
    seen_apps: list[str] = []
    for row in data_plan:
        for a in [row.get("find_app"), row.get("verify_app")] + [
                p.get("app") for p in (row.get("create") or []) if isinstance(p, dict)]:
            if a and a not in ("none", "redmine") and a not in seen_apps:
                seen_apps.append(a)
    seed_case = plan.cases[0] if plan.cases else None
    rules_for_contact = list(data_prep or [])
    for row in data_plan:
        if row.get("rule"):
            rules_for_contact.append(row["rule"])
    # Apps needed for contact lookup must be discovered before the contact turn.
    for rule in rules_for_contact:
        a = rule.get("app") or rule.get("find_app")
        if a and a not in ("none", "redmine") and a not in seen_apps:
            # Only if some selected case actually needs this contact rule.
            if seed_case is not None and any(contact_needed(c, [rule]) for c in plan.cases):
                seen_apps.append(a)
    if seed_case is not None:
        for app in seen_apps:
            ensure_discovery(plan, seed_case, app, state, testers, memory, data_prep=data_prep or [])
        for c in plan.cases:
            if contact_needed(c, rules_for_contact) is None:
                continue
            blocked = ensure_contact(plan, c, state, testers, rules_for_contact)
            if blocked is not None:
                state.data.setdefault("notes", []).append(f"up-front contact lookup: {blocked.note}")
            break
    if data_plan:
        resolve_data_plan(plan, state, testers, data_plan)
    state.data["data_plan_resolved"] = True
    state.save()


def _run_plan(plan: TestPlan, state: RunState, testers: dict[str, Tester], judge: Judge,
             on_case_done: Callable[[CaseResult], None] | None = None, max_retries: int = 1,
             manual_login: Callable[[str, str], bool] | None = None,
             manual_login_apps: list[str] | None = None, memory=None,
             data_prep: list[dict] | None = None,
             pack_cfg: dict | None = None) -> dict[str, CaseResult]:
    state.checkpoint("run started", status="running")
    read_snapshot(plan, state, testers, "baseline")
    by_id = {c.id: c for c in plan.cases}
    pack_cfg = pack_cfg or state.data.get("pack_cfg") or {}
    from .data_prep import ensure_data, after_case, ensure_contact
    from .guidance_redo import apply_redo, report_undelivered
    from .placeholders import case_unresolved_tokens

    _resolve_upfront(plan, state, testers, memory, data_prep or [], pack_cfg)

    queue = list(execution_order(plan))
    i = 0
    while i < len(queue):
        redone = apply_redo(state, plan, pack_cfg)
        if redone:
            # Next-after-current: insert at i, dropping later duplicates of the same ids.
            rest = [c for c in queue[i:] if c not in redone]
            queue = queue[:i] + list(redone) + rest
            continue

        cid = queue[i]
        i += 1
        if state.result(cid):
            continue
        case = by_id.get(cid)
        if case is None:
            continue
        from .agents.llm import TRACE_CASE
        tok = TRACE_CASE.set(cid)
        try:
            result = _deps_block(case, state)
            if result is None and not case.skip_reason:
                missing = case_unresolved_tokens(case, state, pack_cfg)
                if missing:
                    result = CaseResult(
                        case_id=case.id, verdict="BLOCKED", expected=case.expected,
                        note=("prep-missing: placeholder(s) " + ", ".join(missing)
                              + " have no resolved value for this run (find/create failed or was skipped); "
                              "never search the page for the literal token"))
            if result is None and not case.skip_reason:
                ensure_case_discovery(plan, case, state, testers, memory, data_prep or [])
                result = ensure_contact(plan, case, state, testers, data_prep or [])
            if result is None and not case.skip_reason:
                result = ensure_data(plan, case, state, testers, data_prep or [])
            if result is None:
                result = _run_case(plan, case, state, testers, judge, max_retries, manual_login,
                                   manual_login_apps, memory=memory, pack_cfg=pack_cfg)
                after_case(case, state, data_prep or [])
        finally:
            TRACE_CASE.reset(tok)
        state.set_result(result)
        if on_case_done:
            on_case_done(result)

    report_undelivered(state, plan)
    read_snapshot(plan, state, testers, "final")
    state.checkpoint("run finished", status="finished")
    return {cid: state.result(cid) for cid in execution_order(plan)}


def _deps_block(case, state) -> CaseResult | None:
    """SKIPPED / BLOCKED from skip_reason or unmet depends_on; None when the case may run."""
    if case.skip_reason:
        return CaseResult(case_id=case.id, verdict="SKIPPED", expected=case.expected, note=case.skip_reason)
    for dep in case.depends_on:
        r = state.result(dep)
        if r is None:
            return CaseResult(case_id=case.id, verdict="BLOCKED", expected=case.expected,
                              note=f"depends on {dep} (not run)")
        if r.verdict in ("PASS", "PARTIAL"):
            continue
        # A known open bug that is still present is recorded as FAIL, but it must not stop the rest of the
        # suite: later cases still need to run (especially on staging after a deploy).
        if r.verdict == "FAIL" and r.known_bug:
            state.checkpoint(f"{case.id}: {dep} failed with known bug {r.known_bug} still present; continuing")
            continue
        return CaseResult(case_id=case.id, verdict="BLOCKED", expected=case.expected,
                          note=f"depends on {dep} ({r.verdict})")
    return None


def _run_case(plan, case, state, testers, judge, max_retries, manual_login=None,
              manual_login_apps=None, memory=None, pack_cfg: dict | None = None) -> CaseResult:
    blocked = _deps_block(case, state)
    if blocked is not None:
        return blocked
    from .placeholders import inject_placeholders
    results: list[StepResult] = []
    for app, steps in app_turns(case):
        if app in ("none", "redmine"):
            continue
        tester = testers.get(app)
        if tester is None:
            return CaseResult(case_id=case.id, verdict="BLOCKED", expected=case.expected, note=f"no tester for app '{app}'")
        disco = ensure_discovery(plan, case, app, state, testers, memory,
                                 data_prep=None)
        brief = inject_discovery(brief_for(plan, case, steps, state), disco)
        brief = inject_memory(brief, memory, state, app)
        brief = inject_placeholders(brief, state, pack_cfg)
        # Guidance attached for a redo of this case.
        extra = (state.data.get("case_guidance") or {}).pop(case.id, None)
        if extra:
            brief["redo_guidance"] = list(extra)
            brief["placeholder_note"] = (
                (brief.get("placeholder_note") or "")
                + "\nGUIDANCE FROM THE USER (redo of this case):\n"
                + "\n".join(f"- {t}" for t in extra)
            ).strip()
            state.save()
        attempt = outages = 0
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
                outage = is_retryable(e) and not changed and outages < 3
                if outage:                            # model server dropped: wait for it, then replay this turn
                    outages += 1
                    attempt -= 1
                    state.checkpoint(f"{case.id} ({app}): model server unreachable, waiting for it to come back")
                    if wait_for_model():
                        continue
                if changed or attempt > max_retries:  # never replay a turn that changed data; mark the case and go on
                    state.checkpoint(f"{case.id} ({app}) failed: {type(e).__name__}: {e}")
                    return CaseResult(case_id=case.id, verdict="AGENT INCOMPLETE", expected=case.expected,
                                      values={k: v for r in results for k, v in r.values.items()},
                                      evidence=[s for r in results for s in r.screenshots],
                                      note=f"tester on {app} crashed: {type(e).__name__}: {str(e)[:200]}"
                                           + ("; it had already changed data, check the record" if changed else ""))
        sr = _maybe_continue(app, brief, sr, state, tester, log_mark)
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
        if sr.steps_done and not (sr.stuck or sr.limit_hit or sr.needs_login):
            _learn_route(case, app, state, log_mark)
        if (sr.notes or "").strip().upper().startswith("BLOCKED:"):
            note = f"{app}: {sr.notes.strip()[8:].strip()}"
            result = CaseResult(case_id=case.id, verdict="BLOCKED", expected=case.expected,
                                values={k: v for r in results for k, v in r.values.items()},
                                evidence=[s for r in results for s in r.screenshots], note=note)
            _learn_failure(case, app, state, log_mark, "blocked", sr.notes or "")
            return result
        for k, v in sr.values.items():
            state.values.setdefault(k, {"value": v, "source": f"{case.id}/{app}"})
        if sr.stuck or sr.limit_hit:
            why = ("kept repeating the same action without progress" if getattr(sr, "loop", False)
                   else "hit the step/time limit" if sr.limit_hit else "got stuck")
            note = f"tester on {app} {why}: {sr.notes}".strip()
            result = CaseResult(case_id=case.id, verdict="AGENT INCOMPLETE", expected=case.expected,
                                values={k: v for r in results for k, v in r.values.items()},
                                evidence=[s for r in results for s in r.screenshots], note=note)
            _learn_failure(case, app, state, log_mark, why, sr.notes or "")
            # A recipe/path that failed this run must not keep discovery skipped next time.
            mark_discovery_stale(state, case.area, app, why)
            return result
    full_brief = brief_for(plan, case, list(range(len(case.steps))), state)
    try:
        for left in range(2, -1, -1):
            try:
                result = judge(full_brief, state.actions_for(case.id), results, state)
                break
            except Exception as e:                # noqa: BLE001
                if left and is_retryable(e):      # model server dropped: wait for it and ask the judge again
                    state.checkpoint(f"{case.id}: judge lost the model server, waiting for it to come back")
                    if wait_for_model():
                        continue
                raise
    except Exception as e:                        # noqa: BLE001  judge output unusable; keep the run going
        return CaseResult(case_id=case.id, verdict="AGENT INCOMPLETE", expected=case.expected,
                          values={k: v for r in results for k, v in r.values.items()},
                          evidence=[s for r in results for s in r.screenshots],
                          note=f"the judge could not decide: {type(e).__name__}: {str(e)[:300]}")
    if memory is not None and result.verdict in ("PASS", "PARTIAL"):
        _propose_success_recipe(memory, case, state)
    _learn_outcome(case, result, state)
    return result


def _learn_route(case, app, state, log_mark) -> None:
    """Fixed code writes the route this tester used into the pack's learned.md and this run's discovery entry."""
    from . import learned
    try:
        acts = [a for a in state.data["action_log"][log_mark:] if a.get("case_id") == case.id]
        line = learned.record_route(state.data.get("pack_root"), case.area, app, case.id, "reached", acts)
        if line:
            entry = state.data.setdefault("discovery", {}).setdefault(
                discovery_key(case.area, app), {"source": "run", "recipes": [], "notes": ""})
            entry.setdefault("recipes", []).insert(0, line)
            state.data.setdefault("notes", []).append(f"saved route for {case.area or app} from {case.id}")
            state.save()
    except Exception:  # noqa: BLE001  learning must never fail a case
        pass


def _learn_failure(case, app, state, log_mark, why: str, notes: str) -> None:
    """A stuck, looped or blocked turn writes what it hit into the pack's learned.md so the next run of this
    area avoids the same dead end."""
    from . import learned
    try:
        acts = [a for a in state.data["action_log"][log_mark:] if a.get("case_id") == case.id]
        line = learned.record_stuck(state.data.get("pack_root"), case.area, app, case.id, why, notes, acts)
        if line:
            entry = state.data.setdefault("discovery", {}).setdefault(
                discovery_key(case.area, app), {"source": "run", "recipes": [], "notes": ""})
            entry.setdefault("recipes", []).insert(0, line)
            state.data.setdefault("notes", []).append(f"saved stuck note for {case.area or app} from {case.id}")
            state.checkpoint(f"{case.id}: saved where it got stuck to the pack")
            state.save()
    except Exception:  # noqa: BLE001  learning must never fail a case
        pass


def _learn_outcome(case, result, state) -> None:
    """Fixed code keeps what the app actually did for a decided case (PASS/FAIL), so later runs expect it."""
    from . import learned
    try:
        if result.verdict not in ("PASS", "FAIL") or not (result.actual or "").strip():
            return
        apps = [s.app for s in case.steps if s.app not in ("none", "redmine")]
        if not apps:
            return
        text = f"{case.title}: {result.actual}"
        if learned.record_behaviour(state.data.get("pack_root"), case.area, apps[-1], text, case.id):
            state.data.setdefault("notes", []).append(f"saved behaviour for {case.area or apps[-1]} from {case.id}")
            state.save()
    except Exception:  # noqa: BLE001  learning must never fail a case
        pass


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
