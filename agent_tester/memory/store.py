"""Long-term memory in SQLite (tech spec section 7). Rows are scoped `core` or `project:<slug>`.

Kinds: fact | lesson | recipe | known_bug | open_bug_found | resource
Status: active | proposed | needs_recheck | retired   (proposed = waiting for approval at end of run)
Secrets are never stored: resources keep only the env var name in secret_env.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..redact import redact, scrub_secrets
from .embed import Embedder, cosine, pack_vec, unpack_vec

KINDS = {"fact", "lesson", "recipe", "known_bug", "open_bug_found", "resource"}
STATUSES = {"active", "proposed", "needs_recheck", "retired"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL, scope TEXT NOT NULL, area TEXT, applies_when TEXT,
  key TEXT, text TEXT NOT NULL, value_or_ref TEXT, secret_env TEXT,
  source TEXT NOT NULL, linked_issue INTEGER, evidence TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL, last_confirmed_at TEXT,
  success_count INTEGER NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0,
  embedding BLOB
);
CREATE INDEX IF NOT EXISTS ix_scope_kind ON memories(scope, kind, status);
CREATE INDEX IF NOT EXISTS ix_key ON memories(scope, key);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, area, applies_when, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, text, area, applies_when) VALUES (new.id, new.text, new.area, new.applies_when);
END;
CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE OF text, area, applies_when ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, text, area, applies_when) VALUES ('delete', old.id, old.text, old.area, old.applies_when);
  INSERT INTO memories_fts(rowid, text, area, applies_when) VALUES (new.id, new.text, new.area, new.applies_when);
END;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(text: str) -> str:
    return scrub_secrets(redact(text or ""))


class MemoryStore:
    def __init__(self, path: Path, embedder: Embedder | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.embedder = embedder

    # ---- write -------------------------------------------------------------------
    def add(self, kind: str, scope: str, text: str, source: str, *, area: str | None = None,
            applies_when: str | None = None, key: str | None = None, value_or_ref: str | None = None,
            secret_env: str | None = None, linked_issue: int | None = None, evidence: str | None = None,
            status: str = "active") -> int:
        assert kind in KINDS and status in STATUSES, (kind, status)
        assert scope == "core" or scope.startswith("project:"), scope
        text = _clean(text)
        vec = self.embedder.embed(text) if self.embedder else None
        cur = self.db.execute(
            """INSERT INTO memories(kind, scope, area, applies_when, key, text, value_or_ref, secret_env, source,
               linked_issue, evidence, status, created_at, last_confirmed_at, embedding)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (kind, scope, area, applies_when, key, text, _clean(value_or_ref) if value_or_ref else None, secret_env,
             source, linked_issue, evidence, status, now(), now() if status == "active" else None, pack_vec(vec)))
        self.db.commit()
        return cur.lastrowid

    def set_status(self, mid: int, status: str) -> None:
        assert status in STATUSES
        extra = ", last_confirmed_at = ?" if status == "active" else ""
        args = (status, now(), mid) if extra else (status, mid)
        self.db.execute(f"UPDATE memories SET status = ?{extra} WHERE id = ?", args)
        self.db.commit()

    def bump(self, mid: int, ok: bool) -> dict:
        col = "success_count" if ok else "failure_count"
        self.db.execute(f"UPDATE memories SET {col} = {col} + 1" + (", last_confirmed_at = ?" if ok else "") + " WHERE id = ?",
                        (now(), mid) if ok else (mid,))
        self.db.commit()
        return self.get(mid)

    # ---- read --------------------------------------------------------------------
    def get(self, mid: int) -> dict | None:
        r = self.db.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()
        return _row(r)

    def query(self, scopes: list[str], kinds: list[str] | None = None, statuses: tuple[str, ...] = ("active",),
              areas: list[str] | None = None, key: str | None = None) -> list[dict]:
        sql = f"SELECT * FROM memories WHERE scope IN ({','.join('?' * len(scopes))}) AND status IN ({','.join('?' * len(statuses))})"
        args: list = [*scopes, *statuses]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        if areas:
            sql += f" AND (area IS NULL OR area IN ({','.join('?' * len(areas))}))"
            args += areas
        if key:
            sql += " AND key = ?"
            args.append(key)
        sql += " ORDER BY id DESC"
        return [_row(r) for r in self.db.execute(sql, args)]

    def search(self, scopes: list[str], query: str, k: int = 5, kinds: list[str] | None = None,
               statuses: tuple[str, ...] = ("active",)) -> list[dict]:
        """Embedding search when available, else FTS5 keyword search. Only the given scopes are searched."""
        rows = self.query(scopes, kinds, statuses)
        if not rows:
            return []
        qv = self.embedder.embed(query) if self.embedder else None
        if qv and any(r["_vec"] for r in rows):
            scored = sorted(((cosine(qv, r["_vec"]), r) for r in rows if r["_vec"]), key=lambda x: -x[0])
            return [dict(r, score=round(s, 3)) for s, r in scored[:k] if s > 0.3]
        terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
        if not terms:
            return []
        ids = {r["id"]: r for r in rows}
        fts = " OR ".join(f'"{t}"' for t in terms)
        hits = self.db.execute("SELECT rowid, bm25(memories_fts) AS s FROM memories_fts WHERE memories_fts MATCH ? ORDER BY s LIMIT ?",
                               (fts, k * 4)).fetchall()
        out = [dict(ids[h["rowid"]], score=round(-h["s"], 3)) for h in hits if h["rowid"] in ids]
        return out[:k]

    def close(self) -> None:
        self.db.close()


def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["_vec"] = unpack_vec(d.pop("embedding", None))
    return d


def public(row: dict) -> dict:
    """Row without internals, for prompts and reports."""
    return {k: v for k, v in row.items() if not k.startswith("_") and v not in (None, "")}


def dump_json(store: MemoryStore, path: Path) -> None:
    """Plain-text export for the git backup (embeddings dropped)."""
    scopes = [r[0] for r in store.db.execute("SELECT DISTINCT scope FROM memories")]
    rows = [public(r) for r in store.query(scopes, statuses=tuple(STATUSES))] if scopes else []
    path.write_text(json.dumps(rows, indent=2))
