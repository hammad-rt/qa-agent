"""Reuse an approved plan/draft when ticket text + selected cases + pack version are unchanged."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def pack_version_hash(pack) -> str:
    """Hash of pack.yaml + facts.md (content only — no env identity)."""
    root = getattr(pack, "root", None)
    parts = []
    if root:
        for name in ("pack.yaml", "facts.md"):
            p = Path(root) / name
            if p.is_file():
                parts.append(p.read_bytes())
    cfg = getattr(pack, "config", None) or {}
    parts.append(json.dumps({"slug": cfg.get("slug"), "name": cfg.get("name"),
                             "pre": cfg.get("preapprovable_actions"),
                             "data_prep_ids": [r.get("id") for r in (cfg.get("data_prep") or [])]},
                            sort_keys=True).encode())
    return hashlib.sha1(b"\n".join(parts)).hexdigest()[:16]


def plan_fingerprint(task_text: str, case_ids: list[str] | None, pack) -> str:
    ids = ",".join(sorted(case_ids or []))
    raw = f"{_norm(task_text)[:20000]}|{ids}|{pack_version_hash(pack)}"
    return hashlib.sha1(raw.encode()).hexdigest()


def load_cached_plan(run_dir: Path, fingerprint: str):
    """Return TestPlan if runs/<dir>/plan.cache.json matches fingerprint."""
    from .models import TestPlan
    path = Path(run_dir) / "plan.cache.json"
    if not path.is_file():
        # Also check sibling latest under parent? Keep scoped to this run + explicit reuse file.
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if data.get("fingerprint") != fingerprint:
        return None
    plan_data = data.get("plan")
    if not isinstance(plan_data, dict):
        return None
    try:
        return TestPlan.model_validate(plan_data)
    except Exception:  # noqa: BLE001
        return None


def find_reusable_plan(runs_dir: Path, fingerprint: str, *, exclude: Path | None = None):
    """Scan recent run folders for a matching approved plan cache."""
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return None
    dirs = sorted([p for p in runs_dir.iterdir() if p.is_dir()],
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for d in dirs[:30]:
        if exclude and d.resolve() == Path(exclude).resolve():
            continue
        plan = load_cached_plan(d, fingerprint)
        if plan is not None:
            return plan, d
    return None


def save_plan_cache(run_dir: Path, fingerprint: str, plan) -> Path:
    path = Path(run_dir)
    path.mkdir(parents=True, exist_ok=True)
    out = path / "plan.cache.json"
    out.write_text(json.dumps({
        "fingerprint": fingerprint,
        "plan": plan.model_dump(mode="json"),
    }, indent=1))
    return out
