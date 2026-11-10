"""Pick context chunks by relevance within a per-step-type token budget."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .chunks import ALWAYS_SEND_SOURCES, ContextChunk, tokens_of

# Token budgets per step type (configurable via CONTEXT_BUDGET_<TYPE>).
_DEFAULT_BUDGETS = {
    "plan": 4000,
    "prep": 2500,
    "navigate": 1800,
    "read": 2000,
    "verify": 2200,
    "verdict": 2500,
    "discover": 2000,
    "general": 2000,
}


def budgets_for(step_type: str | None = None) -> dict[str, int]:
    out = dict(_DEFAULT_BUDGETS)
    for k in list(out):
        env = os.environ.get(f"CONTEXT_BUDGET_{k.upper()}")
        if env and env.isdigit():
            out[k] = int(env)
    if step_type:
        return {step_type: out.get(step_type, out["general"])}
    return out


def _learning_on() -> bool:
    return os.environ.get("CONTEXT_LEARNING", "1").strip().lower() not in ("0", "false", "no")


@dataclass
class StepContext:
    step_type: str = "general"
    app: str = ""
    module: str = ""
    case_id: str = ""
    keywords: tuple[str, ...] = ()
    query: str = ""


@dataclass
class Selection:
    chunks: list[ContextChunk] = field(default_factory=list)
    prompt: str = ""
    tokens: int = 0
    tokens_available: int = 0
    tokens_omitted: int = 0
    omitted_ids: list[str] = field(default_factory=list)
    always_ids: list[str] = field(default_factory=list)
    demoted_ids: list[str] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)


def _relevance(chunk: ContextChunk, ctx: StepContext) -> float:
    score = 0.0
    if chunk.always_send or chunk.source in ALWAYS_SEND_SOURCES:
        return 1e6
    if chunk.step_types and ctx.step_type not in chunk.step_types and "general" not in chunk.step_types:
        score -= 2.0
    elif not chunk.step_types or ctx.step_type in chunk.step_types:
        score += 1.5
    if chunk.apps and ctx.app:
        score += 2.0 if ctx.app in chunk.apps else -1.0
    if chunk.modules and ctx.module:
        score += 2.0 if ctx.module in chunk.modules else -0.5
    if chunk.case_ids and ctx.case_id:
        score += 3.0 if ctx.case_id in chunk.case_ids else -5.0
    q = (ctx.query or " ".join(ctx.keywords)).lower()
    if q:
        words = {w for w in re.findall(r"[a-z0-9]{3,}", q) if len(w) > 2}
        cwords = {w for w in re.findall(r"[a-z0-9]{3,}", (chunk.text or "").lower()) if len(w) > 2}
        if words and cwords:
            score += 2.0 * len(words & cwords) / len(words)
        for kw in chunk.keywords:
            if kw.lower() in q:
                score += 0.5
    return score


def select_chunks(chunks: list[ContextChunk], ctx: StepContext,
                  scores: dict[str, float] | None = None,
                  budget: int | None = None,
                  demote_below: float = 0.15) -> Selection:
    """Pick always-send first, then relevant chunks within budget. Learning demotes low scores.

    Safety / case_rules / case_steps / guidance / data_plan / approvals are never trimmed
    for budget or demotion — other chunks are dropped first.
    """
    scores = scores or {}
    learning = _learning_on()
    budget = budget if budget is not None else budgets_for().get(ctx.step_type, 2000)

    always, optional = [], []
    demoted = []
    for c in chunks:
        if c.always_send or c.source in ALWAYS_SEND_SOURCES or c.exempt_from_scoring:
            always.append(c)
            continue
        sc = scores.get(c.id)
        if learning and sc is not None and sc < demote_below:
            demoted.append(c)
            continue
        optional.append(c)

    # Always-send first (may exceed soft budget — never pruned).
    chosen: list[ContextChunk] = []
    used = 0
    log: list[dict] = []
    for c in always:
        chosen.append(c)
        used += c.tokens
        log.append({"id": c.id, "source": c.source, "tokens": c.tokens, "always": True})

    # Rank optional by relevance (+ learned score).
    ranked = sorted(
        optional,
        key=lambda c: -(_relevance(c, ctx) + (scores.get(c.id, 0.5) if learning else 0.5)))

    omitted = []
    for c in ranked:
        rel = _relevance(c, ctx)
        if rel < -1.0:
            omitted.append(c)
            continue
        if used + c.tokens > budget and chosen:
            omitted.append(c)
            continue
        chosen.append(c)
        used += c.tokens
        log.append({"id": c.id, "source": c.source, "tokens": c.tokens,
                    "always": False, "relevance": round(rel, 3),
                    "score": scores.get(c.id)})

    # Demoted stay on-demand only (get_context).
    for c in demoted:
        omitted.append(c)

    parts = []
    for c in chosen:
        tag = c.source.upper()
        parts.append(f"[{tag}]\n{c.text.strip()}")
    prompt = "\n\n".join(parts)
    omitted_tokens = sum(c.tokens for c in omitted)
    return Selection(
        chunks=chosen, prompt=prompt, tokens=tokens_of(prompt) if prompt else used,
        tokens_available=budget, tokens_omitted=omitted_tokens,
        omitted_ids=[c.id for c in omitted],
        always_ids=[c.id for c in always],
        demoted_ids=[c.id for c in demoted],
        log=log,
    )


class ContextSelector:
    """Stateful helper around select_chunks + pack/case builders."""

    def __init__(self, pack_cfg: dict | None = None, scores: dict[str, float] | None = None):
        self.pack_cfg = pack_cfg or {}
        self.scores = dict(scores or {})
        self._catalog: dict[str, ContextChunk] = {}

    def register(self, chunks: list[ContextChunk]) -> None:
        for c in chunks:
            self._catalog[c.id] = c

    def get(self, chunk_id: str) -> ContextChunk | None:
        return self._catalog.get(chunk_id)

    def by_topic(self, topic: str, limit: int = 3) -> list[ContextChunk]:
        """On-demand pull for demoted / omitted chunks (get_context tool)."""
        words = {w for w in re.findall(r"[a-z0-9]{3,}", (topic or "").lower())}
        scored = []
        for c in self._catalog.values():
            cwords = {w for w in re.findall(r"[a-z0-9]{3,}", c.text.lower())}
            hit = len(words & cwords) / max(1, len(words)) if words else 0
            if hit > 0 or topic.lower() in c.source.lower():
                scored.append((hit, c))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:limit]]

    def select(self, ctx: StepContext, extra: list[ContextChunk] | None = None) -> Selection:
        chunks = list(self._catalog.values()) + list(extra or [])
        # Dedupe by id, prefer always_send.
        by_id: dict[str, ContextChunk] = {}
        for c in chunks:
            prev = by_id.get(c.id)
            if prev is None or (c.always_send and not prev.always_send):
                by_id[c.id] = c
        return select_chunks(list(by_id.values()), ctx, scores=self.scores)
