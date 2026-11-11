"""Detect 'redo these cases' notes from the web UI guidance feed and re-queue them.

Deterministic (no LLM): multilingual-tolerant redo regex + case ids from _guide_targets.
Each guidance note is acted on at most once. Undelivered notes are listed at run end.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# redo / rerun / retest / again / encore / nochmal / de nuevo / di nuovo / opnieuw / ...
_REDO_INTENT = re.compile(
    r"(?i)\b("
    r"re\s*-?\s*do|redo|re\s*-?\s*run|rerun|re\s*-?\s*test|retest|"
    r"again|once\s+more|one\s+more\s+time|"
    r"encore|nochmal|erneut|de\s+nuevo|otra\s+vez|di\s+nuovo|opnieuw|weer|"
    r"reessayer|r[eé]essayer|recommence|"
    r"from\s+the\s+start|start\s+over"
    r")\b"
)


def _rows(run_dir: Path) -> list[dict]:
    f = run_dir / "guidance.jsonl"
    if not f.exists():
        return []
    try:
        return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


def guide_targets(text: str, plan) -> set[str]:
    """Case ids named in the note (same idea as tester._guide_targets)."""
    ids = [c.id for c in (plan.cases if plan else [])]
    return {i for i in ids if re.search(rf"(?<![A-Za-z0-9]){re.escape(i)}(?![A-Za-z0-9])", text or "", re.I)}


def is_redo_intent(text: str) -> bool:
    return bool(_REDO_INTENT.search(text or ""))


def finished_ids(state) -> set[str]:
    return set((state.data.get("results") or {}).keys())


def dependents(plan, roots: list[str]) -> list[str]:
    """roots plus every case that (transitively) depends_on them, in a stable order."""
    by_id = {c.id: c for c in plan.cases}
    want = set(roots)
    changed = True
    while changed:
        changed = False
        for c in plan.cases:
            if c.id in want:
                continue
            if any(d in want for d in (c.depends_on or [])):
                want.add(c.id)
                changed = True
    # Preserve plan order among selected.
    return [c.id for c in plan.cases if c.id in want]


def mutating_tokens_for_cases(plan, case_ids: list[str], pack_cfg: dict | None) -> list[str]:
    """Placeholder tokens used by these cases that the pack marks mutating (need fresh data on redo)."""
    from .placeholders import tokens_in_case, item_by_token, token_pattern
    pat = token_pattern(pack_cfg)
    by_id = {c.id: c for c in plan.cases}
    out, seen = [], set()
    for cid in case_ids:
        c = by_id.get(cid)
        if not c:
            continue
        for tok in tokens_in_case(c, pat, pack_cfg=pack_cfg):
            item = item_by_token(pack_cfg, tok)
            if item is None:
                continue
            if item.get("mutating", True) is False:
                continue  # read-only redo keeps the same data
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def pending_redo(state, plan) -> dict | None:
    """If a new guidance note asks to redo finished cases, return {note_idx, text, cases}.

    Ambiguous notes that name only finished cases (no explicit redo verb) count as redo.
    Notes already consumed (state.data['guidance_redo_done']) are skipped.
    """
    rows = _rows(state.dir)
    done = set(state.data.get("guidance_redo_done") or [])
    finished = finished_ids(state)
    for i, row in enumerate(rows):
        if i in done:
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        targets = {t for t in guide_targets(text, plan)}
        if not targets:
            continue
        finished_targets = sorted(t for t in targets if t in finished)
        if not finished_targets:
            continue  # still waiting for those cases to run (held by _guide_fits)
        # Explicit redo verb, OR the note names only cases that already finished.
        if is_redo_intent(text) or set(t.upper() for t in targets) <= set(t.upper() for t in finished):
            return {"note_idx": i, "text": text, "cases": finished_targets, "all_targets": sorted(targets)}
    return None


def apply_redo(state, plan, pack_cfg: dict | None = None) -> list[str] | None:
    """Consume one pending redo note: reopen cases (+ dependents), invalidate mutating prep, mark note done.

    Returns the case ids to insert next-after-current, or None when nothing to do.
    """
    hit = pending_redo(state, plan)
    if hit is None:
        return None
    roots = hit["cases"]
    queue = dependents(plan, roots)
    # Fresh data for mutating placeholders used by the reopened set.
    from .placeholders import invalidate_for_redo
    toks = mutating_tokens_for_cases(plan, queue, pack_cfg)
    if toks:
        invalidate_for_redo(state, toks)
    state.reopen_cases(queue)
    done = state.data.setdefault("guidance_redo_done", [])
    done.append(hit["note_idx"])
    # Attach the note text so the redo sessions see it as GUIDANCE.
    state.data.setdefault("case_guidance", {})
    for cid in queue:
        state.data["case_guidance"].setdefault(cid, []).append(hit["text"])
    state.data.setdefault("redos", []).append({
        "note_idx": hit["note_idx"], "text": hit["text"], "cases": queue,
    })
    state.checkpoint(f"Redo requested by guidance: {', '.join(queue)} queued")
    state.data.setdefault("notes", []).append(
        f"Redo requested by guidance: {', '.join(queue)} queued ({hit['text'][:120]})")
    # progress.log line (best-effort)
    try:
        with (state.dir / "progress.log").open("a") as f:
            from datetime import datetime
            f.write(f"{datetime.now().isoformat(timespec='seconds')} "
                    f"Redo requested by guidance: {', '.join(queue)} queued\n")
    except OSError:
        pass
    state.save()
    return queue


def undelivered_guidance(state, plan) -> list[dict]:
    """Notes that named cases which never ran again (or never matched a live session)."""
    rows = _rows(state.dir)
    redo_done = set(state.data.get("guidance_redo_done") or [])
    # Notes delivered into a tester session are tracked loosely via action_log tool=guidance.
    delivered_texts = set()
    for a in state.data.get("action_log") or []:
        if a.get("tool") == "guidance":
            delivered_texts.add(str((a.get("outcome") or ""))[:80])
    out = []
    finished = finished_ids(state)
    for i, row in enumerate(rows):
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        targets = guide_targets(text, plan)
        if i in redo_done:
            continue
        if targets and targets <= finished and not is_redo_intent(text):
            # Named finished cases but we never treated it as redo and no session claimed it.
            out.append({"note_idx": i, "text": text, "targets": sorted(targets),
                        "why": "named finished cases but was never delivered or redone"})
        elif targets and not (targets & finished) and not any(
                state.result(t) is None for t in targets):
            out.append({"note_idx": i, "text": text, "targets": sorted(targets),
                        "why": "targets never ran"})
    return out


def report_undelivered(state, plan) -> None:
    left = undelivered_guidance(state, plan)
    if not left:
        return
    lines = [f"Undelivered guidance ({len(left)}):"]
    for u in left:
        lines.append(f"  - [{', '.join(u['targets']) or '?'}] {u['text'][:160]} ({u['why']})")
    msg = "\n".join(lines)
    state.data.setdefault("notes", []).append(msg)
    try:
        with (state.dir / "progress.log").open("a") as f:
            from datetime import datetime
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass
    state.checkpoint(f"undelivered guidance: {len(left)} note(s)")
    state.save()
