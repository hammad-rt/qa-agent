"""Pick the facts.md sections that matter for one case, so the tester follows the pack's rules even when the
plan was written before those rules were added (old plans, resumed runs)."""
from __future__ import annotations

import json
import re

ALWAYS = re.compile(r"business rules|payments", re.I)
_STOP = set("this that with from have must will when then only each case order step steps page into after before "
            "should shows show they them than over under same does make been were what your".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9&]{3,}", text.lower()) if w not in _STOP}


def sections(facts: str) -> list[tuple[str, str]]:
    out = []
    for part in re.split(r"\n(?=## )", facts or ""):
        part = part.strip()
        if part.startswith("## "):
            head = part.splitlines()[0][3:].strip()
            out.append((head, part))
    return out


def brief_text(brief: dict) -> str:
    keys = ("title", "area", "expected", "preconditions", "steps")
    return " ".join(json.dumps(brief.get(k), default=str) for k in keys if brief.get(k))


def relevant(facts: str, text: str, limit: int = 9000) -> str:
    """Sections ranked by word overlap with the case text, 'always' sections first, until limit characters."""
    want = _words(text)
    scored = []
    for i, (head, body) in enumerate(sections(facts)):
        hits = len(want & _words(body)) + 3 * len(want & _words(head))
        if ALWAYS.search(head):
            hits += 1000
        if hits >= 3:
            scored.append((-hits, i, body))
    picked, used = [], 0
    for _, i, body in sorted(scored):
        if used + len(body) > limit:
            continue
        picked.append((i, body))
        used += len(body)
    return "\n\n".join(b for _, b in sorted(picked))


def safety_rules_text(pack) -> str:
    """Always-on pack safety rules (generic). Empty when the pack has none."""
    items = (getattr(pack, "config", None) or {}).get("safety_rules") or []
    lines = []
    for item in items:
        text = item.get("text") if isinstance(item, dict) else str(item or "")
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines)
