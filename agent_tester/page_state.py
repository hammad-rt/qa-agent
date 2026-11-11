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

# Accessibility roles that are omit-able chrome (never menus or navigation —
# those hold dropdown items the model must click).
_CHROME = re.compile(
    r"^\s*-\s+(banner|contentinfo|complementary|search)\b", re.I | re.M)
_KEEP_NAV = re.compile(
    r"^\s*-\s+(navigation|menubar|menu)\b", re.I)
_MAINISH = re.compile(
    r"^\s*-\s+(main|article|region|document|form|table|grid|dialog|alertdialog|alert|status|"
    r"heading|tabpanel)\b", re.I | re.M)
_KEEP_INLINE = re.compile(
    r"(?i)\b(alert|error|warning|success|subpanel|detailview|editview|listview|"
    r"form|table|grid|dialog|modal|toast)\b")
_PAGE_META = re.compile(r"(?im)^(Page URL:|Page Title:|###\s)")
_HIDDEN = re.compile(r"\[(?:hidden|disabled|readonly)[^\]]*\]", re.I)
# Open popups (not the collapsed menubar/navigation chrome).
_OPEN_MENU = re.compile(r"^\s*-\s+menu\b", re.I)
_ITEMISH = re.compile(
    r"^\s*-\s+(menuitem|menuitemcheckbox|menuitemradio|option|treeitem|link|button)\b", re.I)
_EXPANDED = re.compile(
    r"\[(?:expanded|opened|open|aria-expanded(?:=true)?)\b[^\]]*\]", re.I)
_LABEL_QUOTED = re.compile(r'"([^"]{1,200})"')
_DIALOG = re.compile(r"^\s*-\s+(dialog|alertdialog)\b", re.I)


def page_hash(text: str) -> str:
    """Stable hash of page body ignoring ephemeral element refs."""
    body = re.sub(r"\[ref=[^\]]*\]", "", text or "")
    body = re.sub(r"\s+", " ", body).strip()
    return hashlib.sha1(body.encode()).hexdigest()[:16]


