"""Lean, code-side memory retrieval for one case/turn.

Hard constraint: never dump the store or learned.md into the LLM. Select a few ranked
rows by area/app/query within a small token budget. Caching is per-run per area:app so
repeated turns reuse the same selection without re-querying or re-injecting.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field

from .service import jaccard

log = logging.getLogger("qa.memory")

# ~4 chars/token is the usual estimate used elsewhere in this repo.
CHARS_PER_TOKEN = 4
DEFAULT_TOKEN_BUDGET = int(os.environ.get("MEMORY_TURN_TOKEN_BUDGET", "1200"))
DEFAULT_CHAR_BUDGET = DEFAULT_TOKEN_BUDGET * CHARS_PER_TOKEN
# Cap how many core "always apply" lessons ride along (still inside the budget).
MAX_CORE_GENERAL = 4
# Cap learned.md stuck/route hint lines injected (1–2 failure summaries + few routes).
MAX_LEARNED_HINTS = 4
ROW_TEXT_CAP = 280

KIND_WEIGHT = {"lesson": 3, "recipe": 2, "known_bug": 2, "open_bug_found": 1, "fact": 2}


def tokens_of(text: str) -> int:
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN) if text else 0


def truncate_row_text(text: str, cap: int = ROW_TEXT_CAP) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t if len(t) <= cap else t[: cap - 1].rstrip() + "…"


@dataclass
class RetrievalResult:
    rows: list[dict] = field(default_factory=list)
    learned_hints: list[str] = field(default_factory=list)
    prompt: str = ""
    chars: int = 0
    tokens: int = 0
    cache_hit: bool = False
    key: str = ""

    def as_prompt(self) -> str:
        return self.prompt


def cache_key(area: str | None, app: str, query: str = "") -> str:
    q = re.sub(r"\s+", " ", (query or "").strip().lower())[:120]
    raw = f"{area or 'general'}|{app}|{q}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _score(row: dict, query: str, area: str | None, embedder=None, qvec=None) -> float:
    """Higher is better. Code-only: keyword overlap, kind, confirmations, freshness — no LLM."""
    text = row.get("text") or ""
    score = 0.0
    if area and row.get("area") == area:
        score += 2.0
    elif row.get("area") in (None, "", "general") and row.get("scope") == "core":
        score += 0.8
    # Prefer project-scoped rows over core when both match (same area knowledge wins).
    if row.get("scope") and str(row["scope"]).startswith("project:"):
        score += 1.2
    if query:
        score += 3.0 * jaccard(query, text)
        q_words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
        t_words = {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}
        if q_words and t_words:
            score += 1.5 * len(q_words & t_words) / len(q_words)
        if qvec is not None and row.get("_vec"):
            from .embed import cosine
            score += 2.0 * cosine(qvec, row["_vec"])
    score += 0.4 * KIND_WEIGHT.get(row.get("kind"), 1)
    score += 0.15 * min(int(row.get("confirm_count") or 0), 5)
    score += 0.1 * min(int(row.get("success_count") or 0), 10)
    score -= 0.2 * min(int(row.get("failure_count") or 0), 5)
    if row.get("trust") == "hint":
        score *= 0.6
    elif row.get("trust") == "recheck":
        score *= 0.3
    if row.get("source") == "seed" and row.get("scope") == "core":
        score += 0.5
    return score


def _dedupe(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        if any(jaccard(r.get("text") or "", o.get("text") or "") > 0.85 for o in out):
            continue
        out.append(r)
    return out


def _pack_prompt(rows: list[dict], learned_hints: list[str], char_budget: int) -> tuple[str, list[dict], list[str]]:
    """Fit rows + learned hints into char_budget; truncate long texts; return prompt and kept items."""
    lines: list[str] = []
    kept_rows: list[dict] = []
    kept_hints: list[str] = []
    used = 0
    for h in learned_hints:
        line = f"- hint: {truncate_row_text(h, 160)}"
        n = len(line) + 1
        if used + n > char_budget:
            break
        lines.append(line)
        kept_hints.append(h)
        used += n
    for r in rows:
        tag = r.get("kind", "?")
        trust = r.get("trust")
        suffix = f", {trust}" if trust and trust != "trusted" else ""
        area = f", {r['area']}" if r.get("area") else ""
        body = truncate_row_text(r.get("text") or "")
        line = f"- [{r.get('id', '?')}] ({tag}{area}{suffix}) {body}"
        n = len(line) + 1
        if used + n > char_budget:
            break
        lines.append(line)
        kept_rows.append(r)
        used += n
    prompt = "\n".join(lines) if lines else "(no saved lessons for this turn)"
    return prompt, kept_rows, kept_hints


def learned_hints_for(pack_root, area: str | None, app: str, title: str = "",
                      limit: int = MAX_LEARNED_HINTS) -> list[str]:
    """At most a few Stuck / Route lines for this area:app — never the whole learned.md."""
    from .. import learned
    lines = learned.read(pack_root, area, app, title)
    stuck = [l for l in lines if l.startswith("Stuck")]
    routes = [l for l in lines if l.startswith("Route") or l.startswith("Discovery")]
    # Prefer recent stuck (file stores newest first under each section), then a couple of routes.
    picked = stuck[:2] + routes[: max(0, limit - min(2, len(stuck)))]
    return picked[:limit]


class TurnRetriever:
    """Per-run cache of retrieval results keyed by area:app (+ query fingerprint)."""

    def __init__(self, memory, *, token_budget: int | None = None, state=None):
        self.memory = memory
        self.token_budget = token_budget or DEFAULT_TOKEN_BUDGET
        self.char_budget = self.token_budget * CHARS_PER_TOKEN
        self.state = state
        if state is not None:
            self._cache: dict = state.data.setdefault("memory_retrieval_cache", {})
            self._injected: dict = state.data.setdefault("memory_injected_ids", {})
            self._log: list = state.data.setdefault("memory_injection_log", [])
        else:
            self._cache, self._injected, self._log = {}, {}, []

    def retrieve(self, *, area: str | None = None, app: str = "", query: str = "",
                 title: str = "", pack_root=None, kinds: list[str] | None = None,
                 include_learned: bool = True, force: bool = False) -> RetrievalResult:
        key = cache_key(area, app, query or title)
        if not force and key in self._cache:
            cached = self._cache[key]
            # Same selection already in context for this area:app — return empty delta prompt.
            prev_ids = set(self._injected.get(f"{area or 'general'}:{app}", []))
            cur_ids = [r["id"] for r in cached.get("rows", []) if r.get("id") is not None]
            if cur_ids and set(cur_ids) <= prev_ids:
                res = RetrievalResult(
                    rows=[], learned_hints=[], prompt="", chars=0, tokens=0, cache_hit=True, key=key,
                )
                self._record(res, area, app, skipped=True)
                return res
            res = RetrievalResult(
                rows=list(cached.get("rows") or []),
                learned_hints=list(cached.get("learned_hints") or []),
                prompt=cached.get("prompt") or "",
                chars=cached.get("chars") or 0,
                tokens=cached.get("tokens") or 0,
                cache_hit=True,
                key=key,
            )
            self._mark_injected(area, app, res.rows)
            self._record(res, area, app)
            return res

        mem = self.memory
        kinds = kinds or ["lesson", "recipe", "known_bug", "open_bug_found", "fact"]
        q = " ".join(x for x in (query, title, area, app) if x).strip()
        # Pull a modest candidate pool (active + needs_recheck as hints), score in code.
        # Active only in the auto-inject path; needs_recheck stays on-demand via recall_lessons.
        candidates = mem.store.query(
            mem.scopes, kinds, statuses=("active",),
            areas=[area] if area else None,
        )
        # Always consider a few core general lessons even when area-filtered above excluded them.
        if area:
            core = [r for r in mem.store.query(["core"], ["lesson"], statuses=("active",))
                    if r.get("area") in (None, "", "general") or r.get("applies_when") == "general"]
            candidates = _dedupe(candidates + core[:MAX_CORE_GENERAL * 2])

        for r in candidates:
            r["trust"] = mem.trust_level(r)

        qvec = mem.store.embedder.embed(q) if (q and mem.store.embedder) else None
        ranked = sorted(candidates, key=lambda r: -_score(r, q, area, mem.store.embedder, qvec))
        ranked = _dedupe(ranked)

        hints = learned_hints_for(pack_root, area, app, title) if include_learned and pack_root else []
        # Reserve ~20% of budget for learned hints when present.
        prompt, kept_rows, kept_hints = _pack_prompt(ranked, hints, self.char_budget)
        res = RetrievalResult(
            rows=[{k: v for k, v in r.items() if not k.startswith("_")} for r in kept_rows],
            learned_hints=kept_hints,
            prompt=prompt,
            chars=len(prompt),
            tokens=tokens_of(prompt),
            cache_hit=False,
            key=key,
        )
        self._cache[key] = {
            "rows": res.rows, "learned_hints": res.learned_hints,
            "prompt": res.prompt, "chars": res.chars, "tokens": res.tokens,
        }
        self._mark_injected(area, app, res.rows)
        if self.state is not None:
            self.state.save()
        self._record(res, area, app)
        return res

    def for_planner(self, areas: list[str] | None = None) -> RetrievalResult:
        """Planner gets a tiny set of general + area lessons only (no learned.md dump)."""
        area = areas[0] if areas else None
        return self.retrieve(area=area, app="", query=" ".join(areas or []), include_learned=False,
                             kinds=["lesson", "fact", "known_bug"])

    def _mark_injected(self, area, app, rows) -> None:
        k = f"{area or 'general'}:{app}"
        ids = self._injected.setdefault(k, [])
        for r in rows:
            if r.get("id") is not None and r["id"] not in ids:
                ids.append(r["id"])

    def _record(self, res: RetrievalResult, area, app, skipped: bool = False) -> None:
        entry = {
            "area": area, "app": app, "key": res.key, "chars": res.chars, "tokens": res.tokens,
            "rows": len(res.rows), "hints": len(res.learned_hints),
            "cache_hit": res.cache_hit, "skipped_duplicate": skipped,
        }
        self._log.append(entry)
        log.info("memory inject area=%s app=%s tokens=%s chars=%s rows=%s cache=%s skip=%s",
                 area, app, res.tokens, res.chars, len(res.rows), res.cache_hit, skipped)
