"""Pack-level placeholder tokens (e.g. ORDER-MULTI) and the up-front data plan.

Core stays app-agnostic: token pattern, find/create/verify text and apps all come from the pack.
Resolved values live only in run state — never in pack/memory/settings.
"""
from __future__ import annotations

import re
from typing import Any


DEFAULT_TOKEN_PATTERN = r"(?i)\bORDER-[A-Z][A-Z0-9_-]*\b"


def token_pattern(pack_cfg: dict | None) -> re.Pattern:
    raw = ((pack_cfg or {}).get("placeholders") or {}).get("token_pattern") or DEFAULT_TOKEN_PATTERN
    return re.compile(raw)


def placeholder_items(pack_cfg: dict | None) -> list[dict]:
    """Items from pack.placeholders.items, each with at least token + value (+ find/create/verify)."""
    return list(((pack_cfg or {}).get("placeholders") or {}).get("items") or [])


def item_token(item: dict) -> str:
    """Canonical placeholder name on a pack item (`placeholder` preferred; `token` accepted)."""
    return str(item.get("placeholder") or item.get("token") or item.get("id") or "").strip()


def item_by_token(pack_cfg: dict | None, token: str) -> dict | None:
    want = token.upper()
    for it in placeholder_items(pack_cfg):
        if item_token(it).upper() == want:
            return it
    return None


def scan_tokens(text: str, pattern: re.Pattern | None = None) -> list[str]:
    """Distinct placeholder tokens found in text, preserving first-seen order."""
    pat = pattern or re.compile(DEFAULT_TOKEN_PATTERN)
    out, seen = [], set()
    for m in pat.finditer(text or ""):
        tok = m.group(0).upper()
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _case_blob(case) -> str:
    from .field_text import field_text

    parts = [
        field_text(getattr(case, "title", None), sep=" "),
        field_text(getattr(case, "preconditions", None), sep=" "),
        field_text(getattr(case, "expected", None), sep=" "),
        field_text(getattr(case, "notes", None), sep=" "),
        field_text(getattr(case, "acceptance", None), sep=" "),
    ]
    for s in getattr(case, "steps", None) or []:
        parts.append(field_text(s, sep=" "))
    return " ".join(p for p in parts if p)


def tokens_in_case(case, pattern: re.Pattern | None = None) -> list[str]:
    return scan_tokens(_case_blob(case), pattern)


def tokens_in_plan(plan, pack_cfg: dict | None = None) -> list[str]:
    """Every placeholder token used by the selected cases, order-preserving."""
    pat = token_pattern(pack_cfg)
    out, seen = [], set()
    for c in getattr(plan, "cases", None) or []:
        for t in tokens_in_case(c, pat):
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def create_parts(rule: dict) -> list[dict]:
    """Normalize create to a list of {app, text}. A plain string is one part on rule['app']."""
    c = rule.get("create")
    if not c:
        return []
    if isinstance(c, str):
        return [{"app": rule.get("app"), "text": c}]
    out = []
    for p in c:
        if isinstance(p, str):
            out.append({"app": rule.get("app"), "text": p})
        else:
            out.append({"app": p.get("app") or rule.get("app"),
                        "text": p.get("text") or p.get("do") or ""})
    return out


def subst_client(rule: dict, client: str | None) -> dict:
    """Replace {client} in find / create text. Create may be a string or a list of parts."""
    rule = dict(rule)
    if rule.get("find"):
        rule["find"] = str(rule["find"]).replace("{client}", client or "")
    c = rule.get("create")
    if isinstance(c, str):
        rule["create"] = c.replace("{client}", client or "")
    elif isinstance(c, list):
        parts = []
        for p in c:
            if isinstance(p, str):
                parts.append(p.replace("{client}", client or ""))
            else:
                p = dict(p)
                key = "text" if "text" in p or "do" not in p else "do"
                p[key] = (p.get("text") or p.get("do") or "").replace("{client}", client or "")
                parts.append(p)
        rule["create"] = parts
    return rule


def rule_from_item(item: dict) -> dict:
    """Turn a placeholders.items entry into a data_prep-shaped rule (id defaults to placeholder name)."""
    rule = dict(item)
    tok = item_token(item)
    rule.setdefault("id", (tok or str(item.get("id") or "placeholder")).lower().replace("-", "_"))
    if tok:
        rule["token"] = tok  # internal key used by data_prep.store_resolved
    if tok and "when" not in rule:
        rule["when"] = {"any": [re.escape(tok)]}
    return rule


