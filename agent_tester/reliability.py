"""Reliability helpers: LLM retries with backoff, one-run-at-a-time lock, disk check, run retention."""
from __future__ import annotations

import logging
import asyncio
import fcntl
import os
import random
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

RETRYABLE_HINTS = ("timeout", "timed out", "connection", "502", "503", "504", "429", "rate limit", "overloaded",
                   "temporarily", "reset by peer", "server disconnected")


def is_retryable(e: BaseException) -> bool:
    msg = f"{type(e).__name__} {e}".lower()
    return isinstance(e, (TimeoutError, ConnectionError, asyncio.TimeoutError)) or any(h in msg for h in RETRYABLE_HINTS)


async def with_retries(fn, *, attempts: int = 4, base: float = 2.0, cap: float = 30.0, sleep=asyncio.sleep, on_retry=None):
    """Call an async fn; retry retryable errors with exponential backoff and jitter."""
    for i in range(attempts):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001
            if i == attempts - 1 or not is_retryable(e):
                raise
            delay = min(cap, base * 2 ** i) * (0.8 + 0.4 * random.random())
            if on_retry:
                on_retry(i + 1, e, delay)
            await sleep(delay)


def wait_for_model(max_wait: float | None = None, every: float = 10.0, sleep=time.sleep, probe=None) -> bool:
    """After a connection error, wait until the model server answers again (LLM_OUTAGE_WAIT seconds, default 300).
    Returns True once it answers, False if it stayed down."""
    max_wait = float(os.environ.get("LLM_OUTAGE_WAIT", "300")) if max_wait is None else max_wait
    if probe is None:
        def probe():
            import urllib.request
            from .config import Settings
            st = Settings.load()
            req = urllib.request.Request(st.llm_base_url.rstrip("/") + "/models",
                                         headers={"Authorization": f"Bearer {st.llm_api_key}"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status == 200
    waited = 0.0
    while True:
        try:
            if probe():
                return True
        except Exception:  # noqa: BLE001
            log.debug("model-server probe failed while waiting", exc_info=True)
        if waited >= max_wait:
            return False
        sleep(every)
        waited += every


class RunLocked(RuntimeError):
    pass


class RunLock:
    """Only one run at a time (tech spec section 9). Uses an flock so a crashed run never leaves it stuck."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.seek(0)
            who = self.fh.read().strip()
            self.fh.close()
            raise RunLocked(f"another run is in progress ({who or 'unknown'})")
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(f"pid {os.getpid()} since {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()
        return False


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def apply_retention(runs_dir: Path, keep: int = 5, days: int = 14, now: float | None = None) -> list[Path]:
    """Keep the last `keep` runs or `days` days, whichever is fewer. Older runs keep only
    state.json and report.* (tech spec section 10). Returns the runs that were trimmed."""
    now = now or time.time()
    runs = sorted([p for p in Path(runs_dir).glob("*/") if (p / "state.json").exists()],
                  key=lambda p: p.stat().st_mtime, reverse=True)
    trimmed = []
    for i, run in enumerate(runs):
        recent = (now - run.stat().st_mtime) < days * 86400
        if i < keep and recent:
            continue
        changed = False
        for item in run.iterdir():
            if item.name == "state.json" or item.name.startswith("report."):
                continue
            shutil.rmtree(item) if item.is_dir() else item.unlink()
            changed = True
        if changed:
            trimmed.append(run)
    return trimmed