def _line_depth(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def page_url(text: str) -> str:
    """Page URL: header from a snapshot, or empty."""
    m = re.search(r"(?im)^Page URL:\s*(\S+)", text or "")
    return (m.group(1) if m else "").strip()


def _line_label(line: str) -> str:
    m = _LABEL_QUOTED.search(line or "")
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    plain = re.sub(r"\[ref=[^\]]*\]", "", line or "")
    plain = re.sub(r"\[[^\]]*\]", "", plain)
    plain = re.sub(r"^\s*-\s*[\w-]+\s*", "", plain)
    return re.sub(r"\s+", " ", plain).strip(" :")[:80]


def _has_open_menu(snapshot: str) -> bool:
    for ln in (snapshot or "").splitlines():
        if _HIDDEN.search(ln):
            continue
        if _OPEN_MENU.match(ln) or _EXPANDED.search(ln):
            return True
    return False


def _walk_open_subtrees(snapshot: str):
    """Yield (start_depth, [lines]) for each open menu or expanded control subtree."""
    rows = []
    for ln in (snapshot or "").splitlines():
        if ln.strip():
            rows.append((_line_depth(ln), ln))
    i = 0
    n = len(rows)
    while i < n:
        depth, ln = rows[i]
        if _HIDDEN.search(ln):
            i += 1
            continue
        start = None
        if _OPEN_MENU.match(ln) or _EXPANDED.search(ln):
            start = i
        if start is None:
            i += 1
            continue
        block = [ln]
        j = i + 1
        while j < n and rows[j][0] > depth:
            block.append(rows[j][1])
            j += 1
        yield depth, block
        i = j


def revealed_choice_items(snapshot: str, *, limit: int | None = 20) -> list[str]:
    """Labels of items in an open menu / expanded submenu (collapsed bars yield [])."""
    out: list[str] = []
    seen: set[str] = set()
    for _depth, block in _walk_open_subtrees(snapshot):
        for ln in block[1:] if len(block) > 1 else block:
            if _HIDDEN.search(ln) or not _ITEMISH.search(ln):
                continue
            lab = _line_label(ln)
            if not lab:
                continue
            key = lab.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(lab)
            if limit is not None and len(out) >= limit:
                return out
    return out


def revealed_choice_block(snapshot: str, *, limit: int = 40) -> str:
    """Accessibility-tree lines for open menus / expanded controls (refs kept)."""
    chunks: list[str] = []
    used = 0
    for _depth, block in _walk_open_subtrees(snapshot):
        # Skip a lone expanded toggle with no children (nothing new to show).
        if len(block) <= 1 and not _OPEN_MENU.match(block[0]):
            continue
        take = block[: max(1, limit - used)]
        if take:
            chunks.append("\n".join(take))
            used += len(take)
        if used >= limit:
            break
    return "\n".join(chunks)


def reveal_hint(before: str, after: str) -> str | None:
    """If a menu became expanded as a result of this click, tell the model to pick one item.

    Never hints for a menu that was already open before the click. An empty ``before``
    is inconclusive (the menu may have been open already), so this returns None.
    """
    if not (before or "").strip():
        return None
    after_items = revealed_choice_items(after or "")
    if not after_items:
        return None
    # Full set — the default 20-item cap would treat later already-open items as "new".
    before_set = {x.lower() for x in revealed_choice_items(before or "", limit=None)}
    new_items = [x for x in after_items if x.lower() not in before_set]
    if not new_items:
        return None
    return "the menu opened; pick one of: " + ", ".join(new_items[:12])


def newly_revealed_choice_block(before: str, after: str, *, limit: int = 40) -> str:
    """Accessibility-tree lines for menus that became expanded since ``before`` (refs kept)."""
    if not (before or "").strip():
        return ""
    before_set = {x.lower() for x in revealed_choice_items(before or "", limit=None)}
    chunks: list[str] = []
    used = 0
    for _depth, block in _walk_open_subtrees(after or ""):
        if len(block) <= 1 and not _OPEN_MENU.match(block[0]):
            continue
        labels = []
        for ln in block[1:] if len(block) > 1 else block:
            if _HIDDEN.search(ln) or not _ITEMISH.search(ln):
                continue
            lab = _line_label(ln)
            if lab:
                labels.append(lab)
        if not any(lab.lower() not in before_set for lab in labels):
            continue
        take = block[: max(1, limit - used)]
        if take:
            chunks.append("\n".join(take))
            used += len(take)
        if used >= limit:
            break
    return "\n".join(chunks)


def overlay_hint(snapshot: str) -> str | None:
    """If a dialog/form or validation message is visible, describe it for the model."""
    msgs = form_validation_messages(snapshot or "")
    if msgs:
        return "a validation or status message appeared: " + "; ".join(msgs[:6])
    title = ""
    actions: list[str] = []
    in_dialog = None
    for ln in (snapshot or "").splitlines():
        if not ln.strip():
            continue
        depth = _line_depth(ln)
        if in_dialog is not None and depth <= in_dialog:
            in_dialog = None
        if in_dialog is None and _DIALOG.match(ln):
            in_dialog = depth
            title = _line_label(ln)
            continue
        if in_dialog is not None and _ITEMISH.search(ln) and not _HIDDEN.search(ln):
            lab = _line_label(ln)
            if lab and lab.lower() not in {a.lower() for a in actions}:
                actions.append(lab)
    if in_dialog is None and not title and not actions:
        return None
    bits = ["a dialog or form opened"]
    if title:
        bits.append(f'("{title}")')
    if actions:
        bits.append("visible actions: " + ", ".join(actions[:8]))
    return " ".join(bits)


def append_page_note(tool_response, note: str):
    """Append a short plain-text note the model will see with the tool result."""
    note = (note or "").strip()
    if not note:
        return tool_response
    block = "\n" + note
    if isinstance(tool_response, str):
        return tool_response + block
    if isinstance(tool_response, dict):
        out = dict(tool_response)
        if "text" in out and isinstance(out["text"], str):
            out["text"] = out["text"] + block
        elif "result" in out and isinstance(out["result"], str):
            out["result"] = out["result"] + block
        elif "page_state" in out and isinstance(out["page_state"], str):
            out["page_state"] = out["page_state"] + block
        else:
            out["note"] = note
        return out
    return {"result": str(tool_response)[:2000], "note": note}


def trim_page(text: str, limit: int | None = None, *, before: str | None = None) -> str:
    """Keep the main content region; drop collapsed nav/footers/hidden chrome.

    Always keep open menus, expanded submenu items, forms, tables, dialogs and
    alerts. Falls back to a length-capped full page.

    Newly revealed menus (appeared since ``before``) are placed first so a
    length cap cannot cut them off. An already-open, unrelated user menu is
    not hoisted — it must not crowd out the main content.
    """
    limit = DEFAULT_MAX_PAGE_CHARS if limit is None else limit
    raw = text or ""
    # Under the cap, keep the full tree (including navigation / menus).
    if len(raw) <= limit:
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
        # Navigation / menus hold dropdown items; never omit them as chrome.
        if _KEEP_NAV.match(head) or _OPEN_MENU.match(head):
            keep.append(blob)
            continue
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

    # Hoist only menus newly revealed by the last click, not ones already open.
    revealed = newly_revealed_choice_block(before or "", raw) if before else ""
    parts = meta + stubs
    if revealed:
        parts.append(revealed)
    parts.extend(keep)
    out = "\n".join(parts)
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
            # Keep form fields even if marked readonly (DetailView values), and
            # keep disabled submit/toggle controls so the model can see why
            # Continue/Next is not clickable yet.
            if re.search(
                r"(?i)\b(textbox|spinbutton|combobox|cell|gridcell|rowheader|"
                r"button|checkbox|radio|switch|menuitemcheckbox|menuitemradio)\b",
                ln,
            ):
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
    # Playwright MCP often returns a snapshot after click/navigate. A short
    # tree (few refs, e.g. a frame or just-opened menu) is still a page.
    if re.search(r"(?i)Page URL:", t) and re.search(r"^\s*-\s+", t, re.M):
        return True
    refs = len(re.findall(r"\[ref=", t))
    return refs >= 8 and ("Page URL:" in t or "- " in t)


_VAL_LINE = re.compile(
    r"(?i)(?:alert|alertdialog|status|error|warning|invalid|required|helper|"
    r"validation|toast|message|banner)\b"
)
_VAL_TEXT = re.compile(
    r"(?i)(is required|are required|required field|this field is required|please (?:enter|fill|select|provide)|"
    r"must (?:be|enter|select|provide)|cannot be (?:empty|blank)|invalid (?:email|value|input|format)|"
    r"fill out this field|missing (?:required )?field|enter a value|"
    r"field is (?:required|empty|missing)|error[:\s].{3,120})"
)
_REQUIRED_MARK = re.compile(
    r"(?i)(?:\[(?:required|aria-required|invalid)[^\]]*\]|\*\s*$|required=\"true\"|aria-invalid)"
)


def form_validation_messages(snapshot: str, *, limit: int = 8) -> list[str]:
    """Pull visible validation / required-field messages from an accessibility snapshot.

    Used when Continue/Submit is clicked again with no page change — feed these to the model
    before treating the click as a loop.
    """
    if not snapshot:
        return []
    out: list[str] = []
    seen: set[str] = set()
    lines = (snapshot or "").splitlines()
    for i, ln in enumerate(lines):
        plain = re.sub(r"\[ref=[^\]]*\]", "", ln)
        plain = re.sub(r"\s+", " ", plain).strip(" -*\t")
        hit = None
        if _VAL_LINE.search(ln) or _VAL_TEXT.search(plain) or _REQUIRED_MARK.search(ln):
            # Prefer quoted text on the line, else the cleaned line.
            m = re.search(r'"([^"]{3,200})"', plain)
            hit = (m.group(1) if m else plain)[:200]
            # If the line is only a role, look at the next short line for the message.
            if hit and len(hit) < 12 and i + 1 < len(lines):
                nxt = re.sub(r"\[ref=[^\]]*\]", "", lines[i + 1])
                m2 = re.search(r'"([^"]{3,200})"', nxt)
                if m2:
                    hit = m2.group(1)[:200]
                elif _VAL_TEXT.search(nxt):
                    hit = re.sub(r"\s+", " ", nxt).strip(" -*\t")[:200]
        if not hit or len(hit) < 3:
            continue
        key = hit.lower()
        if key in seen:
            continue
        # Skip chrome / generic role-only labels and bare field names with no message.
        if key in {"alert", "error", "warning", "status", "required", "invalid",
                   "content", "field", "null", "undefined", "none", "validation"}:
            continue
        seen.add(key)
        out.append(hit)
        if len(out) >= limit:
            break
    return out

# Checkbox / radio / switch state (Playwright MCP aria snapshots use [checked]).
_TOGGLE_ROLE = re.compile(
    r"^\s*-\s+(checkbox|radio|switch|menuitemcheckbox|menuitemradio)\b", re.I)
_CHECKED_YES = re.compile(r"\[(?:checked|selected|pressed)(?:=true)?\]", re.I)
_CHECKED_NO = re.compile(
    r"\[(?:unchecked|checked=false|selected=false|pressed=false)\]", re.I)
_DISABLED_MARK = re.compile(r"\[disabled(?:[^\]]*)\]", re.I)
_REQUIRED_FIELD = re.compile(r"\[(?:required|aria-required)(?:[^\]]*)\]", re.I)
_INVALID_MARK = re.compile(r"\[(?:invalid|aria-invalid)(?:[^\]]*)\]", re.I)
_SUBMIT_LABEL = re.compile(
    r"\b(save|submit|update|apply|confirm|continue|next|create|finish|send|proceed)\b", re.I)
_NEXTISH = re.compile(r"\b(continue|next|proceed|submit|finish|confirm)\b", re.I)
_SUBMIT_ROLE = re.compile(r"^\s*-\s+(button|link|menuitem)\b", re.I)
_FIELD_ROLE = re.compile(
    r"^\s*-\s+(textbox|searchbox|combobox|spinbutton|listbox|checkbox|radio|switch|"
    r"menuitemcheckbox|menuitemradio)\b", re.I)
_ROLE_WORDS = re.compile(
    r"\b(checkbox|radio|switch|button|link|menuitem|menuitemcheckbox|menuitemradio)\b", re.I)
_UNCHECK_ROLES = frozenset({"checkbox", "switch", "menuitemcheckbox"})


def _norm_label(text: str) -> str:
    s = _ROLE_WORDS.sub(" ", (text or "").lower())
    s = re.sub(r"[^\w\s]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _labels_match(a: str, b: str) -> bool:
    na, nb = _norm_label(a), _norm_label(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    # Short generic words ("agree", "yes") must not match unrelated controls.
    if len(short) < 8:
        return False
    return short in long


def _ref_on_line(line: str, ref: str) -> bool:
    ref = (ref or "").removeprefix("ref=").strip()
    if not ref:
        return False
    return f"ref={ref}]" in line or f"ref={ref} " in line


def _line_role(line: str) -> str:
    m = re.match(r"\s*-\s*([\w-]+)", line or "")
    return (m.group(1) if m else "").lower()


def _line_checked(line: str) -> bool | None:
    """True/False when the line is a toggle; None otherwise."""
    if not line:
        return None
    if _CHECKED_NO.search(line):
        return False
    if _CHECKED_YES.search(line):
        return True
    if _TOGGLE_ROLE.search(line):
        return False  # Playwright omits [checked] when the box is off
    return None


def _field_value(line: str) -> str:
    m = re.search(r"\]:(.*)$", line or "")
    if not m:
        return ""
    return m.group(1).strip().strip('"')


def find_toggle_line(snapshot: str, *, label: str = "", ref: str = "") -> str | None:
    """Accessibility-tree line for a checkbox/radio/switch, by ref or label.

    If the ref points at a wrapper (a <label> around a visually hidden input),
    the toggle child of that wrapper is returned.
    """
    rows = [(_line_depth(ln), ln) for ln in (snapshot or "").splitlines() if ln.strip()]
    ref = (ref or "").removeprefix("ref=").strip()
    if ref:
        for i, (depth, ln) in enumerate(rows):
            if not _ref_on_line(ln, ref):
                continue
            if _TOGGLE_ROLE.search(ln):
                return ln
            # Wrapper / label: first toggle descendant.
            for depth2, child in rows[i + 1:]:
                if depth2 <= depth:
                    break
                if _TOGGLE_ROLE.search(child):
                    return child
            break
    if label:
        for _depth, ln in rows:
            if _TOGGLE_ROLE.search(ln) and _labels_match(label, _line_label(ln)):
                return ln
    return None


def toggle_is_checked(snapshot: str, *, label: str = "", ref: str = "") -> bool | None:
    """True if the named toggle is checked, False if unchecked, None if not found."""
    line = find_toggle_line(snapshot, label=label, ref=ref)
    if line is None:
        return None
    return bool(_line_checked(line))


def submit_buttons(snapshot: str) -> list[dict]:
    """Visible submit/next-style buttons: {label, disabled, role, line}."""
    out: list[dict] = []
    seen: set[str] = set()
    for ln in (snapshot or "").splitlines():
        if not _SUBMIT_ROLE.search(ln) or not _SUBMIT_LABEL.search(ln):
            continue
        lab = _line_label(ln)
        if not lab or not _SUBMIT_LABEL.search(lab):
            continue
        key = lab.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "label": lab,
            "disabled": bool(_DISABLED_MARK.search(ln)),
            "role": _line_role(ln) or "button",
            "line": ln,
        })
    return out


def _prefer_submit(labels: list[str]) -> list[str]:
    preferred = [x for x in labels if _NEXTISH.search(x)]
    rest = [x for x in labels if x not in preferred]
    return preferred + rest


def empty_required_fields(snapshot: str, *, limit: int = 8) -> list[str]:
    """Labels of required fields that are still empty or unchecked."""
    lines = [ln for ln in (snapshot or "").splitlines() if ln.strip()]
    any_radio_checked = any(
        _line_checked(ln) is True for ln in lines if _line_role(ln) == "radio")
    out: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        if not (_REQUIRED_FIELD.search(ln) or _INVALID_MARK.search(ln)):
            continue
        if not _FIELD_ROLE.search(ln):
            continue
        lab = _line_label(ln)
        if not lab:
            continue
        role = _line_role(ln)
        if role in ("checkbox", "switch", "menuitemcheckbox", "menuitemradio", "radio"):
            if _line_checked(ln) is True:
                continue
            if role == "radio" and any_radio_checked:
                continue
            desc = f"{lab} ({role}, unchecked)"
        else:
            if _field_value(ln):
                continue
            desc = f"{lab} ({role or 'field'}, empty)"
        key = desc.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(desc)
        if len(out) >= limit:
            break
    return out


def toggle_state_hint(after: str, *, label: str = "", ref: str = "",
                      before: str = "", already: bool = False) -> str | None:
    """Tell the model the toggle's checked state and which submit control to use next."""
    line = find_toggle_line(after or "", label=label, ref=ref)
    if not line:
        return None
    role = _line_role(line) or "checkbox"
    name = _line_label(line) or _norm_label(label) or "control"
    checked = bool(_line_checked(line))
    if already and not checked:
        return None
    if already or checked:
        lead = f"{role} {name!r} is {'already' if already else 'now'} checked; do not click it again"
        if role in _UNCHECK_ROLES:
            lead += " (a second click would uncheck it)"
    else:
        lead = f"{role} {name!r} is now unchecked"
    bits = [lead]

    after_btns = submit_buttons(after or "")
    enabled = [b["label"] for b in after_btns if not b["disabled"]]
    disabled = [b["label"] for b in after_btns if b["disabled"]]
    newly: list[str] = []
    if before:
        was_disabled = {b["label"].lower() for b in submit_buttons(before) if b["disabled"]}
        newly = [b["label"] for b in after_btns
                 if not b["disabled"] and b["label"].lower() in was_disabled]

    missing = empty_required_fields(after or "")
    if checked:
        point = _prefer_submit(newly or enabled)
        if point:
            bits.append(f"The {point[0]!r} button is now enabled — click it to proceed")
        elif disabled:
            why = (f"{disabled[0]!r} is still disabled; required fields still empty: "
                   + "; ".join(missing) if missing else f"{disabled[0]!r} is still disabled")
            bits.append(why)
        elif missing:
            bits.append("required fields still empty: " + "; ".join(missing))
    else:
        if disabled:
            bits.append(
                f"{disabled[0]!r} is disabled until this {role} is checked again")
        elif missing:
            bits.append("required fields still empty: " + "; ".join(missing))
    return ". ".join(bits) + "."
