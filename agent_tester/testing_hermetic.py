"""Hermetic defaults for the unit suite (safe to import from any discovery mode).

Laptop auto-mode (QA_INTERACTIVE unset) and a real `.env` must not leak into approval /
ask / prefs tests. Product auto mode is covered by `tests/test_auto_mode.py`, which
patches `QA_INTERACTIVE` explicitly.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

os.environ["QA_INTERACTIVE"] = "1"
os.environ["QA_TEST"] = "1"
os.environ.setdefault("LOGIN_LEARN", "0")
os.environ.setdefault("LLM_OUTAGE_WAIT", "0")
os.environ.setdefault("REPORT_PDF", os.environ.get("REPORT_PDF", "0"))

_PREFS_DIR = tempfile.mkdtemp(prefix="qa-test-prefs-")
_MEM_DIR = tempfile.mkdtemp(prefix="qa-test-mem-")
os.environ["MEMORY_PREFS_PATH"] = str(Path(_PREFS_DIR) / "prefs.json")
os.environ["MEMORY_DB"] = str(Path(_MEM_DIR) / "memory.db")


def _cleanup() -> None:
    for d in (_PREFS_DIR, _MEM_DIR):
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup)


def clear_pack_secrets() -> None:
    """Drop pack password env vars so a laptop `.env` cannot satisfy gap answers."""
    for k in list(os.environ):
        ku = k.upper()
        if any(h in ku for h in ("PASSWORD", "TOKEN", "API_KEY", "SECRET")) and (
            ku.startswith("SAVO") or ku.startswith("SAVOSTORE") or ku.startswith("DEMO_")
        ):
            os.environ.pop(k, None)
