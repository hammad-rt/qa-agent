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
        "read_only": bool(getattr(case, "read_only", False)),
        "source_section": getattr(case, "source_section", None),
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


def mark_discovery_stale(state, area: str | None, app: str, why: str = "",
                         checklist_item: str | None = None, memory=None,
                         plan=None) -> None:
    """Invalidate a cached discovery entry so the next ensure_discovery re-runs (recipe failed / env change).

    When checklist_item is set, only that item is marked stale in the durable map (fail_count+1)
    and queued for incremental rediscovery — a reused map must never block a case.
    """
    key = discovery_key(area, app)
    stale = state.data.setdefault("discovery_stale", {})
    if checklist_item:
        items = state.data.setdefault("discovery_stale_items", {}).setdefault(key, [])
        if checklist_item not in items:
            items.append(checklist_item)
        # Bump fail on the durable row when we can.
        try:
            from .discovery_map import DiscoveryMapStore, match_item_to_rows, scope_for
            if memory is not None and plan is not None:
                fp = env_fingerprint(plan, [app])
                ms = DiscoveryMapStore.from_memory(memory)
                if ms is not None:
                    rows = ms.list_for(scope_for(memory), area, app, fp)
                    row = match_item_to_rows(rows, checklist_item)
                    if row:
                        ms.mark_stale(row["id"], why or "route failed in case")
        except Exception:  # noqa: BLE001
            pass
        # Drop only this item from the in-run cache notes/recipes so incremental runs.
        entry = (state.data.get("discovery") or {}).get(key)
        if isinstance(entry, dict):
            entry["checklist"] = [c for c in (entry.get("checklist") or []) if c != checklist_item]
        state.save()
        state.checkpoint(f"discovery item stale for {key}/{checklist_item}" + (f": {why}" if why else ""))
        return
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
    """Short deduped checklist for this area:app from ALL selected cases + up-front data-plan parts.

    Admin/system actions (Quick Repair, cache clear, …) are not seeded unless a case step names
    them — and even then they become locate-only goals (discovery must not run them).
    """
    from .discovery_guard import filter_discovery_checklist, is_admin_checklist_item
    from .placeholders import (checklist_from_cases, discovery_items_from_plan, merge_checklist)
    cases = [c for c in plan.cases if (area is None or c.area == area)]
    # Prefer all plan cases that touch this app (not only current area) so later cases are covered.
    cases_for_app = [c for c in plan.cases
                     if any((s.app or "none") == app for s in c.steps)]
    case_items = checklist_from_cases(cases_for_app or cases, app, limit=10)
    # Only keep admin-ish cues when some selected case step explicitly names that admin action.
    named_admin = any(
        is_admin_checklist_item(getattr(s, "text", "") or "")
        for c in (cases_for_app or cases) for s in (c.steps or [])
    )
    if not named_admin:
        case_items = [x for x in case_items if not is_admin_checklist_item(x)]
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
    merged = merge_checklist(case_items, plan_items, prep_items, limit=10)
    return filter_discovery_checklist(merged)


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


def _force_rediscover(state) -> bool:
    """CLI --rediscover, web checkbox, or DISCOVERY_REUSE=0."""
    if state.data.get("rediscover"):
        return True
    from .discovery_map import reuse_enabled
    return not reuse_enabled()


def _discovery_change_text(plan, case, state) -> str:
    from .discovery_map import ticket_case_blob
    extra = "\n".join(str(x) for x in (state.data.get("ticket_texts") or []) if x)
    return ticket_case_blob(plan, case) + ("\n" + extra if extra else "")


