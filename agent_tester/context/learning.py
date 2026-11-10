"""Per-chunk usefulness scores in SQLite (keyed by chunk id/hash — never env identity)."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .chunks import ContextChunk

SCHEMA = """
CREATE TABLE IF NOT EXISTS context_scores (
  chunk_id TEXT PRIMARY KEY,
  source TEXT,
  step_type TEXT NOT NULL DEFAULT 'general',
  score REAL NOT NULL DEFAULT 0.5,
  sent_count INTEGER NOT NULL DEFAULT 0,
  used_count INTEGER NOT NULL DEFAULT 0,
  stall_fix INTEGER NOT NULL DEFAULT 0,
  last_sent_at TEXT,
  last_used_at TEXT,
  runs_unused INTEGER NOT NULL DEFAULT 0,
  meta TEXT
);
CREATE INDEX IF NOT EXISTS ix_ctx_source ON context_scores(source);
"""

DECAY = float(os.environ.get("CONTEXT_SCORE_DECAY", "0.92"))
DEMOTE_AFTER_UNUSED_RUNS = int(os.environ.get("CONTEXT_DEMOTE_AFTER_RUNS", "5"))
PROMOTE_BUMP = 0.18
USE_BUMP = 0.08
UNUSED_PENALTY = 0.06
STALL_PENALTY = 0.12


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _learning_on() -> bool:
    return os.environ.get("CONTEXT_LEARNING", "1").strip().lower() not in ("0", "false", "no")


class ContextScores:
    """Store/retrieve usefulness scores. Exempt chunks are never written down."""

    def __init__(self, path: Path | sqlite3.Connection | None = None):
        if isinstance(path, sqlite3.Connection):
            self.db = path
            self._owns = False
        else:
            p = Path(path) if path else Path(":memory:")
            if str(p) != ":memory:":
                p.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(p))
            self._owns = True
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        if self._owns:
            self.db.close()

    def get(self, chunk_id: str, step_type: str = "general") -> float:
        r = self.db.execute(
            "SELECT score FROM context_scores WHERE chunk_id = ? AND step_type = ?",
            (chunk_id, step_type)).fetchone()
        if r:
            return float(r["score"])
        r = self.db.execute(
            "SELECT score FROM context_scores WHERE chunk_id = ?", (chunk_id,)).fetchone()
        return float(r["score"]) if r else 0.5

    def as_dict(self, step_type: str | None = None) -> dict[str, float]:
        if step_type:
            rows = self.db.execute(
                "SELECT chunk_id, score FROM context_scores WHERE step_type = ?",
                (step_type,)).fetchall()
        else:
            # Prefer per-chunk max across step types for general selection.
            rows = self.db.execute(
                "SELECT chunk_id, MAX(score) AS score FROM context_scores GROUP BY chunk_id"
            ).fetchall()
        return {r["chunk_id"]: float(r["score"]) for r in rows}

    def _row(self, chunk_id: str, step_type: str, source: str = "") -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO context_scores(chunk_id, source, step_type) VALUES (?,?,?)",
            (chunk_id, source, step_type))

    def mark_sent(self, chunk: ContextChunk, step_type: str = "general") -> None:
        if chunk.exempt_from_scoring or not _learning_on():
            return
        self._row(chunk.id, step_type, chunk.source)
        self.db.execute(
            "UPDATE context_scores SET sent_count = sent_count + 1, last_sent_at = ?, "
            "runs_unused = 0 WHERE chunk_id = ? AND step_type = ?",
            (_now(), chunk.id, step_type))
        self.db.commit()

    def mark_used(self, chunk_id: str, step_type: str = "general", *, source: str = "") -> None:
        if not _learning_on():
            return
        self._row(chunk_id, step_type, source)
        self.db.execute(
            "UPDATE context_scores SET used_count = used_count + 1, last_used_at = ?, "
            "score = MIN(1.0, score + ?), runs_unused = 0 "
            "WHERE chunk_id = ? AND step_type = ?",
            (_now(), USE_BUMP, chunk_id, step_type))
        self.db.commit()

    def mark_stall_fix(self, chunk_id: str, step_type: str = "general") -> None:
        """Chunk was pulled (or added later) and resolved a stall — promote."""
        if not _learning_on():
            return
        self._row(chunk_id, step_type)
        self.db.execute(
            "UPDATE context_scores SET stall_fix = stall_fix + 1, "
            "score = MIN(1.0, score + ?), runs_unused = 0, last_used_at = ? "
            "WHERE chunk_id = ? AND step_type = ?",
            (PROMOTE_BUMP, _now(), chunk_id, step_type))
        self.db.commit()

    def end_run_decay(self, sent_ids: set[str], used_ids: set[str],
                      exempt_ids: set[str] | None = None) -> dict:
        """Decay scores; bump unused-run counter for sent-but-unused; demote after N runs."""
        if not _learning_on():
            return {"decayed": 0, "demoted": 0}
        exempt_ids = exempt_ids or set()
        demoted = 0
        rows = self.db.execute("SELECT chunk_id, step_type, score, runs_unused FROM context_scores").fetchall()
        for r in rows:
            cid = r["chunk_id"]
            if cid in exempt_ids:
                continue
            score = float(r["score"]) * DECAY
            unused = int(r["runs_unused"] or 0)
            if cid in sent_ids and cid not in used_ids:
                unused += 1
                score = max(0.0, score - UNUSED_PENALTY)
            if unused >= DEMOTE_AFTER_UNUSED_RUNS:
                score = min(score, 0.1)  # below demote_below default
                demoted += 1
            self.db.execute(
                "UPDATE context_scores SET score = ?, runs_unused = ? WHERE chunk_id = ? AND step_type = ?",
                (round(score, 4), unused, cid, r["step_type"]))
        self.db.commit()
        return {"decayed": len(rows), "demoted": demoted}


def record_injection(state, selection, *, case_id: str = "", step_type: str = "general",
                     call_label: str = "") -> None:
    """Append which chunks were sent (and token cost) to the run ledger / state."""
    entry = {
        "at": _now(), "case_id": case_id, "step_type": step_type, "call": call_label,
        "tokens": selection.tokens, "budget": selection.tokens_available,
        "omitted_tokens": selection.tokens_omitted,
        "chunks": [{"id": c["id"], "source": c["source"], "tokens": c["tokens"],
                    "always": c.get("always")} for c in selection.log],
        "demoted": list(selection.demoted_ids),
    }
    log = state.data.setdefault("context_injection_log", [])
    log.append(entry)
    # Ledger-friendly summary row (no env identity).
    ledger = state.data.setdefault("ledger", [])
    ledger.append({
        "case_id": case_id or "", "app": "", "record": f"context:{step_type}",
        "action": "context_inject", "final_state": json.dumps({
            "tokens": selection.tokens, "omitted": selection.tokens_omitted,
            "n_chunks": len(selection.chunks),
        }), "note": call_label, "tag": "context", "cleaned": True,
    })
    try:
        state.save()
    except Exception:  # noqa: BLE001
        pass


def record_usage_signals(state, *, case_id: str, used_chunk_ids: list[str] | None = None,
                         stall: bool = False, asked: bool = False, explored: bool = False,
                         stuck_ask: bool = False, resolved_by: str | None = None) -> None:
    """Learning signal: which chunks were used / step stalled for missing info."""
    sig = {
        "at": _now(), "case_id": case_id, "used": list(used_chunk_ids or []),
        "stall": stall, "asked": asked, "explored": explored, "stuck_ask": stuck_ask,
        "resolved_by": resolved_by,
    }
    state.data.setdefault("context_usage_log", []).append(sig)
    try:
        state.save()
    except Exception:  # noqa: BLE001
        pass


def update_scores_from_run(store: ContextScores, state, exempt_ids: set[str] | None = None) -> dict:
    """Code-built retrospective: update per-chunk scores from this run's injection/usage logs."""
    if not _learning_on():
        return {"skipped": True}
    sent: set[str] = set()
    used: set[str] = set()
    exempt = set(exempt_ids or [])
    for e in state.data.get("context_injection_log") or []:
        st = e.get("step_type") or "general"
        for c in e.get("chunks") or []:
            cid = c.get("id")
            if not cid:
                continue
            if c.get("always"):
                exempt.add(cid)
                continue
            sent.add(cid)
            # Lightweight: create row + mark sent
            ch = ContextChunk(source=c.get("source") or "unknown", text=cid, id=cid)
            if not ch.exempt_from_scoring:
                store.mark_sent(ch, st)
    for u in state.data.get("context_usage_log") or []:
        st = "general"
        for cid in u.get("used") or []:
            used.add(cid)
            store.mark_used(cid, st)
        if u.get("resolved_by"):
            store.mark_stall_fix(u["resolved_by"], st)
            used.add(u["resolved_by"])
        if u.get("stall") or u.get("stuck_ask"):
            # Soft penalty on recently sent unused chunks for this case is handled in decay.
            pass
    summary = store.end_run_decay(sent, used, exempt_ids=exempt)
    # Aggregate tokens saved for the report.
    inj = state.data.get("context_injection_log") or []
    if inj:
        state.data["context_stats"] = {
            "calls": len(inj),
            "tokens_sent": sum(e.get("tokens", 0) for e in inj),
            "tokens_omitted": sum(e.get("omitted_tokens", 0) for e in inj),
            "avg_tokens": round(sum(e.get("tokens", 0) for e in inj) / len(inj), 1),
            **summary,
        }
    try:
        state.save()
    except Exception:  # noqa: BLE001
        pass
    return summary
