"""Data preparation before a case: find a record the case can use (e.g. an unpaid order),
or create one, then check it is usable. Generic: everything project-specific comes from the pack's rules.
Rules come from the pack's `data_prep` list. Card pay (4242) is allowed on staging/preprod/local;
production never pays. M-Pesa only with an explicit phone on the case."""
from __future__ import annotations

import os
import json
import re

from .models import CaseResult, PlannedCase, Step


PREP_MAX_ACTIONS = int(os.environ.get("PREP_MAX_ACTIONS", "120"))
# Cap LLM find turns so a missing record switches to create instead of looping 40+ searches.
DATA_FIND_MAX_STEPS = int(os.environ.get("DATA_FIND_MAX_STEPS", "12"))
DATA_FIND_MAX_SEARCHES = int(os.environ.get("DATA_FIND_MAX_SEARCHES", "2"))

# Create parts that pay (Stripe / card / M-Pesa) are refused on production only.
# Preprod may use the Stripe test card (4242); M-Pesa needs an explicit phone (see mpesa_gate).
_PAY_CREATE = re.compile(r"(?i)\b(pay|stripe|4242|m-?pesa|card payment|complete payment)\b")
_CARD_CREATE = re.compile(r"(?i)\b(pay|stripe|4242|card payment|complete payment)\b")
_MPESA = re.compile(r"(?i)\bm-?pesa\b")
_PHONE_HINT = re.compile(
    r"(?i)\b(?:phone|msisdn|mobile|mpesa[_\s-]?number|till)\b[:\s#=]*([+]?\d[\d\s\-()]{7,}\d)"
    r"|(?<!\d)(\+?\d{10,15})(?!\d)")
_NO_PAY_ENVS = frozenset({"production", "prod"})


def _case_text(case) -> str:
    from .field_text import field_text
    parts = [
        field_text(getattr(case, "title", None), sep=" "),
        field_text(getattr(case, "preconditions", None), sep=" "),
        field_text(getattr(case, "expected", None), sep=" "),
        field_text(getattr(case, "notes", None), sep=" "),
        field_text(getattr(case, "acceptance", None), sep=" "),
        *(field_text(s, sep=" ") for s in (getattr(case, "steps", None) or [])),
    ]
    return " ".join(p for p in parts if p).lower()


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


def client_label(name: str | None, user: str | None, record_id: str | None = None) -> str | None:
    """CRM contact label for prep find/create text.

    The portal (or other client_login_app) account picked for THIS run is the only identity
    when set. Pack ``test_client`` names are ignored whenever a login/email is present so
    env-bound contact names never override the run account. When a CRM record id was resolved
    into run state, open by that id — never by typing a person's name from facts.
    """
    if record_id and user:
        return (f"the CRM contact record id {record_id} (login/email {user}; "
                f"open by record id, do not search by a person's name)")
    if record_id:
        return f"the CRM contact record id {record_id} (open by record id, do not search by name)"
    if user:
        return f"the client whose login / email is {user}"
    return name


def portal_login(plan, login_app: str | None, prep_app: str | None = None) -> str | None:
    """The client-app login/email picked for this run (e.g. portal customer), if any."""
    for r in getattr(plan, "requirements", []) or []:
        if not r.value:
            continue
        if login_app and r.key.startswith(f"account.{login_app}."):
            return r.value
        if not login_app and r.key.startswith("account.") and not (
                prep_app and r.key.startswith(f"account.{prep_app}.")):
            return r.value
    return None


def contact_needed(case, rules: list[dict]) -> dict | None:
    """First matching prep rule that identifies a CRM contact via a client login app."""
    for rule in matching_rules(case, rules):
        if rule.get("client_login_app") and rule.get("app"):
            return rule
    return None


LOOKUP_VALUE = "crm_contact_id"
LOOKUP_EMAIL_VALUE = "crm_contact_email"


def contact_lookup_brief_text(email: str, display_name: str | None = None) -> str:
    """Pack-agnostic CRM/contact search ladder. Email for this run always wins over any name hint."""
    name_step = (
        f"1. If a display name is known ({display_name!r}), try the module's basic name search once.\n"
        if display_name else
        "1. Skip basic name search (no display name known for this run).\n"
    )
    return (
        f"LOOK UP the CRM contact for portal login/email {email!r}. READ-ONLY: open lists and records; change nothing.\n"
        f"Search ladder (try in order; do not finish stuck until you have asked):\n"
        f"{name_step}"
        f"2. On the Clients/Contacts list, filter or use Advanced Search by email = {email}.\n"
        f"3. If that fails, use the app's global search for the email.\n"
        f"4. After two or three failed routes, call ask_user with what you tried and one concrete question "
        f"(e.g. which CRM contact record is portal email {email}?). Only finish(stuck=True) if the user "
        f"does not answer or declines.\n"
        f"On a candidate, open the record and confirm its email field matches {email} before accepting it.\n"
        f"When confirmed, record_value('{LOOKUP_VALUE}', '<CRM record id from the URL record=>') and "
        f"record_value('{LOOKUP_EMAIL_VALUE}', '<email shown on the record>'), then finish.\n"
        f"Never type a fixed person name from facts/learned as the client; the portal email for this run wins.\n"
        f"Do not write the record id into learned.md — run state only."
    )


