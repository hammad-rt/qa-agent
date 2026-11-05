"""Data preparation before a case: find a record the case can use (e.g. an unpaid shipping-only order),
or create one, then check it is usable. Rules come from the pack's `data_prep` list. Staging/pre-prod only;
on production too (only for the pack test user; payments stay skipped there)."""
from __future__ import annotations

import os
import json
import re

from .models import CaseResult, PlannedCase, Step


PREP_MAX_ACTIONS = int(os.environ.get("PREP_MAX_ACTIONS", "120"))


def _case_text(case) -> str:
    parts = [case.title or "", " ".join(case.preconditions or []), case.expected or ""]
    parts += [s.text for s in case.steps]
    return " ".join(parts).lower()


def matching_rule(case, rules: list[dict]) -> dict | None:
    m = matching_rules(case, rules)
    return m[0] if m else None


def matching_rules(case, rules: list[dict]) -> list[dict]:
    text, out = _case_text(case), []
    for r in rules or []:
        w = r.get("when", {})
        if w.get("areas") and case.area not in w["areas"]:
            continue
        if any(re.search(k, text, re.I) for k in w.get("any", [])) and not any(
                re.search(k, text, re.I) for k in w.get("none", [])):
            out.append(r)
    return out


def consumes(case, rule: dict) -> bool:
    """True when the case uses up the record (e.g. pays the order), so the next case needs a fresh one."""
    return any(re.search(k, _case_text(case), re.I) for k in rule.get("consumed_by", []))


_NO_REWARDS = r"without (any |the |savo )*rewards|no (savo )?rewards (applied|used)|(do not|don't|not) (apply|use) (any |savo )*rewards"


def uses_rewards(case) -> bool:
    """True when the case applies or redeems Savo Rewards; False for a plain card payment
    (e.g. the case that pays without rewards to fill the wallet)."""
    text = _case_text(case)
    if re.search(_NO_REWARDS, text, re.I):
        return False
    return bool(re.search(r"\breward|\bredeem|use up to|wallet", text, re.I))


def verify_text(case, v: dict) -> str:
    """The check's text, with {rewards} filled for this case: the rewards-box condition only when the case
    uses rewards."""
    t = v["text"]
    if "{rewards}" in t:
        part = v.get("rewards" if uses_rewards(case) else "no_rewards", "")
        t = t.replace("{rewards}", part.replace("{space}", " "))
    return t


def _turn(tester, plan, state, rule, app, text, read_only, case_id):
    from .coordinator import brief_for
    pseudo = PlannedCase(id=case_id, title=f"data prep: {rule['id']}", area=rule.get("area"),
                         steps=[Step(text=text, app=app)], values_to_record=[rule["value"]])
    brief = brief_for(plan, pseudo, [0], state)
    brief["data_prep"] = True
    brief["max_tool_calls"] = PREP_MAX_ACTIONS            # creating an order is one long step; let it finish
    brief["actions_per_step"] = PREP_MAX_ACTIONS
    brief["read_only"] = read_only
    brief["values_to_record"] = [rule["value"]] + list(rule.get("extra_values", []))
    return tester(app, brief, state)


def ensure_data(plan, case, state, testers: dict, rules: list[dict]) -> CaseResult | None:
    """Run every matching prep rule in pack order. None when all are ready, else the first BLOCKED result."""
    for rule in matching_rules(case, rules):
        r = _ensure_one(plan, case, state, testers, rule)
        if r is not None:
            return r
    return None


_NONE = {"", "none", "no", "n/a", "na", "null", "not found", "nothing", "-"}
_REWARDS_ONLY = re.compile(r"no savo rewards|rewards box|use up to|wallet|no rewards", re.I)


def _is_none(v) -> bool:
    return v is None or str(v).strip().strip("'\"").lower() in _NONE


def _num(v) -> float | None:
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(v or "").replace("\u2212", "-"))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _answer(v) -> str:
    """'yes', 'no' or 'unclear' for a <value>_ok answer."""
    t = str(v or "").strip().strip("'\"").lower()
    if re.match(r"yes\b", t):
        return "yes"
    if re.match(r"no\b", t):
        return "no"
    return "unclear"


def _call(tester, plan, state, rule, app, text, read_only, case_id, key):
    """One prep turn. Like a case turn, a dropped model server is waited for and the turn replayed,
    as long as nothing was changed yet."""
    from .coordinator import RunPaused
    from .reliability import is_retryable, wait_for_model
    outages = 0
    while True:
        mark = len(state.data.get("action_log", []))
        try:
            return _turn(tester, plan, state, rule, app, text, read_only, case_id)
        except RunPaused:
            raise
        except Exception as e:  # noqa: BLE001
            changed = [a for a in state.data.get("action_log", [])[mark:]
                       if a.get("approval") in ("approved", "pre_approved")]
            if is_retryable(e) and not changed and outages < 3:
                outages += 1
                state.checkpoint(f"data prep {key}: model server unreachable, waiting for it to come back")
                if wait_for_model():
                    continue
            raise


