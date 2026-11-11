"""Same-run rerun: re-check data-plan items against find criteria before reusing prep data.

When a case is reopened we do not blindly keep ORDER-1 / CONTACT-1 from the last attempt.
Each placeholder is checked against its pack find criteria (and consumed flag). Items that
still match are reused; others are invalidated so ensure_data recreates them. Cases that
depend on a recreated token are added to the reopen queue.
"""
from __future__ import annotations

from typing import Callable

from . import placeholders as ph
from .guidance_redo import dependents


LookupFn = Callable[[dict, dict], dict | None]
# lookup(rule, criteria) -> record dict or None (sync). Injected in tests; live runs may
# pass a wrapper around agent_tester.lookups.lookup_from_pack when a browser is open.


def _prep_consumed(state, prep_id: str | None) -> bool:
    if not prep_id:
        return False
    return bool(((state.data.get("prep") or {}).get(prep_id) or {}).get("consumed"))


def _stored_entry(state, token: str) -> dict | None:
    return (state.data.get("placeholders") or {}).get(token.upper())


def _min_value_ok(entry: dict | None, rule: dict) -> bool:
    """Soft numeric floor from rule.min_value or find criteria (e.g. available_credit)."""
    if not entry:
        return False
    crit = rule.get("find") if isinstance(rule.get("find"), dict) else {}
    min_v = rule.get("min_value")
    if min_v is None and isinstance(crit, dict):
        min_v = crit.get("min_value") or crit.get("available_credit") or crit.get("min_available")
    if min_v is None:
        return True
    try:
        floor = float(min_v)
    except (TypeError, ValueError):
        return True
    raw = entry.get("value")
    if raw is None and isinstance(entry.get("record"), dict):
        raw = entry["record"].get("available") or entry["record"].get("value")
    try:
        return float(str(raw).replace(",", "").replace("$", "").strip()) >= floor
    except (TypeError, ValueError):
        return False


def criteria_still_match(
    state,
    token: str,
    rule: dict,
    *,
    lookup: LookupFn | None = None,
) -> tuple[bool, str]:
    """Return (ok, reason). ok means the stored value can be reused for the next attempt."""
    entry = _stored_entry(state, token)
    if not entry:
        return False, "no stored value"
    if _prep_consumed(state, entry.get("prep_id") or rule.get("id")):
        return False, "prep marked consumed"
    if not _min_value_ok(entry, rule):
        return False, "below find min_value"

    find = rule.get("find")
    # Full find criteria via deterministic lookup when a callback is provided (PR #8 lookups).
    if lookup is not None and find:
        criteria = find if isinstance(find, dict) else {"query": str(find)}
        try:
            hit = lookup(rule, criteria)
        except Exception as e:  # noqa: BLE001 — never block rerun on a flaky lookup
            return False, f"lookup error: {e}"
        if hit is None:
            # Lookup kind unavailable → soft pass on store (same as no lookup).
            return True, "stored value ok (no lookup kind)"
        if not hit:
            return False, "find criteria returned no match"
        new_val = hit.get("value") or hit.get("id") or hit.get("record_id") or hit.get("name")
        old_val = entry.get("value")
        if new_val is not None and old_val is not None and str(new_val).strip() != str(old_val).strip():
            return False, "stored value no longer matches find result"
        return True, "lookup confirmed"

    # No live lookup: accept non-consumed store that passes soft criteria.
    return True, "stored value ok"


