"""Judge (LLM). Decides from the brief, the code-built action log, values and evidence names, plus
known bugs. Never sees the tester's reasoning. Output is validated; a few rules are enforced in code."""
from __future__ import annotations

import asyncio
import json

from ..config import Settings
from ..models import CaseResult
from . import prompts
from .llm import extract_json, model, run_agent


def enforce_rules(r: CaseResult, brief: dict, actions: list[dict], step_results: list,
                  unrestored: list[dict] | None = None) -> CaseResult:
    """Fixed checks on top of the judge's call."""
    shots = [s for sr in step_results for s in sr.screenshots]
    r.evidence = r.evidence or shots
    if r.verdict == "PASS" and brief.get("evidence_required") and not shots:
        r.verdict, r.note = "NOT VERIFIED", ((r.note or "") + " Required evidence missing.").strip()
    if r.verdict == "FAIL" and not (r.actual or r.values):
        r.verdict, r.note = "NOT VERIFIED", ((r.note or "") + " FAIL without expected vs actual.").strip()
    devs = [a for a in actions if a.get("deviation") or a.get("inferred")]
    if devs and "deviation" not in (r.note or "").lower() and "inferred" not in (r.note or "").lower():
        r.note = ((r.note or "") + f" {len(devs)} deviation/inferred step(s) in the action log.").strip()
    if unrestored:
        names = ", ".join(f"{u['app']}:{u['setting']} (was {u['original']})" for u in unrestored)
        if r.verdict == "PASS":
            r.verdict = "PARTIAL"
        r.note = ((r.note or "") + f" Setting not restored and verified: {names}.").strip()
    return r


def make_judge(settings: Settings, pack, memory=None):
    bugs = "\n".join(f"{b['n']}. [{b['severity']}] {b['text']}" for b in pack.known_bugs) or "(none)"
    instr = prompts.JUDGE.format(bugs=bugs) + "\n\nProject facts:\n" + pack.facts

    def judge(brief: dict, actions: list[dict], step_results: list, state) -> CaseResult:
        from google.adk.agents import LlmAgent
        agent = LlmAgent(name="judge", model=model(settings), instruction=lambda _c: instr)
        from ..ledger import Ledger
        unrestored = Ledger(state).unrestored(brief["case_id"])
        failure_text = brief.get("title", "") + " " + brief.get("expected", "") + " " + json.dumps(
            {k: v for sr in step_results for k, v in sr.values.items()})
        payload = {
            "lessons": memory.preload([brief["area"]] if brief.get("area") else None) if memory else [],
            "known_bug_candidates": memory.known_bug_candidates(failure_text, pack.known_bugs) if memory else [],
            "unrestored_settings": unrestored,
            "brief": brief, "action_log": actions,
            "values": {k: v for sr in step_results for k, v in sr.values.items()},
            "evidence": [s for sr in step_results for s in sr.screenshots],
            "steps_done": sorted({n for sr in step_results for n in sr.steps_done}),
        }
        raw = asyncio.run(run_agent(agent, json.dumps(payload, indent=1, default=str), settings,
                                    trace_path=state.dir / "trace.jsonl", retry=True))
        data = extract_json(raw)
        data["case_id"] = brief["case_id"]
        data.setdefault("expected", brief.get("expected", ""))
        return enforce_rules(CaseResult.model_validate(data), brief, actions, step_results, unrestored)

    return judge
