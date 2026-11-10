"""Pack-local navigation notes: packs/<slug>/learned.md.

Split of responsibilities:
  - learned.md  — Routes, Stuck, Discovery only (operational hints for this pack). Scrubbed of env details.
  - SQLite      — lasting facts / lessons / recipes / bugs (source of truth for preload/recall).

Behaviour and lasting recipes are proposed into SQLite; a short Behaviour line may still be
appended here as a pack hint but never carries emails, URLs, logins, or record ids.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .memory.env_guard import EnvDetailError, assert_safe_lasting, scrub_env_details

HEADER = (
    "# What the agent learned\n\n"
    "Pack-local navigation notes (routes, stuck, discovery). Lasting lessons and recipes live in "
    "SQLite memory — this file is not dumped wholesale into the LLM.\n"
    "Edit or delete freely; wrong lines are worse than none. Env details (emails, URLs, logins, "
    "record ids) are stripped on write.\n"
)
_NAV_TOOLS = {"browser_navigate", "browser_click", "browser_select_option"}


def path_for(pack_root) -> Path | None:
    return Path(pack_root) / "learned.md" if pack_root else None


def scrub(text: str) -> str:
    t = re.sub(r"\[?ref=?[a-z]?\d+\]?", "", text or "")
    t = re.sub(r"(record|id|order_id|invoice_id)=[^&\s]+", r"\1=<id>", t, flags=re.I)
    t = re.sub(r"/\d{2,}(?=/|$|\?)", "/<id>", t)
    t = re.sub(r"\b[A-Z]{1,3}\d{3,}\b", "<a record>", t)
    t = re.sub(r"https?://[^/\s]+", "", t)      # host differs per env; keep the path
    t = scrub_env_details(t)
    return re.sub(r"\s+", " ", t).strip()


def route_from_actions(actions: list[dict], app: str, limit: int = 8, changes: bool = False) -> list[str]:
    """Navigation the tester did on this app before its first data-changing step, as readable hops.
    With changes=True the whole procedure, data-changing clicks included (a recipe that worked)."""
    hops: list[str] = []
    for a in actions:
        if a.get("app") != app:
            continue
        if a.get("approval") in ("approved", "pre_approved") and not changes:
            break
        tool, args = a.get("tool"), a.get("args") or {}
        if (a.get("outcome") or "ok") != "ok" and not str(a.get("outcome", "")).startswith("ok"):
            continue
        if tool == "browser_navigate" and args.get("url"):
            hop = f"open {scrub(args['url']) or '/'}"
        elif tool == "browser_click" and (args.get("element") or args.get("name")):
            hop = f"click '{scrub(args.get('element') or args.get('name'))}'"
        elif tool == "browser_select_option" and args.get("element"):
            vals = args.get("values")
            hop = f"choose {scrub(', '.join(map(str, vals)))!r} in '{scrub(args['element'])}'" if changes and vals \
                else f"choose in '{scrub(args['element'])}'"
        elif tool == "run_helper" and args.get("name"):
            hop = f"run helper {args['name']}"
        else:
            continue
        if hop and (not hops or hops[-1] != hop):
            hops.append(hop)
    return hops[-limit:]


def _sections(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            out.setdefault(cur, [])
        elif cur and line.startswith("- "):
            out[cur].append(line[2:].strip())
    return out


def _words(s: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(w) > 2}


def read(pack_root, area: str | None, app: str, title: str = "") -> list[str]:
    """Return matching lines for area:app. Callers must still budget/truncate before LLM inject."""
    p = path_for(pack_root)
    if not p or not p.is_file():
        return []
    want = _words(f"{area or ''} {title}")
    out = []
    for key, lines in _sections(p.read_text()).items():
        k_area, _, k_app = key.rpartition(":")
        if k_app != app:
            continue
        if k_area == (area or "general") or (want & _words(k_area)):
            out += lines
    return out


def add(pack_root, area: str | None, app: str, line: str) -> bool:
    """Append one line under '## <area>:<app>'. Skips near-duplicates and env-only noise."""
    p = path_for(pack_root)
    line = (line or "").strip().replace("\n", " ")
    if not p or not line:
        return False
    line = scrub(line)
    try:
        # Allow short route lines; assert_safe_lasting rejects empty-after-scrub env noise.
        body_for_guard = re.sub(r"\s*\(\d{4}-\d\d-\d\d.*?\)$", "", line)
        if body_for_guard.lower().startswith(("route:", "stuck", "discovery:", "behaviour:", "recipe")):
            # Structural prefixes are fine; guard the substance after the prefix.
            substance = body_for_guard.split(":", 1)[-1].strip() if ":" in body_for_guard else body_for_guard
            if substance:
                assert_safe_lasting(substance, context="learned.md")
        else:
            assert_safe_lasting(body_for_guard, context="learned.md")
    except EnvDetailError:
        return False
    text = p.read_text() if p.is_file() else HEADER
    key = f"{area or 'general'}:{app}"
    secs = _sections(text)
    body = re.sub(r"\s*\(\d{4}-\d\d-\d\d.*?\)$", "", line)
    if any(re.sub(r"\s*\(\d{4}-\d\d-\d\d.*?\)$", "", x) == body for x in secs.get(key, [])):
        return False
    entry = f"- {line}"
    if f"## {key}\n" in text:
        text = text.replace(f"## {key}\n", f"## {key}\n{entry}\n", 1)
    else:
        text = text.rstrip("\n") + f"\n\n## {key}\n{entry}\n"
    p.write_text(text)
    return True


def record_route(pack_root, area, app, case_id, verdict, actions) -> str | None:
    hops = route_from_actions(actions, app)
    if not hops:
        return None
    line = "Route: " + " -> ".join(hops) + f" ({date.today().isoformat()}, {case_id} {verdict})"
    return line if add(pack_root, area, app, line) else None


BEHAVIOUR_PREFIX = "Behaviour: "


def record_behaviour(pack_root, area, app, text: str, case_id: str) -> str | None:
    """Short pack hint of how the app reacts. Env details stripped. Lasting rules go to SQLite separately."""
    body = scrub(text)[:400]
    if len(body) < 12:
        return None
    line = f"{BEHAVIOUR_PREFIX}{body} ({date.today().isoformat()}, {case_id})"
    return line if add(pack_root, area, app, line) else None


def record_recipe(pack_root, area, app, what: str, actions: list[dict]) -> str | None:
    """Operational recipe hops for the pack file. Lasting recipes should also be proposed to SQLite."""
    hops = route_from_actions(actions, app, limit=30, changes=True)
    if len(hops) < 2:
        return None
    line = f"Recipe ({scrub(what)}): " + " -> ".join(hops) + f" ({date.today().isoformat()})"
    return line if add(pack_root, area, app, line) else None


def record_stuck(pack_root, area, app, case_id: str, why: str, notes: str,
                 actions: list[dict] | None = None) -> str | None:
    """What went wrong on a failed or stuck turn — read back only for the same area (budgeted)."""
    hops = route_from_actions(actions or [], app, limit=12, changes=True)
    tried: list[str] = []
    for a in reversed(actions or []):
        if a.get("tool") == "explore":
            tried = [scrub(str(t)) for t in (a.get("args") or {}).get("tried") or [] if t][:8]
            break
    note = scrub((notes or "").strip())[:280]
    kind = "loop" if "loop" in (why or "").lower() or "repeat" in (why or "").lower() else "stuck"
    parts = [f"Stuck ({kind}): {scrub(why or 'stopped').strip()[:120]}"]
    if hops:
        parts.append("reached " + " -> ".join(hops[-8:]))
    if tried:
        parts.append("already tried: " + "; ".join(tried))
    if note:
        parts.append("note: " + note)
    parts.append("Next time: do not repeat the same clicks; pick a route that was not tried, or finish stuck if none.")
    line = ". ".join(parts) + f" ({date.today().isoformat()}, {case_id})"
    return line if add(pack_root, area, app, line) else None


def scrub_file(pack_root) -> int:
    """Rewrite learned.md scrubbing env details in place. Returns number of lines changed."""
    p = path_for(pack_root)
    if not p or not p.is_file():
        return 0
    text = p.read_text()
    changed = 0
    out_lines = []
    for line in text.splitlines():
        if line.startswith("- "):
            cleaned = scrub(line[2:])
            new = f"- {cleaned}"
            if new != line:
                changed += 1
            out_lines.append(new)
        else:
            out_lines.append(line)
    if changed:
        p.write_text("\n".join(out_lines) + ("\n" if text.endswith("\n") else ""))
    return changed
