"""Conservative parallel case execution (default OFF).

Only cases whose every step is read-only AND that don't touch shared session state may
run concurrently. Many "non-mutating" rewards cases still create/clear Applied rewards
rows — those must NOT run together. Enable with PARALLEL_READONLY=1.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict

# Steps that apply/remove/create rewards or touch the portal rewards session.
_REWARDS_SESSION = re.compile(
    r"(?i)\b(apply|remove|redeem|create|clear|use)\b.{0,40}\b(reward|savo rewards|applied)\b|"
    r"\brewards?\s+(box|session|wallet)\b|pay\s+by\s+card|stripe|4242")
_MUTATE = re.compile(
    r"(?i)\b(create|update|save|delete|edit|pay|apply|remove|move|ship|invoice|restore|"
    r"change|submit|confirm|type|enter|paste)\b")


def parallel_enabled() -> bool:
    return os.environ.get("PARALLEL_READONLY", "").strip().lower() in ("1", "true", "yes")


def max_parallel() -> int:
    try:
        return max(1, min(3, int(os.environ.get("PARALLEL_READONLY_MAX", "2"))))
    except ValueError:
        return 2


def pack_safe_ids(pack_cfg: dict | None) -> set[str]:
    cfg = pack_cfg or {}
    raw = cfg.get("parallel_safe_cases") or cfg.get("parallel_safe") or []
    if isinstance(raw, dict):
        raw = [k for k, v in raw.items() if v]
    return {str(x).strip() for x in raw if str(x).strip()}


def case_is_parallel_safe(case, pack_cfg: dict | None = None) -> bool:
    """Eligible only if pack marks parallel_safe OR (no mutating/rewards-session steps)."""
    safe = pack_safe_ids(pack_cfg)
    # Explicit mark on case object or pack list.
    if getattr(case, "parallel_safe", None) is True:
        return True
    if case.id in safe or case.id.split(".")[-1] in safe:
        return True
    # Heuristic fallback (still conservative).
    if any(getattr(s, "changes_data", False) or getattr(s, "costs_money", False) for s in case.steps):
        return False
    blob = " ".join([case.title or "", case.expected or ""] + [s.text for s in case.steps])
    if _REWARDS_SESSION.search(blob):
        return False
    if _MUTATE.search(blob):
        return False
    return True


def shared_placeholders(case, pack_cfg: dict | None = None) -> set[str]:
    """Order/session placeholder tokens this case touches (ORDER-*, CUSTOMER, Wallet, …)."""
    pat = ((pack_cfg or {}).get("placeholders") or {}).get("token_pattern")
    rx = re.compile(pat) if pat else re.compile(
        r"(?i)\b(?:ORDER-[A-Z][A-Z0-9_-]*|CUSTOMER|Wallet|test_order|order_\w+)\b")
    blob = " ".join([case.title or "", " ".join(case.preconditions or []), case.expected or ""]
                    + [s.text for s in case.steps])
    return {m.group(0).upper() if m.group(0).upper().startswith("ORDER") else m.group(0)
            for m in rx.finditer(blob)}


def partition_parallel_batches(cases: list, pack_cfg: dict | None = None) -> list[list]:
    """Group consecutive parallel-safe cases that don't share placeholders; others alone.

    Never put two cases that share an order/session placeholder in the same batch unless
    each has its own data (we approximate: shared token => not together).
    """
    if not parallel_enabled():
        return [[c] for c in cases]
    limit = max_parallel()
    batches: list[list] = []
    open_batch: list = []
    used_tokens: set[str] = set()

    def flush():
        nonlocal open_batch, used_tokens
        if open_batch:
            batches.append(open_batch)
        open_batch, used_tokens = [], set()

    for c in cases:
        if not case_is_parallel_safe(c, pack_cfg):
            flush()
            batches.append([c])
            continue
        toks = {t.upper() for t in shared_placeholders(c, pack_cfg)}
        if toks & {t.upper() for t in used_tokens} or len(open_batch) >= limit:
            flush()
        open_batch.append(c)
        used_tokens |= toks
    flush()
    return batches


def group_ids(plan_cases: list, order: list[str], pack_cfg: dict | None = None) -> list[list[str]]:
    by_id = {c.id: c for c in plan_cases}
    cases = [by_id[i] for i in order if i in by_id]
    return [[c.id for c in batch] for batch in partition_parallel_batches(cases, pack_cfg)]
