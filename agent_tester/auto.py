"""Automatic mode (default): runs need no answers from a person except CAPTCHA / manual logins.
Set QA_INTERACTIVE=1 to get the old prompts (plan gate, approvals, questions)."""
from __future__ import annotations

import os


def is_auto() -> bool:
    return os.environ.get("QA_INTERACTIVE", "").strip().lower() not in ("1", "true", "yes")


def prompter(default: str = "", out=print):
    """input() replacement: in automatic mode prints the prompt and answers `default`."""
    def ask(prompt: str = "") -> str:
        if not is_auto():
            return input(prompt)
        out(f"{prompt}{default}  (automatic)")
        return default
    return ask
