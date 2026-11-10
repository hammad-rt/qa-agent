"""Per-machine run preferences (last-used account, etc.). Not lasting memory; gitignored.

Env values (logins, URLs, record ids) live here or in pack.yaml / .env for the current run —
never in SQLite lasting rows or learned.md.
"""
from __future__ import annotations

import json
from pathlib import Path

import os

from ..config import ROOT

DEFAULT_PATH = ROOT / "memory" / "run-prefs.json"


def default_prefs_path() -> Path:
    return Path(os.environ.get("MEMORY_PREFS_PATH") or DEFAULT_PATH)


class RunPrefs:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or default_prefs_path())
        self.data: dict = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, scope: str, key: str) -> str | None:
        row = (self.data.get(scope) or {}).get(key)
        if isinstance(row, dict):
            return row.get("value")
        return row if isinstance(row, str) else None

    def put(self, scope: str, key: str, value: str) -> None:
        self.data.setdefault(scope, {})[key] = {"value": value}
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2) + "\n")
