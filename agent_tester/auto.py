"""Automatic mode (default): runs need no answers from a person except CAPTCHA / manual logins.
Set QA_INTERACTIVE=1 to get the old prompts (plan gate, approvals, questions)."""
from __future__ import annotations

import json
import os
import sys

ASK_MARK = "\x1eASK "   # web UI: a line starting with this is a question waiting for an answer on stdin


def web_mode() -> bool:
    return os.environ.get("QA_WEB") == "1"


def ask_line(prompt: str = "", secret: bool = False) -> str:
    """Read one answer. Under the web UI, announce the question on its own marker line (with whether it is a
    secret) and read a plain line from stdin, so the browser always knows exactly what is being asked."""
    if web_mode():
        sys.stdout.write(ASK_MARK + json.dumps({"prompt": prompt.strip(), "secret": bool(secret)}) + "\n")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            raise EOFError("no answer (the web UI closed the run)")
        return line.rstrip("\r\n")
    if secret:
        import getpass
        return getpass.getpass(prompt)
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
