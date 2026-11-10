"""Platform-aware record lookup without an LLM.

SuiteCRM 7 list views: basic/advanced search via URL query params; parse the table.
DetailView fields: read by label from the accessibility snapshot / page text.
Optional API path when the pack configures one (requests); browser path is default.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urljoin

from ..helpers.core.read_table import parse_tables


@dataclass
class LookupResult:
    ok: bool
    record_id: str | None = None
    values: dict[str, Any] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    notes: str = ""
    evidence: list[str] = field(default_factory=list)
    via: str = "browser"  # browser | api


# ---- SuiteCRM 7 URL builders -------------------------------------------------
def suitecrm7_list_url(base: str, module: str, filters: dict[str, str] | None = None,
                       *, advanced: bool = False) -> str:
    """Build a SuiteCRM 7 list-view URL with basic or advanced search params."""
    base = (base or "").rstrip("/") + "/"
    q: dict[str, str] = {"module": module, "action": "index", "query": "true"}
    filters = filters or {}
    if advanced:
        q["searchFormTab"] = "advanced_search"
        for field, value in filters.items():
            # Advanced: name_advanced / email_advanced etc.
            key = field if field.endswith("_advanced") else f"{field}_advanced"
            q[key] = value
            q[f"{key}_basic"] = value  # some installs mirror both
    else:
        q["searchFormTab"] = "basic_search"
        for field, value in filters.items():
            key = field if field.endswith("_basic") else f"{field}_basic"
            q[key] = value
            # Also set unsuffixed for modules that use plain names.
            q.setdefault(field, value)
    return urljoin(base, "index.php?" + urlencode(q))


def suitecrm7_detail_url(base: str, module: str, record_id: str) -> str:
    base = (base or "").rstrip("/") + "/"
    return urljoin(base, "index.php?" + urlencode({
        "module": module, "action": "DetailView", "record": record_id}))


def list_url_for_platform(platform: str, base: str, module: str,
                          filters: dict[str, str], *, advanced: bool = False) -> str:
    if platform in ("suitecrm7", "suitecrm", ""):
        return suitecrm7_list_url(base, module, filters, advanced=advanced)
    # SuiteCRM 8 / Sugar: best-effort legacy index; caller may fall back to LLM.
    return suitecrm7_list_url(base, module, filters, advanced=advanced)


# ---- Parsing -----------------------------------------------------------------
_RECORD_HREF = re.compile(
    r"(?:record=|/DetailView/)([0-9a-fA-F-]{8,36}|\d+)", re.I)
_LABEL_VALUE = re.compile(
    r"""(?is)(?:^|\n)\s*[-*]?\s*(?:heading|text|cell|gridcell|rowheader|label)?\s*"?"""
    r"""(?P<label>[^"\n]{2,80}?)"?\s*(?:\[ref=[^\]]+\])?\s*(?:[:\-–]|value)?\s*"""
    r"""(?:\[ref=[^\]]+\])?\s*"?(?P<value>[^"\n]{0,200})"?""")


def rows_from_snapshot(snapshot: str) -> list[dict]:
    tables = parse_tables(snapshot or "")
    if not tables:
        return []
    t = max(tables, key=lambda x: len(x.get("rows") or []))
    header = t.get("header") or []
    out = []
    for r in t.get("rows") or []:
        if header and len(header) == len(r):
            out.append(dict(zip(header, r)))
        else:
            out.append({"cells": list(r)})
    return out


def record_id_from_row(row: dict, snapshot_line: str = "") -> str | None:
    blob = " ".join(str(v) for v in (row.values() if isinstance(row, dict) else [row]))
    blob += " " + (snapshot_line or "")
    m = _RECORD_HREF.search(blob)
    if m:
        return m.group(1)
    # Sometimes the id is a bare cell.
    for v in (row.values() if isinstance(row, dict) else []):
        s = str(v).strip()
        if re.fullmatch(r"[0-9a-fA-F-]{8,36}|\d{3,}", s):
            return s
    return None


