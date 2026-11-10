"""Trim and cache browser page state so the model rarely needs a separate snapshot turn.

After every state-changing browser tool, code captures a trimmed snapshot and returns it with
the tool result. A later browser_snapshot on an unchanged page gets a short stub (not a loop step).
"""
from __future__ import annotations

import hashlib
import os
import re

# Soft default: leaner than the old 30k (~8k tokens). Override with TESTER_MAX_PAGE_CHARS.
DEFAULT_MAX_PAGE_CHARS = int(os.environ.get("TESTER_MAX_PAGE_CHARS", "12000"))

STATE_CHANGING = frozenset({
    "browser_click", "browser_navigate", "browser_navigate_back", "browser_type",
    "browser_fill_form", "browser_select_option", "browser_press_key", "browser_drag",
    "browser_drop", "browser_handle_dialog", "browser_file_upload", "paste_text",
    "run_helper",
})
# Evaluate / run_code can navigate; when allowed, treat as state-changing.
MAYBE_NAV = frozenset({"browser_evaluate", "browser_run_code", "browser_run_code_unsafe"})

UNCHANGED = ("Page unchanged since the last captured page state — see that result "
             "(no new snapshot taken).")

# Accessibility roles that are chrome / repeated UI.
_CHROME = re.compile(
    r"^\s*-\s+(navigation|banner|contentinfo|menubar|menu|complementary|search)\b", re.I | re.M)
_MAINISH = re.compile(
    r"^\s*-\s+(main|article|region|document|form|table|grid|dialog|alertdialog|alert|status|"
    r"heading|tabpanel)\b", re.I | re.M)
_KEEP_INLINE = re.compile(
    r"(?i)\b(alert|error|warning|success|subpanel|detailview|editview|listview|"
    r"form|table|grid|dialog|modal|toast)\b")
_PAGE_META = re.compile(r"(?im)^(Page URL:|Page Title:|###\s)")
_HIDDEN = re.compile(r"\[(?:hidden|disabled|readonly)[^\]]*\]", re.I)


def page_hash(text: str) -> str:
    """Stable hash of page body ignoring ephemeral element refs."""
    body = re.sub(r"\[ref=[^\]]*\]", "", text or "")
    body = re.sub(r"\s+", " ", body).strip()
    return hashlib.sha1(body.encode()).hexdigest()[:16]


def _line_depth(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def trim_page(text: str, limit: int | None = None) -> str:
    """Keep the main content region; drop nav/menus/footers/hidden chrome when possible.

    Generic for SuiteCRM-like UIs: prefer the largest main/content container; always keep
    forms, tables, subpanel headers and alerts. Falls back to a length-capped full page.
    """
    limit = DEFAULT_MAX_PAGE_CHARS if limit is None else limit
    raw = text or ""
    if len(raw) <= limit and not _CHROME.search(raw):
        return _drop_hidden(raw)

    lines = raw.splitlines()
    meta = [ln for ln in lines if _PAGE_META.match(ln)]
    body = [ln for ln in lines if not _PAGE_META.match(ln)]

    # Split into top-level blocks by indent-0 "- role" lines.
    blocks: list[list[str]] = []
    cur: list[str] = []
    for ln in body:
        if ln.startswith("- ") and cur:
            blocks.append(cur)
            cur = [ln]
        elif not cur and not ln.strip():
            continue
        else:
            if not cur:
                cur = [ln]
            else:
                cur.append(ln)
    if cur:
        blocks.append(cur)

    keep: list[str] = []
    chrome: list[str] = []
    for blk in blocks:
        head = blk[0] if blk else ""
        blob = "\n".join(blk)
        if _CHROME.match(head) and not _KEEP_INLINE.search(blob[:400]):
            chrome.append(blob)
            continue
        if _MAINISH.match(head) or _KEEP_INLINE.search(blob[:400]) or "subpanel" in blob[:400].lower():
            keep.append(blob)
            continue
        # Prefer large non-chrome blocks (SuiteCRM often lacks a clean "main" role).
        if len(blob) >= 400:
            keep.append(blob)
        else:
            chrome.append(blob)

    if not keep:
        # Largest block wins when nothing looked like main content.
        keep = sorted(blocks, key=lambda b: -sum(len(x) for x in b))[:2]
        keep = ["\n".join(b) for b in keep]

    # Keep a one-line stub of dropped chrome so the model knows menus exist.
    stubs = []
    for c in chrome[:3]:
        first = next((ln.strip() for ln in c.splitlines() if ln.strip()), "")
        if first:
            stubs.append(f"[chrome omitted: {first[:80]}]")

    out = "\n".join(meta + stubs + keep)
    out = _drop_hidden(out)
    if len(out) > limit:
        out = (out[:limit] + f"\n[... page text cut: {len(out) - limit} more characters. "
               "Use browser_find for the rest.]")
    return out


def _drop_hidden(text: str) -> str:
    """Drop lines that are only hidden/disabled chrome noise when labelled as such."""
    out = []
    for ln in (text or "").splitlines():
        if _HIDDEN.search(ln) and not _KEEP_INLINE.search(ln):
            # Keep form fields even if marked readonly (DetailView values).
            if re.search(r"(?i)\b(textbox|spinbutton|combobox|cell|gridcell|rowheader)\b", ln):
                out.append(ln)
            continue
        out.append(ln)
    return "\n".join(out)


def is_state_changing(tool_name: str, result_text: str = "") -> bool:
    if tool_name in STATE_CHANGING:
        return True
    if tool_name in MAYBE_NAV:
        # Heuristic: result shows a new Page URL or "navigat".
        t = result_text or ""
        return bool(re.search(r"(?i)Page URL:|navigat", t))
    return False


def merge_page_into_result(tool_response, page_text: str, keep_emails=(), cap: int | None = None):
    """Attach trimmed page state to a tool result object/string/dict."""
    from .redact import redact
    cap = DEFAULT_MAX_PAGE_CHARS if cap is None else cap
    trimmed = trim_page(redact(page_text or "", keep_emails=keep_emails), cap)
    block = f"\n--- page state (auto) ---\n{trimmed}"
    if isinstance(tool_response, str):
        return tool_response + block
    if isinstance(tool_response, dict):
        out = dict(tool_response)
        if "text" in out and isinstance(out["text"], str):
            out["text"] = out["text"] + block
        elif "content" in out and isinstance(out["content"], list):
            out = {**out, "page_state": trimmed}
        else:
            out["page_state"] = trimmed
        return out
    # ADK / MCP objects: return a plain dict the model can read.
    return {"result": str(tool_response)[:2000], "page_state": trimmed}


def result_already_has_page(text: str) -> bool:
    """True when the tool result already embeds a substantial accessibility tree."""
    t = text or ""
    if "--- page state (auto) ---" in t:
        return True
    # Playwright MCP often returns a snapshot after click/navigate.
    refs = len(re.findall(r"\[ref=", t))
    return refs >= 8 and ("Page URL:" in t or "- " in t)