def _deterministic_contact(plan, case, state, testers: dict, app: str, email: str) -> bool:
    """Try pack lookups (no LLM). Returns True when contact id was stored.

    Uses a short-lived Playwright MCP session on the app's storage state when available.
    Unit tests / missing MCP fall through to the LLM tester (returns False).
    """
    from .config import under_unit_tests
    pack_cfg = state.data.get("pack_cfg") or {}
    if not (pack_cfg.get("lookups") or {}).get("maps"):
        return False
    if os.environ.get("LOOKUP_DETERMINISTIC", "1").strip().lower() in ("0", "false", "no"):
        return False
    if under_unit_tests() and not os.environ.get("LOOKUP_FORCE"):
        return False
    base = None
    for r in getattr(plan, "requirements", []) or []:
        if r.value and str(r.key).startswith("env.") and str(r.key).endswith(f".{app}"):
            base = r.value
            break
    if not base:
        return False
    platform = ((state.data.get("discovery") or {}).get(f"{case.area}:{app}") or {}).get("platform") \
        or (pack_cfg.get("lookups") or {}).get("platform") or "suitecrm7"
    tester = testers.get(app)
    browser = getattr(tester, "lookup_browser", None) if tester is not None else None
    close = None
    import asyncio
    from .lookups import find_contact_by_email
    res = None
    try:
        if browser is None:
            browser, close = _mcp_browser_for_lookup(state.dir, app)
            if browser is None:
                return False
        res = asyncio.run(find_contact_by_email(
            browser, base_url=base, email=email, platform=platform,
            screenshot_name=str((state.dir / "shots" / f"contact-lookup-{case.id}.png").resolve())))
    except Exception as e:  # noqa: BLE001
        state.checkpoint(f"deterministic contact lookup failed: {type(e).__name__}: {e}")
        return False
    finally:
        if close:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    if res is None or not res.ok or not res.record_id:
        return False
    store = state.data.setdefault("contact", {})
    store.update({"email": email, "record_id": res.record_id,
                  "email_on_record": res.values.get("email_on_record") or email,
                  "app": app, "failed": False, "via": "deterministic"})
    state.values[LOOKUP_VALUE] = {"value": res.record_id, "source": "contact_lookup_code"}
    state.values[LOOKUP_EMAIL_VALUE] = {
        "value": store["email_on_record"], "source": "contact_lookup_code"}
    for ev in res.evidence or []:
        state.data.setdefault("notes", []).append(f"contact lookup evidence: {ev}")
    state.save()
    state.checkpoint(f"contact lookup (code): {email} -> record {res.record_id}")
    return True


def _mcp_browser_for_lookup(run_dir, app: str):
    """Return (McpBrowser, close_fn) or (None, None) when MCP/npx is unavailable."""
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from .agents.tester import browser_env, headed, profile_path, storage_state_path
        from .helpers import McpBrowser
        import asyncio
    except Exception:  # noqa: BLE001
        return None, None
    storage = storage_state_path(run_dir, app)
    out = run_dir / "shots"
    out.mkdir(parents=True, exist_ok=True)
    if storage.is_file():
        args = ["-y", "@playwright/mcp@latest", *([] if headed() else ["--headless"]),
                "--isolated", "--storage-state", str(storage.resolve()),
                "--output-dir", str(out), "--viewport-size", "1366,900"]
    else:
        profile = profile_path(run_dir, app)
        profile.mkdir(parents=True, exist_ok=True)
        args = ["-y", "@playwright/mcp@latest", *([] if headed() else ["--headless"]),
                "--user-data-dir", str(profile),
                "--output-dir", str(out), "--viewport-size", "1366,900"]

    # Hold the session open across async lookup calls via a dedicated event loop runner.
    class _SessionBrowser:
        def __init__(self):
            self._cm = None
            self._sess = None
            self.last_snapshot = ""

        async def _ensure(self):
            if self._sess is not None:
                return
            self._stdio = stdio_client(StdioServerParameters(
                command="npx", args=args, env=browser_env()))
            self._read_write = await self._stdio.__aenter__()
            self._sess_cm = ClientSession(*self._read_write)
            self._sess = await self._sess_cm.__aenter__()
            await self._sess.initialize()

        async def _do(self, tool, a):
            await self._ensure()
            res = await self._sess.call_tool(tool, a)
            text = "\n".join(getattr(c, "text", "") or "" for c in (getattr(res, "content", None) or []))
            return text

        async def navigate(self, url: str) -> str:
            return await self._do("browser_navigate", {"url": url})

        async def snapshot(self) -> str:
            self.last_snapshot = await self._do("browser_snapshot", {})
            return self.last_snapshot

        async def screenshot(self, filename: str) -> str:
            from pathlib import Path
            path = Path(filename)
            path.parent.mkdir(parents=True, exist_ok=True)
            await self._do("browser_take_screenshot", {"filename": str(path.resolve()), "type": "png"})
            return path.name

        async def aclose(self):
            try:
                if self._sess is not None:
                    await self._sess.call_tool("browser_close", {})
            except Exception:  # noqa: BLE001
                pass
            try:
                if self._sess_cm is not None:
                    await self._sess_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            try:
                if self._stdio is not None:
                    await self._stdio.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    b = _SessionBrowser()

    def close():
        try:
            asyncio.run(b.aclose())
        except Exception:  # noqa: BLE001
            pass

    return b, close


