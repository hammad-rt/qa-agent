"""Mask card numbers, emails, phones and tokens before text reaches the LLM, reports or snapshots."""
from __future__ import annotations

import re

_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s-]{8,}\d(?![\w.])")
_TOKEN = re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}\b|\b[A-Fa-f0-9]{32,}\b|eyJ[\w-]+\.[\w-]+\.[\w-]+")
_PASSWORD_KV = re.compile(r"(?i)(password|passwd|pwd|secret|api[_ -]?key)\s*[:=]\s*\S+")


_STAMP = re.compile(r"20\d{6}-\d{6}")           # run folder ids like 7-20261006-100000 are not cards


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _mask_card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group())
    if not 13 <= len(digits) <= 19 or _STAMP.search(m.group()) or not _luhn(digits):
        return m.group()
    return f"[card ****{digits[-4:]}]"


def redact(text: str, keep_emails: tuple[str, ...] = ()) -> str:
    if not text:
        return text
    t = _PASSWORD_KV.sub(lambda m: f"{m.group(1)}: [masked]", text)
    t = _TOKEN.sub("[token]", t)
    t = _CARD.sub(_mask_card, t)
    keep = {e.lower() for e in keep_emails}
    t = _EMAIL.sub(lambda m: m.group() if m.group().lower() in keep else "[email]", t)
    t = _PHONE.sub(lambda m: m.group() if _STAMP.search(m.group()) else "[phone]", t)
    return t


SECRET_ENV_HINTS = ("PASSWORD", "TOKEN", "API_KEY", "SECRET")


def scrub_secrets(text: str, env: dict | None = None) -> str:
    """Replace the literal value of any secret env var with its name, wherever it appears."""
    import os
    env = env if env is not None else os.environ
    if not text:
        return text
    for k, v in env.items():
        if v and len(v) >= 4 and any(h in k.upper() for h in SECRET_ENV_HINTS) and v in text:
            text = text.replace(v, f"[{k}]")
    return text
