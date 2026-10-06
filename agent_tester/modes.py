"""Run modes (FR-24): full run, rerun selected cases, retest cases linked to a fixed bug, plan-only dry run."""
from __future__ import annotations

import json
from pathlib import Path

from .models import TestPlan


def parse_cases(text: str | None) -> list[str]:
    return [c.strip() for c in (text or "").replace(" ", ",").split(",") if c.strip()]


def filter_plan(plan: TestPlan, cases: list[str]) -> TestPlan:
    """Keep only the chosen cases plus the cases they depend on (so dependencies still run first)."""
    if not cases:
        return plan
    by_id = {c.id: c for c in plan.cases}
    unknown = [c for c in cases if c not in by_id]
    if unknown:
        raise ValueError(f"not in the plan: {', '.join(unknown)}")
    keep, todo = set(), list(cases)
    while todo:
        cid = todo.pop()
        if cid in keep:
            continue
        keep.add(cid)
        todo += [d for d in by_id[cid].depends_on if d in by_id]
    return plan.model_copy(update={"cases": [c for c in plan.cases if c.id in keep]})


def past_runs(runs_dir: Path, task_id: int) -> list[Path]:
    return sorted((p for p in runs_dir.glob(f"{task_id}-*") if (p / "state.json").exists()), reverse=True)


def cases_for_bug(runs_dir: Path, task_id: int, bug: int) -> tuple[list[str], Path | None]:
    """Cases linked to a bug in the latest run of this task that saw it: a known bug number from facts.md
    (result.known_bug) or a Redmine issue posted from a bug draft (bug_drafts.json posted_issue)."""
    for run in past_runs(runs_dir, task_id):
        state = json.loads((run / "state.json").read_text())
        hit = [cid for cid, r in state.get("results", {}).items() if r.get("known_bug") == bug]
        drafts = run / "bug_drafts.json"
        if drafts.exists():
            for d in json.loads(drafts.read_text()).get("drafts", []):
                if d.get("posted_issue") == bug:
                    hit += [c for c in d["linked_cases"] if c not in hit]
        if hit:
            return hit, run
    return [], None


def latest_plan(runs_dir: Path, task_id: int) -> tuple[TestPlan | None, Path | None]:
    """The most recent approved plan for this task, so a rerun skips planning."""
    for run in past_runs(runs_dir, task_id):
        state = json.loads((run / "state.json").read_text())
        if state.get("plan") and state.get("approved"):
            return TestPlan.model_validate(state["plan"]), run
    return None, None
