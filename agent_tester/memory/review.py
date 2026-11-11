"""End-of-run retrospective: built from the action log and verdicts by code (no LLM required).

Optionally one short LLM polish call with a tight output cap; candidate lessons are extracted
and deduped against the store in code.
"""
from __future__ import annotations

import logging
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

from .env_guard import scrub_env_details
from .service import jaccard

log = logging.getLogger(__name__)

# Hard cap if an optional LLM polish is used.
REVIEW_MAX_TOKENS = 400
REVIEW_MAX_CHARS = 1600


def _cause(verdict: str, note: str) -> str:
    n = (note or "").lower()
    if verdict == "AGENT INCOMPLETE":
        return "agent_mistake"
    if verdict in ("BLOCKED", "NOT VERIFIED", "SKIPPED"):
        if any(w in n for w in ("login", "captcha", "timeout", "env", "unreachable", "connection")):
            return "env"
        return "env" if verdict == "BLOCKED" else "agent_mistake"
    if verdict == "FAIL":
        if "known bug" in n or "still present" in n:
            return "app_bug"
        if any(w in n for w in ("agent", "did not", "forgot", "incomplete", "stuck", "loop")):
            return "agent_mistake"
        return "app_bug"
    if verdict == "PARTIAL":
        if "not restored" in n:
            return "agent_mistake"
        return "agent_mistake"
    return "ok"


def build_retrospective(state, results: list) -> dict:
    """Code-built review: what failed, why, what to do differently. No LLM."""
    cases = []
    for r in results:
        if r is None:
            continue
        cause = _cause(r.verdict, r.note or "")
        entry = {
            "case_id": r.case_id,
            "verdict": r.verdict,
            "cause": cause,
            "note": scrub_env_details((r.note or "")[:240]),
        }
        if r.verdict not in ("PASS",):
            cases.append(entry)
    # Stuck / loop signals from the action log
    stuck_notes = []
    for a in state.data.get("action_log") or []:
        if a.get("tool") in ("finish",) and (a.get("args") or {}).get("stuck"):
            stuck_notes.append(scrub_env_details(str((a.get("args") or {}).get("why") or a.get("outcome") or "stuck")[:160]))
        out = str(a.get("outcome") or "")
        if "loop" in out.lower() or "maximum repeated" in out.lower():
            stuck_notes.append(scrub_env_details(f"loop on {a.get('tool')}: {out}")[:160])
    stuck_notes = list(dict.fromkeys(stuck_notes))[:6]

    do_differently = []
    for c in cases:
        if c["cause"] == "agent_mistake":
            do_differently.append(f"For {c['case_id']}: avoid repeating the failed approach; re-read the page and try another path.")
        elif c["cause"] == "app_bug":
            do_differently.append(f"For {c['case_id']}: treat as app bug; match known bugs before drafting a new one.")
        elif c["cause"] == "env":
            do_differently.append(f"For {c['case_id']}: confirm env/login/build before retrying.")
    if any("not restored" in (c.get("note") or "").lower() for c in cases):
        do_differently.append("Restore every changed setting and prove it with a re-read.")
    do_differently = list(dict.fromkeys(do_differently))[:8]

    review = {
        "date": date.today().isoformat(),
        "run": state.dir.name,
        "failed": cases,
        "stuck": stuck_notes,
        "do_differently": do_differently,
        "summary": _summary_line(cases, stuck_notes),
    }
    return review


def _summary_line(cases, stuck) -> str:
    if not cases and not stuck:
        return "All decided cases passed; no stuck turns."
    parts = [f"{c['case_id']} {c['verdict']} ({c['cause']})" for c in cases[:5]]
    if stuck:
        parts.append(f"{len(stuck)} stuck/loop note(s)")
    return "; ".join(parts)


def write_retrospective(state, review: dict) -> Path:
    path = state.dir / "retrospective.md"
    lines = [
        f"# Run retrospective — {review.get('run')}",
        "",
        f"Date: {review.get('date')}",
        "",
        f"## Summary",
        review.get("summary") or "",
        "",
        "## Failures",
    ]
    if not review.get("failed"):
        lines.append("- (none)")
    for c in review.get("failed") or []:
        lines.append(f"- **{c['case_id']}** {c['verdict']} [{c['cause']}]: {c.get('note') or ''}")
    lines += ["", "## Stuck / loops"]
    if not review.get("stuck"):
        lines.append("- (none)")
    for s in review.get("stuck") or []:
        lines.append(f"- {s}")
    lines += ["", "## Do differently next time"]
    if not review.get("do_differently"):
        lines.append("- (nothing flagged)")
    for d in review.get("do_differently") or []:
        lines.append(f"- {d}")
    lines.append("")
    path.write_text("\n".join(lines))
    return path


