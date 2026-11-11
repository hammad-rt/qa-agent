"""Mask card numbers, emails, phones and tokens before text reaches the LLM, reports or snapshots."""
from __future__ import annotations

import re

_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d\s-]{8,}\d(?![\w.])")
_TOKEN = re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}\b|\b[A-Fa-f0-9]{32,}\b|eyJ[\w-]+\.[\w-]+\.[\w-]+")
_PASSWORD_KV = re.compile(r"(?i)(password|passwd|pwd|secret|api[_ -]?key)\s*[:=]\s*\S+")


_STAMP = re.compile(r"20\d{6}-\d{6}")           # run folder ids like 7-20261006-100000 are not cards

# Values that look like "no secret set" — never register these for scrubbing.
# Keeps LLM_API_KEY=none from turning every "none" / "found none" into [LLM_API_KEY].
SECRET_PLACEHOLDERS = frozenset({
    "none", "null", "empty", "dummy", "na", "n/a", "n\\a", "not-needed", "not_needed",
    "notneeded", "sk-none", "x", "-", "--", "true", "false", "yes", "no", "0", "1",
    "changeme", "password", "secret", "apikey", "api_key", "your-api-key", "no-key",
    "nokey", "undefined", "nil",
})
MIN_SECRET_LEN = 8

# Scrubber output like [LLM_API_KEY] / [SAVOSTORE_CRM_PASSWORD] — never show raw to a person.
_REDACTION_TOKEN = re.compile(
    r"\[(?:[A-Z][A-Z0-9_]*(?:PASSWORD|TOKEN|API[_-]?KEY|SECRET|KEY)[A-Z0-9_]*|"
    r"[A-Z][A-Z0-9_]{4,})\]"
)
USER_HIDDEN = "(hidden value)"


def is_placeholder_secret(value: str | None) -> bool:
    """True when an env secret value is a common 'no key' / empty placeholder (not a real secret)."""
    if value is None:
        return True
    v = str(value).strip()
    if not v:
        return True
    if len(v) < MIN_SECRET_LEN:
        return True
    low = v.lower()
    if low in SECRET_PLACEHOLDERS:
        return True
    if re.fullmatch(r"\d+", v):
        return True
    return False


def is_redactable_secret(value: str | None) -> bool:
    """Only real secrets are scrubbed from logs/notes/model text."""
    if value is None:
        return False
    v = str(value)
    if not v or is_placeholder_secret(v):
        return False
    return True


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


def _secret_pattern(value: str) -> re.Pattern:
    """Match the secret as a whole token (not a substring of a longer word)."""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])")


def iter_secret_env(env: dict | None = None) -> list[tuple[str, str, bool]]:
    """Yield (name, value, redactable) for env keys that look like secrets. Values never logged by callers."""
    import os
    env = env if env is not None else os.environ
    out = []
    for k, v in env.items():
        if not any(h in k.upper() for h in SECRET_ENV_HINTS):
            continue
        out.append((k, str(v) if v is not None else "", is_redactable_secret(v)))
    return out


def warn_skipped_secrets(env: dict | None = None, out=print) -> list[str]:
    """Startup check: warn (names only) when a secret env value is short/common and so skipped."""
    skipped = [k for k, _v, ok in iter_secret_env(env) if not ok]
    for k in skipped:
        out(f"Note: {k} looks like a placeholder or short value and will not be redacted from logs.")
    return skipped


def scrub_secrets(text: str, env: dict | None = None, *, user_answer: bool = False) -> str:
    """Replace literal secret env values with ``[ENV_NAME]``.

    Skips short values and common placeholders (``none``, ``null``, …) so
    ``LLM_API_KEY=none`` cannot rewrite ordinary English. Matches on token
    boundaries only. When ``user_answer=True`` (stdin / stuck-ask answers),
    only long exact secrets are scrubbed — same filter, explicit call site.
    """
    import os
    env = env if env is not None else os.environ
    if not text:
        return text
    # Longest first so overlapping values replace cleanly.
    secrets: list[tuple[str, str]] = []
    for k, v, ok in iter_secret_env(env):
        if not ok or not v:
            continue
        secrets.append((k, v))
    secrets.sort(key=lambda kv: -len(kv[1]))
    for k, v in secrets:
        text = _secret_pattern(v).sub(f"[{k}]", text)
    return text


def scrub_user_answer(text: str, env: dict | None = None) -> str:
    """Scrub only exact long secrets from a person-typed answer; never placeholder noise."""
    return scrub_secrets(text, env, user_answer=True)


