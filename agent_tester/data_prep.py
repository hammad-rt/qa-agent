"""Data preparation before a case: find a record the case can use (e.g. an unpaid shipping-only order),
or create one, then check it is usable. Rules come from the pack's `data_prep` list. Staging/pre-prod only;
on production too (only for the pack test user; payments stay skipped there)."""
from __future__ import annotations

import json
import re

from .models import CaseResult, PlannedCase, Step


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


def _turn(tester, plan, state, rule, app, text, read_only, case_id):
    from .coordinator import brief_for
    pseudo = PlannedCase(id=case_id, title=f"data prep: {rule['id']}", area=rule.get("area"),
                         steps=[Step(text=text, app=app)], values_to_record=[rule["value"]])
    brief = brief_for(plan, pseudo, [0], state)
    brief["data_prep"] = True
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


def _ensure_one(plan, case, state, testers: dict, rule: dict) -> CaseResult | None:
    store = state.data.setdefault("prep", {})
    key, value = rule["id"], rule["value"]
    have = store.get(key)
    if have and not have.get("consumed"):
        state.values[value] = {"value": have["value"], "source": f"prep/{key}"}
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
        rule = {**rule, k: rule[k].replace("{client}", client or "")}
    app = rule.get("app", "crm")
    tester = testers.get(app)
    if tester is None:
        return blocked(f"no tester for {app}")
    used = [s["value"] for s in store.values() if s.get("consumed")] + list(rule.get("never_use", []))
    text = (f"FIND: {rule['find']}\nDo not use: {', '.join(str(u) for u in used) or 'none'}.\n"
            f"If one fits, record it with record_value('{value}', '<its number exactly as shown, with its letter prefix, e.g. A1142>') "
            f"and record_value('{value}_record', '<the CRM record id from the page URL, the record=... part>'), then finish. "
            f"If none fits, CREATE: {rule['create']} Then record_value('{value}', <the new number>).")
    try:
        sr = _turn(tester, plan, state, rule, app, text, False, f"PREP-{key}")
    except Exception as e:  # noqa: BLE001
        return blocked(f"{type(e).__name__}: {e}")
    rec = sr.values.get(value)
    if not rec:
        return blocked(sr.notes or "no record found or created")
    if rule.get("verify"):
        v = rule["verify"]
        vt = testers.get(v.get("app", "portal"))
        if vt is not None:
            try:
                vr = _turn(vt, plan, state, rule, v.get("app", "portal"),
                           v["text"].replace("{value}", str(rec)).replace("{record}", str(sr.values.get(f"{value}_record") or "unknown")) +
                           f"\nIf it is usable, record_value('{value}_ok', 'yes'); otherwise record_value('{value}_ok', 'no: <why>').",
                           True, f"PREP-{key}-check")
                ok = str(vr.values.get(f"{value}_ok", "")).lower()
                if not ok.startswith("yes"):
                    store[key] = {"value": rec, "consumed": True}
                    state.save()
                    return blocked(f"{rec} is not usable: {ok or vr.notes or 'check gave no answer'}")
            except Exception as e:  # noqa: BLE001
                return blocked(f"check failed: {type(e).__name__}: {e}")
    store[key] = {"value": rec, "consumed": False, "for": case.id}
    state.values[value] = {"value": rec, "source": f"prep/{key}"}
    state.save()
    state.checkpoint(f"data prep {key}: using {rec} for {case.id}")
    return None


def after_case(case, state, rules: list[dict]) -> None:
    for rule in matching_rules(case, rules):
        if consumes(case, rule) and rule["id"] in state.data.get("prep", {}):
            state.data["prep"][rule["id"]]["consumed"] = True
            state.save()
