"""Seed lasting lessons. Core + domain are app-agnostic; platform packs and project packs hold the rest."""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

CORE_LESSONS: list[dict] = [
    {
        "text": "Test a lock by actually trying the change, not by checking the control looks disabled.",
        "area": None,
        "applies_when": "general",
    },
    {
        "text": "Confirm the build under test is the new one before starting.",
        "area": None,
        "applies_when": "general",
    },
    {
        "text": "Check what's on screen as well as what was saved.",
        "area": None,
        "applies_when": "general",
    },
    {
        "text": "If a search finds nothing, try other search methods before assuming the record doesn't exist, and ask if still stuck.",
        "area": None,
        "applies_when": "general",
    },
    {
        "text": "Put test data back the way it was, with proof.",
        "area": None,
        "applies_when": "general",
    },
    {
        "text": "Never remember environment details; find them fresh each run.",
        "area": None,
        "applies_when": "general",
    },
]

# Short CRM/ERP behaviour — always loaded. No product or project names.
DOMAIN_LESSONS: dict[str, list[dict]] = {
    "crm": [
        {
            "text": "Records usually have list, detail and edit views; confirm which view you are on before acting.",
            "area": "crm",
            "applies_when": "navigation",
        },
        {
            "text": "Basic search often matches names only; use Advanced Search or list filters for email, status and other fields.",
            "area": "crm",
            "applies_when": "search",
        },
        {
            "text": "Some list filters apply only after pressing Search; others stay applied across visits and must be cleared explicitly.",
            "area": "crm",
            "applies_when": "filters",
        },
        {
            "text": "List views paginate; if a record is missing, check the next page or raise the page size before concluding it is absent.",
            "area": "crm",
            "applies_when": "pagination",
        },
        {
            "text": "Statuses follow a workflow; not every transition is valid from the current status, and some need approval.",
            "area": "crm",
            "applies_when": "workflow",
        },
        {
            "text": "Money fields use currency and rounding rules; compare amounts at the same precision the UI shows.",
            "area": "crm",
            "applies_when": "currency",
        },
        {
            "text": "Related records often live in subpanels; open the parent record before looking for child rows.",
            "area": "crm",
            "applies_when": "subpanels",
        },
        {
            "text": "Inline edit and full edit forms differ; a field missing inline may exist only on the full edit screen.",
            "area": "crm",
            "applies_when": "edit",
        },
    ],
    "erp": [
        {
            "text": "Documents and line items are linked; change the header only when the lines are in a state that allows it.",
            "area": "erp",
            "applies_when": "documents",
        },
        {
            "text": "Posting, approval and void steps are usually irreversible in practice; verify status before and after.",
            "area": "erp",
            "applies_when": "posting",
        },
        {
            "text": "List filters and saved searches can hide open documents; clear filters before reporting a missing record.",
            "area": "erp",
            "applies_when": "filters",
        },
    ],
}

# App-specific seeds: only loaded into project:<slug>, never core/domain.
PACK_LESSONS: dict[str, list[dict]] = {
    "savostore": [
        {
            "text": "Portal wallet and CRM Current Rewards must be read after reload; a greyed control alone is not proof of a lock.",
            "area": "rewards",
            "applies_when": "rewards",
        },
    ],
}


def _platforms_dir() -> Path:
    from ..config import ROOT
    return ROOT / "packs" / "_platforms"


def load_platform_seeds(platform_id: str, platforms_dir: Path | None = None) -> list[dict]:
    """Read packs/_platforms/<id>/seeds.yaml (list of {text, area?, applies_when?})."""
    root = platforms_dir or _platforms_dir()
    path = root / platform_id / "seeds.yaml"
    if not path.is_file():
        return []
    try:
        import yaml
        data = yaml.safe_load(path.read_text()) or []
    except Exception:  # noqa: BLE001
        log.debug("failed to load platform seed yaml", exc_info=True)
        return []
    out = []
    for row in data if isinstance(data, list) else data.get("lessons") or []:
        if isinstance(row, dict) and row.get("text"):
            out.append(row)
        elif isinstance(row, str) and row.strip():
            out.append({"text": row.strip()})
    return out


def ensure_seeds(store, slug: str | None = None, *, platforms: list[str] | None = None,
                 platforms_dir: Path | None = None) -> dict:
    """Insert missing seed lessons (source=seed). Idempotent by text match within scope."""
    from .layers import platform_scope
    from .service import jaccard

    added = {"core": 0, "domain": 0, "platform": 0, "project": 0}
    existing_core = store.query(["core"], ["lesson"], statuses=("active", "proposed", "needs_recheck"))
    for s in CORE_LESSONS:
        if any(jaccard(s["text"], r["text"]) > 0.85 for r in existing_core):
            continue
        store.add(
            "lesson", "core", s["text"], "seed",
            area=s.get("area"), applies_when=s.get("applies_when"),
            status="active",
        )
        added["core"] += 1
        existing_core = store.query(["core"], ["lesson"], statuses=("active", "proposed", "needs_recheck"))

    for domain, lessons in DOMAIN_LESSONS.items():
        scope = f"domain:{domain}"
        existing = store.query([scope], ["lesson"], statuses=("active", "proposed", "needs_recheck"))
        for s in lessons:
            if any(jaccard(s["text"], r["text"]) > 0.85 for r in existing):
                continue
            store.add(
                "lesson", scope, s["text"], "seed",
                area=s.get("area"), applies_when=s.get("applies_when"),
                status="active",
            )
            added["domain"] += 1
            existing = store.query([scope], ["lesson"], statuses=("active", "proposed", "needs_recheck"))

    for pid in platforms or []:
        scope = platform_scope(pid)
        if not scope:
            continue
        existing = store.query([scope], ["lesson"], statuses=("active", "proposed", "needs_recheck"))
        for s in load_platform_seeds(pid, platforms_dir=platforms_dir):
            if any(jaccard(s["text"], r["text"]) > 0.85 for r in existing):
                continue
            # Platform seeds are starting assumptions — active but trust=hint at retrieve time.
            store.add(
                "lesson", scope, s["text"], "seed_platform",
                area=s.get("area"), applies_when=s.get("applies_when"),
                status="active",
            )
            added["platform"] += 1
            existing = store.query([scope], ["lesson"], statuses=("active", "proposed", "needs_recheck"))

    if slug:
        scope = f"project:{slug}"
        existing = store.query([scope], ["lesson"], statuses=("active", "proposed", "needs_recheck"))
        for s in PACK_LESSONS.get(slug, []):
            if any(jaccard(s["text"], r["text"]) > 0.85 for r in existing):
                continue
            store.add(
                "lesson", scope, s["text"], "seed",
                area=s.get("area"), applies_when=s.get("applies_when"),
                status="active",
            )
            added["project"] += 1
            existing = store.query([scope], ["lesson"], statuses=("active", "proposed", "needs_recheck"))
    return added
