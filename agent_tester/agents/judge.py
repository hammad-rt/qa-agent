"""Judge (LLM). Decides from the brief, the code-built action log, values and evidence names, plus
known bugs. Never sees the tester's reasoning. Output is validated; a few rules are enforced in code."""
from __future__ import annotations

import asyncio
import json

from ..config import Settings
from ..models import CaseResult
from . import prompts
from .llm import model, run_json


def enforce_rules(r: CaseResult, brief: dict, actions: list[dict], step_results: list,
                  unrestored: list[dict] | None = None) -> CaseResult:
    """Fixed checks on top of the judge's call."""
    shots = [s for sr in step_results for s in sr.screenshots]
    r.evidence = r.evidence or shots
    if r.verdict == "PASS" and not shots:
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

    def page_text(state, step_results, per=2500, most=8) -> list[dict]:
        """What the page showed right after each step (snapshots taken by code, not the tester's words):
        form fields, messages and headings only, so the judge can read typed/saved values itself."""
        import re as _re
        keep = _re.compile(r"textbox|spinbutton|combobox|checkbox|radio|alert|status|error|invalid|heading|"
                           r"dialog|toast|message|required|value|saved|success", _re.I)
        out = []
        for sr in step_results:
            for name in (getattr(sr, "snapshots", None) or [])[-most:]:
                p = state.dir / "snapshots" / name
                try:
                    lines = [_re.sub(r"\s*\[ref=[^\]]+\]", "", l).rstrip() for l in p.read_text().splitlines()]
                except OSError:
                    continue
                txt = "\n".join(l for l in lines if keep.search(l))[:per]
                if txt:
                    out.append({"after": name.rsplit(".", 1)[0], "page": txt})
        return out[-most:]

    def judge(brief: dict, actions: list[dict], step_results: list, state) -> CaseResult:
        from google.adk.agents import LlmAgent
        agent = LlmAgent(name="judge", model=model(settings, json_mode=True, job="judge"), instruction=lambda _c: instr)
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
            "page_text_at_steps": page_text(state, step_results),
        }
        def to_result(data: dict) -> CaseResult:
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            data["case_id"] = brief["case_id"]
            data["steps_done"] = [str(n) for n in payload["steps_done"]]
            # small type slips from the model must never cost a verdict: coerce instead of rejecting
            for k in ("evidence", "bug_draft_ids"):
                if k in data:
                    v = data[k] if isinstance(data[k], list) else [data[k]]
                    data[k] = [str(x) for x in v if x is not None]
            if "lessons_applied" in data:
                v = data["lessons_applied"] if isinstance(data["lessons_applied"], list) else [data["lessons_applied"]]
                ids = [str(x).strip().lstrip("#") for x in v]
                data["lessons_applied"] = [int(x) for x in ids if x.isdigit()]
            for k in ("actual", "note", "expected", "bug_title"):
                if k in data and data[k] is not None and not isinstance(data[k], str):
                    data[k] = json.dumps(data[k]) if isinstance(data[k], (dict, list)) else str(data[k])
            if isinstance(data.get("values"), dict):
                data["values"] = {str(k): (v if isinstance(v, (int, float, str)) and not isinstance(v, bool) else
                                           json.dumps(v) if isinstance(v, (dict, list)) else str(v))
                                  for k, v in data["values"].items()}
            elif "values" in data:
                data["values"] = {}
            if isinstance(data.get("severity"), str):
                data["severity"] = data["severity"].strip().capitalize() or None
                if data["severity"] not in ("Critical", "High", "Medium", "Low"):
                    data["severity"] = None
            kb = data.get("known_bug")
            if kb is not None and not isinstance(kb, int):
                kb = str(kb).strip().lstrip("#")
                data["known_bug"] = int(kb) if kb.isdigit() else None
            if isinstance(data.get("verdict"), str):
                data["verdict"] = data["verdict"].strip().upper().replace("_", " ")
            data.setdefault("expected", brief.get("expected", ""))
            return CaseResult.model_validate(data)

        r = asyncio.run(run_json(agent, json.dumps(payload, indent=1, default=str), settings, validate=to_result,
                                 label=f"judge_{brief['case_id']}", raw_dir=state.dir, trace_path=state.dir / "trace.jsonl"))
        return enforce_rules(r, brief, actions, step_results, unrestored)

    return judge