def sync_lookup_from_pack(pack_cfg: dict | None, browser, *, base_url: str,
                          platform: str = "suitecrm7") -> LookupFn | None:
    """Wrap lookups.lookup_from_pack for reverify when a browser session is already open."""
    if browser is None or not base_url:
        return None
    import asyncio
    from .lookups import lookup_from_pack

    def _fn(rule: dict, criteria: dict) -> dict | None:
        kind = (rule.get("lookup") or "").strip()
        if not kind:
            maps = ((pack_cfg or {}).get("lookups") or {}).get("maps") or {}
            # Infer contact_by_email when find looks like an email filter.
            if isinstance(criteria, dict) and (criteria.get("email") or "email" in str(criteria).lower()):
                kind = "contact_by_email"
            elif not maps:
                return None
            else:
                return None
        params = dict(criteria) if isinstance(criteria, dict) else {"value": criteria}
        try:
            res = asyncio.run(lookup_from_pack(
                browser, pack_cfg or {}, kind, base_url=base_url, platform=platform, **params))
        except Exception:  # noqa: BLE001
            return {}
        if not res or not res.ok:
            return {}
        return {"value": res.record_id or (res.values or {}).get("value"),
                "id": res.record_id, "record_id": res.record_id, "values": res.values}

    return _fn


def reverify_data_plan(
    state,
    plan,
    pack_cfg: dict | None,
    *,
    case_ids: list[str] | None = None,
    lookup: LookupFn | None = None,
) -> dict[str, dict]:
    """Check each data-plan token used by case_ids (or all plan tokens).

    Returns {TOKEN: {"action": "reuse"|"recreate", "why": str, "cases": [...]}}.
    Side effect: invalidate_for_redo on tokens that must be recreated.
    """
    rows = ph.build_data_plan(plan, pack_cfg)
    by_tok = {r["token"]: r for r in rows if not r.get("missing")}
    if case_ids:
        want = set()
        for cid in case_ids:
            case = next((c for c in plan.cases if c.id == cid), None)
            if case:
                want |= {t.upper() for t in ph.tokens_in_case(case, ph.token_pattern(pack_cfg), pack_cfg=pack_cfg)}
        tokens = [t for t in by_tok if t in want]
    else:
        tokens = list(by_tok)

    out: dict[str, dict] = {}
    recreate: list[str] = []
    for tok in tokens:
        row = by_tok[tok]
        rule = row.get("rule") or ph.rule_from_item(ph.item_by_token(pack_cfg, tok) or {"name": tok})
        ok, why = criteria_still_match(state, tok, rule, lookup=lookup)
        action = "reuse" if ok else "recreate"
        out[tok] = {"action": action, "why": why, "cases": list(row.get("cases") or [])}
        if action == "recreate":
            recreate.append(tok)

    if recreate:
        ph.invalidate_for_redo(state, recreate)

    state.data["data_reuse"] = out
    state.save()
    return out


def expand_rerun_queue(
    plan,
    roots: list[str],
    data_reuse: dict[str, dict] | None = None,
) -> list[str]:
    """Roots + guidance dependents + any case that uses a token marked recreate."""
    queue = list(dependents(plan, roots))
    if data_reuse:
        for tok, info in data_reuse.items():
            if info.get("action") != "recreate":
                continue
            for cid in info.get("cases") or []:
                if cid not in queue:
                    queue.append(cid)
            # Also pull dependents of those cases (mutating recreate can poison later steps).
            queue = list(dependents(plan, queue))
    return queue


def prepare_same_run_rerun(
    state,
    plan,
    pack_cfg: dict | None,
    case_ids: list[str],
    *,
    lookup: LookupFn | None = None,
    archive: bool = True,
) -> dict:
    """Archive current attempt, reverify data plan, expand queue, reopen cases.

    Returns a summary dict for logs / UI: attempt, cases, data_reuse.
    """
    archived = state.archive_attempt() if archive else None
    # Reverify against the tokens touched by the requested roots (before expand).
    data_reuse = reverify_data_plan(
        state, plan, pack_cfg, case_ids=case_ids, lookup=lookup,
    )
    queue = expand_rerun_queue(plan, case_ids, data_reuse)
    state.reopen_cases(queue)
    state.data["data_reuse"] = data_reuse
    state.save()
    return {
        "archived_attempt": archived,
        "attempt": int(state.data.get("attempt") or 1),
        "cases": queue,
        "data_reuse": data_reuse,
    }