def _usable(rule, rec, used) -> bool:
    if _is_none(rec) or str(rec).strip() in {str(u) for u in used}:
        return False
    if rule.get("min_value") is not None:                   # e.g. a wallet balance: negative or 0 is not enough
        n = _num(rec)
        return n is not None and n >= float(rule["min_value"])
    return True


CREATE_TRIES = 2


def _ensure_one(plan, case, state, testers: dict, rule: dict) -> CaseResult | None:
    store = state.data.setdefault("prep", {})
    key, value = rule["id"], rule["value"]
    have = store.get(key)
    if have and not have.get("consumed") and not _usable(rule, have.get("value"), []):
        have = None                     # e.g. a resumed run that stored a negative wallet as "ready"
    if have and not have.get("consumed"):
        state.values[value] = {"value": have["value"], "source": f"prep/{key}"}
        if have.get("record"):
            state.values[f"{value}_record"] = {"value": have["record"], "source": f"prep/{key}"}
        return None

    def blocked(why):
        state.checkpoint(f"data prep {key} for {case.id} failed: {why}")
        return CaseResult(case_id=case.id, verdict="BLOCKED", expected=case.expected,
                          note=f"test data not ready ({key}): {why}")

    if rule.get("envs") and plan.env not in rule["envs"]:
        return blocked(rule.get("blocked_note") or f"this preparation is not possible on {plan.env}")
    clients = rule.get("test_client")
    name = clients.get(plan.env) if isinstance(clients, dict) else clients
    user = next((r.value for r in getattr(plan, "requirements", []) or []
                 if r.key.startswith("account.portal.") and r.value), None)
    if not (name or user):
        return blocked("no portal test user picked and no test client set for this environment; "
                       "the agent will not create orders for an unconfirmed account")
    client = (f"{name} (the CRM client of portal login {user})" if name and user else name
              or f"the CRM client whose portal login / email is {user}")
    for k in ("find", "create"):
        if rule.get(k):
            rule = {**rule, k: rule[k].replace("{client}", client or "")}
    app = rule.get("app", "crm")
    tester = testers.get(app)
    if tester is None:
        return blocked(f"no tester for {app}")
    rejected = state.data.setdefault("prep_rejected", {}).setdefault(key, [])
    used = ([s["value"] for s in store.values() if s.get("consumed")] + [r["value"] for r in rejected]
            + list(rule.get("never_use", [])))
    skip = ", ".join(f"{r['value']} ({r['why'][:80]})" for r in rejected)
    skip = ", ".join(x for x in [", ".join(str(u) for u in used if u not in [r["value"] for r in rejected]), skip] if x)

    def rec_of(sr):
        return sr.values.get(value), sr.values.get(f"{value}_record")

    # 1. FIND: read-only, exact fit only. A near-fit is never edited to make it fit (that is what made prep loop).
    find = (f"FIND ONLY. This turn is read-only: open lists and records and read them; change nothing.\n"
            f"{rule['find']}\nDo not use: {skip or 'none'}.\n"
            f"Only an exact fit counts. Never edit, fix, re-status or add to a record to make it fit; a near-fit "
            f"is a 'none'. If one fits exactly, record_value('{value}', '<its number exactly as shown, with its "
            f"letter prefix, e.g. A1142>') and record_value('{value}_record', '<the CRM record id from the page "
            f"URL, the record=... part>'), then finish. An order with rewards still pending from an earlier run is "
            f"still a fit (the check clears them). If none fits exactly, record_value('{value}', 'none') and finish. "
            f"Look at no more than the first two list pages.")
    sr = None
    try:
        sr = _call(tester, plan, state, rule, app, find, True, f"PREP-{key}-find", key)
        rec, record = rec_of(sr)
    except Exception as e:  # noqa: BLE001
        from .logsetup import short_error
        state.checkpoint(f"data prep {key}: find failed ({short_error(e, f'data prep {key} find')}); creating one")
        rec, record = None, None
    if not _usable(rule, rec, used):
        if rec is not None and not _is_none(rec):
            state.checkpoint(f"data prep {key}: found {rec}, which is not enough or already used; creating one")
        rec = record = None

    for attempt in range(CREATE_TRIES + 1):
        if rec is None:
            if attempt == CREATE_TRIES:
                break
            if not rule.get("create"):
                return blocked(f"nothing fits and this rule has no create step. {(sr.notes or '') if sr else ''}".strip())
            create = (f"CREATE: {rule['create']}\nDo not reuse: {skip or 'none'}.\n"
                      f"When it is done, record_value('{value}', '<the new number, with its letter prefix>') and "
                      f"record_value('{value}_record', '<the CRM record id from the page URL, record=...>'), then "
                      f"finish. If you cannot finish it, call finish with notes starting 'BLOCKED:' saying where you "
                      f"stopped and the number of any record you created.")
            try:
                sr = _call(tester, plan, state, rule, app, create, False, f"PREP-{key}-create", key)
            except Exception as e:  # noqa: BLE001
                from .logsetup import short_error
                return blocked(short_error(e, f"data prep {key} create"))
            rec, record = rec_of(sr)
            if not _usable(rule, rec, used):
                why = (sr.notes or "").strip()
                why = why[8:].strip() if why.upper().startswith("BLOCKED:") else why
                return blocked(why or f"creating a record did not give a usable {value} ({rec})")
        if not rule.get("verify"):
            break
        verdict, why = _verify(plan, case, state, testers, rule, rec, record, key)
        if verdict == "yes":
            break
        if verdict == "error":
            return blocked("check failed: " + why)
        if verdict == "no" and uses_rewards(case) and _REWARDS_ONLY.search(why):
            # the order is fine; it is the wallet that has nothing to redeem. Keep the order for other cases.
            store[key] = {"value": rec, "record": record, "consumed": False, "for": case.id}
            state.save()
            return blocked(f"{rec} is fine but the wallet has no usable rewards ({why}); the wallet needs a top-up "
                           f"first (pay an order and deliver it, or add rewards by hand)")
        rejected.append({"value": rec, "why": why or verdict})
        used.append(rec)
        skip = (skip + ", " if skip else "") + f"{rec} ({(why or verdict)[:80]})"
        state.save()
        state.checkpoint(f"data prep {key}: {rec} is not usable ({why or verdict}); trying a new one")
        rec = record = None
    if rec is None:
        return blocked(f"no usable record after {CREATE_TRIES} tries; rejected: "
                       + "; ".join(f"{r['value']}: {r['why']}" for r in rejected[-CREATE_TRIES:]))
    store[key] = {"value": rec, "record": record, "consumed": False, "for": case.id}
    state.values[value] = {"value": rec, "source": f"prep/{key}"}
    if record:
        state.values[f"{value}_record"] = {"value": record, "source": f"prep/{key}"}
    state.save()
    state.checkpoint(f"data prep {key}: using {rec} for {case.id}")
    return None


