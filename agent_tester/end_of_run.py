"""End-of-run memory work: open bugs, proposal approvals, promotion proposals, backup export."""
from __future__ import annotations

from .memory import promotion
from .memory.store import dump_json


def finish_memory(memory, pack, state, input_fn=None, out=print) -> dict:
    from .auto import is_auto, prompter
    auto = is_auto() and input_fn is None
    input_fn = input_fn or prompter("")
    plan = state.plan
    results = [state.result(c.id) for c in plan.cases]
    summary = {"open_bugs_saved": memory.record_open_bugs(results, state.dir.name, plan.task_id), "approved": [], "rejected": [], "promoted": []}

    pend = memory.pending()
    if pend and auto:
        out(f"\n{len(pend)} memory suggestion(s) left pending for review: agent_tester memory")
    elif pend:
        out("\nThe agents suggested saving these. Approve each (y/N):")
        for r in pend:
            a = input_fn(f"  [{r['id']}] ({r['kind']}) {r['text']}\n   > ").strip().lower()
            (summary["approved"] if a in ("y", "yes") else summary["rejected"]).append(r["id"])
        memory.approve(summary["approved"])
        memory.reject(summary["rejected"])

    words = [pack.slug, pack.config.get("name", "")]
    for c in ([] if auto else promotion.candidates(memory.store, pack.slug, words)):
        out(f"\nThis lesson may apply to every project ({c['why']}):\n  {c['text']}\n  Core version: {c['core_text']}")
        a = input_fn("  Promote to core? (y / N / type a better wording) > ").strip()
        if a.lower() in ("y", "yes"):
            summary["promoted"].append(promotion.promote(memory.store, c["id"], c["core_text"]))
        elif a and a.lower() not in ("n", "no"):
            summary["promoted"].append(promotion.promote(memory.store, c["id"], a))
    state.data["learned"] = summary
    state.save()
    return summary


def export_memory(memory, path) -> None:
    dump_json(memory.store, path)