def ensure_contact(plan, case, state, testers: dict, rules: list[dict]) -> CaseResult | None:
    """Resolve the run's portal email to a CRM contact record id before mutating prep.

    Stores the id in state.data['contact'] / state.values only (never learned.md).
    Returns a BLOCKED CaseResult when lookup fails; None when ready or not needed.
    """
    rule = contact_needed(case, rules)
    if rule is None:
        return None
    login_app = rule.get("client_login_app")
    app = rule.get("app")
    email = portal_login(plan, login_app, app)
    if not email:
        return None
    store = state.data.setdefault("contact", {})
    if store.get("email") == email and store.get("record_id") and not store.get("failed"):
        state.values[LOOKUP_VALUE] = {"value": store["record_id"], "source": "contact_lookup"}
        state.values[LOOKUP_EMAIL_VALUE] = {"value": store.get("email_on_record") or email,
                                            "source": "contact_lookup"}
        return None

    # Code path first (no LLM); fall back to the tester agent when it fails or has no browser.
    if _deterministic_contact(plan, case, state, testers, app, email):
        return None

    tester = testers.get(app) if app else None
    if tester is None:
        return CaseResult(case_id=case.id, verdict="BLOCKED", expected=case.expected,
                          note=f"contact lookup needs app '{app}' but no tester is available")

    # Optional display name from pack test_client only as a search hint — never as identity.
    clients = rule.get("test_client")
    name_hint = clients.get(plan.env) if isinstance(clients, dict) else clients
    if name_hint and str(name_hint).lower() in {str(email).lower(), "h", "x"}:
        name_hint = None

    text = contact_lookup_brief_text(email, name_hint)
    from .coordinator import brief_for
    pseudo = PlannedCase(id=f"CONTACT-{case.id}", title=f"look up contact for {email}",
                         area=rule.get("area") or case.area,
                         steps=[Step(text=text, app=app)],
                         values_to_record=[LOOKUP_VALUE, LOOKUP_EMAIL_VALUE])
    brief = brief_for(plan, pseudo, [0], state)
    brief["data_prep"] = True
    brief["contact_lookup"] = True
    brief["read_only"] = True
    brief["prefer_ask_user"] = True
    brief["max_tool_calls"] = min(PREP_MAX_ACTIONS, 60)
    brief["actions_per_step"] = min(PREP_MAX_ACTIONS, 60)
    brief["values_to_record"] = [LOOKUP_VALUE, LOOKUP_EMAIL_VALUE]

    from .coordinator import RunPaused
    from .reliability import is_retryable, wait_for_model
    outages = 0
    while True:
        mark = len(state.data.get("action_log", []))
        try:
            sr = tester(app, brief, state)
            break
        except RunPaused:
            raise
        except Exception as e:  # noqa: BLE001
            changed = [a for a in state.data.get("action_log", [])[mark:]
                       if a.get("approval") in ("approved", "pre_approved")]
            if is_retryable(e) and not changed and outages < 3:
                outages += 1
                state.checkpoint("contact lookup: model server unreachable, waiting for it to come back")
                if wait_for_model():
                    continue
            from .logsetup import short_error
            return CaseResult(case_id=case.id, verdict="BLOCKED", expected=case.expected,
                              note=f"contact lookup failed: {short_error(e, 'contact lookup')}")

    rec = str((sr.values or {}).get(LOOKUP_VALUE) or "").strip()
    shown = str((sr.values or {}).get(LOOKUP_EMAIL_VALUE) or "").strip()
    asked = [a for a in state.data.get("action_log", [])
             if a.get("case_id") == pseudo.id and a.get("tool") == "ask_user"]
    if _is_none(rec):
        store.update({"email": email, "failed": True, "notes": (sr.notes or "")[:300],
                      "asked": bool(asked)})
        state.save()
        why = (sr.notes or "").strip() or "no matching contact record id"
        if asked and not any((a.get("args") or {}).get("answer") for a in asked):
            why = f"asked the user after failed lookup routes; no answer ({why})"
        state.checkpoint(f"contact lookup failed for {email}: {why}")
        return CaseResult(case_id=case.id, verdict="BLOCKED", expected=case.expected,
                          note=f"CRM contact not resolved for portal email {email}: {why}")

    # Soft confirm: if the turn recorded an email, it should match the portal login.
    if shown and email.lower() not in shown.lower() and shown.lower() not in email.lower():
        store.update({"email": email, "failed": True, "notes": f"email mismatch: page={shown}"})
        state.save()
        return CaseResult(case_id=case.id, verdict="BLOCKED", expected=case.expected,
                          note=f"CRM contact {rec} email {shown!r} does not match portal {email!r}")

    store.update({"email": email, "record_id": rec, "email_on_record": shown or email,
                  "app": app, "failed": False})
    state.values[LOOKUP_VALUE] = {"value": rec, "source": "contact_lookup"}
    state.values[LOOKUP_EMAIL_VALUE] = {"value": shown or email, "source": "contact_lookup"}
    state.save()
    state.checkpoint(f"contact lookup: {email} -> record {rec}")
    return None


def consumes(case, rule: dict) -> bool:
    """True when the case uses up the record (e.g. pays the order), so the next case needs a fresh one."""
    return any(re.search(k, _case_text(case), re.I) for k in rule.get("consumed_by", []))


def case_matches(case, cond: dict | None) -> bool:
    """A pack condition on the case text: {when: [regex...], unless: [regex...]}. No condition = False."""
    if not cond:
        return False
    text = _case_text(case)
    if any(re.search(k, text, re.I) for k in cond.get("unless", [])):
        return False
    return any(re.search(k, text, re.I) for k in cond.get("when", []))


def verify_text(case, v: dict) -> str:
    """The check's text with {condition} filled for this case from v['condition'] (then / else text)."""
    t = v["text"]
    c = v.get("condition") or {}
    if "{condition}" in t:
        part = c.get("then" if case_matches(case, c) else "else", "")
        t = t.replace("{condition}", part.replace("{space}", " "))
    return t