def detail_field(snapshot: str, label: str) -> str | None:
    """Read a DetailView field value by its visible label (generic)."""
    if not snapshot or not label:
        return None
    want = label.strip().lower().rstrip(":")
    emailish = "email" in want
    lines = (snapshot or "").splitlines()
    for i, ln in enumerate(lines):
        # Skip table column headers — they share labels with DetailView fields.
        if re.search(r"\bcolumnheader\b", ln, re.I):
            continue
        plain = re.sub(r"\[ref=[^\]]*\]", "", ln)
        m = re.search(r'"([^"]+)"', plain)
        text = (m.group(1) if m else plain).strip().rstrip(":").lower()
        if text == want or text.startswith(want + " "):
            after = plain.split(":", 1)[1].strip().strip('"') if ":" in plain else ""
            if after and after.lower() != want:
                if not emailish or "@" in after:
                    return after
            for j in range(i + 1, min(i + 6, len(lines))):
                nxt = re.sub(r"\[ref=[^\]]*\]", "", lines[j])
                if re.search(r"\bcolumnheader\b", lines[j], re.I):
                    continue
                m2 = re.search(r'"([^"]*)"', nxt)
                val = (m2.group(1) if m2 else nxt.split(":", 1)[-1]).strip()
                if not val or val.lower().rstrip(":") == want:
                    continue
                if emailish and "@" not in val:
                    continue
                return val
    # Fallback: labelled "Email: user@x.com" prose / DetailView dumps
    for m in re.finditer(
            rf"(?i){re.escape(label)}\s*:?\s*[\"']?([^\n\"']{{1,200}})", snapshot):
        val = m.group(1).strip()
        if val and val.lower() != want and (not emailish or "@" in val):
            return val
    if emailish:
        m = re.search(r"[\w.+-]+@[\w.-]+\.\w+", snapshot or "")
        if m:
            return m.group(0)
    return None


def match_row(rows: list[dict], field: str, value: str) -> dict | None:
    want = (value or "").strip().lower()
    field_l = (field or "").strip().lower()
    for r in rows:
        if not isinstance(r, dict):
            continue
        for k, v in r.items():
            if field_l and field_l not in str(k).lower() and str(k).lower() not in field_l:
                # Also accept any-column match when field is email-like.
                if field_l not in ("email", "email_address", "email1"):
                    continue
            if want and want in str(v).lower():
                return r
        # any cell contains
        if want and any(want in str(v).lower() for v in r.values()):
            return r
    return None


# ---- High-level helpers (async browser via McpBrowser-like adapter) ----------
async def find_records(browser, *, base_url: str, module: str, filters: dict[str, str],
                       platform: str = "suitecrm7", advanced: bool = False,
                       match_field: str = "", match_value: str = "",
                       screenshot_name: str = "") -> LookupResult:
    """Navigate to module list with URL search, parse table, optional row match."""
    url = list_url_for_platform(platform, base_url, module, filters, advanced=advanced)
    try:
        snap = await browser.navigate(url)
        snap = await browser.snapshot() if hasattr(browser, "snapshot") else snap
    except Exception as e:  # noqa: BLE001
        return LookupResult(ok=False, notes=f"navigate/list failed: {type(e).__name__}: {e}")
    rows = rows_from_snapshot(snap if isinstance(snap, str) else str(snap))
    evidence = []
    if screenshot_name and hasattr(browser, "screenshot"):
        try:
            evidence.append(await browser.screenshot(screenshot_name))
        except Exception:  # noqa: BLE001
            pass
    if not rows:
        return LookupResult(ok=False, notes=f"no rows on {module} list", evidence=evidence)
    row = match_row(rows, match_field or next(iter(filters), ""),
                    match_value or next(iter(filters.values()), "")) if (match_value or filters) else rows[0]
    if row is None:
        return LookupResult(ok=False, rows=rows, notes="no matching row", evidence=evidence)
    rid = record_id_from_row(row, str(snap))
    return LookupResult(ok=bool(rid), record_id=rid, values=dict(row) if isinstance(row, dict) else {},
                        rows=rows, evidence=evidence, notes="ok" if rid else "row found but no record id")


async def find_contact_by_email(browser, *, base_url: str, email: str,
                                platform: str = "suitecrm7",
                                module: str = "Contacts",
                                screenshot_name: str = "contact-lookup.png") -> LookupResult:
    """Search Contacts by email (advanced search preferred), open DetailView, confirm email."""
    # Try advanced email search first (SuiteCRM basic name search ignores email).
    for advanced, filters in (
        (True, {"email": email, "email_address": email, "email1": email}),
        (False, {"email": email, "name": email}),
    ):
        # Use a single primary filter key for URL; extras confuse some builds.
        primary = {"email1": email} if advanced else {"name": email}
        res = await find_records(
            browser, base_url=base_url, module=module, filters=primary,
            platform=platform, advanced=advanced, match_field="email", match_value=email,
            screenshot_name=screenshot_name if advanced else "")
        if res.ok and res.record_id:
            detail = await read_detail_fields(
                browser, base_url=base_url, module=module, record_id=res.record_id,
                labels=["Email Address", "Email", "Email address"],
                platform=platform)
            shown = ""
            for v in detail.values.values():
                if email.lower() in str(v).lower():
                    shown = str(v)
                    break
            if shown or not detail.values:
                res.values["email_on_record"] = shown or email
                res.values.update(detail.values)
                res.evidence += detail.evidence
                res.ok = True
                res.notes = "contact matched" if shown else "contact opened (email field not parsed; id accepted)"
                return res
            res.ok = False
            res.notes = f"email mismatch on record: {detail.values}"
    return LookupResult(ok=False, notes=f"no contact for email {email}")