def _verify(plan, case, state, testers, rule, rec, record, key) -> tuple[str, str]:
    """Ask the check app whether the record is usable. ('yes'|'no'|'unclear'|'error', reason).
    An answer that is not a plain yes / no is asked once more."""
    v, value = rule["verify"], rule["value"]
    vt = testers.get(v.get("app", "portal"))
    if vt is None:
        return "yes", ""
    text = (verify_text(case, v).replace("{value}", str(rec)).replace("{record}", str(record or "unknown")) +
            f"\nIf it is usable, record_value('{value}_ok', 'yes'); otherwise record_value('{value}_ok', 'no: <why>'). "
            f"Record exactly 'yes' or 'no: <why>' as {value}_ok, never a number or amount from the page.")
    for n in range(2):
        try:
            vr = _call(vt, plan, state, rule, v.get("app", "portal"), text, True, f"PREP-{key}-check", key)
        except Exception as e:  # noqa: BLE001
            from .logsetup import short_error
            return "error", short_error(e, f"data prep {key} check")
        raw = vr.values.get(f"{value}_ok", "")
        a = _answer(raw)
        if a != "unclear":
            why = re.sub(r"^\s*no\b[:\s-]*", "", str(raw), flags=re.I).strip()
            return a, why
        text += (f"\nYour last answer was {str(raw)[:60]!r}, which is not a yes or no. Answer only "
                 f"record_value('{value}_ok', 'yes') or record_value('{value}_ok', 'no: <why>').")
    return "unclear", f"the check gave no yes/no answer ({str(raw)[:60] or (vr.notes or '')[:120] or 'nothing'})"


def after_case(case, state, rules: list[dict]) -> None:
    for rule in matching_rules(case, rules):
        if consumes(case, rule) and rule["id"] in state.data.get("prep", {}):
            state.data["prep"][rule["id"]]["consumed"] = True
            state.save()
