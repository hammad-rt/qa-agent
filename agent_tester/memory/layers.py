"""Knowledge layers: domain / platform / project merge and override suppression.

More specific wins: project > platform > domain > core. Project rows with
key ``overrides:<sha1>`` suppress matching domain/platform notes for that project.
"""
from __future__ import annotations

import hashlib
import re

def _words(t: str) -> set[str]:
    stop = {"the", "and", "for", "with", "not", "but", "are", "was", "when", "that", "this", "from", "after"}
    return {w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if len(w) > 2 and w not in stop}


def jaccard(a: str, b: str) -> float:
    x, y = _words(a), _words(b)
    return len(x & y) / len(x | y) if x and y else 0.0


LAYER_RANK = {
    "project": 40,
    "platform": 30,
    "domain": 20,
    "core": 10,
}


def scope_layer(scope: str) -> str:
    s = scope or ""
    if s.startswith("project:"):
        return "project"
    if s.startswith("platform:"):
        return "platform"
    if s.startswith("domain:"):
        return "domain"
    return "core"


def text_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def override_key(contradicted_text: str) -> str:
    return f"overrides:{text_hash(contradicted_text)}"


def parse_override_target(row: dict) -> str | None:
    key = row.get("key") or ""
    if key.startswith("overrides:"):
        return key.split(":", 1)[1]
    return None


def override_text(contradicted: str, true_here: str) -> str:
    c = re.sub(r"\s+", " ", (contradicted or "").strip())[:160]
    t = re.sub(r"\s+", " ", (true_here or "").strip())[:200]
    return f"Not true here: {c}. True here: {t}"


def suppressed_hashes(rows: list[dict]) -> set[str]:
    """Hashes of notes a project override says are not true here."""
    out: set[str] = set()
    for r in rows:
        if scope_layer(r.get("scope") or "") != "project":
            continue
        h = parse_override_target(r)
        if h:
            out.add(h)
            # Also parse "Not true here: …" body for fuzzy match helpers.
            text = r.get("text") or ""
            m = re.match(r"(?i)not true here:\s*(.+?)\.\s*true here:", text)
            if m:
                out.add(text_hash(m.group(1)))
    return out


def is_suppressed(row: dict, suppress: set[str], project_rows: list[dict] | None = None) -> bool:
    """True when a domain/platform row is contradicted by a project override."""
    layer = scope_layer(row.get("scope") or "")
    if layer not in ("domain", "platform"):
        return False
    h = text_hash(row.get("text") or "")
    if h in suppress:
        return True
    # Fuzzy: project override body highly overlaps this note.
    for p in project_rows or []:
        if parse_override_target(p) is None and not (p.get("text") or "").lower().startswith("not true here"):
            continue
        body = p.get("text") or ""
        m = re.match(r"(?i)not true here:\s*(.+?)\.\s*true here:", body)
        target = m.group(1) if m else body
        if jaccard(target, row.get("text") or "") >= 0.55:
            return True
    return False


def merge_layers(rows: list[dict]) -> list[dict]:
    """Drop suppressed domain/platform notes; prefer project when near-duplicates."""
    project = [r for r in rows if scope_layer(r.get("scope") or "") == "project"]
    suppress = suppressed_hashes(project)
    kept: list[dict] = []
    for r in rows:
        if is_suppressed(r, suppress, project):
            continue
        # Near-duplicate across layers: keep the more specific one.
        twin_i = next(
            (i for i, k in enumerate(kept)
             if jaccard(k.get("text") or "", r.get("text") or "") > 0.85),
            None,
        )
        if twin_i is None:
            kept.append(r)
            continue
        old = kept[twin_i]
        if LAYER_RANK.get(scope_layer(r.get("scope") or ""), 0) > LAYER_RANK.get(
            scope_layer(old.get("scope") or ""), 0
        ):
            kept[twin_i] = r
    return kept


def layer_score_bonus(row: dict) -> float:
    layer = scope_layer(row.get("scope") or "")
    if layer == "project":
        return 1.2
    if layer == "platform":
        return 0.4  # present but not trusted fully
    if layer == "domain":
        return 0.6
    return 0.0


def default_domain_scopes() -> list[str]:
    return ["domain:crm", "domain:erp"]


def platform_scope(platform_id: str | None) -> str | None:
    if not platform_id:
        return None
    pid = re.sub(r"[^a-z0-9]+", "", platform_id.lower())
    return f"platform:{pid}" if pid else None
