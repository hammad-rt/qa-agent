"""Reject environment details from lasting memory and learned.md.

Never persist as lasting knowledge: test client names/emails, logins, order/record ids,
hosts/URLs, or passwords. Run settings and pack.yaml supply those fresh each run.
"""
from __future__ import annotations

import re

# Hosts / URLs (http(s) or bare host:port)
_URL = re.compile(r"https?://[^\s]+|\b[\w.-]+\.(?:com|net|org|io|local|localhost)(?::\d+)?(?:/[^\s]*)?", re.I)
_HOSTPORT = re.compile(r"\b(?:localhost|127\.0\.0\.1)(?::\d+)?\b|\b[\w.-]+\.[a-z]{2,}:\d{2,5}\b", re.I)

# Emails and common login patterns
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_LOGIN_KV = re.compile(r"(?i)\b(?:user(?:name)?|login|account|email)\s*[:=]\s*\S+")

# Record / order ids (A1158, B2001, BX123, record=…, order_id=…)
_RECORD_ID = re.compile(r"\b[A-Z]{1,3}\d{3,}\b")
_RECORD_KV = re.compile(r"(?i)\b(?:record|order_id|invoice_id|contact_id|id)\s*=\s*[^\s&,;]+")
_PATH_ID = re.compile(r"/\d{3,}(?=/|$|\?)")

# Passwords / secrets already partly handled by redact; catch explicit forms too
_PASSWORD = re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_ -]?key)\s*[:=]\s*\S+")

# Keys whose *values* are always environment details (resource rows)
ENV_VALUE_KEYS = re.compile(
    r"^(env\.|account\.|data\.)|(\.last_used$)|(^url\.)|(password|secret|host)",
    re.I,
)


class EnvDetailError(ValueError):
    """Raised when a write path would store environment details as lasting memory."""


def find_env_details(text: str) -> list[str]:
    """Return human-readable labels for env-detail matches in text (empty if clean)."""
    if not text:
        return []
    hits: list[str] = []
    checks = (
        (_PASSWORD, "password/secret"),
        (_EMAIL, "email"),
        (_URL, "url/host"),
        (_HOSTPORT, "host"),
        (_LOGIN_KV, "login"),
        (_RECORD_KV, "record id"),
        (_RECORD_ID, "record id"),
        (_PATH_ID, "numeric path id"),
    )
    for rx, label in checks:
        if rx.search(text):
            hits.append(label)
    return hits


def contains_env_detail(text: str) -> bool:
    return bool(find_env_details(text))


def scrub_env_details(text: str) -> str:
    """Replace env details with placeholders (for learned.md / display). Not for secret safety alone."""
    if not text:
        return text
    t = _PASSWORD.sub(lambda m: re.split(r"[:=]", m.group(), 1)[0] + "=[masked]", text)
    t = _EMAIL.sub("[email]", t)
    t = _URL.sub("[url]", t)
    t = _HOSTPORT.sub("[host]", t)
    t = _LOGIN_KV.sub(lambda m: re.split(r"[:=]", m.group(), 1)[0].strip() + "=[login]", t)
    t = _RECORD_KV.sub(lambda m: re.split(r"=", m.group(), 1)[0] + "=<id>", t)
    t = _RECORD_ID.sub("<a record>", t)
    t = _PATH_ID.sub("/<id>", t)
    return re.sub(r"\s+", " ", t).strip()


def assert_safe_lasting(text: str, *, context: str = "memory") -> str:
    """Scrub then refuse if env details remain (or were the substance of the text).

    Returns the scrubbed text. Raises EnvDetailError when the text is primarily env details
    or still matches after scrub (should not happen) / empty after scrub of a non-empty input
    that was only env noise.
    """
    raw = (text or "").strip()
    if not raw:
        raise EnvDetailError(f"{context}: empty text")
    hits = find_env_details(raw)
    cleaned = scrub_env_details(raw)
    # After scrub, placeholder-only / tiny residue means the line was just env noise
    substance = re.sub(r"\[(?:email|url|host|login|masked)\]|<a record>|<id>|/\s*", " ", cleaned)
    substance = re.sub(r"\s+", " ", substance).strip(" -.:;,").strip()
    if hits and len(substance) < 12:
        raise EnvDetailError(
            f"{context}: refused env details ({', '.join(dict.fromkeys(hits))}): {raw[:120]}"
        )
    if find_env_details(cleaned):
        # Defensive: scrub should have cleared known patterns
        raise EnvDetailError(f"{context}: env details remain after scrub: {cleaned[:120]}")
    return cleaned


def is_env_resource_key(key: str | None) -> bool:
    return bool(key and ENV_VALUE_KEYS.search(key))
