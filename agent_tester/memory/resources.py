"""Phase 1 resource memory: confirmed answers to gaps (URLs, account names, data facts).

Stored as JSON now; phase 2 moves these rows into the SQLite memory with the same fields.
Secrets are never stored, only the env var that holds them.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class ResourceMemory:
    def __init__(self, path: Path):
        self.path = path
        self.rows: list[dict] = json.loads(path.read_text()) if path.exists() else []

    def get(self, scope: str, key: str) -> dict | None:
        for r in reversed(self.rows):
            if r["scope"] == scope and r["key"] == key and not r.get("stale"):
                return r
        return None

    def put(self, scope: str, key: str, value: str, source: str, *, secret_env: str | None = None) -> dict:
        for r in self.rows:
            if r["scope"] == scope and r["key"] == key:
                r["stale"] = True
        row = {"scope": scope, "key": key, "value": value, "secret_env": secret_env, "source": source,
               "kind": "resource", "at": datetime.now(timezone.utc).isoformat()}
        self.rows.append(row)
        self.save()
        return row

    def mark_stale(self, scope: str, key: str) -> None:
        for r in self.rows:
            if r["scope"] == scope and r["key"] == key:
                r["stale"] = True
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.rows, indent=2))
