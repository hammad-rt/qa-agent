"""Mask card numbers, emails, phones and tokens before text reaches the LLM, reports or snapshots."""
from __future__ import annotations

import re

_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s-]{8,}\d(?![\w.])")
_TOKEN = re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}\b|\b[A-Fa-f0-9]{32,}\b|eyJ[\w-]+\.[\w-]+\.[\w-]+")
_PASSWORD_KV = re.compile(r"(?i)(password|passwd|pwd|secret|api[_ -]?key)\s*[:=]\s*\S+")


def _mask_card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group())
    if not 13 <= len(digits) <= 19:
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
    t = _PHONE.sub("[phone]", t)
    return t
