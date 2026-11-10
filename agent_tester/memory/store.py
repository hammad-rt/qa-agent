"""Long-term memory in SQLite.

Scopes: `core`, `domain:<name>`, `platform:<id>`, `project:<slug>`.
Project always wins over platform/domain at retrieval; see memory/layers.py.

Kinds: fact, lesson, recipe, known_bug, open_bug_found, resource.
`resource` may only store secret_env *names*, never env values (URLs, logins, ids).
Per-run logs and retrospectives live with the run, never as standing facts.

Status: active | proposed | needs_recheck | retired
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..redact import redact, scrub_secrets
from .embed import Embedder, cosine, pack_vec, unpack_vec
from .env_guard import EnvDetailError, assert_safe_lasting, is_env_resource_key, scrub_env_details

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

_EXTRA_COLS = {
    "confirm_count": "INTEGER NOT NULL DEFAULT 0",
    "env_at_confirm": "TEXT",
    "decided_by": "TEXT",
}


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
        self._migrate()
        self._ensure_discovery_maps()
        self.embedder = embedder

    def _migrate(self) -> None:
        have = {r[1] for r in self.db.execute("PRAGMA table_info(memories)")}
        for col, decl in _EXTRA_COLS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE memories ADD COLUMN {col} {decl}")
        self.db.commit()

    def _ensure_discovery_maps(self) -> None:
        """Verified UI routes per checklist item (see discovery_map.py). Same DB file, separate table."""
        from ..discovery_map import MAP_SCHEMA
        self.db.executescript(MAP_SCHEMA)
        self.db.commit()

    # ---- write -------------------------------------------------------------------
    def add(self, kind: str, scope: str, text: str, source: str, *, area: str | None = None,
            applies_when: str | None = None, key: str | None = None, value_or_ref: str | None = None,
            secret_env: str | None = None, linked_issue: int | None = None, evidence: str | None = None,
            status: str = "active", decided_by: str | None = None,
            confirm_count: int = 0, env_at_confirm: str | None = None,
            skip_env_guard: bool = False) -> int:
        assert kind in KINDS and status in STATUSES, (kind, status)
        assert (
            scope == "core"
            or scope.startswith("project:")
            or scope.startswith("domain:")
            or scope.startswith("platform:")
        ), scope
        text = _clean(text)

        # Resources must never hold env values (URLs, logins, record ids) as lasting memory.
        if kind == "resource":
            value_or_ref = None
            if secret_env:
                text = f"{key or 'resource'} (secret in env var {secret_env})"
            else:
                text = f"{key or 'resource'} (value from run settings each run; not stored)"
            text = _clean(text)
        elif not skip_env_guard and kind in ("fact", "lesson", "recipe"):
            try:
                text = assert_safe_lasting(text, context=f"memory.{kind}")
            except EnvDetailError:
                # Soft-scrub then re-check; still refuse if substance was only env noise.
                text = assert_safe_lasting(scrub_env_details(text), context=f"memory.{kind}")

        vec = self.embedder.embed(text) if self.embedder else None
        confirmed = now() if status == "active" else None
        cur = self.db.execute(
            """INSERT INTO memories(kind, scope, area, applies_when, key, text, value_or_ref, secret_env, source,
               linked_issue, evidence, status, created_at, last_confirmed_at, embedding,
               confirm_count, env_at_confirm, decided_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (kind, scope, area, applies_when, key, text, _clean(value_or_ref) if value_or_ref else None, secret_env,
             source, linked_issue, evidence, status, now(), confirmed, pack_vec(vec),
             confirm_count, env_at_confirm, decided_by or (source if source.startswith("user") else None)))
        self.db.commit()
        return cur.lastrowid

    def set_status(self, mid: int, status: str, *, env_at_confirm: str | None = None) -> None:
        assert status in STATUSES
        if status == "active":
            self.db.execute(
                "UPDATE memories SET status = ?, last_confirmed_at = ?, env_at_confirm = COALESCE(?, env_at_confirm) WHERE id = ?",
                (status, now(), env_at_confirm, mid))
        else:
            self.db.execute("UPDATE memories SET status = ? WHERE id = ?", (status, mid))
        self.db.commit()

    def bump(self, mid: int, ok: bool) -> dict:
        col = "success_count" if ok else "failure_count"
        if ok:
            self.db.execute(
                f"UPDATE memories SET {col} = {col} + 1, last_confirmed_at = ?, confirm_count = confirm_count + 1 WHERE id = ?",
                (now(), mid))
        else:
            self.db.execute(f"UPDATE memories SET {col} = {col} + 1 WHERE id = ?", (mid,))
        self.db.commit()
        return self.get(mid)

    def bump_confirm(self, mid: int, *, env_fp: str | None = None) -> dict:
        """Another run confirmed the same lesson; may activate provisional (proposed) lessons/recipes."""
        self.db.execute(
            "UPDATE memories SET confirm_count = confirm_count + 1, last_confirmed_at = ?, "
            "env_at_confirm = COALESCE(?, env_at_confirm) WHERE id = ?",
            (now(), env_fp, mid))
        self.db.commit()
        row = self.get(mid)
        if row and row["kind"] in ("lesson", "recipe") and row["status"] == "proposed" and row["confirm_count"] >= 2:
            self.set_status(mid, "active", env_at_confirm=env_fp)
            row = self.get(mid)
        return row

    def update_text(self, mid: int, text: str) -> None:
        """Update text; re-embed only when the cleaned text actually changed."""
        row = self.get(mid)
        if not row:
            return
        text = assert_safe_lasting(_clean(text), context="memory.update")
        if text == row["text"]:
            return
        vec = self.embedder.embed(text) if self.embedder else None
        self.db.execute("UPDATE memories SET text = ?, embedding = ? WHERE id = ?",
                        (text, pack_vec(vec), mid))
        self.db.commit()

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

    def prune_duplicates(self, scopes: list[str] | None = None, threshold: float = 0.9) -> list[int]:
        """Merge near-duplicate active/proposed rows: keep the oldest, retire the rest, sum counts."""
        from .service import jaccard
        scopes = scopes or [r[0] for r in self.db.execute("SELECT DISTINCT scope FROM memories")]
        retired = []
        for scope in scopes:
            rows = self.query([scope], statuses=("active", "proposed", "needs_recheck"))
            rows.sort(key=lambda r: r["id"])
            kept: list[dict] = []
            for r in rows:
                twin = next((k for k in kept if k["kind"] == r["kind"] and jaccard(k["text"], r["text"]) >= threshold), None)
                if twin is None:
                    kept.append(r)
                    continue
                self.db.execute(
                    "UPDATE memories SET success_count = success_count + ?, failure_count = failure_count + ?, "
                    "confirm_count = confirm_count + ?, status = 'retired' WHERE id = ?",
                    (r["success_count"], r["failure_count"], r.get("confirm_count") or 0, r["id"]))
                retired.append(r["id"])
        self.db.commit()
        return retired

    def scrub_env_resources(self) -> int:
        """Retire resource rows that stored env values; clear value_or_ref. Returns rows touched."""
        n = 0
        for r in self.query(
            [x[0] for x in self.db.execute("SELECT DISTINCT scope FROM memories")],
            ["resource"], statuses=tuple(STATUSES),
        ):
            if r.get("value_or_ref") or is_env_resource_key(r.get("key")):
                text = (f"{r.get('key') or 'resource'} (secret in env var {r['secret_env']})"
                        if r.get("secret_env") else
                        f"{r.get('key') or 'resource'} (value from run settings each run; not stored)")
                self.db.execute(
                    "UPDATE memories SET value_or_ref = NULL, text = ?, status = CASE WHEN status = 'active' THEN 'retired' ELSE status END WHERE id = ?",
                    (text, r["id"]))
                n += 1
        self.db.commit()
        return n

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