def _turn(tester, plan, state, rule, app, text, read_only, case_id):
    from .coordinator import brief_for
    from . import learned
    from .placeholders import inject_placeholders
    kind = "create" if "-create" in case_id else ("find" if case_id.endswith("-find") or "-find" in case_id else "check")
    known = [l for l in learned.read(state.data.get("pack_root"), rule.get("area"), app, rule["id"])
             if f"{kind} test data {rule['id']}" in l]
    if known:   # the pack learned this on an earlier run: start from it, re-read the screen, and adapt if it moved
        text += ("\nThis worked before (learned, not yet confirmed; follow it but read each screen first):\n- "
                 + "\n- ".join(known[-2:]))
    # Never steer create toward a wrong app via old Stuck lines about creating on another app.
    if kind == "create":
        text += (f"\nThis create part runs on app '{app}' only. Do not switch to another app for this part; "
                 f"later/earlier create parts (if any) run on their own apps.")
    if kind == "find":
        text += (f"\nBudget: at most {DATA_FIND_MAX_STEPS} actions and about {DATA_FIND_MAX_SEARCHES} failed "
                 f"search attempts. If nothing exact fits by then, finish(notes='none found') — do NOT "
                 f"record_value '{rule['value']}' as none/null/not found (those are rejected). "
                 f"Create runs next when find is not_found.")
    pseudo = PlannedCase(id=case_id, title=f"data prep: {rule['id']}", area=rule.get("area"),
                         steps=[Step(text=text, app=app)], values_to_record=[rule["value"]])
    brief = brief_for(plan, pseudo, [0], state)
    brief["data_prep"] = True
    # Find is short and read-only; create may be a long multi-screen flow.
    budget = DATA_FIND_MAX_STEPS if kind == "find" else PREP_MAX_ACTIONS
    brief["max_tool_calls"] = budget
    brief["actions_per_step"] = budget
    if kind == "find":
        brief["find_budget"] = True
    brief["read_only"] = read_only
    brief["values_to_record"] = [rule["value"]] + list(rule.get("extra_values", []))
    brief = inject_placeholders(brief, state)
    return tester(app, brief, state)


def _lookup_kind_for_rule(rule: dict, pack_cfg: dict | None = None) -> str | None:
    """Map a prep rule to a pack lookups.maps key (explicit ``lookup`` wins; else module hints)."""
    if rule.get("lookup"):
        return str(rule["lookup"])
    maps = ((pack_cfg or {}).get("lookups") or {}).get("maps") or {}
    blob = " ".join(str(rule.get(k) or "") for k in ("find", "id", "value", "placeholder", "token")).lower()
    # Prefer an explicit maps entry whose name appears in the rule text.
    for kind in maps:
        if str(kind).lower() in blob:
            return str(kind)
    if "aos_quotes" in blob or ("quote" in blob):
        return "quotes" if "quotes" in maps else None
    if "aos_invoices" in blob or "invoice" in blob:
        return "shipping_orders" if "shipping_orders" in maps else None
    if "contact" in blob or "customer" in blob:
        return "contact_by_email" if "contact_by_email" in maps else None
    return None


def _deterministic_prep_find(plan, case, state, testers: dict, rule: dict, *,
                             find_app: str, client_email: str | None) -> tuple[str | None, str | None, str]:
    """Try pack lookups (no LLM) before the find agent.

    Returns (value, record_id, status) where status is 'found' | 'not_found' | 'skipped'.
    A 'not_found' from code counts toward ending find and switching to create.
    """
    from .config import under_unit_tests
    pack_cfg = state.data.get("pack_cfg") or {}
    maps = (pack_cfg.get("lookups") or {}).get("maps") or {}
    kind = _lookup_kind_for_rule(rule, pack_cfg)
    if not kind or kind not in maps:
        return None, None, "skipped"
    if os.environ.get("LOOKUP_DETERMINISTIC", "1").strip().lower() in ("0", "false", "no"):
        return None, None, "skipped"
    if under_unit_tests() and not os.environ.get("LOOKUP_FORCE"):
        return None, None, "skipped"
    base = None
    for r in getattr(plan, "requirements", []) or []:
        if r.value and str(r.key).startswith("env.") and str(r.key).endswith(f".{find_app}"):
            base = r.value
            break
    if not base:
        return None, None, "skipped"
    platform = ((state.data.get("discovery") or {}).get(f"{case.area}:{find_app}") or {}).get("platform") \
        or (pack_cfg.get("lookups") or {}).get("platform") or "suitecrm7"
    tester = testers.get(find_app)
    browser = getattr(tester, "lookup_browser", None) if tester is not None else None
    close = None
    import asyncio
    from .lookups import lookup_from_pack
    try:
        if browser is None:
            browser, close = _mcp_browser_for_lookup(state.dir, find_app)
            if browser is None:
                return None, None, "skipped"
        contact = state.data.get("contact") or {}
        params = {"email": client_email or contact.get("email") or "",
                  "contact": contact.get("record_id") or "",
                  "record_id": contact.get("record_id") or "",
                  "value": client_email or ""}
        res = asyncio.run(lookup_from_pack(
            browser, pack_cfg, kind, base_url=base, platform=platform, **params))
    except Exception as e:  # noqa: BLE001
        state.checkpoint(f"deterministic prep find {rule.get('id')}: {type(e).__name__}: {e}")
        return None, None, "not_found"
    finally:
        if close:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    if res is None or not res.ok or not res.record_id:
        state.checkpoint(f"data prep {rule.get('id')}: deterministic find → not_found "
                         f"({getattr(res, 'notes', '') or 'no match'})")
        return None, None, "not_found"
    # Prefer a human-facing number from the row when present; else the CRM record id.
    value = None
    for k in ("Number", "Invoice Number", "Quote Number", "Name", "number", "name"):
        if res.values.get(k):
            value = str(res.values[k]).strip()
            break
    if not value:
        value = res.record_id
    if _is_none(value):
        return None, None, "not_found"
    state.checkpoint(f"data prep {rule.get('id')}: deterministic find → {value} (record {res.record_id})")
    return value, res.record_id, "found"