def _screen_label(text: str, limit: int = 40) -> str:
    """Short screen name for reuse logs — never truncated PREP create instructions."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if re.match(r"(?i)^(WALLET|ORDER|CUSTOMER|PREP)\b.*\bcreate:", t) or len(t) > 80:
        # Prefer the leading screen-ish phrase before create/instruction prose.
        m = re.match(r"^([^:]{3,40})", t)
        t = (m.group(1) if m else t[:40]).strip(" -")
    return (t or "?")[:limit]


def _log_discovery_reuse(state, key: str, reused: list[dict], live: list[str],
                         spot_checked: int = 0, calls_saved: int = 0) -> None:
    """One progress.log line per area:app describing reuse vs live discovery.

    Coexists with Progress.finished token/timing lines: per-area lines go to progress.log
    immediately; aggregate counts land in state.data['discovery_reuse_stats'] for the finish summary.
    """
    from datetime import datetime
    from .discovery_map import age_days, estimate_calls_saved
    bits = []
    # Dedupe reused rows by checklist_item / item_key; skip PREP instruction blobs.
    seen_keys: set[str] = set()
    unique_reused: list[dict] = []
    for r in reused or []:
        label = (r.get("checklist_item") or "")
        if re.search(r"(?i)\bcreate:\s*", label) and len(label) > 60:
            continue
        ik = r.get("item_key") or label.lower()
        if ik in seen_keys:
            continue
        seen_keys.add(ik)
        unique_reused.append(r)
    if unique_reused:
        ages = []
        for r in unique_reused:
            age = age_days(r.get("last_success_at") or r.get("verified_at") or r.get("updated_at"))
            label = _screen_label(r.get("checklist_item") or "?")
            ages.append(f"{label} ({age:.0f}d)" if age is not None else label)
        bits.append("reused from memory: " + ", ".join(ages))
    if spot_checked:
        bits.append(f"deploy changed, spot-checked {spot_checked} route(s)")
    if live:
        bits.append("discovered live: " + ", ".join(_screen_label(x) for x in live))
    if not bits:
        bits.append("no checklist items")
    # Honest savings: unique reused screens only. Never claim the full solo-reuse
    # estimate (~60) when live discovery is still running for other items.
    n_saved = len(unique_reused)
    if not n_saved:
        saved = 0
    elif not live:
        saved = int(calls_saved or estimate_calls_saved(n_saved))
    else:
        saved = min(estimate_calls_saved(n_saved) // 2, 8 * n_saved, 24)
    line = f"discovery {key}: " + "; ".join(bits) + f"; ~{saved} tool calls saved"
    state.data.setdefault("notes", []).append(line)
    stats = state.data.setdefault("discovery_reuse_stats", {
        "areas": 0, "reused": 0, "live": 0, "spot_checked": 0, "calls_saved": 0,
    })
    stats["areas"] = int(stats.get("areas") or 0) + 1
    stats["reused"] = int(stats.get("reused") or 0) + len(unique_reused)
    stats["live"] = int(stats.get("live") or 0) + len(live)
    stats["spot_checked"] = int(stats.get("spot_checked") or 0) + int(spot_checked or 0)
    stats["calls_saved"] = int(stats.get("calls_saved") or 0) + int(saved or 0)
    try:
        with (state.dir / "progress.log").open("a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass
    state.checkpoint(line)


def _persist_discovered_routes(plan, case, app, state, memory, fp, checklist, notes, recipes,
                               entry: dict) -> list[str]:
    """Write scrubbed routes for covered checklist items immediately (atomic). Returns covered items."""
    from .discovery_map import (
        DiscoveryMapStore, buffer_pending, labels_from_route, parse_routes_from_discovery_notes,
        scope_for,
    )
    from .memory.env_guard import EnvDetailError
    covered: list[str] = []
    store = DiscoveryMapStore.from_memory(memory) if memory is not None else None
    if store is None:
        return covered
    marker = (state.data.get("deploy_markers") or {}).get(app) or ""
    parsed = parse_routes_from_discovery_notes(notes or "", checklist)
    # Also treat proposed recipes as routes when they mention checklist words.
    for rec in recipes or []:
        for item in checklist:
            if item in parsed:
                continue
            words = [w for w in re.findall(r"[a-z0-9]{4,}", item.lower()) if len(w) > 3]
            if words and any(w in rec.lower() for w in words) and re.search(r"(?i)(click|menu|->|→|>)", rec):
                try:
                    from .discovery_map import _safe_route
                    parsed[item] = (_safe_route(rec[:400]), labels_from_route(rec))
                except EnvDetailError:
                    pass
    scope = scope_for(memory)
    for item, (route, labels) in parsed.items():
        payload = {
            "scope": scope, "area": case.area, "app": app, "checklist_item": item,
            "env_fingerprint": fp, "route_text": route, "key_labels": labels,
            "deploy_marker": marker, "platform": entry.get("platform"),
            "platform_version": entry.get("platform_version") or state.data.get("detected_platform_version"),
            "status": "proposed",
        }
        try:
            store.upsert_route(**payload)
            buffer_pending(state, payload)
            covered.append(item)
        except EnvDetailError:
            continue
        except Exception:  # noqa: BLE001
            continue
    if covered and len(covered) < len(checklist):
        # Partial map: next run reuses covered, discovers the rest.
        try:
            from .discovery_map import item_key
            store.mark_partial_coverage(scope, case.area, app, fp, [item_key(c) for c in covered])
            entry["map_status"] = "partial"
            entry["covered_items"] = list(covered)
        except Exception:  # noqa: BLE001
            pass
    elif covered:
        entry["map_status"] = "complete"
        entry["covered_items"] = list(covered)
    return covered


def ensure_discovery(plan, case, app: str, state, testers: dict, memory,
                     data_prep: list[dict] | None = None,
                     checklist: list[str] | None = None) -> dict:
    """Discovery for this area/app: reuse verified maps when safe, else live (incremental) pass.

    Per checklist item, skip live discovery when the memory DB has a reusable route for the same
    env fingerprint (verified success within DISCOVERY_REUSE_MAX_DAYS, or covered proposed from a
    prior/partial discovery), no change signal, and no failure since last success. Deploy-marker
    changes trigger a code-driven spot check instead of full rediscovery. Force with --rediscover
    / web checkbox / DISCOVERY_REUSE=0.
    """
    from .discovery_map import (
        DiscoveryMapStore, age_days, backfill_from_learned, estimate_calls_saved,
        is_reusable, load_route_synonym_groups, match_item_to_rows, route_hints_from_rows,
        scope_for, spot_check_route,
    )
    key = discovery_key(case.area, app)
    store = state.data.setdefault("discovery", {})
    fp = env_fingerprint(plan, [app])
    stale = (state.data.get("discovery_stale") or {}).get(key)
    stale_items = set(state.data.get("discovery_stale_items", {}).get(key) or [])
    cached = store.get(key)
    want = list(checklist) if checklist is not None else discovery_checklist(
        plan, app, state, area=case.area, data_prep=data_prep)
    force = _force_rediscover(state)
    change_text = _discovery_change_text(plan, case, state)
    marker = (state.data.get("deploy_markers") or {}).get(app) or ""
    plat_ver = state.data.get("detected_platform_version")

    # Empty checklist: still map the case title on the first pass this run; if we already
    # attempted discovery for this key, reuse the in-run cache (avoid a second try that
    # would trip DISCOVERY_TRIES when the tester is flaky in unit tests).
    if not want:
        if cached and not stale and cached.get("env") == fp and not force:
            return cached
        want = [case.title or case.id or f"map {app}"]

    # Per-run cache: if already fully covered this run and not forced stale, return it.
    if cached and not stale and cached.get("env") == fp and not force and not stale_items:
        missing = _checklist_covered(cached, want)
        if not missing:
            return cached

    reused_rows: list[dict] = []
    reuse_routes: list[str] = []
    need_live: list[str] = []
    spot_n = 0
    map_store = DiscoveryMapStore.from_memory(memory) if memory is not None else None
    sc = scope_for(memory) if memory is not None else "project"
    # Drop stuck/garbage rows so they never get reused (and never block lazy skip).
    if map_store is not None and not state.data.get("_discovery_bad_staled"):
        try:
            n_bad = map_store.mark_bad_rows_stale(sc, fp)
            if n_bad:
                state.data.setdefault("notes", []).append(
                    f"discovery_map: marked {n_bad} stuck/garbage route(s) stale")
            state.data["_discovery_bad_staled"] = True
        except Exception:  # noqa: BLE001
            pass
    # Once per run: import pack ## Routes as verified (source=pack); skip env-bound lines.
    if map_store is not None and not state.data.get("_learned_routes_backfilled"):
        warnings: list[str] = []
        try:
            backfill_from_learned(
                state.data.get("pack_root"), map_store,
                scope=sc, env_fingerprint=fp,
                platform=state.data.get("detected_platform"),
                platform_version=plat_ver,
                warn=warnings.append,
            )
        except Exception:  # noqa: BLE001
            pass
        for w in warnings:
            state.data.setdefault("notes", []).append(w)
        state.data["_learned_routes_backfilled"] = True
        try:
            state.save()
        except Exception:  # noqa: BLE001
            pass
    # Pack routes land under area=general; also list area-specific rows for this case.
    known_rows = []
    synonym_groups = load_route_synonym_groups(
        state.data.get("pack_cfg") if state else None,
        platform_id=state.data.get("detected_platform") if state else None,
        pack_root=state.data.get("pack_root") if state else None,
    )
    if map_store is not None:
        known_rows = map_store.list_for(sc, case.area, app, fp)
        for r in map_store.list_for(sc, "general", app, fp):
            if r not in known_rows and not any(
                    x.get("item_key") == r.get("item_key") for x in known_rows):
                known_rows.append(r)

    for item in want:
        if force or item in stale_items:
            need_live.append(item)
            continue
        row = match_item_to_rows(known_rows, item, synonym_groups=synonym_groups)
        if not row:
            need_live.append(item)
            continue
        decision = is_reusable(
            row, force=False, change_text=change_text,
            current_platform_version=plat_ver, current_marker=marker,
        )
        if decision == "reuse":
            reused_rows.append(row)
            if row.get("route_text"):
                reuse_routes.append(row["route_text"])
            continue
        if decision == "spot_check":
            labels = list(row.get("key_labels") or [])
            # Prefer injectable nav/snap (tests); else last auto page_state for this app if present.
            nav = (state.data.get("_spot_check_nav") or {}).get(app)
            snap = ((state.data.get("_spot_check_snap") or {}).get(app)
                    or (state.data.get("_page_state_by_app") or {}).get(app)
                    or "")
            ok, missing, _snap = spot_check_route(
                route_text=row.get("route_text") or "", key_labels=labels,
                snapshot_text=snap,
                navigate_and_snapshot=nav,
            )
            spot_n += 1
            if ok and map_store is not None:
                map_store.refresh_marker(row["id"], marker)
                map_store.mark_success(row["id"], deploy_marker=marker)
                reused_rows.append(row)
                if row.get("route_text"):
                    reuse_routes.append(row["route_text"])
                continue
            # Missing labels → incremental discovery for this item only.
            if map_store is not None:
                map_store.mark_stale(row["id"], "spot-check missing: " + ", ".join(missing)[:120])
            need_live.append(item)
            continue
        need_live.append(item)

    # Also honour in-run cache coverage for items not in the durable map.
    if cached and not stale and cached.get("env") == fp and not force:
        still = _checklist_covered(cached, need_live)
        # Items covered by this-run notes but not needing rediscovery.
        need_live = still

    saved = estimate_calls_saved(len(reused_rows))
    if not need_live and reused_rows:
        # All reusable — no discovery LLM turn.
        entry = dict(cached) if cached and cached.get("env") == fp else {
            "source": "memory", "recipes": [], "hints": [], "notes": "", "env": fp, "checklist": want,
        }
        entry["source"] = "memory" if reused_rows else entry.get("source", "memory")
        entry["env"] = fp
        entry["checklist"] = list(dict.fromkeys((entry.get("checklist") or []) + want))
        entry["recipes"] = list(dict.fromkeys(list(entry.get("recipes") or []) + reuse_routes))[:12]
        entry["reused_items"] = [r.get("checklist_item") for r in reused_rows]
        entry["notes"] = (entry.get("notes") or "")
        if reuse_routes and "UI map (reused from memory)" not in entry["notes"]:
            entry["notes"] = (
                (entry["notes"] + "\n" if entry["notes"] else "")
                + "UI map (reused from memory):\n" + "\n".join(f"- {r}" for r in reuse_routes[:8])
            ).strip()
        if spot_n:
            entry["spot_checked"] = spot_n
        store[key] = entry
        (state.data.get("discovery_stale") or {}).pop(key, None)
        state.save()
        _log_discovery_reuse(state, key, reused_rows, [], spot_checked=spot_n, calls_saved=saved)
        return entry

    if cached and (stale or cached.get("env") != fp):
        why = stale or f"env changed ({cached.get('env')!r} -> {fp!r})"
        state.checkpoint(f"discovery re-run for {key}: {why}")
        if stale or cached.get("env") != fp:
            # Keep durable maps; only clear this-run cache when env/stale forces a full re-eval.
            if cached.get("env") != fp:
                store.pop(key, None)
                cached = None
        (state.data.get("discovery_stale") or {}).pop(key, None)

    _log_discovery_reuse(state, key, reused_rows, need_live, spot_checked=spot_n, calls_saved=saved)
    prior = cached if cached and cached.get("env") == fp else None
    if prior is None and reuse_routes:
        prior = {"source": "memory", "recipes": reuse_routes, "notes": "\n".join(reuse_routes),
                 "checklist": [r.get("checklist_item") for r in reused_rows], "env": fp}
    # Even when a saved route is not fully reusable (or does not match a checklist
    # name exactly), pass its menu path so live discovery does not start from scratch.
    path_hints = route_hints_from_rows(known_rows)
    entry = _run_discovery(plan, case, app, state, testers, memory, fp, key, store,
                           want=need_live, incremental=bool(reused_rows or prior), prior=prior,
                           route_hints=path_hints)
    if reuse_routes:
        entry["recipes"] = list(dict.fromkeys(reuse_routes + list(entry.get("recipes") or [])))[:12]
        entry["reused_items"] = [r.get("checklist_item") for r in reused_rows]
        store[key] = entry
        state.save()
    # Clear per-item stale flags for items we just rediscovered.
    if stale_items:
        left = [i for i in stale_items if i in need_live and i not in (entry.get("covered_items") or [])]
        # After a live pass, drop stale flags for items we attempted.
        si = state.data.setdefault("discovery_stale_items", {})
        si[key] = [i for i in (si.get(key) or []) if i not in need_live]
        state.save()
    return entry


def _run_discovery(plan, case, app, state, testers, memory, fp, key, store, *,
                   want: list[str], incremental: bool, prior: dict | None,
                   route_hints: list[str] | None = None) -> dict:
    from . import learned
    learned_hints = recipes_covering(memory, case.area, app, state=state, title=case.title or "",
                             pack_root=state.data.get("pack_root"))
    # Known menu paths first so live discovery can follow saved routes (e.g. Orders > …).
    hints = list(dict.fromkeys(list(route_hints or []) + list(learned_hints or [])))
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
    # Per-app case id so CRM and portal discovery never share stuck chat / title.
    area_bit = re.sub(r"[^a-z0-9]+", "-", (case.area or app or "ui").lower()).strip("-") or "ui"
    app_bit = re.sub(r"[^a-z0-9]+", "-", (app or "app").lower()).strip("-") or "app"
    disco_case_id = f"DISCO-{area_bit}-{app_bit}" + ("-inc" if incremental else "")
    hint_note = ""
    if hints:
        hint_note = (
            f" Prior recipes and known menu paths are HINTS only ({len(hints)}): verify each against the live UI; "
            "follow a matching path rather than exploring menus from scratch; "
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
    pseudo = PlannedCase(
        id=disco_case_id,
        title=f"discover UI for {case.area or app} on {app}",
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
    # So propose_memory during discovery can attach routes to checklist items immediately.
    state.data["_active_discovery"] = {
        "area": case.area, "app": app, "env": fp, "checklist": goal_lines, "key": key,
    }
    state.save()
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
    finally:
        # Flush any routes proposed mid-discovery even if the turn crashed / was stopped.
        try:
            from .discovery_map import flush_pending
            flush_pending(state, memory)
        except Exception:  # noqa: BLE001
            pass
        state.data.pop("_active_discovery", None)
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
    # Persist covered routes immediately (partial OK if discovery stopped mid-way after some finds).
    try:
        _persist_discovered_routes(plan, case, app, state, memory, fp, goal_lines,
                                   notes, proposed + list(hints), entry)
    except Exception:  # noqa: BLE001
        pass
    from .memory.pollution import is_infra_error_text
    # Never persist infrastructure failures (timeouts, cancellations, MCP errors)
    # as learned hints, even if other proposed recipes already exist for the area.
    failed = bool(notes) and (notes.startswith("discovery failed") or is_infra_error_text(notes))
    # Live recipes still count when discovery found them, unless the turn itself failed.
    useful = (bool(proposed) and not failed) or bool(
        notes and not blocked and not failed and not _NO_PROGRESS.search(notes))
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
                     + (f" platform={entry.get('platform')}" if entry.get("platform") else "")
                     + (f" covered={len(entry.get('covered_items') or [])}" if entry.get("covered_items") else ""))
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
    """Attach a small slice of discovery hints — never the whole recipe list or learned.md.

    Stays inside the lean retrieval budget (~MEMORY_TURN_TOKEN_BUDGET / 1200 tokens shared with memory).
    """
    from .memory.retrieve import DEFAULT_TOKEN_BUDGET, tokens_of
    brief = dict(brief)
    # Cap route inject to ~half the turn budget so memory + discovery stay lean together.
    route_budget = max(200, DEFAULT_TOKEN_BUDGET // 2)
    recipes: list[str] = []
    used = 0
    for r in list(entry.get("recipes") or [])[:max_recipes]:
        t = tokens_of(r)
        if used + t > route_budget and recipes:
            break
        recipes.append(r)
        used += t
    note_cap = min(max_note_chars, max(120, (route_budget - used) * 4))
    notes = (entry.get("notes") or "")[:note_cap]
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
    # Soft ref so verify/flush paths can reach the Memory handle without serializing it.
    state.data["_memory_ref"] = memory
    try:
        import os as _os
        from .discovery_map import markers_for_plan
        # Cheap deploy markers (no LLM). Skip network fetch in the unit suite / when pre-seeded.
        do_fetch = not (
            state.data.get("_skip_deploy_fetch")
            or state.data.get("deploy_markers")
            or _os.environ.get("QA_TEST")
        )
        markers_for_plan(plan, state, fetch=do_fetch)
    except Exception:  # noqa: BLE001
        pass
    read_snapshot(plan, state, testers, "baseline")
    by_id = {c.id: c for c in plan.cases}
    pack_cfg = pack_cfg or state.data.get("pack_cfg") or {}
    from .guidance_redo import apply_redo, report_undelivered

    try:
        _resolve_upfront(plan, state, testers, memory, data_prep or [], pack_cfg)

        queue = list(execution_order(plan))
        from .parallel import group_ids, parallel_enabled
        i = 0
        while i < len(queue):
            redone = apply_redo(state, plan, pack_cfg)
            if redone:
                # Next-after-current: insert at i, dropping later duplicates of the same ids.
                rest = [c for c in queue[i:] if c not in redone]
                queue = queue[:i] + list(redone) + rest
                continue

            # Conservative parallel: only when PARALLEL_READONLY=1 and pack marks cases safe.
            if parallel_enabled():
                batches = group_ids(plan.cases, queue[i:], pack_cfg)
                batch_ids = batches[0] if batches else [queue[i]]
            else:
                batch_ids = [queue[i]]
            i += len(batch_ids)

            if len(batch_ids) == 1:
                results = [_execute_one_case(plan, by_id.get(batch_ids[0]), state, testers, judge,
                                             max_retries, manual_login, manual_login_apps, memory,
                                             data_prep or [], pack_cfg)]
            else:
                state.checkpoint(f"parallel readonly batch: {', '.join(batch_ids)}")
                results = _run_parallel_cases(batch_ids, plan, by_id, state, testers, judge,
                                              max_retries, manual_login, manual_login_apps, memory,
                                              data_prep or [], pack_cfg)
            for result in results:
                if result is None:
                    continue
                state.set_result(result)
                if on_case_done:
                    on_case_done(result)

        report_undelivered(state, plan)
        read_snapshot(plan, state, testers, "final")
        state.checkpoint("run finished", status="finished")
        # Context-learning retrospective (scores in memory.db beside discovery_maps).
        try:
            from .context import ContextScores, update_scores_from_run
            from .config import Settings
            scores = ContextScores(Settings.load().memory_db)
            update_scores_from_run(scores, state)
            scores.close()
        except Exception:  # noqa: BLE001
            pass
        return {cid: state.result(cid) for cid in execution_order(plan)}
    finally:
        # Persist any verified/proposed discovery routes even on stop / crash / Ctrl-C.
        try:
            from .discovery_map import flush_pending
            flush_pending(state, memory)
        except Exception:  # noqa: BLE001
            pass
        state.data.pop("_memory_ref", None)


def _execute_one_case(plan, case, state, testers, judge, max_retries, manual_login,
                      manual_login_apps, memory, data_prep, pack_cfg):
    if case is None:
        return None
    if state.result(case.id):
        return state.result(case.id)
    from .agents.llm import TRACE_CASE
    from .data_prep import ensure_data, after_case, ensure_contact
    from .placeholders import case_unresolved_tokens
    tok = TRACE_CASE.set(case.id)
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
            ensure_case_discovery(plan, case, state, testers, memory, data_prep)
            result = ensure_contact(plan, case, state, testers, data_prep)
        if result is None and not case.skip_reason:
            result = ensure_data(plan, case, state, testers, data_prep)
        if result is None:
            result = _run_case(plan, case, state, testers, judge, max_retries, manual_login,
                               manual_login_apps, memory=memory, pack_cfg=pack_cfg)
            after_case(case, state, data_prep)
        return result
    finally:
        TRACE_CASE.reset(tok)


def _run_parallel_cases(batch_ids, plan, by_id, state, testers, judge, max_retries,
                        manual_login, manual_login_apps, memory, data_prep, pack_cfg):
    """Run 2–3 parallel-safe cases with separate browser contexts (thread pool).

    Shared RunState is checkpoint-serialized; each case still uses its own Playwright profile
    key (`<app>__<case_id>`) when testers support it — otherwise they serialize on the same app
    profile (still correct, just less parallel).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    # Prep/discovery must stay serial (shared session / data plan); only the case body can fan out
    # when every case in the batch is already discovered and contact-resolved.
    prepared = []
    for cid in batch_ids:
        case = by_id.get(cid)
        if case is None or state.result(cid):
            continue
        block = _deps_block(case, state)
        if block is not None:
            results.append(block)
            continue
        from .placeholders import case_unresolved_tokens
        missing = case_unresolved_tokens(case, state, pack_cfg)
        if missing:
            results.append(CaseResult(
                case_id=case.id, verdict="BLOCKED", expected=case.expected,
                note="prep-missing: " + ", ".join(missing)))
            continue
        ensure_case_discovery(plan, case, state, testers, memory, data_prep)
        from .data_prep import ensure_contact, ensure_data, after_case
        blocked = ensure_contact(plan, case, state, testers, data_prep)
        if blocked is not None:
            results.append(blocked)
            continue
        blocked = ensure_data(plan, case, state, testers, data_prep)
        if blocked is not None:
            results.append(blocked)
            continue
        prepared.append(case)

    if not prepared:
        return results
    if len(prepared) == 1:
        results.append(_run_case(plan, prepared[0], state, testers, judge, max_retries,
                                 manual_login, manual_login_apps, memory=memory, pack_cfg=pack_cfg))
        from .data_prep import after_case
        after_case(prepared[0], state, data_prep)
        return results

    def work(case):
        from .agents.llm import TRACE_CASE
        tok = TRACE_CASE.set(case.id)
        try:
            return _run_case(plan, case, state, testers, judge, max_retries, manual_login,
                             manual_login_apps, memory=memory, pack_cfg=pack_cfg)
        finally:
            TRACE_CASE.reset(tok)

    with ThreadPoolExecutor(max_workers=min(3, len(prepared))) as pool:
        futs = {pool.submit(work, c): c for c in prepared}
        for fut in as_completed(futs):
            case = futs[fut]
            try:
                r = fut.result()
            except Exception as e:  # noqa: BLE001
                r = CaseResult(case_id=case.id, verdict="AGENT INCOMPLETE", expected=case.expected,
                               note=f"parallel run failed: {type(e).__name__}: {e}")
            results.append(r)
            from .data_prep import after_case
            after_case(case, state, data_prep)
    return results


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
    # M-Pesa only when the case asks for it AND a phone is provided; otherwise SKIPPED with reason.
    from .data_prep import case_calls_for_mpesa, mpesa_gate
    if case_calls_for_mpesa(case):
        why = mpesa_gate(case=case, state=state)
        if why:
            return CaseResult(case_id=case.id, verdict="SKIPPED", expected=case.expected, note=why)
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
            # Never promote placeholder/negative words into lasting run state.
            from .tools.value_kind import is_negative_word, has_redaction_token
            if is_negative_word(v) or has_redaction_token(v):
                continue
            state.values.setdefault(k, {"value": v, "source": f"{case.id}/{app}"})
        if getattr(sr, "not_found", False) and not (sr.stuck or sr.limit_hit):
            note = f"tester on {app} found nothing usable: {sr.notes}".strip()
            result = CaseResult(case_id=case.id, verdict="BLOCKED", expected=case.expected,
                                values={k: v for r in results for k, v in r.values.items()
                                        if not is_negative_word(v) and not has_redaction_token(v)},
                                evidence=[s for r in results for s in r.screenshots], note=note)
            _learn_failure(case, app, state, log_mark, "not_found", sr.notes or "")
            return result
        if sr.stuck or sr.limit_hit:
            why = ("kept repeating the same action without progress" if getattr(sr, "loop", False)
                   else "hit the step/time limit" if sr.limit_hit else "got stuck")
            # Safety net: if this turn used a reused map and the route failed (missing label / loop / 404 / XSRF),
            # mark that item stale, rediscover it only, and retry the turn once — never block on a reused map.
            retried = state.data.setdefault("_disco_safety_retry", {}).get(f"{case.id}:{app}")
            failed_item = None if retried else _reused_route_failure(disco, state, log_mark, sr)
            if failed_item:
                state.data.setdefault("_disco_safety_retry", {})[f"{case.id}:{app}"] = True
                state.checkpoint(f"{case.id}: reused route failed ({why}); rediscovering {failed_item!r}")
                rediscover_failed_item(plan, case, app, state, testers, memory, failed_item, why)
                disco = (state.data.get("discovery") or {}).get(discovery_key(case.area, app)) or disco
                brief = inject_discovery(brief_for(plan, case, steps, state), disco)
                brief = inject_memory(brief, memory, state, app)
                brief = inject_placeholders(brief, state, pack_cfg)
                log_mark = len(state.data["action_log"])
                try:
                    sr = tester(app, brief, state)
                    sr = _maybe_continue(app, brief, sr, state, tester, log_mark)
                    results[-1] = sr
                    if sr.steps_done and not (sr.stuck or sr.limit_hit or sr.needs_login):
                        _learn_route(case, app, state, log_mark)
                    if not (sr.stuck or sr.limit_hit or sr.needs_login):
                        continue
                except Exception:  # noqa: BLE001
                    pass
                why = ("kept repeating the same action without progress" if getattr(sr, "loop", False)
                       else "hit the step/time limit" if sr.limit_hit else "got stuck")
            note = f"tester on {app} {why}: {sr.notes}".strip()
            result = CaseResult(case_id=case.id, verdict="AGENT INCOMPLETE", expected=case.expected,
                                values={k: v for r in results for k, v in r.values.items()
                                        if not is_negative_word(v) and not has_redaction_token(v)},
                                evidence=[s for r in results for s in r.screenshots], note=note)
            _learn_failure(case, app, state, log_mark, why, sr.notes or "")
            # A recipe/path that failed this run must not keep discovery skipped next time.
            mark_discovery_stale(state, case.area, app, why, memory=memory, plan=plan)
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
    """Fixed code writes the route this tester used into the pack's learned.md and this run's discovery entry.

    Also marks matching discovery-map items verified (mark_step progress after following the route).
    """
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
        verify_discovery_maps_after_case(case, app, state, acts)
    except Exception:  # noqa: BLE001  learning must never fail a case
        pass


