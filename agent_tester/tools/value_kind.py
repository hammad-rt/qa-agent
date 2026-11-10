"""Validate recorded values by expected kind so placeholder words / redaction tokens never enter state."""
from __future__ import annotations

import re
from typing import Any

# Words that mean "nothing / unknown" — never store as a real recorded value.
NEGATIVE_WORDS = frozenset({
    "none", "null", "n/a", "na", "n\\a", "not found", "notfound", "unknown", "empty",
    "tbd", "?", "-", "--", "nil", "undefined", "nothing", "no value", "missing",
    "not-needed", "not_needed", "sk-none", "dummy",
})

# Optional pack declaration: placeholders.items[].value_kind / data_prep[].value_kind
KINDS = ("record_id", "email", "order_number", "money", "status", "yes_no", "free_text")

_REDACTION_TOKEN = re.compile(
    r"\[[A-Z][A-Z0-9_]*(?:PASSWORD|TOKEN|API[_-]?KEY|SECRET|KEY)[A-Z0-9_]*\]"
    r"|\[[A-Z][A-Z0-9_]{4,}\]"
)
_EMAIL = re.compile(r"^[\w.+-]+@[\w.-]+\.\w{2,}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")
_HEX_ID = re.compile(r"^[0-9a-fA-F]{8,36}$")
_ORDER = re.compile(r"^[A-Za-z]{0,4}-?[A-Za-z]?\d[\dA-Za-z-]{1,20}$")  # A1234 / INV-42 / SO-1001
_MONEY = re.compile(r"(?i)^(?:USD|KES|EUR|GBP|\$|£)?\s*-?\d[\d,]*(?:\.\d+)?\s*(?:USD|KES|EUR|GBP|\$)?$")
_STATUS = re.compile(r"^[A-Za-z][A-Za-z0-9 &/_-]{1,80}$")
_TOKENISH = re.compile(r"(?i)^(ORDER-[A-Z0-9_-]+|CUSTOMER|WALLET|\{[A-Za-z_]+\})")
_PLACEHOLDER_NAME = re.compile(r"(?i)\b(?:ORDER-[A-Z][A-Z0-9_-]*|CUSTOMER|Wallet)\b|\{[A-Za-z_]+\}")


def infer_kind(name: str, declared: str | None = None) -> str:
    """Infer expected kind from an explicit pack declaration or the value name."""
    if declared and declared in KINDS:
        return declared
    n = (name or "").strip().lower()
    if n.endswith("_ok") or n in ("yes", "no"):
        return "yes_no"
    if "email" in n or n.endswith("_login") or n == "customer_email":
        return "email"
    if n.endswith("_record") or n.endswith("_id") or "record_id" in n or n.endswith("contact_id"):
        return "record_id"
    if any(k in n for k in ("order", "quote", "invoice", "number")) and "email" not in n:
        return "order_number"
    if any(k in n for k in ("wallet", "balance", "amount", "total", "reward", "shipping", "price",
                            "paid", "unpaid", "cap", "fee")):
        return "money"
    if "status" in n or n.endswith("_state"):
        return "status"
    return "free_text"


def kind_map_from_pack(pack_cfg: dict | None) -> dict[str, str]:
    """value_name -> kind from placeholders.items and data_prep rules."""
    out: dict[str, str] = {}
    cfg = pack_cfg or {}
    for it in ((cfg.get("placeholders") or {}).get("items") or []):
        v = it.get("value")
        if not v:
            continue
        out[str(v)] = infer_kind(str(v), it.get("value_kind") or it.get("kind"))
        for a in it.get("aliases") or []:
            out[str(a)] = out[str(v)]
        # record companion
        out.setdefault(f"{v}_record", "record_id")
    for rule in cfg.get("data_prep") or []:
        v = rule.get("value")
        if not v:
            continue
        out[str(v)] = infer_kind(str(v), rule.get("value_kind") or rule.get("kind"))
        for a in rule.get("aliases") or []:
            out.setdefault(str(a), out[str(v)])
        out.setdefault(f"{v}_record", "record_id")
        out.setdefault(f"{v}_ok", "yes_no")
    return out


