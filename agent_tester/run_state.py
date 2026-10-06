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
        return [a for a in self.data["action_log"] if a["case_id"] == case_id]

    def checkpoint(self, label: str, status: str | None = None) -> None:
        if status:
            self.data["status"] = status
        self.data["checkpoints"].append({"at": datetime.now().isoformat(timespec="seconds"), "label": label})
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str))
        tmp.replace(self.path)