def _find_exhausted(sr, rec, state, case_id: str, key: str) -> bool:
    """True when the find turn should end as not_found (budget / none / empty / finish notes)."""
    if getattr(sr, "not_found", False):
        state.checkpoint(f"data prep {key}: find finish → not_found")
        return True
    if _is_none(rec):
        return True
    if getattr(sr, "limit_hit", False):
        state.checkpoint(f"data prep {key}: find hit action budget → not_found")
        return True
    # Count failed search-like navigations in this find turn.
    acts = [a for a in state.data.get("action_log", []) if a.get("case_id") == case_id]
    searches = [a for a in acts if a.get("tool") in ("browser_navigate", "browser_type", "browser_click")
                and re.search(r"search|query|filter|advanced", json.dumps(a.get("args") or {}), re.I)]
    if len(searches) >= DATA_FIND_MAX_SEARCHES and _is_none(rec):
        state.checkpoint(f"data prep {key}: {len(searches)} failed searches → not_found")
        return True
    notes = (getattr(sr, "notes", None) or "")
    if re.search(r"(?i)\b(none found|not found|nothing fits|no match)\b", notes):
        state.checkpoint(f"data prep {key}: find notes → not_found")
        return True
    return False


def mpesa_phone(case=None, state=None, text: str = "") -> str | None:
    """Return a phone number provided for M-Pesa, or None. Never invent one."""
    blobs: list[str] = [text or ""]
    if case is not None:
        from .field_text import field_text
        blobs.append(field_text(getattr(case, "title", None), sep=" "))
        blobs.append(field_text(getattr(case, "expected", None), sep=" "))
        blobs.append(field_text(getattr(case, "preconditions", None), sep=" "))
        blobs.append(field_text(getattr(case, "notes", None), sep=" "))
        for s in getattr(case, "steps", None) or []:
            blobs.append(field_text(s, sep=" "))
    if state is not None:
        for v in (getattr(state, "values", None) or {}).values():
            if isinstance(v, dict):
                blobs.append(str(v.get("value") or ""))
            else:
                blobs.append(str(v or ""))
        data = getattr(state, "data", None) or {}
        for key in ("mpesa_phone", "phone", "msisdn", "mobile"):
            if data.get(key):
                blobs.append(str(data[key]))
        for r in (data.get("plan") or {}).get("requirements") or []:
            if isinstance(r, dict) and re.search(r"(?i)phone|mpesa|msisdn|mobile", str(r.get("key") or "")):
                blobs.append(str(r.get("value") or ""))
    env_phone = os.environ.get("MPESA_PHONE") or os.environ.get("QA_MPESA_PHONE")
    if env_phone:
        blobs.append(env_phone)
    for blob in blobs:
        if not blob:
            continue
        m = _PHONE_HINT.search(str(blob))
        if m:
            raw = next((g for g in m.groups() if g), None)
            if raw:
                digits = re.sub(r"[^\d+]", "", raw)
                if len(re.sub(r"\D", "", digits)) >= 9:
                    return digits
    return None


def case_calls_for_mpesa(case=None, text: str = "") -> bool:
    """True when the case / step text itself asks for M-Pesa (not a generic pay)."""
    from .field_text import field_text
    parts = [text or ""]
    if case is not None:
        parts += [
            field_text(getattr(case, "title", None), sep=" "),
            field_text(getattr(case, "expected", None), sep=" "),
            *(field_text(s, sep=" ") for s in (getattr(case, "steps", None) or [])),
        ]
    return bool(_MPESA.search(" ".join(p for p in parts if p)))


def mpesa_gate(case=None, state=None, text: str = "") -> str | None:
    """If M-Pesa is required but no phone was provided, return a BLOCKED/skip reason; else None."""
    blob = text or ""
    if case is not None and not blob:
        from .field_text import field_text
        blob = " ".join(p for p in [
            field_text(getattr(case, "title", None), sep=" "),
            field_text(getattr(case, "expected", None), sep=" "),
            *(field_text(s, sep=" ") for s in (getattr(case, "steps", None) or [])),
        ] if p)
    if not _MPESA.search(blob):
        return None
    if mpesa_phone(case=case, state=state, text=blob):
        return None
    return ("M-Pesa is required but no phone number was provided for it; "
            "skip this step (do not guess a number). Provide a phone on the case, "
            "as MPESA_PHONE, or in run values, then rerun.")


def _create_parts_for_env(plan, rule: dict, parts: list[dict], case=None, state=None) -> list[dict] | str:
    """Refuse pay-bearing create parts on production; allow card on preprod; gate M-Pesa on phone."""
    env = (getattr(plan, "env", None) or "").lower()
    kept = []
    for p in parts:
        text = p.get("text") or ""
        if re.search(r"(?i)do not pay|don't pay|never pay", text):
            kept.append(p)
            continue
        # M-Pesa: only when the text calls for it AND a phone is provided; else skip/block.
        if _MPESA.search(text):
            why = mpesa_gate(case=case, state=state, text=text)
            if why:
                return why
            kept.append(p)
            continue
        if env in _NO_PAY_ENVS and _CARD_CREATE.search(text):
            return (f"create needs a payment step, which is not allowed on {plan.env}; "
                    f"top up / pay by hand on this environment and rerun")
        kept.append(p)
    return kept