def verify_discovery_maps_after_case(case, app, state, acts: list[dict] | None = None) -> int:
    """Mark durable map items verified when a case used their route (mark_step progress). Returns count."""
    from .discovery_map import (
        DiscoveryMapStore, buffer_pending, item_key, labels_from_route, scope_for, _safe_route,
    )
    from .memory.env_guard import EnvDetailError
    memory = state.data.get("_memory_ref")  # optional; callers may pass via state
    # Prefer live memory handle stashed by main/run_plan.
    mem = memory or state.data.get("memory")
    # run_plan keeps memory only as arg — stash on state if present.
    if mem is None:
        return 0
    plan = state.plan
    if plan is None:
        return 0
    ms = DiscoveryMapStore.from_memory(mem)
    if ms is None:
        return 0
    fp = env_fingerprint(plan, [app])
    marker = (state.data.get("deploy_markers") or {}).get(app) or ""
    rows = ms.list_for(scope_for(mem), case.area, app, fp)
    if not rows:
        return 0
    from . import learned as _learned
    hops = _learned.route_from_actions(acts or [], app)
    hop_blob = " ".join(hops).lower()
    n = 0
    for row in rows:
        labels = [str(x).lower() for x in (row.get("key_labels") or [])]
        route = (row.get("route_text") or "").lower()
        item = (row.get("checklist_item") or "").lower()
        hit = False
        if labels and any(lab and lab in hop_blob for lab in labels):
            hit = True
        elif route and any(tok and tok in hop_blob for tok in re.findall(r"[a-z0-9]{4,}", route)[:6]):
            hit = True
        elif item and any(tok and tok in hop_blob for tok in re.findall(r"[a-z0-9]{4,}", item)[:4]):
            hit = True
        # Also: any mark_step this turn after navigating counts as using the injected map.
        if not hit and hops and row.get("status") in ("proposed", "verified", "partial"):
            # Soft verify when the case progressed on this app after discovery inject.
            if any(a.get("tool") == "mark_step" for a in (acts or []) if a.get("app") == app):
                hit = True
        if not hit:
            continue
        try:
            ms.mark_verified(row["id"], deploy_marker=marker or None)
            n += 1
        except Exception:  # noqa: BLE001
            continue
    return n


