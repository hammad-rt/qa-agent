"""Evidence file naming: <case>-<nn>-<what>.png plus a masked page-text snapshot .txt."""
from __future__ import annotations

import re
from pathlib import Path

from ..redact import redact


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "shot"


class Evidence:
    def __init__(self, run_dir: Path):
        self.shots = run_dir / "shots"
        self.snaps = run_dir / "snapshots"
        self.shots.mkdir(parents=True, exist_ok=True)
        self.snaps.mkdir(parents=True, exist_ok=True)
        self._n: dict[str, int] = {}

    def next_name(self, case_id: str, what: str) -> str:
        n = self._n.get(case_id, 0) + 1
        self._n[case_id] = n
        return f"{slug(case_id)}-{n:02d}-{slug(what)}"

    def paths(self, case_id: str, what: str) -> tuple[Path, Path]:
        base = self.next_name(case_id, what)
        return self.shots / f"{base}.png", self.snaps / f"{base}.txt"

    def save_snapshot(self, path: Path, page_text: str, keep_emails: tuple[str, ...] = ()) -> Path:
        path.write_text(redact(page_text, keep_emails=keep_emails))
        return path