def ensure_data(plan, case, state, testers: dict, rules: list[dict]) -> CaseResult | None:
    """Run every matching prep rule in pack order. None when all are ready, else the first BLOCKED result."""
    for rule in matching_rules(case, rules):
        r = _ensure_one(plan, case, state, testers, rule)
        if r is not None:
            return r
    return None


_NONE = {"", "none", "no", "n/a", "na", "null", "not found", "nothing", "-"}


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
    if rule.get("min_value") is not None:                   # e.g. a balance: negative or 0 is not enough
        n = _num(rec)
        return n is not None and n >= float(rule["min_value"])
    return True


CREATE_TRIES = 2


def _create_parts(rule: dict) -> list[dict]:
    """Create may be a string (one app) or a sequence of app-tagged parts from the pack yaml."""
    from .placeholders import create_parts
    return create_parts(rule)


def _apps_for_rule(rule: dict) -> list[str]:
    apps = []
    for a in (rule.get("find_app") or rule.get("app"),):
        if a and a not in apps:
            apps.append(a)
    for p in _create_parts(rule):
        if p.get("app") and p["app"] not in apps:
            apps.append(p["app"])
    vapp = (rule.get("verify") or {}).get("app")
    if vapp and vapp not in apps:
        apps.append(vapp)
    return apps