def strip_redaction_tokens(text: str, replacement: str = USER_HIDDEN) -> str:
    """Replace raw ``[ENV_NAME]`` markers so people never see scrubber tokens."""
    if not text:
        return text
    return _REDACTION_TOKEN.sub(replacement, text)


def for_user(text: str, env: dict | None = None, keep_emails: tuple[str, ...] = ()) -> str:
    """Text safe to show a person: scrub real secrets, then hide any remaining ``[KEY]`` markers."""
    return strip_redaction_tokens(scrub_secrets(redact(text or "", keep_emails=keep_emails), env))


# Dummy passed to LiteLLM when the configured key is a placeholder (server needs no key).
LLM_NO_KEY_DUMMY = "no-key"


def effective_api_key(key: str | None) -> str:
    """Map placeholder API keys to a stable dummy for LiteLLM; real keys pass through."""
    if key is None or is_placeholder_secret(key):
        return LLM_NO_KEY_DUMMY
    return str(key)


# Call sites that must scrub before persistence / model / display (kept for tests + audit).
SCRUB_CALL_SITES = (
    "run_state.save",
    "memory.store._clean",
    "agents.llm.trace/raw",
    "web.server.guidance",
    "progress._write",
    "ask_user/stuck_ask answers",
    "report builder (via state)",
    "tool args via mask_tool_args (_safe_args)",
)

_PASSWORD_HINT = re.compile(r"(?i)password|passwd|pwd|secret|api[_ -]?key|one.?time|otp|passcode")


def _field_looks_secret(args: dict, key: str = "") -> bool:
    """True when this arg is a password/secret field by context (not by value length)."""
    if args.get("_secret") is True:
        return True
    blob = " ".join(str(args.get(k) or "") for k in ("element", "name", "label", "type", "role"))
    if _PASSWORD_HINT.search(blob):
        return True
    if str(args.get("type") or "").lower() == "password":
        return True
    if key.lower() in ("password", "passwd", "pwd", "secret", "token", "api_key"):
        return True
    return False


def mask_tool_args(args: dict | None, env: dict | None = None) -> dict:
    """Mask secrets in tool-call args for the action log / traces (context-based).

    Masks:
    - ``text`` / ``value`` typed into a password or secret field (``type=password``,
      element/name hints, or ``_secret=True`` — as ``login_form`` sets).
    - ``fill_form`` field values that are password-typed or secret-hinted.
    - Any arg value that equals a secret env var **at the point it is passed to a tool**,
      including short secrets (global ``scrub_secrets`` still skips short values).

    Does **not** change length-gated global redaction: ordinary notes/page text that
    happen to equal a short env secret are left alone by ``scrub_secrets``.
    """
    import os
    args = dict(args or {})
    env = env if env is not None else os.environ
    # Exact secret-env values (including short ones) — only for this tool-arg context.
    secret_vals = []
    for _k, v, _ok in iter_secret_env(env):
        if v and str(v).strip():
            secret_vals.append(str(v))
    secret_vals.sort(key=len, reverse=True)

    def _mask_if_secret_value(val):
        if not isinstance(val, str) or not val:
            return val
        for s in secret_vals:
            if val == s:
                return "[masked]"
        return val

    out: dict = {}
    for k, v in args.items():
        if k == "_secret":
            continue  # never persist the marker
        if k == "fields" and isinstance(v, list):
            fields = []
            for f in v:
                if not isinstance(f, dict):
                    fields.append(f)
                    continue
                ff = dict(f)
                if _field_looks_secret(ff) and isinstance(ff.get("value"), str) and ff["value"]:
                    ff["value"] = "[masked]"
                else:
                    ff["value"] = _mask_if_secret_value(ff.get("value"))
                ff.pop("_secret", None)
                fields.append(ff)
            out[k] = fields
            continue
        if k in ("text", "value", "password") and isinstance(v, str) and v:
            if _field_looks_secret(args, k):
                out[k] = "[masked]"
            else:
                out[k] = _mask_if_secret_value(v)
            continue
        if isinstance(v, str):
            out[k] = _mask_if_secret_value(v)
        else:
            out[k] = v
    return out


def mask_tool_result(result, *, secret: bool = False, env: dict | None = None):
    """Mask tool results that echo a just-typed secret (or equal a secret env value in context)."""
    import os
    env = env if env is not None else os.environ
    if secret and isinstance(result, str) and result:
        return "[masked]"
    if isinstance(result, dict):
        return mask_tool_args(result, env)
    if isinstance(result, str):
        # Only replace exact secret-env equals when caller marked the call secret.
        return result
    return result