def placeholder_tokens(pack_cfg: dict | None) -> set[str]:
    toks = set()
    for it in (((pack_cfg or {}).get("placeholders") or {}).get("items") or []):
        t = str(it.get("placeholder") or it.get("token") or "").strip()
        if t:
            toks.add(t.upper())
    return toks


def is_negative_word(raw: Any) -> bool:
    t = str(raw or "").strip().strip("'\"").lower()
    if not t:
        return True
    if t in NEGATIVE_WORDS:
        return True
    # "no: …" is a valid yes_no answer, not a bare negative for other kinds
    if t.startswith("not found") or t.startswith("no match"):
        return True
    return False


def has_redaction_token(raw: Any) -> bool:
    return bool(_REDACTION_TOKEN.search(str(raw or "")))


def looks_like_placeholder_literal(raw: Any, *, name: str = "", tokens: set[str] | None = None) -> bool:
    t = str(raw or "").strip()
    if not t:
        return True
    if _TOKENISH.match(t) or _PLACEHOLDER_NAME.fullmatch(t):
        return True
    if tokens and t.upper() in {x.upper() for x in tokens}:
        return True
    if name and t.lower() == name.lower():
        return True
    if "{client}" in t.lower() or t.lower() in ("{client}", "{value}", "{record}"):
        return True
    return False


def validate_value(name: str, raw: Any, *, kind: str | None = None,
                   pack_kinds: dict[str, str] | None = None,
                   tokens: set[str] | None = None) -> dict:
    """Return {ok, value, reason, not_found, kind}.

    ``not_found=True`` means the model signalled absence (none/not found) — never store it,
    but callers (find prep) may treat the turn as not_found.
    """
    declared = (pack_kinds or {}).get(name) if pack_kinds else None
    k = infer_kind(name, kind or declared)
    text = str(raw if raw is not None else "").strip()
    if has_redaction_token(text):
        return {"ok": False, "value": None, "reason": "value contains a redaction token; read the real page text",
                "not_found": False, "kind": k}
    if looks_like_placeholder_literal(text, name=name, tokens=tokens):
        return {"ok": False, "value": None,
                "reason": f"value looks like a placeholder name ({text!r}), not a real page value",
                "not_found": True, "kind": k}
    if is_negative_word(text):
        # yes_no allows bare "no"
        if k == "yes_no" and re.match(r"(?i)^no\b", text):
            return {"ok": True, "value": text, "reason": "", "not_found": False, "kind": k}
        return {"ok": False, "value": None,
                "reason": f"refusing to store placeholder/negative word {text!r} for {name}",
                "not_found": True, "kind": k}

    if k == "email":
        if not _EMAIL.match(text):
            return {"ok": False, "value": None, "reason": f"{name} must be an email address",
                    "not_found": False, "kind": k}
    elif k == "record_id":
        if not (_UUID.match(text) or _HEX_ID.match(text) or re.fullmatch(r"\d{3,}", text)):
            return {"ok": False, "value": None,
                    "reason": f"{name} must be a CRM record id (uuid/hex/digits from the URL)",
                    "not_found": False, "kind": k}
    elif k == "order_number":
        if not _ORDER.match(text) or is_negative_word(text):
            return {"ok": False, "value": None,
                    "reason": f"{name} must look like an order/invoice number (e.g. A1234), not {text!r}",
                    "not_found": False, "kind": k}
    elif k == "money":
        if not _MONEY.match(text.replace(" ", "")) and not re.search(r"\d", text):
            return {"ok": False, "value": None, "reason": f"{name} must be a money amount",
                    "not_found": False, "kind": k}
    elif k == "status":
        if not _STATUS.match(text) or is_negative_word(text):
            return {"ok": False, "value": None, "reason": f"{name} must be a status label",
                    "not_found": False, "kind": k}
    elif k == "yes_no":
        if not re.match(r"(?i)^(yes|no)\b", text):
            return {"ok": False, "value": None,
                    "reason": f"{name} must start with yes or no (got {text!r})",
                    "not_found": False, "kind": k}
    # free_text: only the shared negative / token checks above
    return {"ok": True, "value": text, "reason": "", "not_found": False, "kind": k}
