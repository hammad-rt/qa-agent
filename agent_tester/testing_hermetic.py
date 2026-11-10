"""Hermetic defaults for the unit suite (safe to import from any discovery mode).

Laptop auto-mode (QA_INTERACTIVE unset) and a real `.env` (MEMORY_DB, passwords, REDMINE_*, …)
must not leak into approval / ask / prefs tests. Product auto mode is covered by
`tests/test_auto_mode.py`, which patches `QA_INTERACTIVE` explicitly.

``QA_TEST=1`` is set here and also auto-detected from ``python -m unittest`` in
``config.load_dotenv`` / ``Settings.load``, which then refuse to read `.env` and redirect
``MEMORY_DB`` / ``MEMORY_PREFS_PATH`` away from the repo's real memory files.
"""
from __future__ import annotations

import os

from .config import ensure_unit_test_isolation

# Interactive prompts under test (auto mode has its own tests).
os.environ["QA_INTERACTIVE"] = "1"
os.environ.setdefault("LOGIN_LEARN", "0")
os.environ.setdefault("LLM_OUTAGE_WAIT", "0")
# Prefer REPORT_PDF=0 in the suite unless the caller already set it.
os.environ.setdefault("REPORT_PDF", "0")

ensure_unit_test_isolation()  # sets QA_TEST=1, temp MEMORY_DB / prefs, clears pack secrets


def clear_pack_secrets() -> None:
    """Drop pack password env vars so a laptop `.env` / shell export cannot satisfy gap answers."""
    for k in list(os.environ):
        ku = k.upper()
        if any(h in ku for h in ("PASSWORD", "TOKEN", "API_KEY", "SECRET")) and (
            ku.startswith("SAVO") or ku.startswith("SAVOSTORE") or ku.startswith("DEMO_")
        ):
            os.environ.pop(k, None)
