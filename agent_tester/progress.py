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
        from .redact import for_user
        safe = for_user(line)
        self.out(safe)
        with self.log.open("a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {safe}\n")

    def started(self, label: str) -> None:
        self._write(f"Started {label}: {self.total} case(s)" + (f", {self.done} already done" if self.done else ""))
        self._milestone(f"QA run started: {label}", f"{self.total} case(s) planned.")

    def case_done(self, r) -> None:
        self.done += 1
        mins = (time.time() - self.start) / 60
        extra = f" (still present: bug {r.known_bug})" if r.known_bug else ""
        stats = (self.state.data.get("llm_stats") or {}).get(r.case_id) or {}
        timing = ""
        if stats:
            timing = (f" · {stats.get('turns', 0)} LLM turns, "
                      f"{stats.get('prompt_tokens', 0) + stats.get('completion_tokens', 0)} tok, "
                      f"LLM {stats.get('llm_s', 0):.0f}s, tools {stats.get('tool_s', 0):.0f}s")
        self._write(f"[{self.done}/{self.total}] {r.case_id}: {r.verdict}{extra} · {mins:.1f} min{timing}")
        if self.total >= 4 and self.done == self.total // 2:
            self._milestone("QA run halfway", f"{self.done} of {self.total} cases done.")

    def paused(self, why: str) -> None:
        self._write(f"Paused: {why}")
        self._milestone("QA run paused", why)

    def finished(self, counts: dict, report) -> None:
        summary = ", ".join(f"{v} {k}" for k, v in counts.items())
        self._write(f"Finished: {summary}. Report: {report}")
        ctx = self.state.data.get("context_stats") or {}
        if ctx:
            self._write(
                f"Context: {ctx.get('calls', 0)} injects, "
                f"{ctx.get('tokens_sent', 0)} tokens sent, "
                f"{ctx.get('tokens_omitted', 0)} tokens saved by selection")
        llm = self.state.data.get("llm_stats") or {}
        if llm:
            turns = sum(v.get("turns", 0) for v in llm.values())
            pt = sum(v.get("prompt_tokens", 0) for v in llm.values())
            ct = sum(v.get("completion_tokens", 0) for v in llm.values())
            ls = sum(v.get("llm_s", 0) for v in llm.values())
            ts = sum(v.get("tool_s", 0) for v in llm.values())
            self._write(f"LLM totals: {turns} turns, {pt}+{ct} tok, LLM {ls:.0f}s, tools {ts:.0f}s")
        self._milestone("QA run finished", f"{summary}\nReport: {report}")

    def _milestone(self, subject: str, body: str) -> None:
        if self.notifier:
            try:
                self.notifier(subject, body, None)
            except Exception as e:  # noqa: BLE001
                self.out(f"(milestone email failed: {type(e).__name__})")
