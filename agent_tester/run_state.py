"""Run folder, state.json checkpoints and the code-built action log. Lets a run resume."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .models import ActionLogEntry, CaseResult, TestPlan


class RunState:
    def __init__(self, run_dir: Path):
        self.dir = run_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = run_dir / "state.json"
        self.data: dict = json.loads(self.path.read_text()) if self.path.exists() else {
            "status": "new", "plan": None, "approved": False, "preapproved": [],
            "results": {}, "values": {}, "action_log": [], "checkpoints": [], "notes": [],
        }

    @classmethod
    def new(cls, runs_dir: Path, task_id: int) -> "RunState":
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return cls(runs_dir / f"{task_id}-{stamp}")

    # plan
    def set_plan(self, plan: TestPlan) -> None:
        self.data["plan"] = plan.model_dump(mode="json")
        self.save()

    @property
    def plan(self) -> TestPlan | None:
        return TestPlan.model_validate(self.data["plan"]) if self.data.get("plan") else None

    # results
    def set_result(self, r: CaseResult) -> None:
        self.data["results"][r.case_id] = r.model_dump(mode="json")
        self.checkpoint(f"case {r.case_id} done: {r.verdict}")

    def result(self, case_id: str) -> CaseResult | None:
        d = self.data["results"].get(case_id)
        return CaseResult.model_validate(d) if d else None

    @property
    def values(self) -> dict:
        return self.data["values"]

    # action log
    def log_action(self, **kw) -> ActionLogEntry:
        e = ActionLogEntry(seq=len(self.data["action_log"]) + 1, **kw)
        self.data["action_log"].append(e.model_dump(mode="json"))
        self.save()
        return e

    def actions_for(self, case_id: str) -> list[dict]:
        floor = (self.data.get("attempt_floor") or {}).get(case_id, 0)   # a rerun in this run starts fresh
        return [a for a in self.data["action_log"] if a["case_id"] == case_id and a.get("seq", 0) > floor]

    def reopen_cases(self, case_ids: list[str]) -> None:
        """Rerun cases inside this same run: their old results move to previous_attempts, their old actions stop
        counting, and per-case counters reset. Passed cases stay as they are and are skipped."""
        prev = self.data.setdefault("previous_attempts", {})
        floors = self.data.setdefault("attempt_floor", {})
        for cid in case_ids:
            old = self.data["results"].pop(cid, None)
            if old:
                prev.setdefault(cid, []).append(old)
            floors[cid] = len(self.data["action_log"])
            for key in ("entered_values", "save_clicks"):
                (self.data.get(key) or {}).pop(cid, None)
        self.data["status"] = "running"
        self.checkpoint(f"rerunning {', '.join(case_ids)} in this run")

    def checkpoint(self, label: str, status: str | None = None) -> None:
        if status:
            self.data["status"] = status
        self.data["checkpoints"].append({"at": datetime.now().isoformat(timespec="seconds"), "label": label})
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        from .redact import scrub_secrets
        tmp.write_text(scrub_secrets(json.dumps(self.data, indent=2, default=str)))
        tmp.replace(self.path)
