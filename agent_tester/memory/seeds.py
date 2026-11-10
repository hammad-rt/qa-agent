"""Seed lasting lessons. Core seeds are app-agnostic; pack seeds live under packs/<slug>/ only."""
from __future__ import annotations

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

# App-specific seeds: only loaded into project:<slug>, never core.
PACK_LESSONS: dict[str, list[dict]] = {
    "savostore": [
        {
            "text": "SuiteCRM basic Clients search matches names only; use filter/Advanced Search for email.",
            "area": "crm",
            "applies_when": "searching clients",
        },
    ],
}


def ensure_seeds(store, slug: str | None = None) -> dict:
    """Insert missing seed lessons (source=seed, active). Idempotent by text match within scope."""
    from .service import jaccard

    added = {"core": 0, "project": 0}
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