def candidate_lessons(review: dict) -> list[str]:
    """Short lasting-lesson candidates from the review (scrubbed, no env details)."""
    out = []
    for d in review.get("do_differently") or []:
        # Drop case-id prefixes to make lessons reusable
        t = re.sub(r"^For\s+\S+:\s*", "", d).strip()
        t = scrub_env_details(t)
        if len(t) >= 20:
            out.append(t)
    for s in review.get("stuck") or []:
        t = scrub_env_details(f"When stuck or looping: {s}. Pick a route not already tried, or finish stuck.")
        if len(t) >= 20:
            out.append(t)
    # Dedupe among candidates
    uniq = []
    for t in out:
        if any(jaccard(t, u) > 0.8 for u in uniq):
            continue
        uniq.append(t)
    return uniq[:6]


def propose_from_review(memory, review: dict, *, env_fp: str | None = None) -> list[int]:
    """Propose/bump lessons from the retrospective (code dedupe against store)."""
    ids = []
    for text in candidate_lessons(review):
        try:
            ids.append(memory.propose("lesson", text, area=None, evidence=review.get("run"), env_fp=env_fp))
        except Exception:  # noqa: BLE001  env guard or validation — skip that candidate
            log.warning("failed to propose a lesson from the run retrospective", exc_info=True)
            continue
    return ids


def append_run_history(memory_root: Path, state, review: dict, ledger_snapshot: dict) -> Path:
    """Append-only dated history (not lasting facts). Lives under memory/run-history/ (gitignored)."""
    d = Path(memory_root) / "run-history"
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = d / f"{stamp}-{state.dir.name}.md"
    lines = [
        f"# {state.dir.name}",
        f"date: {review.get('date')}",
        f"summary: {review.get('summary')}",
        "",
        "## Records / settings touched",
        json.dumps(ledger_snapshot, indent=2)[:4000],
        "",
        f"## Review summary",
        review.get("summary") or "",
        "",
    ]
    path.write_text("\n".join(lines))
    # Also a one-line index
    index = d / "index.log"
    with index.open("a") as f:
        f.write(f"{review.get('date')}\t{state.dir.name}\t{review.get('summary')}\n")
    return path


def ledger_snapshot(state) -> dict:
    return {
        "ledger": state.data.get("ledger") or [],
        "settings": state.data.get("settings") or {},
        "open": {
            "unrestored": [k for k, v in (state.data.get("settings") or {}).items() if not v.get("verified")],
            "leftovers": [e for e in (state.data.get("ledger") or [])
                          if not e.get("cleaned") and e.get("action") in ("created", "changed")],
        },
    }


async def optional_llm_polish(settings, review: dict) -> str | None:
    """At most one short LLM call to polish the summary. Tight cap. Returns None on skip/failure."""
    if not review.get("failed") and not review.get("stuck"):
        return None
    if os_environ_flag("MEMORY_REVIEW_LLM") is False:
        return None
    try:
        from ..agents.llm import model, run_agent
        from google.adk.agents import LlmAgent
        instr = (
            "Summarize this QA run retrospective in at most 4 short sentences. "
            "Name cause as agent mistake, app bug, or env. No URLs, emails, passwords, or record ids."
        )
        agent = LlmAgent(
            name="memory_review",
            model=model(settings, max_tokens=REVIEW_MAX_TOKENS, job="judge"),
            instruction=lambda _c: instr,
        )
        prompt = json.dumps({
            "summary": review.get("summary"),
            "failed": review.get("failed"),
            "do_differently": review.get("do_differently"),
        })[:REVIEW_MAX_CHARS]
        text = await run_agent(agent, prompt, settings, retry=True)
        return scrub_env_details(str(text or ""))[:800] or None
    except Exception:  # noqa: BLE001
        log.debug("optional retrospective LLM polish failed", exc_info=True)
        return None


def os_environ_flag(name: str) -> bool | None:
    import os
    v = os.environ.get(name)
    if v is None:
        return None
    return v.strip().lower() in ("1", "true", "yes", "on")
