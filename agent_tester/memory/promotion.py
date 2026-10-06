"""Promotion of project lessons to core (tech spec 15.4). Always needs the user's approval.

A lesson becomes a candidate when the user marked it general, or a similar lesson exists in another
project. Project business rules (kind `fact`) are never promoted. The core text drops project
numbers, order IDs, money amounts and names.
"""
from __future__ import annotations

import re

from .embed import cosine
from .service import jaccard
from .store import MemoryStore


def generalize(text: str, project_words: list[str] = ()) -> str:
    t = re.sub(r"#\d+", "the task", text)
    t = re.sub(r"\b[A-Z]{1,3}\d{3,}\b", "the record", t)
    t = re.sub(r"(?:\$|KES\s?)\s?\d[\d,]*(?:\.\d+)?", "an amount", t)
    t = re.sub(r"\(verdict should be .*?\)", "", t)
    for w in project_words:
        if w:
            t = re.sub(re.escape(w), "the app", t, flags=re.I)
    return re.sub(r"\s{2,}", " ", t).strip()


def candidates(store: MemoryStore, slug: str, project_words: list[str] = (), threshold_vec: float = 0.85,
               threshold_kw: float = 0.5) -> list[dict]:
    mine = store.query([f"project:{slug}"], ["lesson", "recipe"])
    others_scopes = [r[0] for r in store.db.execute("SELECT DISTINCT scope FROM memories WHERE scope LIKE 'project:%' AND scope != ?", (f"project:{slug}",))]
    others = store.query(others_scopes, ["lesson", "recipe"]) if others_scopes else []
    core = store.query(["core"], ["lesson", "recipe"], statuses=("active", "proposed"))
    out = []
    for m in mine:
        if any(jaccard(generalize(m["text"], project_words), c["text"]) > 0.6 for c in core):
            continue                              # already in core
        why = None
        if m.get("applies_when") == "general":
            why = "you marked it general"
        else:
            for o in others:
                s = cosine(m["_vec"], o["_vec"]) if m["_vec"] and o["_vec"] else 0.0
                if s >= threshold_vec or jaccard(m["text"], o["text"]) >= threshold_kw:
                    why = f"similar lesson in {o['scope']} (#{o['id']})"
                    break
        if why:
            out.append({"id": m["id"], "why": why, "text": m["text"], "core_text": generalize(m["text"], project_words)})
    return out


def promote(store: MemoryStore, mid: int, approved_text: str) -> int:
    src = store.get(mid)
    if src is None or src["kind"] not in ("lesson", "recipe"):
        raise ValueError("only lessons and recipes can be promoted")
    return store.add(src["kind"], "core", approved_text, f"promotion:{mid}", area=src["area"], applies_when=None)
