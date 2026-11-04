"""The pack's own knowledge base, written by every run: packs/<slug>/learned.md.

Fixed code (not the model) records how a tester actually reached a screen, built from the action log, and what
discovery found. Later cases in the same run and every later run read it back, so a route is found once.
Record ids (order numbers, record= values) are stripped: records change, routes don't."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

HEADER = ("# What the agent learned\n\nWritten by the agent after each run (routes it used, discovery notes).\n"
          "Edit or delete freely; wrong lines are worse than none.\n")
_NAV_TOOLS = {"browser_navigate", "browser_click", "browser_select_option"}


def path_for(pack_root) -> Path | None:
    return Path(pack_root) / "learned.md" if pack_root else None


def scrub(text: str) -> str:
    t = re.sub(r"\[?ref=?[a-z]?\d+\]?", "", text or "")
    t = re.sub(r"(record|id|order_id|invoice_id)=[^&\s]+", r"\1=<id>", t, flags=re.I)
    t = re.sub(r"/\d{2,}(?=/|$|\?)", "/<id>", t)
    t = re.sub(r"\b[A-Z]{1,3}\d{3,}\b", "<a record>", t)
    t = re.sub(r"https?://[^/\s]+", "", t)      # host differs per env; keep the path
    return re.sub(r"\s+", " ", t).strip()


def route_from_actions(actions: list[dict], app: str, limit: int = 8) -> list[str]:
    """Navigation the tester did on this app before its first data-changing step, as readable hops."""
    hops: list[str] = []
    for a in actions:
        if a.get("app") != app:
            continue
        if a.get("approval") in ("approved", "pre_approved"):
            break
        tool, args = a.get("tool"), a.get("args") or {}
        if (a.get("outcome") or "ok") != "ok" and not str(a.get("outcome", "")).startswith("ok"):
            continue
        if tool == "browser_navigate" and args.get("url"):
            hop = f"open {scrub(args['url']) or '/'}"
        elif tool == "browser_click" and (args.get("element") or args.get("name")):
            hop = f"click '{scrub(args.get('element') or args.get('name'))}'"
        elif tool == "browser_select_option" and args.get("element"):
            hop = f"choose in '{scrub(args['element'])}'"
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
    """Append one line under '## <area>:<app>'. Skips near-duplicates. Returns True if written."""
    p = path_for(pack_root)
    line = (line or "").strip().replace("\n", " ")
    if not p or not line:
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
    """How the app reacts (silent rejects, new tabs, values that need a reload). Written next to the routes so
    later cases read it before they start. Record ids are stripped like routes."""
    body = scrub(text)[:600]
    if len(body) < 12:
        return None
    line = f"{BEHAVIOUR_PREFIX}{body} ({date.today().isoformat()}, {case_id})"
    return line if add(pack_root, area, app, line) else None
