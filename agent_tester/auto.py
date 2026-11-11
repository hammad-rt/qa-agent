"""Automatic mode (default): runs need no answers from a person except CAPTCHA / manual logins.
Set QA_INTERACTIVE=1 to get the old prompts (plan gate, approvals, questions)."""
from __future__ import annotations

import logging
import json
import os
import select
import sys
import uuid

log = logging.getLogger(__name__)

ASK_MARK = "\x1eASK "   # web UI: a line starting with this is a question waiting for an answer on stdin


def web_mode() -> bool:
    return os.environ.get("QA_WEB") == "1"


def flush_stdin() -> int:
    """Discard any lines already waiting on stdin so a new prompt never sees a stale answer.

    Returns how many lines were dropped. Safe on tty and pipes; no-op when stdin is unavailable.
    """
    dropped = 0
    try:
        if not sys.stdin or sys.stdin.closed:
            return 0
        while True:
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0)
            except (ValueError, OSError, TypeError):
                break
            if not ready:
                break
            line = sys.stdin.readline()
            if not line:
                break
            dropped += 1
    except Exception:  # noqa: BLE001
        log.debug("stdin flush failed", exc_info=True)
        return dropped
    return dropped


def _parse_answer_line(line: str) -> tuple[str, str | None]:
    """Return (text, ask_id). Plain lines have ask_id None; JSON may carry ask_id + text."""
    raw = (line or "").rstrip("\r\n")
    if raw.startswith("{") and '"ask_id"' in raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and "text" in obj:
                return str(obj.get("text") if obj.get("text") is not None else ""), (
                    str(obj["ask_id"]) if obj.get("ask_id") is not None else None)
        except ValueError:
            pass
    return raw, None


def ask_line(prompt: str = "", secret: bool = False, *, ask_id: str | None = None) -> str:
    """Read one fresh answer for this prompt only.

    Flushes any buffered stdin first. Under the web UI, announces the question with a unique
    ``ask_id``; answers tagged with a different id are ignored until a matching (or untagged) line arrives.
    """
    flush_stdin()
    ask_id = ask_id or uuid.uuid4().hex[:12]
    if web_mode():
        sys.stdout.write(ASK_MARK + json.dumps({
            "prompt": prompt.strip(), "secret": bool(secret), "ask_id": ask_id,
        }) + "\n")
        sys.stdout.flush()
        while True:
            line = sys.stdin.readline()
            if not line:
                raise EOFError("no answer (the web UI closed the run)")
            text, got_id = _parse_answer_line(line)
            if got_id and got_id != ask_id:
                continue  # leftover / double-submit for a previous prompt
            return text
    if secret:
        import getpass
        return getpass.getpass(prompt)
    # Interactive tty: input() after flush; still accept tagged JSON from a pipe.
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if not line:
            raise EOFError("no answer on stdin")
        text, got_id = _parse_answer_line(line)
        if got_id and got_id != ask_id:
            # Keep reading until a matching or plain line (plain accepted after flush).
            while True:
                line = sys.stdin.readline()
                if not line:
                    raise EOFError("no answer on stdin")
                text, got_id = _parse_answer_line(line)
                if not got_id or got_id == ask_id:
                    return text
        return text
    return input(prompt)


def secret_input(prompt: str = "") -> str:
    return ask_line(prompt, secret=True)


def is_auto() -> bool:
    return os.environ.get("QA_INTERACTIVE", "").strip().lower() not in ("1", "true", "yes")


def prompter(default: str = "", out=print):
    """input() replacement: in automatic mode prints the prompt and answers `default`."""
    def ask(prompt: str = "") -> str:
        if not is_auto():
            return ask_line(prompt)
        out(f"{prompt}{default}  (automatic)")
        return default
    return ask