async def read_detail_fields(browser, *, base_url: str, module: str, record_id: str,
                             labels: list[str], platform: str = "suitecrm7",
                             screenshot_name: str = "") -> LookupResult:
    url = suitecrm7_detail_url(base_url, module, record_id)
    try:
        await browser.navigate(url)
        snap = await browser.snapshot()
    except Exception as e:  # noqa: BLE001
        return LookupResult(ok=False, record_id=record_id,
                            notes=f"detail open failed: {type(e).__name__}: {e}")
    values = {}
    for lab in labels:
        val = detail_field(snap if isinstance(snap, str) else str(snap), lab)
        if val is not None:
            values[lab] = val
    evidence = []
    if screenshot_name and hasattr(browser, "screenshot"):
        try:
            evidence.append(await browser.screenshot(screenshot_name))
        except Exception:  # noqa: BLE001
            pass
    return LookupResult(ok=bool(values), record_id=record_id, values=values,
                        evidence=evidence, notes="ok" if values else "no labelled fields found")


async def read_wallet_fields(browser, *, base_url: str, contact_id: str,
                             platform: str = "suitecrm7",
                             labels: list[str] | None = None) -> LookupResult:
    labels = labels or ["Current Rewards", "Total Rewards", "Rewards Used",
                        "Current", "Total", "Used"]
    return await read_detail_fields(
        browser, base_url=base_url, module="Contacts", record_id=contact_id,
        labels=labels, platform=platform, screenshot_name="wallet-fields.png")


def api_find(pack_lookup: dict, **params) -> LookupResult | None:
    """Optional API path when pack configures lookups.api_url + api_key_env. Returns None to fall back."""
    import os
    import requests
    api = (pack_lookup or {}).get("api") or {}
    url = api.get("url") or api.get("api_url")
    if not url:
        return None
    key_env = api.get("api_key_env") or ""
    headers = {}
    if key_env and os.environ.get(key_env):
        headers["Authorization"] = f"Bearer {os.environ[key_env]}"
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return LookupResult(ok=False, notes=f"api failed: {type(e).__name__}: {e}", via="api")
    rid = data.get("id") or data.get("record_id")
    return LookupResult(ok=bool(rid), record_id=str(rid) if rid else None,
                        values=data if isinstance(data, dict) else {}, via="api",
                        notes="api ok" if rid else "api returned no id")


async def lookup_from_pack(browser, pack_cfg: dict, kind: str, *,
                           base_url: str, platform: str = "suitecrm7",
                           **params) -> LookupResult:
    """Dispatch a named lookup from pack.yaml `lookups.maps.<kind>`."""
    maps = ((pack_cfg or {}).get("lookups") or {}).get("maps") or {}
    conf = maps.get(kind) or {}
    # API first when configured.
    api_res = api_find((pack_cfg or {}).get("lookups") or {}, kind=kind, **params)
    if api_res is not None and api_res.ok:
        return api_res
    module = conf.get("module") or params.get("module") or "Contacts"
    if kind in ("contact_by_email", "contact"):
        email = params.get("email") or params.get("value") or ""
        return await find_contact_by_email(
            browser, base_url=base_url, email=email, platform=platform,
            module=module or "Contacts")
    if kind in ("wallet", "wallet_fields"):
        return await read_wallet_fields(
            browser, base_url=base_url, contact_id=params.get("record_id") or "",
            platform=platform, labels=conf.get("fields") or conf.get("labels"))
    # Generic list filter: shipping orders, quotes, etc.
    filters = dict(conf.get("filters") or {})
    for k, v in list(params.items()):
        if k in ("email", "contact", "status", "currency", "name") and v:
            field = (conf.get("field_map") or {}).get(k, k)
            filters[field] = str(v)
    match_field = conf.get("match_field") or next(iter(filters), "")
    match_value = filters.get(match_field, params.get("value", ""))
    return await find_records(
        browser, base_url=base_url, module=module, filters=filters,
        platform=platform, advanced=bool(conf.get("advanced", True)),
        match_field=match_field, match_value=str(match_value),
        screenshot_name=f"lookup-{kind}.png")