def rediscover_failed_item(plan, case, app, state, testers, memory, item: str, why: str = "") -> dict | None:
    """Safety net: mark item stale, run incremental discovery for it only, return updated entry."""
    mark_discovery_stale(state, case.area, app, why or "reused route failed",
                         checklist_item=item, memory=memory, plan=plan)
    try:
        return ensure_discovery(plan, case, app, state, testers, memory, checklist=[item])
    except Exception:  # noqa: BLE001 — never block the case on rediscovery failure
        state.data.setdefault("notes", []).append(
            f"incremental rediscovery for {item!r} failed after reused route error ({why})")
        return None


_ROUTE_FAIL = re.compile(
    r"(?i)not found|no such (?:element|button|link|label)|target label|404|xsrf|"
    r"acceptable referer|could not find|unable to (?:find|click|open)|maximum repeated"
)


def _reused_route_failure(disco: dict, state, log_mark: int, sr) -> str | None:
    """If this turn used a reused map and failed on a missing label / loop / 404 / XSRF, return the checklist item."""
    if not disco or disco.get("source") not in ("memory", "discovery", "discovery-inc"):
        return None
    reused = list(disco.get("reused_items") or [])
    if not reused and disco.get("source") != "memory":
        return None
    acts = state.data.get("action_log") or []
    blob = " ".join(
        str(a.get("outcome") or "") + " " + str((a.get("args") or {}).get("element") or "")
        for a in acts[log_mark:]
    )
    blob += " " + (sr.notes or "")
    if getattr(sr, "loop", False):
        pass
    elif not _ROUTE_FAIL.search(blob) and not any(
            a.get("tool") in ("not_found", "xsrf_detected") for a in acts[log_mark:]):
        return None
    # Prefer the first reused item; callers rediscover one item at a time.
    if reused:
        return reused[0]
    checklist = list(disco.get("checklist") or [])
    return checklist[0] if checklist else "UI map"


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
