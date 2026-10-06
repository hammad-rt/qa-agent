"""Progress lines on the console and in progress.log, plus optional milestone emails to the user's own inbox."""
from __future__ import annotations

import time
from datetime import datetime


class Progress:
    def __init__(self, state, total: int, out=print, notifier=None):
        self.state, self.total, self.out, self.notifier = state, total, out, notifier
        self.start = time.time()
        self.done = sum(1 for _ in state.data.get("results", {}))
        self.log = state.dir / "progress.log"

    def _write(self, line: str) -> None:
        self.out(line)
        with self.log.open("a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")

    def started(self, label: str) -> None:
        self._write(f"Started {label}: {self.total} case(s)" + (f", {self.done} already done" if self.done else ""))
        self._milestone(f"QA run started: {label}", f"{self.total} case(s) planned.")

    def case_done(self, r) -> None:
        self.done += 1
        mins = (time.time() - self.start) / 60
        extra = f" (still present: bug {r.known_bug})" if r.known_bug else ""
        self._write(f"[{self.done}/{self.total}] {r.case_id}: {r.verdict}{extra} · {mins:.1f} min")
        if self.total >= 4 and self.done == self.total // 2:
            self._milestone("QA run halfway", f"{self.done} of {self.total} cases done.")

    def paused(self, why: str) -> None:
        self._write(f"Paused: {why}")
        self._milestone("QA run paused", why)

    def finished(self, counts: dict, report) -> None:
        summary = ", ".join(f"{v} {k}" for k, v in counts.items())
        self._write(f"Finished: {summary}. Report: {report}")
        self._milestone("QA run finished", f"{summary}\nReport: {report}")

    def _milestone(self, subject: str, body: str) -> None:
        if self.notifier:
            try:
                self.notifier(subject, body, None)
            except Exception as e:  # noqa: BLE001
                self.out(f"(milestone email failed: {type(e).__name__})")
