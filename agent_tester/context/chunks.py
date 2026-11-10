"""Named, tagged context chunks for per-step LLM injection."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

CHARS_PER_TOKEN = 4
STEP_TYPES = ("plan", "prep", "navigate", "read", "verify", "verdict", "discover", "general")

# Sources that are never pruned / never demoted by learning.
ALWAYS_SEND_SOURCES = frozenset({
    "safety", "case_rules", "case_steps", "guidance", "data_plan", "approvals",
})


def tokens_of(text: str) -> int:
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN) if text else 0


def chunk_id(source: str, text: str, *, extra: str = "") -> str:
    """Stable id/hash for scoring — content-based, never env-bound identity."""
    raw = f"{source}|{extra}|{re.sub(r'\\s+', ' ', (text or '').strip().lower())[:2000]}"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def always_send_sources() -> frozenset[str]:
    return ALWAYS_SEND_SOURCES


@dataclass
class ContextChunk:
    source: str                          # facts|memory|routes|guidance|page|case_steps|safety|...
    text: str
    step_types: tuple[str, ...] = ()     # empty = any
    apps: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    case_ids: tuple[str, ...] = ()       # empty = any; non-empty = tagged to those cases
    keywords: tuple[str, ...] = ()
    always_send: bool = False
    exempt_from_scoring: bool = False    # safety + case-tagged rules: never demoted
    id: str = ""
    tokens: int = 0
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = chunk_id(self.source, self.text,
                               extra="|".join(self.case_ids) + "|" + "|".join(self.step_types))
        if not self.tokens:
            self.tokens = tokens_of(self.text)
        if self.source in ALWAYS_SEND_SOURCES:
            self.always_send = True
            self.exempt_from_scoring = True


_RULE_REF = re.compile(r"(?i)\brules?\s+(\d+(?:\s*,\s*\d+)*)")
_NUMBERED = re.compile(r"(?m)^\s*(?:[-*]\s+)?(?:Rule\s+)?(\d+)[.)]\s+(.+)$")


def parse_numbered_rules(text: str) -> dict[int, str]:
    """Parse '1. ...' / 'Rule 2: ...' lines from pack facts or a rules section."""
    out: dict[int, str] = {}
    for m in _NUMBERED.finditer(text or ""):
        out[int(m.group(1))] = m.group(2).strip()
    return out


def case_rule_refs(case_text: str) -> list[int]:
    """Ids referenced as 'rules 2, 14' (or 'rule 3') on a case."""
    ids: list[int] = []
    for m in _RULE_REF.finditer(case_text or ""):
        for part in m.group(1).split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
    return ids


def safety_chunks_from_pack(pack_cfg: dict | None) -> list[ContextChunk]:
    """Pack-declared safety rules — always_send on every call."""
    cfg = pack_cfg or {}
    raw = cfg.get("safety_rules") or []
    chunks: list[ContextChunk] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            text, always = item, True
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("rule") or "").strip()
            always = bool(item.get("always_send", True))
        else:
            continue
        if not text:
            continue
        chunks.append(ContextChunk(
            source="safety", text=f"SAFETY: {text}",
            always_send=always, exempt_from_scoring=True,
            meta={"index": i}))
    return chunks


def case_tagged_rule_chunks(case_id: str, case_text: str, numbered: dict[int, str]) -> list[ContextChunk]:
    """Rules tagged onto this case (e.g. 'rules 2, 14' on E1) — always sent while it runs."""
    refs = case_rule_refs(case_text)
    out: list[ContextChunk] = []
    for n in refs:
        body = numbered.get(n)
        if not body:
            continue
        out.append(ContextChunk(
            source="case_rules", text=f"Case rule {n}: {body}",
            case_ids=(case_id,), always_send=True, exempt_from_scoring=True,
            meta={"rule_n": n}))
    return out


def facts_to_chunks(facts: str, *, app: str = "", area: str = "") -> list[ContextChunk]:
    """Split facts.md ## sections into selectable chunks (non-safety; may be budgeted)."""
    from ..facts_pick import sections
    chunks = []
    for head, body in sections(facts or ""):
        if re.search(r"(?i)safety|payments|business rules", head):
            # Still selectable, but payments/business often relevant; not auto always_send
            # (pack safety_rules cover the hard gates).
            st = ("read", "verify", "prep", "navigate", "general")
        else:
            st = ("navigate", "read", "verify", "prep", "general")
        chunks.append(ContextChunk(
            source="facts", text=body.strip(),
            step_types=st, apps=(app,) if app else (),
            keywords=tuple(re.findall(r"[a-z][a-z0-9&]{3,}", head.lower())[:12]),
            meta={"section": head}))
    return chunks
