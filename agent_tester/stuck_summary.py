"""Human-readable stuck / guidance summaries for CLI and the web UI.

Keeps the ask app-agnostic: no client names, logins, or environment-bound ids.
Never leaks raw element refs or JSON blobs into the text a person reads.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .page_state import form_validation_messages

_REF = re.compile(r"\[ref=[^\]]*\]")
_REF_TOKEN = re.compile(r"\b[a-z]?\d*e\d+\b", re.I)
_CLICK_INTENT = re.compile(r"^(?:click|type|select):(.+)$", re.I)
_URL_LINE = re.compile(r"(?im)^Page URL:\s*(.+)$")
_TITLE_LINE = re.compile(r"(?im)^Page Title:\s*(.+)$")
_HEADING = re.compile(r'(?im)^\s*-\s*heading\s+"([^"]+)"')
_CONTROL = re.compile(
    r'(?im)^\s*-\s*(button|link|checkbox|radio|textbox|combobox|tab|option|switch)\s+'
    r'(?:"([^"]*)")?(.*)$'
)
_DISABLED = re.compile(r"(?i)\bdisabled\b")
_REQUIRED = re.compile(r"(?i)\b(?:required|aria-required|invalid)\b")
_JSONISH = re.compile(r'^\s*[\{\[]|"content"\s*:\s*\[')


def _clean(s: str, limit: int = 160) -> str:
    t = _REF.sub("", s or "")
    t = _REF_TOKEN.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" -:;,")
    return t[:limit]


def _intent_human(sig: str) -> str:
    """'click:checkbox Agree to the above statement' → 'clicked "Agree…" checkbox'."""
    s = (sig or "").strip()
    if not s:
        return ""
    m = _CLICK_INTENT.match(s)
    if not m:
        return _clean(s, 80)
    rest = m.group(1).strip()
    # intent uses 'role name' lowercased, or a bare ref key
    if _REF_TOKEN.fullmatch(rest) or re.fullmatch(r"f?\d*e\d+", rest, re.I):
        return "clicked an unnamed control"
    role, _, name = rest.partition(" ")
    name = _clean(name or role, 80)
    role = role.lower()
    verb = "clicked" if s.lower().startswith("click:") else (
        "typed into" if s.lower().startswith("type:") else "selected in")
    if role in ("button", "link", "checkbox", "radio", "tab", "switch", "textbox", "combobox", "option"):
        if name and name != role:
            return f'{verb} "{name}" {role}'
        return f"{verb} a {role}"
    if name:
        return f'{verb} "{name}"'
    return _clean(s, 80)


def humanize_tried(recent: list[str], *, limit: int = 6) -> list[str]:
    """Collapse repeat intents into human sentences with counts."""
    cleaned = []
    for t in recent or []:
        h = _intent_human(t)
        if h:
            cleaned.append(h)
    if not cleaned:
        return []
    counts = Counter(cleaned)
    # Preserve first-seen order
    seen: list[str] = []
    for h in cleaned:
        if h not in seen:
            seen.append(h)
    out = []
    for h in seen[:limit]:
        n = counts[h]
        if n > 1:
            # "clicked X" → "clicked X 3 times"
            out.append(f"{h} {n} times")
        else:
            out.append(h)
    return out


def resolve_step(brief: dict | None, current_step: int | None, case_id: str = "") -> dict:
    """Plain-language step location. Never returns 'step ?'."""
    brief = brief or {}
    steps = list(brief.get("steps") or [])
    title = _clean(str(brief.get("title") or case_id or "this item"), 120)
    cid = str(brief.get("case_id") or case_id or "")
    kind = "case"
    if cid.upper().startswith("PREP-"):
        kind = "data_prep"
    elif cid.upper().startswith("DISCO-"):
        kind = "discovery"
    elif cid.upper().startswith("CONTACT-"):
        kind = "contact_lookup"

    step_n = current_step
    step_text = ""
    total = len(steps) or None
    if step_n is not None:
        for st in steps:
            if isinstance(st, dict) and st.get("n") == step_n:
                step_text = _clean(str(st.get("text") or ""), 200)
                break
            if not isinstance(st, dict) and steps.index(st) + 1 == step_n:
                step_text = _clean(str(st), 200)
                break
        if not step_text and 1 <= int(step_n) <= len(steps):
            st = steps[int(step_n) - 1]
            step_text = _clean(str(st.get("text") if isinstance(st, dict) else st), 200)
    else:
        # Infer next unfinished step, or before step 1 / data prep.
        done = set(brief.get("steps_done") or [])
        unfinished = [st for st in steps if isinstance(st, dict) and st.get("n") not in done]
        if unfinished:
            st = unfinished[0]
            step_n = st.get("n")
            step_text = _clean(str(st.get("text") or ""), 200)
        elif steps:
            step_n = None
            step_text = ""
        else:
            step_n = None

    if kind == "data_prep" and step_n is None:
        where = f"data prep item {cid or title}"
        step_label = "during data prep (before case steps)"
    elif kind == "data_prep" and step_n is not None:
        where = f"data prep item {cid or title}"
        step_label = f"data prep · step {step_n}" + (f" of {total}" if total else "")
        if step_text:
            step_label += f': "{step_text}"'
    elif step_n is None and not steps:
        where = f"{kind.replace('_', ' ')} {cid or title}".strip()
        step_label = "before step 1"
    elif step_n is None:
        where = f"{cid or title}"
        step_label = "before step 1"
    else:
        where = f"{cid or title}"
        step_label = f"step {step_n}" + (f" of {total}" if total else "")
        if step_text:
            step_label += f': "{step_text}"'

    goal = step_text or _clean(str(brief.get("expected") or brief.get("goal") or title), 200)
    return {
        "case_id": cid,
        "kind": kind,
        "title": title,
        "step_n": step_n,
        "step_total": total,
        "step_text": step_text,
        "step_label": step_label,
        "goal": goal,
        "where": where,
    }


def page_location(snapshot: str | dict | None, app: str = "") -> dict:
    """App + URL path + title + wizard heading from a snapshot string or dict."""
    text = ""
    if isinstance(snapshot, dict):
        # Never dump JSON into the human text — pull known fields only.
        text = str(snapshot.get("text") or snapshot.get("page_state") or "")
        if not text and isinstance(snapshot.get("content"), list):
            bits = []
            for part in snapshot["content"]:
                if isinstance(part, dict) and part.get("type") == "text":
                    bits.append(str(part.get("text") or ""))
            text = "\n".join(bits)
        url = str(snapshot.get("url") or "")
        title = str(snapshot.get("title") or "")
    else:
        text = str(snapshot or "")
        url = title = ""
    if _JSONISH.search(text[:80]) and "Page URL:" not in text:
        # Defensive: refuse to surface raw tool JSON.
        text = _clean(text, 200)
    um = _URL_LINE.search(str(snapshot) if not isinstance(snapshot, dict) else text)
    tm = _TITLE_LINE.search(str(snapshot) if not isinstance(snapshot, dict) else text)
    if um:
        url = url or um.group(1).strip()
    if tm:
        title = title or tm.group(1).strip()
    path = ""
    if url:
        try:
            u = urlparse(url)
            path = (u.path or "/") + (("?" + u.query) if u.query else "")
            if len(path) > 120:
                path = path[:117] + "..."
        except Exception:  # noqa: BLE001
            path = _clean(url, 120)
    heading = ""
    for m in _HEADING.finditer(text):
        heading = _clean(m.group(1), 80)
        # Prefer the last prominent wizard-ish heading.
    return {
        "app": _clean(app or "", 40),
        "url": url,
        "path": path or "(unknown page)",
        "title": _clean(title, 100) or "(untitled)",
        "wizard_heading": heading,
    }


def visible_controls(snapshot: str | dict | None, *, limit: int = 10) -> dict:
    """Short list of key controls and their states (disabled / required)."""
    if isinstance(snapshot, dict):
        text = str(snapshot.get("text") or snapshot.get("page_state") or "")
    else:
        text = str(snapshot or "")
    controls: list[dict] = []
    for m in _CONTROL.finditer(text):
        role, name, rest = m.group(1), m.group(2) or "", m.group(3) or ""
        label = _clean(name or role, 60)
        if not label or label in {c["label"] for c in controls}:
            continue
        state = []
        line = m.group(0)
        if _DISABLED.search(rest) or _DISABLED.search(line):
            state.append("disabled")
        if _REQUIRED.search(rest) or _REQUIRED.search(line):
            state.append("required")
        controls.append({"role": role.lower(), "label": label, "state": state})
        if len(controls) >= limit:
            break
    msgs = form_validation_messages(text, limit=5)
    disabled = [c for c in controls if "disabled" in c["state"]]
    required = [c for c in controls if "required" in c["state"]]
    return {
        "controls": controls,
        "disabled": disabled,
        "required": required,
        "validation": msgs,
    }


def guess_and_suggestions(why: str, loc: dict, seeing: dict, tried: list[str]) -> tuple[str, list[str]]:
    """Best-guess diagnosis + 1–3 short suggested replies (app-agnostic)."""
    suggestions: list[str] = []
    guess = _clean(why, 200) or "The agent is not making progress on this step."
    disabled_submits = [
        c for c in seeing.get("disabled") or []
        if re.search(r"(?i)\b(continue|next|submit|save|finish|create)\b", c.get("label") or "")
    ]
    if disabled_submits and (seeing.get("required") or seeing.get("validation")):
        need = seeing.get("validation") or [c["label"] for c in (seeing.get("required") or [])[:3]]
        guess = (
            f'A "{disabled_submits[0]["label"]}" control is disabled — a required choice on this step '
            f'is probably still empty ({"; ".join(_clean(x, 60) for x in need[:3])}).'
        )
        if seeing.get("required"):
            suggestions.append(
                f'Select "{seeing["required"][0]["label"]}" then click "{disabled_submits[0]["label"]}"'
            )
        else:
            suggestions.append(f'Fill the required field, then click "{disabled_submits[0]["label"]}"')
    elif disabled_submits:
        guess = (
            f'"{disabled_submits[0]["label"]}" is disabled on this page. Pick the unfilled required '
            f'option on the current step instead of clicking it again.'
        )
        if loc.get("wizard_heading"):
            suggestions.append(f'Choose an option under "{loc["wizard_heading"]}", then continue')
        suggestions.append("Take a fresh snapshot and fill the required choice on this step")
    elif seeing.get("validation"):
        guess = "The form shows validation messages; fix those fields before continuing."
        suggestions.append(f'Fix: {seeing["validation"][0]}')
    elif tried and any("times" in t for t in tried):
        guess = "The same control was used repeatedly without the page moving forward."
        suggestions.append("Try a different control or route to the same goal")
        if loc.get("wizard_heading"):
            suggestions.append(f'Look for another field on "{loc["wizard_heading"]}"')
    if not suggestions:
        suggestions = [
            "Re-read the page and use a control that was not tried yet",
            "Skip this case if the screen cannot do this step",
        ]
    # Dedupe / cap
    out, seen = [], set()
    for s in suggestions:
        k = s.lower()
        if k not in seen:
            seen.add(k)
            out.append(s[:120])
        if len(out) >= 3:
            break
    return guess, out


def latest_shot(run_dir: Path | None, screenshots: list[str] | None = None) -> str | None:
    """Basename of the newest png under shots/, or the last entry in screenshots."""
    if screenshots:
        for name in reversed(list(screenshots)):
            if str(name).lower().endswith(".png"):
                return Path(str(name)).name
    if not run_dir:
        return None
    shots = run_dir / "shots"
    if not shots.is_dir():
        return None
    pngs = sorted(shots.glob("*.png"), key=lambda p: p.stat().st_mtime)
    return pngs[-1].name if pngs else None


def build_stuck_summary(
    *,
    brief: dict | None,
    case_id: str,
    app: str,
    current_step: int | None,
    why: str,
    recent: list[str] | None,
    snapshot: str | dict | None,
    run_dir: Path | None = None,
    screenshots: list[str] | None = None,
    timeout_sec: int | None = None,
    thread: list[dict] | None = None,
) -> dict:
    """Structured stuck ask for CLI text + web UI cards."""
    step = resolve_step(brief, current_step, case_id=case_id)
    loc = page_location(snapshot, app=app)
    tried = humanize_tried(list(recent or []))
    seeing = visible_controls(snapshot)
    guess, suggestions = guess_and_suggestions(why, loc, seeing, tried)
    shot = latest_shot(run_dir, screenshots)
    timeout_sec = int(timeout_sec if timeout_sec is not None else os.environ.get("STUCK_ASK_TIMEOUT_SEC", "600"))
    seeing_lines = []
    for c in (seeing.get("controls") or [])[:8]:
        st = f" ({', '.join(c['state'])})" if c.get("state") else ""
        seeing_lines.append(f'{c["role"]} "{c["label"]}"{st}')
    for msg in seeing.get("validation") or []:
        seeing_lines.append(f"validation: {msg}")
    summary = {
        "kind": "stuck_chat",
        "case_id": step["case_id"] or case_id,
        "title": step["title"],
        "goal": step["goal"],
        "step_label": step["step_label"],
        "step_n": step["step_n"],
        "step_total": step["step_total"],
        "step_text": step["step_text"],
        "app": loc["app"] or app,
        "page_title": loc["title"],
        "page_path": loc["path"],
        "wizard_heading": loc["wizard_heading"],
        "tried": tried,
        "seeing": seeing_lines,
        "controls": seeing.get("controls") or [],
        "validation": seeing.get("validation") or [],
        "guess": guess,
        "suggestions": suggestions,
        "screenshot": shot,
        "timeout_sec": timeout_sec,
        "why": _clean(why, 240),
        "thread": list(thread or []),
    }
    # Guard: never leave raw refs / JSON in string fields.
    for k, v in list(summary.items()):
        if isinstance(v, str):
            summary[k] = _clean(v, 400) if ("ref=" in v or _JSONISH.search(v or "")) else v
        elif isinstance(v, list) and v and isinstance(v[0], str):
            summary[k] = [_clean(x, 200) if ("ref=" in x or x.strip().startswith("{")) else x for x in v]
    return summary


def format_stuck_prompt(summary: dict) -> str:
    """CLI / ASK prompt text a human can skim. No raw refs or JSON."""
    s = summary or {}
    lines = [
        f"Stuck on {s.get('case_id') or s.get('title') or 'this item'} ({s.get('step_label') or 'before step 1'}).",
        f"Goal: {s.get('goal') or '(not stated)'}.",
    ]
    where = f"App: {s.get('app') or '?'} · Page: {s.get('page_title') or ''} {s.get('page_path') or ''}".strip()
    if s.get("wizard_heading"):
        where += f' · Wizard step: "{s["wizard_heading"]}"'
    lines.append(where)
    if s.get("tried"):
        lines.append("Tried: " + "; ".join(s["tried"]) + ".")
    else:
        lines.append("Tried: (nothing recorded).")
    if s.get("seeing"):
        lines.append("Sees: " + "; ".join(s["seeing"][:6]) + ".")
    if s.get("guess"):
        lines.append(f"Likely issue: {s['guess']}")
    if s.get("screenshot"):
        lines.append(f"Latest screenshot: shots/{s['screenshot']}")
    if s.get("suggestions"):
        lines.append("Suggested replies:")
        for i, sug in enumerate(s["suggestions"], 1):
            lines.append(f"  S{i}: {sug}")
    lines.append(
        "Chat with the agent (it will answer, but will NOT continue the case yet), "
        "or continue when ready."
    )
    lines.append(
        "Options: (1) send a chat message  (2) skip this case  (3) stop the run  "
        "or type go / continue to resume with everything said as guidance."
    )
    to = s.get("timeout_sec") or 600
    lines.append(f"Reply with free text, S1/S2/S3, 2:<reason>, 3:<reason>, or go (wait up to {to}s):")
    # Final sweep against JSON / refs
    text = "\n".join(lines)
    text = _REF.sub("", text)
    if '"content"' in text or text.strip().startswith("{"):
        text = re.sub(r"\{[^{}]{0,400}\}", "[omitted]", text)
    return text


def agent_chat_reply(summary: dict, user_text: str) -> str:
    """Template agent reply for conversational stuck guidance (no LLM required)."""
    s = summary or {}
    u = _clean(user_text, 300)
    parts = [
        f"Thanks — I will treat that as guidance once you say go.",
        f"Still on {s.get('case_id') or 'this item'} ({s.get('step_label') or 'before step 1'}).",
        f"Goal: {s.get('goal') or '(not stated)'}.",
    ]
    if s.get("tried"):
        parts.append("Already tried: " + "; ".join(s["tried"][:4]) + ".")
    if s.get("seeing"):
        parts.append("I currently see: " + "; ".join(s["seeing"][:5]) + ".")
    if s.get("guess"):
        parts.append(f"My best guess: {s['guess']}")
    parts.append(
        f'I heard: "{u}". '
        "Ask another question, pick a suggestion, or press Continue / type go when I should resume the case."
    )
    return " ".join(parts)


def is_continue(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"go", "continue", "resume", "ok go", "please go"} or re.match(
        r"^(?:go|continue)\b", t) is not None


def persist_thread(run_dir: Path, case_id: str, thread: list[dict], summary: dict | None = None) -> None:
    """Write stuck_chat.json so resume keeps the conversation."""
    if not run_dir:
        return
    path = Path(run_dir) / "stuck_chat.json"
    data = {"case_id": case_id, "thread": thread, "summary": summary or {}}
    try:
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def load_thread(run_dir: Path, case_id: str = "") -> list[dict]:
    path = Path(run_dir) / "stuck_chat.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if case_id and data.get("case_id") and data.get("case_id") != case_id:
        return list(data.get("thread") or [])  # still return; resume may switch cases
    return list(data.get("thread") or [])


def build_now_doing(
    *,
    brief: dict | None,
    case_id: str,
    app: str,
    current_step: int | None,
    snapshot: str | dict | None,
    screenshots: list[str] | None = None,
    run_dir: Path | None = None,
    status: str = "running",
) -> dict:
    """Plain-language Live pane strip: case, step n/N, page, latest shot."""
    step = resolve_step(brief, current_step, case_id=case_id)
    loc = page_location(snapshot, app=app)
    shot = latest_shot(run_dir, screenshots)
    total = step.get("step_total")
    n = step.get("step_n")
    if n is not None and total:
        step_bit = f"step {n}/{total}"
    elif n is not None:
        step_bit = f"step {n}"
    else:
        step_bit = step.get("step_label") or "before step 1"
    line = f"{step.get('case_id') or case_id or '—'} · {step_bit}"
    if step.get("step_text"):
        line += f' · {step["step_text"]}'
    page = loc.get("wizard_heading") or loc.get("title") or loc.get("path") or ""
    if loc.get("app"):
        page = f"{loc['app']}: {page}" if page else loc["app"]
    return {
        "line": line,
        "case_id": step.get("case_id") or case_id,
        "step_n": n,
        "step_total": total,
        "step_text": step.get("step_text") or "",
        "app": loc.get("app") or app,
        "page": page,
        "page_path": loc.get("path") or "",
        "screenshot": shot,
        "status": status,
    }