def _ensure_one(plan, case, state, testers: dict, rule: dict) -> CaseResult | None:
    from .placeholders import subst_client, store_resolved
    rule = dict(rule)
    if rule.get("placeholder") and not rule.get("token"):
        rule["token"] = rule["placeholder"]
    store = state.data.setdefault("prep", {})
    key, value = rule["id"], rule["value"]
    have = store.get(key)
    if have and not have.get("consumed") and not _usable(rule, have.get("value"), []):
        have = None                     # e.g. a resumed run that stored a negative balance as "ready"
    if have and not have.get("consumed"):
        state.values[value] = {"value": have["value"], "source": f"prep/{key}"}
        if have.get("record"):
            state.values[f"{value}_record"] = {"value": have["record"], "source": f"prep/{key}"}
        for a in rule.get("aliases") or []:
            state.values[a] = {"value": have["value"], "source": f"prep/{key}"}
            if have.get("record"):
                state.values[f"{a}_record"] = {"value": have["record"], "source": f"prep/{key}"}
        if rule.get("token"):
            store_resolved(state, rule["token"], value=have["value"], record=have.get("record"),
                           value_key=value, prep_id=key, aliases=list(rule.get("aliases") or []))
        return None

    find_app = rule.get("find_app") or rule.get("app")
    app = find_app  # used by blocked()'s learn path

    def blocked(why):
        state.checkpoint(f"data prep {key} for {case.id} failed: {why}")
        from . import learned
        try:
            for turn in (f"PREP-{key}-create", f"PREP-{key}-create-0", f"PREP-{key}-find"):
                acts = [a for a in state.data.get("action_log", []) if a.get("case_id") == turn]
                if acts and learned.record_stuck(state.data.get("pack_root"), rule.get("area"), app or find_app or "crm",
                                                 turn, why, why, acts):
                    state.checkpoint(f"data prep {key}: saved where it got stuck to the pack")
                    break
        except Exception:  # noqa: BLE001
            pass
        return CaseResult(case_id=case.id, verdict="BLOCKED", expected=case.expected,
                          note=f"test data not ready ({key}): {why}")

    if rule.get("envs") and plan.env not in rule["envs"]:
        return blocked(rule.get("blocked_note") or f"this preparation is not possible on {plan.env}")
    clients = rule.get("test_client")
    name = clients.get(plan.env) if isinstance(clients, dict) else clients
    login_app = rule.get("client_login_app")       # whose picked login identifies the test client (e.g. the customer app)
    user = portal_login(plan, login_app, find_app)
    contact = state.data.get("contact") or {}
    record_id = contact.get("record_id") if contact.get("email") == user and not contact.get("failed") else None
    if not (name or user):
        return blocked("no test user picked and no test client set for this environment; "
                       "the agent will not create orders for an unconfirmed account")
    client = client_label(name, user, record_id)
    rule = subst_client(rule, client)
    parts = _create_parts(rule)
    need_apps = _apps_for_rule(rule)
    for a in need_apps:
        if a and testers.get(a) is None:
            return blocked(f"no tester for {a}")
    if not find_app and not parts:
        return blocked("this prep rule names no app")
    find_tester = testers.get(find_app) if find_app else None
    rejected = state.data.setdefault("prep_rejected", {}).setdefault(key, [])
    used = ([s["value"] for s in store.values() if s.get("consumed")] + [r["value"] for r in rejected]
            + list(rule.get("never_use", [])))
    skip = ", ".join(f"{r['value']} ({r['why'][:80]})" for r in rejected)
    skip = ", ".join(x for x in [", ".join(str(u) for u in used if u not in [r["value"] for r in rejected]), skip] if x)

    def rec_of(sr):
        return sr.values.get(value), sr.values.get(f"{value}_record")

    hint = rule.get("record_hint") or "the record's id from the page URL"
    # 1. FIND: deterministic lookup first, then a short read-only LLM find. Exact fit only.
    find = (f"FIND ONLY. This turn is read-only: open lists and records and read them; change nothing.\n"
            f"{rule.get('find') or ''}\nDo not use: {skip or 'none'}.\n"
            f"Only an exact fit counts. Never edit, fix, re-status or add to a record to make it fit; a near-fit "
            f"is a not_found. If one fits exactly, record_value('{value}', '<its number exactly as shown, with its "
            f"prefix, exactly as shown>') and record_value('{value}_record', '<{hint}>'), then finish. "
            f"If none fits exactly, finish(notes='none found') — never record_value '{value}' as none/null/"
            f"not found (rejected) and never keep searching past the budget. "
            f"Use the list's search, filters and sorting to narrow it down rather than paging through everything; "
            f"open a candidate to check it rather than assuming from the list.")
    sr = None
    rec = record = None
    find_case = f"PREP-{key}-find"
    det_status = "skipped"
    if find_app and rule.get("find"):
        d_val, d_rec, det_status = _deterministic_prep_find(
            plan, case, state, testers, rule, find_app=find_app, client_email=user)
        if det_status == "found" and _usable(rule, d_val, used):
            rec, record = d_val, d_rec
        elif det_status == "found":
            state.checkpoint(f"data prep {key}: deterministic hit {d_val} not usable; will create")
            rec = record = None
            det_status = "not_found"
    # LLM find only when code did not already settle not_found / found.
    if rec is None and det_status != "not_found" and find_tester is not None and rule.get("find"):
        try:
            sr = _call(find_tester, plan, state, rule, find_app, find, True, find_case, key)
            rec, record = rec_of(sr)
        except Exception as e:  # noqa: BLE001
            from .logsetup import short_error
            state.checkpoint(f"data prep {key}: find failed ({short_error(e, f'data prep {key} find')}); creating one")
            rec, record = None, None
            sr = None
        if sr is not None and _find_exhausted(sr, rec, state, find_case, key):
            if rec is not None and not _is_none(rec) and not _usable(rule, rec, used):
                state.checkpoint(f"data prep {key}: found {rec}, which is not enough or already used; creating one")
            else:
                state.checkpoint(f"data prep {key}: find → not_found; running create parts")
            rec = record = None
        elif not _usable(rule, rec, used):
            if rec is not None and not _is_none(rec):
                state.checkpoint(f"data prep {key}: found {rec}, which is not enough or already used; creating one")
            rec = record = None
    elif rec is None and det_status == "not_found":
        state.checkpoint(f"data prep {key}: deterministic find → not_found; running create parts")

    # Never treat a literal 'none'/empty as a resolved placeholder value.
    if _is_none(rec):
        rec = record = None

    for attempt in range(CREATE_TRIES + 1):
        if rec is None:
            if attempt == CREATE_TRIES:
                break
            if not parts:
                return blocked(f"nothing fits and this rule has no create step. {(sr.notes or '') if sr else ''}".strip())
            env_parts = _create_parts_for_env(plan, rule, parts, case=case, state=state)
            if isinstance(env_parts, str):
                return blocked(env_parts)
            # Multi-app create: run each pack-tagged part in order (e.g. portal then crm).
            for i, part in enumerate(env_parts):
                papp, ptext = part.get("app"), part.get("text") or ""
                last = i == len(env_parts) - 1
                create = (f"CREATE part {i + 1}/{len(env_parts)} on app '{papp}': {ptext}\n"
                          f"Do not reuse: {skip or 'none'}.\n"
                          f"Stay on app '{papp}' for this part only.")
                if last:
                    create += (f"\nWhen this part is done, record_value('{value}', '<the new number, exactly as shown>') "
                               f"and record_value('{value}_record', '<{hint}>'), then finish. "
                               f"If you cannot finish it, call finish with notes starting 'BLOCKED:' saying where you "
                               f"stopped and the number of any record you created.")
                else:
                    create += ("\nWhen this part is done, finish (record the number/id if you already have it). "
                               "A later part on another app continues the prep.")
                try:
                    sr = _call(testers[papp], plan, state, rule, papp, create, False,
                               f"PREP-{key}-create-{i}" if len(env_parts) > 1 else f"PREP-{key}-create", key)
                except Exception as e:  # noqa: BLE001
                    from .logsetup import short_error
                    return blocked(short_error(e, f"data prep {key} create part {i + 1}"))
                got_v, got_r = rec_of(sr)
                if not _is_none(got_v):
                    rec, record = got_v, got_r or record
                why = (sr.notes or "").strip()
                if why.upper().startswith("BLOCKED:"):
                    return blocked(why[8:].strip() or f"create part {i + 1} blocked")
            if _is_none(rec):
                rec = record = None
            if not _usable(rule, rec, used):
                why = (sr.notes or "").strip() if sr else ""
                why = why[8:].strip() if why.upper().startswith("BLOCKED:") else why
                return blocked(why or f"creating a record did not give a usable {value} ({rec})")
        if not rule.get("verify"):
            break
        verdict, why = _verify(plan, case, state, testers, rule, rec, record, key)
        if verdict == "yes":
            break
        if verdict == "error":
            return blocked("check failed: " + why)
        keep = (rule.get("verify") or {}).get("keep_record_if") or {}
        if verdict == "no" and keep.get("reason") and re.search(keep["reason"], why, re.I) and (
                not keep.get("only_when_condition") or case_matches(case, rule["verify"].get("condition"))):
            # the record itself is fine; something else (named by the pack) is missing. Keep it for other cases.
            store[key] = {"value": rec, "record": record, "consumed": False, "for": case.id}
            state.save()
            return blocked(f"{rec} is fine but {keep.get('note', 'something else it needs is missing')} ({why})")
        rejected.append({"value": rec, "why": why or verdict})
        used.append(rec)
        skip = (skip + ", " if skip else "") + f"{rec} ({(why or verdict)[:80]})"
        state.save()
        state.checkpoint(f"data prep {key}: {rec} is not usable ({why or verdict}); trying a new one")
        rec = record = None
    if rec is None:
        return blocked(f"no usable record after {CREATE_TRIES} tries; rejected: "
                       + "; ".join(f"{r['value']}: {r['why']}" for r in rejected[-CREATE_TRIES:]))
    learn_app = (parts[-1]["app"] if parts else find_app) or find_app
    _learn(state, rule, key, learn_app)
    store[key] = {"value": rec, "record": record, "consumed": False, "for": case.id}
    state.values[value] = {"value": rec, "source": f"prep/{key}"}
    if record:
        state.values[f"{value}_record"] = {"value": record, "source": f"prep/{key}"}
    for a in rule.get("aliases") or []:
        state.values[a] = {"value": rec, "source": f"prep/{key}"}
        if record:
            state.values[f"{a}_record"] = {"value": record, "source": f"prep/{key}"}
    if rule.get("token"):
        store_resolved(state, rule["token"], value=rec, record=record, value_key=value,
                       prep_id=key, aliases=list(rule.get("aliases") or []))
    state.save()
    state.checkpoint(f"data prep {key}: using {rec} for {case.id}")
    return None


