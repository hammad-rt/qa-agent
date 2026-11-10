"""Stamp PlannedCase markers from the cases-file / task markdown (not the LLM title)."""
from __future__ import annotations

import re

from .models import TestPlan

# Case id headings: ### C1. Title  / ## TC3: ...
_CASE_HEAD = re.compile(
    r"^(#{2,4})\s+([A-Z]{1,3}\d+(?:-[A-Za-z0-9]+)?)(?=[.:)\s(]|$)\s*(.*)$",
    re.M,
)
# Non-case section headings (## Final checks (read-only))
_SECTION_HEAD = re.compile(r"^(#{2,4})\s+(.+?)\s*$", re.M)
_RO_MARK = re.compile(r"(?i)read[\s-]*only|final checks?")


def source_case_markers(text: str) -> dict[str, dict]:
    """Per case id: source_section heading text and read_only from that section / case line.

    Real test-cases.md shape::

        ## Final checks (read-only)

        ### C1. Portal and CRM wallet parity
        ...
    """
    text = text or ""
    # Collect section headings that are NOT themselves case ids, with positions.
    case_spans = [(m.start(), m.group(2)) for m in _CASE_HEAD.finditer(text)]
    case_starts = {m.start() for m in _CASE_HEAD.finditer(text)}
    sections: list[tuple[int, str]] = []
    for m in _SECTION_HEAD.finditer(text):
        if m.start() in case_starts:
            continue
        sections.append((m.start(), m.group(2).strip()))

    out: dict[str, dict] = {}
    for m in _CASE_HEAD.finditer(text):
        cid = m.group(2)
        case_line = (m.group(0) or "").strip()
        # Nearest preceding non-case section heading.
        section = ""
        for pos, title in sections:
            if pos < m.start():
                section = title
            else:
                break
        read_only = bool(_RO_MARK.search(section) or _RO_MARK.search(case_line))
        out[cid] = {"source_section": section or None, "read_only": read_only,
                    "case_heading": case_line}
    return out


def apply_source_case_markers(plan: TestPlan, task_text: str) -> TestPlan:
    """Overlay source-file read_only / section onto planned cases (LLM title may drop them)."""
    markers = source_case_markers(task_text)
    for c in plan.cases:
        m = markers.get(c.id)
        if not m:
            continue
        if m.get("source_section"):
            c.source_section = m["source_section"]
        if m.get("read_only"):
            c.read_only = True
    return plan
