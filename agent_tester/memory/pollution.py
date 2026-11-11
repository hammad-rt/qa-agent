"""Detect and remove memory / learned hints that should not be persisted.

Infrastructure errors (LLM timeouts, cancellations, MCP/browser failures) and
stuck-loop click trails must not be injected into later prompts. Tester or
agent mistakes must not be stored as product bugs (`open_bug_found`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Infrastructure / harness failures — not product behaviour.
_INFRA = re.compile(
    r"(?is)("
    r"LLMOutputError|"
    r"LLM call hit the|"
    r"per-call timeout|"
    r"idle timeout|"
    r"no text for the whole LLM call|"
    r"CancelledError|"
    r"was cancelled by an external cancellation|"
    r"Root node \S+ was cancelled|"
    r"discovery failed\s*:|"
    r"\bMCP(?:Error| timeout| server)?\b|"
    r"Browser is already in use|"
    r"Playwright(?:Error)?|"
    r"TargetClosedError|"
    r"BrowserContextError"
    r")"
)

# Stuck-loop lines that replay a click trail as if it were a recipe.
_STUCK_TRAIL = re.compile(
    r"(?is)("
    r"^stuck\s*\(\s*loop\s*\)|"
    r"kept repeating.{0,80}(already tried|reached click|click:)"
    r")"
)

# Verdict / note text that blames the agent, tester, or missing evidence — not the app.
_TESTER_OR_AGENT = re.compile(
    r"(?is)("
    r"\bthe agent (used|typed|failed to|did not|forgot|attempted|clicked)\b|"
    r"\bthe tester (used|typed|failed to|did not|forgot|attempted)\b|"
    r"\bagent error\b|"
    r"\bagent mistake\b|"
    r"\bagent incomplete\b|"
    r"\btest not executed properly\b|"
    r"\bevidence missing\b|"
    r"\brequired evidence missing\b|"
    r"\bfail without expected vs actual\b|"
    r"\blog fails to verify\b|"
    r"\binstead of pasting\b|"
    r"\bwithout clearing\b"
    r")"
)


def is_infra_error_text(text: str | None) -> bool:
    """True when the text is an LLM / timeout / cancel / MCP / browser harness failure."""
    return bool(text) and bool(_INFRA.search(text))


def is_stuck_loop_trail(text: str | None) -> bool:
    """True for stuck-loop click trails that must not be reused as hints."""
    t = (text or "").strip()
    if not t:
        return False
    if _STUCK_TRAIL.search(t):
        return True
    # Classic learned.md shape: "Stuck (loop): ... reached click 'X' -> click 'Y'"
    if re.match(r"(?i)^stuck\b", t) and re.search(r"(?i)(already tried:|reached click\s|' -> click)", t):
        return True
    return False


def is_tester_or_agent_attribution(text: str | None) -> bool:
    """True when a verdict note / actual / expected blames the agent, tester, or log."""
    return bool(text) and bool(_TESTER_OR_AGENT.search(text))


def is_ephemeral_learned_line(text: str | None) -> bool:
    """Lines that must not be written to learned.md or injected as hints."""
    return is_infra_error_text(text) or is_stuck_loop_trail(text)


def is_polluted_memory_text(text: str | None, *, kind: str | None = None) -> bool:
    """True if this memory row should be retired / dropped from the export."""
    if not text:
        return False
    if is_ephemeral_learned_line(text):
        return True
    if kind == "open_bug_found" and is_tester_or_agent_attribution(text):
        return True
    return False


def is_product_bug_failure(result) -> bool:
    """Whether a FAIL result should be stored as open_bug_found (a product bug)."""
    verdict = getattr(result, "verdict", None) or ""
    if verdict != "FAIL":
        return False
    if getattr(result, "known_bug", None):
        return False
    blob = " ".join(
        str(x) for x in (
            getattr(result, "note", None) or "",
            getattr(result, "expected", None) or "",
            getattr(result, "actual", None) or "",
            verdict,
        ) if x
    )
    if is_tester_or_agent_attribution(blob) or is_infra_error_text(blob):
        return False
    return True


def _export_rows(raw) -> list[dict]:
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        for key in ("rows", "memory", "items"):
            val = raw.get(key)
            if isinstance(val, list):
                return [r for r in val if isinstance(r, dict)]
    return []


def clean_polluted_memory(store, *, export_path: Path | None = None,
                          dry_run: bool = False) -> dict[str, Any]:
    """Retire polluted SQLite rows and drop matching export entries. Idempotent.

    Does not touch pack learned.md. ``dry_run`` only reports what would change.
    """
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "db_retired": [],
        "export_removed": [],
        "export_path": str(export_path) if export_path else None,
    }
    scopes = [r[0] for r in store.db.execute("SELECT DISTINCT scope FROM memories")]
    if scopes:
        from .store import STATUSES
        rows = store.query(scopes, statuses=tuple(STATUSES))
    else:
        rows = []
    for r in rows:
        if (r.get("status") or "") == "retired":
            continue
        if not is_polluted_memory_text(r.get("text") or "", kind=r.get("kind")):
            continue
        entry = {"id": r.get("id"), "kind": r.get("kind"), "text": (r.get("text") or "")[:200]}
        report["db_retired"].append(entry)
        if not dry_run:
            store.set_status(r["id"], "retired")

    if export_path:
        path = Path(export_path)
        if path.is_file():
            try:
                raw = json.loads(path.read_text())
            except (OSError, ValueError):
                raw = None
            if raw is not None:
                rows_ex = _export_rows(raw)
                kept: list[dict] = []
                for r in rows_ex:
                    if is_polluted_memory_text(r.get("text") or "", kind=r.get("kind")):
                        report["export_removed"].append({
                            "id": r.get("id"), "kind": r.get("kind"),
                            "text": (r.get("text") or "")[:200],
                        })
                    else:
                        kept.append(r)
                if not dry_run and report["export_removed"]:
                    if isinstance(raw, list):
                        path.write_text(json.dumps(kept, indent=2) + "\n")
                    elif isinstance(raw, dict):
                        out = dict(raw)
                        for key in ("rows", "memory", "items"):
                            if isinstance(raw.get(key), list):
                                out[key] = kept
                                break
                        path.write_text(json.dumps(out, indent=2) + "\n")
    return report


def format_clean_report(report: dict[str, Any]) -> str:
    """Human-readable summary for the CLI (dry-run or applied)."""
    prefix = "Would remove" if report.get("dry_run") else "Removed"
    lines = [
        f"{prefix} {len(report.get('db_retired') or [])} memory.db row(s), "
        f"{len(report.get('export_removed') or [])} memory-export.json entr"
        f"{'y' if len(report.get('export_removed') or []) == 1 else 'ies'}.",
    ]
    for label, key in (("memory.db", "db_retired"), ("memory-export.json", "export_removed")):
        for e in report.get(key) or []:
            lines.append(f"  [{label} id={e.get('id')} {e.get('kind')}] {e.get('text')}")
    if report.get("export_path"):
        lines.append(f"export: {report['export_path']}")
    return "\n".join(lines)