def resolve_data_plan(plan, state, testers: dict, data_plan: list[dict],
                      *, pseudo_case=None) -> list[CaseResult]:
    """Resolve every placeholder row up front (find or create). Returns BLOCKED results for failures;
    successful rows are stored in run state only. Does not stop the run — caller continues other cases."""
    from .models import PlannedCase, CaseType
    from .placeholders import tokens_in_case
    blocked: list[CaseResult] = []
    for row in data_plan or []:
        if row.get("missing"):
            note = f"prep-missing: placeholder {row['token']} is not defined in the pack"
            state.checkpoint(note)
            state.data.setdefault("notes", []).append(note)
            for cid in row.get("cases") or []:
                blocked.append(CaseResult(case_id=cid, verdict="BLOCKED", note=note))
            continue
        rule = dict(row.get("rule") or {})
        if not rule:
            continue
        tok = row["token"]
        have = (state.data.get("placeholders") or {}).get(tok.upper())
        prep_have = (state.data.get("prep") or {}).get(rule.get("id"))
        if have and have.get("value") and prep_have and not prep_have.get("consumed"):
            row["status"] = "ready"
            row["resolved"] = have.get("value")
            continue
        case = pseudo_case or PlannedCase(
            id=f"DATA-{tok}", title=f"prepare {tok}", area=rule.get("area"),
            type=CaseType.DATA_SETUP,
            steps=[Step(text=f"Prepare placeholder {tok}", app=rule.get("find_app") or rule.get("app") or "none")],
            expected=f"{tok} ready")
        for c in plan.cases:
            if tok.upper() in {t.upper() for t in tokens_in_case(c)}:
                case = c
                break
        err = _ensure_one(plan, case, state, testers, rule)
        if err is not None:
            row["status"] = "failed"
            row["error"] = err.note
            state.data.setdefault("notes", []).append(f"data plan {tok}: {err.note}")
            for cid in row.get("cases") or [case.id]:
                blocked.append(CaseResult(case_id=cid, verdict="BLOCKED", expected=getattr(case, "expected", ""),
                                          note=err.note or f"prep-missing: {tok}"))
        else:
            row["status"] = "ready"
            got = (state.data.get("placeholders") or {}).get(tok.upper()) or {}
            row["resolved"] = got.get("value") or (state.data.get("prep") or {}).get(rule.get("id"), {}).get("value")
            state.checkpoint(f"data plan: {tok} ready ({row.get('resolved')})")
    state.data["data_plan"] = [
        {**{k: v for k, v in r.items() if k != "rule"},
         **({"rule_id": (r.get("rule") or {}).get("id")} if r.get("rule") else {})}
        for r in data_plan
    ]
    state.data["_data_plan_rules"] = data_plan
    state.save()
    return blocked


def _learn(state, rule, key, app) -> None:
    """The pack builds itself: how the record was found or made is saved as a recipe in learned.md, and the
    next prep turn reads it (it is in the brief for this area and app)."""
    from . import learned
    log = state.data.get("action_log", [])
    # Prefer the last create part (multi-app sequences end with the finishing app).
    create_turns = [f"PREP-{key}-create"] + [f"PREP-{key}-create-{i}" for i in range(8)]
    for turn in create_turns:
        acts = [a for a in log if a.get("case_id") == turn]
        if acts and learned.record_recipe(state.data.get("pack_root"), rule.get("area"), app,
                                          f"create test data {key}", acts):
            state.checkpoint(f"data prep {key}: saved how it was done to the pack (create test data {key})")
            return
    acts = [a for a in log if a.get("case_id") == f"PREP-{key}-find"]
    if acts and learned.record_recipe(state.data.get("pack_root"), rule.get("area"), app,
                                      f"find test data {key}", acts):
        state.checkpoint(f"data prep {key}: saved how it was done to the pack (find test data {key})")


def _verify(plan, case, state, testers, rule, rec, record, key) -> tuple[str, str]:
    """Ask the check app whether the record is usable. ('yes'|'no'|'unclear'|'error', reason).
    An answer that is not a plain yes / no is asked once more."""
    v, value = rule["verify"], rule["value"]
    check_app = v.get("app") or rule.get("app")
    vt = testers.get(check_app)
    if vt is None:
        return "yes", ""
    text = (verify_text(case, v).replace("{value}", str(rec)).replace("{record}", str(record or "unknown")) +
            f"\nIf it is usable, record_value('{value}_ok', 'yes'); otherwise record_value('{value}_ok', 'no: <why>'). "
            f"Record exactly 'yes' or 'no: <why>' as {value}_ok, never a number or amount from the page.")
    for n in range(2):
        try:
            vr = _call(vt, plan, state, rule, check_app, text, True, f"PREP-{key}-check", key)
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
