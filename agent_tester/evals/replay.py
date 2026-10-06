"""Replay self-test (tech spec section 11 and phase 5): feed each recorded case's expected and actual text to the
judge and compare its verdicts with the known answers. Pass bar: at least 90% verdict agreement, no known FAIL
judged PASS, and repeat known bugs labelled as still present with the right bug number."""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..models import StepResult
from ..run_state import RunState

SCORED = {"PASS", "FAIL", "PARTIAL"}          # NOT VERIFIED / SKIPPED cases have nothing for the judge to read


def load_known_answers(pack) -> dict:
    p = pack.root / "evals" / "known_answers.json"
    if not p.exists():
        raise FileNotFoundError(f"{p} missing: run `eval --project {pack.slug} --rebuild` first")
    return json.loads(p.read_text())


def brief_for(case: dict) -> dict:
    return {"case_id": case["id"], "title": case["title"], "expected": case["expected"], "area": None,
            "steps": [{"n": 1, "text": case["steps"] or case["title"], "inferred": False}],
            "replay": True}


def inputs_for(case: dict) -> tuple[list[dict], list[StepResult]]:
    actions = [{"seq": 1, "case_id": case["id"], "app": "replay", "tool": "recorded_observation",
                "args": {}, "plan_step": 1, "inferred": False, "approval": "not_needed", "outcome": "ok"}]
    sr = StepResult(case_id=case["id"], app="replay", values={"observed": case["actual"]},
                    screenshots=[f"recorded/{f}" for f in case.get("figures", [])], steps_done=[1],
                    notes="Recorded result from a past report; no live page.")
    return actions, [sr]


@dataclass
class ReplayScore:
    total: int = 0
    agree: int = 0
    rows: list[dict] = field(default_factory=list)
    fail_to_pass: list[str] = field(default_factory=list)
    bug_mislabels: list[str] = field(default_factory=list)

    @property
    def agreement(self) -> float:
        return self.agree / self.total if self.total else 0.0

    @property
    def passed(self) -> bool:
        return self.agreement >= 0.9 and not self.fail_to_pass and not self.bug_mislabels

    def summary(self) -> str:
        lines = [f"Verdict agreement: {self.agree}/{self.total} ({self.agreement:.0%}); bar is 90%.",
                 f"Known FAIL judged PASS: {', '.join(self.fail_to_pass) or 'none'}.",
                 f"Known bug not labelled still present: {', '.join(self.bug_mislabels) or 'none'}.",
                 "Result: " + ("PASSED" if self.passed else "NOT PASSED")]
        return "\n".join(lines)


def replay(cases: list[dict], judge, work_dir: Path | None = None, only: list[str] | None = None) -> ReplayScore:
    work_dir = work_dir or Path(tempfile.mkdtemp(prefix="replay-"))
    state = RunState.new(work_dir, task_id=0)
    state.data["mode"] = {"replay": True}
    score = ReplayScore()
    for case in cases:
        if case["verdict"] not in SCORED or (only and case["id"] not in only):
            continue
        actions, srs = inputs_for(case)
        try:
            got = judge(brief_for(case), actions, srs, state)
            verdict, bug, note = got.verdict, got.known_bug, got.note
        except Exception as e:  # a crash counts as disagreement, never as a pass
            verdict, bug, note = "AGENT INCOMPLETE", None, f"judge error: {e}"
        ok = verdict == case["verdict"]
        score.total += 1
        score.agree += ok
        if case["verdict"] == "FAIL" and verdict == "PASS":
            score.fail_to_pass.append(case["key"])
        if case["known_bugs"] and verdict in ("FAIL", "PARTIAL") and bug not in case["known_bugs"]:
            score.bug_mislabels.append(f"{case['key']} (expected bug {'/'.join(map(str, case['known_bugs']))}, got {bug})")
        score.rows.append({"key": case["key"], "expected": case["verdict"], "got": verdict, "agree": ok,
                           "known_bugs": case["known_bugs"], "got_bug": bug, "note": note})
    (work_dir / "replay_score.json").write_text(json.dumps(
        {"agreement": score.agreement, "passed": score.passed, "fail_to_pass": score.fail_to_pass,
         "bug_mislabels": score.bug_mislabels, "rows": score.rows}, indent=2, default=str))
    return score


def oracle_judge(cases: list[dict]):
    """Returns the known answers: checks the replay plumbing and the scoring without an LLM."""
    from ..models import CaseResult
    by_id = {c["id"]: c for c in cases}

    def judge(brief, actions, srs, state):
        c = by_id[brief["case_id"]]
        return CaseResult(case_id=c["id"], verdict=c["verdict"], known_bug=(c["known_bugs"] or [None])[0],
                          expected=c["expected"], actual=c["actual"])
    return judge
