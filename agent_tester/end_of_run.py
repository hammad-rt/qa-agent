"""End-of-run memory work: retrospective, open bugs, proposal approvals, promotion, prune, export."""
from __future__ import annotations

import logging
from pathlib import Path

from .memory import promotion, review
from .memory.store import dump_json
from .config import ROOT

log = logging.getLogger(__name__)


def finish_memory(memory, pack, state, input_fn=None, out=print) -> dict:
    from .auto import is_auto, prompter
    auto = is_auto() and input_fn is None
    input_fn = input_fn or prompter("")
    plan = state.plan
    results = [state.result(c.id) for c in plan.cases]
    summary = {
        "open_bugs_saved": memory.record_open_bugs(results, state.dir.name, plan.task_id),
        "approved": [], "rejected": [], "promoted": [],
        "review_lessons": [], "pruned": {}, "retrospective": None,
    }

    env_fp = None
    try:
        from .coordinator import env_fingerprint
        env_fp = env_fingerprint(plan)
    except Exception:  # noqa: BLE001
        log.debug("end-of-run env fingerprint failed", exc_info=True)

    # Approve agent proposals from the run first (before review adds more candidates).
    pend = memory.pending()
    if pend and auto:
        out(f"\n{len(pend)} memory suggestion(s) left pending for review: agent_tester memory")
    elif pend:
        out("\nThe agents suggested saving these. Approve each (y/N):")
        for r in pend:
            a = input_fn(f"  [{r['id']}] ({r['kind']}) {r['text']}\n   > ").strip().lower()
            (summary["approved"] if a in ("y", "yes") else summary["rejected"]).append(r["id"])
        memory.approve(summary["approved"], env_fp=env_fp)
        memory.reject(summary["rejected"])

    # Code-built retrospective (no LLM). New candidates stay proposed for a later approval pass.
    retro = review.build_retrospective(state, results)
    review.write_retrospective(state, retro)
    snap = review.ledger_snapshot(state)
    review.append_run_history(ROOT / "memory", state, retro, snap)
    state.data["retrospective"] = {
        "summary": retro.get("summary"),
        "failed": len(retro.get("failed") or []),
        "stuck": len(retro.get("stuck") or []),
    }
    summary["retrospective"] = state.data["retrospective"]
    summary["review_lessons"] = review.propose_from_review(memory, retro, env_fp=env_fp)
    if summary["review_lessons"] and auto:
        out(f"{len(summary['review_lessons'])} retrospective lesson(s) left pending for review.")

    words = [pack.slug, pack.config.get("name", "")]
    for c in ([] if auto else promotion.candidates(memory.store, pack.slug, words)):
        out(f"\nThis lesson may apply to every project ({c['why']}):\n  {c['text']}\n  Core version: {c['core_text']}")
        a = input_fn("  Promote to core? (y / N / type a better wording) > ").strip()
        if a.lower() in ("y", "yes"):
            summary["promoted"].append(promotion.promote(memory.store, c["id"], c["core_text"]))
        elif a and a.lower() not in ("n", "no"):
            summary["promoted"].append(promotion.promote(memory.store, c["id"], a))

    summary["pruned"] = memory.prune()
    state.data["learned"] = summary
    # Log injection sizes for this run (optimization evidence).
    inj = state.data.get("memory_injection_log") or []
    if inj:
        state.data["memory_injection_stats"] = {
            "turns": len(inj),
            "max_tokens": max(e.get("tokens", 0) for e in inj),
            "avg_tokens": round(sum(e.get("tokens", 0) for e in inj) / len(inj), 1),
            "cache_hits": sum(1 for e in inj if e.get("cache_hit")),
        }
        out(f"Memory inject: {state.data['memory_injection_stats']}")
    state.save()
    return summary


def export_memory(memory, path) -> None:
    dump_json(memory.store, path)