def build_data_plan(plan, pack_cfg: dict | None, *, corrections: dict | None = None) -> list[dict]:
    """Up-front plan: one row per placeholder used by selected cases (find-or-create + create parts).

    corrections (optional, from the plan gate) may override create parts per token for this run:
      { "ORDER-MULTI": {"create": [{"app": "portal", "text": "..."}, ...]} }
    """
    pat = token_pattern(pack_cfg)
    tokens = tokens_in_plan(plan, pack_cfg)
    # Also include pack items whose when matches a case even without a literal token (legacy data_prep).
    needed: list[str] = list(tokens)
    rows = []
    for tok in needed:
        item = item_by_token(pack_cfg, tok)
        if item is None:
            rows.append({
                "token": tok, "status": "unknown",
                "description": "named in a case but not listed in pack placeholders",
                "find_app": None, "create": [], "value": None, "prep_id": None,
                "missing": True,
            })
            continue
        rule = rule_from_item(item)
        corr = (corrections or {}).get(tok) or (corrections or {}).get(tok.upper()) or {}
        if corr.get("create") is not None:
            rule["create"] = corr["create"]
        if corr.get("find") is not None:
            rule["find"] = corr["find"]
        if corr.get("find_app"):
            rule["find_app"] = corr["find_app"]
        parts = create_parts(rule)
        cases = [c.id for c in plan.cases if tok in {t.upper() for t in tokens_in_case(c, pat)}]
        rows.append({
            "token": item_token(item).upper() or tok,
            "status": "pending",
            "description": item.get("description") or "",
            "find_app": rule.get("find_app") or rule.get("app"),
            "find": rule.get("find") or "",
            "create": parts,
            "verify_app": (rule.get("verify") or {}).get("app") or rule.get("app"),
            "value": rule.get("value"),
            "aliases": list(rule.get("aliases") or []),
            "prep_id": rule.get("id"),
            "mutating": bool(rule.get("mutating", True)),
            "cases": cases,
            "missing": False,
            "rule": rule,
        })
    return rows


def format_data_plan(rows: list[dict]) -> str:
    """Plain-text table for the plan gate / progress.log / report."""
    if not rows:
        return "Data plan: no placeholders in the selected cases."
    lines = ["Data plan (find existing first; create only when nothing fits):"]
    for r in rows:
        if r.get("missing"):
            lines.append(f"  {r['token']}: NOT IN PACK — cases that need it will be blocked.")
            continue
        lines.append(f"  {r['token']}: {r.get('description') or r.get('prep_id') or ''}")
        lines.append(f"    find on [{r.get('find_app') or '?'}]: {(r.get('find') or '')[:160]}")
        parts = r.get("create") or []
        if not parts:
            lines.append("    create: (none — find only)")
        else:
            lines.append("    create:")
            for i, p in enumerate(parts, 1):
                lines.append(f"      {i}. [{p.get('app')}] {(p.get('text') or '')[:160]}")
        if r.get("cases"):
            lines.append(f"    used by: {', '.join(r['cases'])}")
    return "\n".join(lines)


def resolved_map(state) -> dict[str, dict]:
    """token -> {value, record, value_key, prep_id} from run state."""
    return dict((state.data.get("placeholders") or {}))


_NONE_VALUES = frozenset({"", "none", "no", "n/a", "na", "null", "not found", "nothing", "-"})


def _is_none_value(v) -> bool:
    return v is None or str(v).strip().strip("'\"").lower() in _NONE_VALUES


def store_resolved(state, token: str, *, value, record=None, value_key: str, prep_id: str,
                   aliases: list[str] | None = None) -> None:
    # Never persist placeholder sentinels like "none" / "not found" as the resolved value.
    if _is_none_value(value):
        return
    entry = {"value": value, "record": record, "value_key": value_key, "prep_id": prep_id}
    state.data.setdefault("placeholders", {})[token.upper()] = entry
    state.values[value_key] = {"value": value, "source": f"placeholder/{token}"}
    if record:
        state.values[f"{value_key}_record"] = {"value": record, "source": f"placeholder/{token}"}
    for a in aliases or []:
        state.values[a] = {"value": value, "source": f"placeholder/{token}"}
        if record:
            state.values[f"{a}_record"] = {"value": record, "source": f"placeholder/{token}"}
    state.save()


def case_unresolved_tokens(case, state, pack_cfg: dict | None) -> list[str]:
    """Tokens named in the case that have no resolved value yet (and are listed in the pack)."""
    got = resolved_map(state)
    out = []
    for tok in tokens_in_case(case, token_pattern(pack_cfg)):
        item = item_by_token(pack_cfg, tok)
        if item is None:
            continue  # unknown token: don't block here (prompt/BLOCKED handles)
        if tok not in got or not got[tok].get("value"):
            out.append(tok)
    return out


