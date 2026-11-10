"""Map finish notes that claim failure / nothing found away from a silent success."""
from __future__ import annotations

import re

_NOT_FOUND = re.compile(
    r"(?i)\b("
    r"found\s+none|none\s+found|not\s+found|no\s+match(?:es|ing)?|"
    r"nothing\s+(?:fits|found|usable|exact)|no\s+usable|"
    r"could(?:\s*not|n't)\s+find|unable\s+to\s+find|failed\s+to\s+find|"
    r"no\s+(?:exact\s+)?fit|search(?:ed)?\s+(?:exhausted|failed)|gave\s+up\s+search"
    r")\b"
)
_FAILED = re.compile(
    r"(?i)\b("
    r"could(?:\s*not|n't)\s+(?:complete|finish|continue|do)|"
    r"unable\s+to\s+(?:complete|finish|continue)|"
    r"failed(?:\s+to)?|gave\s+up|blocked:|"
    r"no\s+way\s+to|cannot\s+proceed|can'?t\s+proceed"
    r")\b"
)
_BLOCKED = re.compile(r"(?i)^\s*BLOCKED:")


def classify_finish(notes: str, *, stuck: bool = False, data_prep: bool = False,
                    find_budget: bool = False, not_found_flag: bool = False) -> dict:
    """Return {outcome, stuck, not_found, limit_hit}.

    Outcomes: ok | not_found | blocked | stuck | failed
    A find/prep summary that says nothing was found is not_found (never success).
    A case finish that claims failure becomes stuck so the coordinator does not treat it as done.
    """
    text = (notes or "").strip()
    if _BLOCKED.match(text):
        return {"outcome": "blocked", "stuck": True, "not_found": False, "limit_hit": False}
    if not_found_flag or _NOT_FOUND.search(text):
        if data_prep or find_budget:
            return {"outcome": "not_found", "stuck": False, "not_found": True, "limit_hit": False}
        # Case turn: nothing found / couldn't find → not a success
        return {"outcome": "not_found", "stuck": True, "not_found": True, "limit_hit": False}
    if stuck:
        return {"outcome": "stuck", "stuck": True, "not_found": False, "limit_hit": False}
    if _FAILED.search(text):
        return {"outcome": "failed", "stuck": True, "not_found": False, "limit_hit": False}
    return {"outcome": "ok", "stuck": False, "not_found": False, "limit_hit": False}