def inject_placeholders(brief: dict, state, pack_cfg: dict | None = None) -> dict:
    """Attach resolved placeholder map so the tester never searches for the literal token."""
    brief = dict(brief)
    got = resolved_map(state)
    if not got:
        brief["placeholders"] = {}
        return brief
    brief["placeholders"] = {
        tok: {"value": e.get("value"), "record": e.get("record"), "value_key": e.get("value_key")}
        for tok, e in got.items()
    }
    # Also surface in values_so_far under the token name for prompt clarity.
    vs = dict(brief.get("values_so_far") or {})
    for tok, e in got.items():
        vs[tok] = e.get("value")
        if e.get("record"):
            vs[f"{tok}_record"] = e.get("record")
        if e.get("value_key"):
            vs[e["value_key"]] = e.get("value")
    brief["values_so_far"] = vs
    note = (
        "PLACEHOLDERS (never search the page for these literal tokens; use the resolved value / record id): "
        + "; ".join(f"{t}={e.get('value')}" + (f" (record {e.get('record')})" if e.get("record") else "")
                    for t, e in got.items())
    )
    prev = (brief.get("placeholder_note") or "").strip()
    brief["placeholder_note"] = (prev + "\n" if prev else "") + note
    return brief


def invalidate_for_redo(state, tokens: list[str]) -> None:
    """Mutating redo: drop resolved placeholders so prep creates fresh data of the same kind."""
    store = state.data.get("placeholders") or {}
    prep = state.data.get("prep") or {}
    for tok in tokens:
        e = store.pop(tok.upper(), None)
        if not e:
            continue
        pid = e.get("prep_id")
        if pid and pid in prep:
            prep[pid]["consumed"] = True
        vk = e.get("value_key")
        if vk:
            state.values.pop(vk, None)
            state.values.pop(f"{vk}_record", None)
    state.save()


def discovery_items_from_plan(data_plan: list[dict], app: str) -> list[str]:
    """Short checklist lines for discovery from data-plan parts that run on this app."""
    items = []
    for row in data_plan or []:
        if row.get("missing"):
            continue
        if (row.get("find_app") or "") == app and row.get("find"):
            items.append(f"find/list search for {row['token']}")
        for p in row.get("create") or []:
            if p.get("app") == app and (p.get("text") or "").strip():
                # Keep lean: first clause / ~80 chars of the part.
                bit = re.split(r"[.;\n]", p["text"], maxsplit=1)[0].strip()[:80]
                items.append(f"{row['token']} create: {bit}")
        if (row.get("verify_app") or "") == app:
            items.append(f"verify {row['token']} on {app}")
    # dedupe preserve order
    out, seen = [], set()
    for x in items:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def checklist_from_cases(cases, app: str, *, limit: int = 10) -> list[str]:
    """Dedupe short action phrases from case steps that target this app."""
    # Verb-ish cues kept generic (no project names).
    cues = [
        (r"(?i)\bcreate\b.*\border\b", "create order"),
        (r"(?i)\brecord\b.*\bpayment\b|\badd\b.*\bpayment\b|\bpayments?\s+subpanel\b", "record payment"),
        (r"(?i)\bmove\b.*\bstatus\b|\border\s+status\b|\bstatus\b.*\b(ship|deliver|invoice)", "move status"),
        (r"(?i)\binvoice\b.*\bshipping\b|\bshipping\b.*\binvoice\b", "invoice shipping"),
        (r"(?i)\bdetail\s*view\b|\bopen\b.*\border\b|\bsubpanel\b",
         "open record detail view and list its subpanels and their Create buttons"),
        (r"(?i)\bpay\b.*\bcard\b|\bstripe\b", "pay by card"),
        (r"(?i)\bwallet\b|\brewards?\b", "open wallet / rewards"),
        (r"(?i)\bparcel\b|\bpackage\b", "parcel / package"),
        (r"(?i)\blog\s*in\b|\blogin\b", "login"),
        (r"(?i)\bquote\b|\bbuy\s*&?\s*ship\b", "buy & ship / quote"),
    ]
    out, seen = [], set()
    for c in cases or []:
        for s in getattr(c, "steps", None) or []:
            sapp = getattr(s, "app", None) or (s.get("app") if isinstance(s, dict) else None) or "none"
            if sapp != app:
                continue
            text = getattr(s, "text", None) or (s.get("text") if isinstance(s, dict) else "") or ""
            for pat, label in cues:
                if re.search(pat, text) and label not in seen:
                    seen.add(label)
                    out.append(label)
                    if len(out) >= limit:
                        return out
    return out


def merge_checklist(*groups: list[str], limit: int = 10) -> list[str]:
    out, seen = [], set()
    for g in groups:
        for x in g or []:
            k = x.lower()
            if k not in seen:
                seen.add(k)
                out.append(x)
            if len(out) >= limit:
                return out
    return out
