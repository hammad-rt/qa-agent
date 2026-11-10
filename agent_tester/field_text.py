"""Coerce case/plan text fields that may be str, list, or nested after resume/JSON load."""
from __future__ import annotations


def field_text(value, *, sep: str = "\n") -> str:
    """Turn a case/plan field into a plain string for join/concat.

    - None / empty → ""
    - str → as-is
    - list/tuple → recursively flatten, join non-empty parts with ``sep``
    - dict / object → prefer a ``text`` key/attr (also coerced), else ``str(value)``
    - anything else → ``str(value)``
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        parts = [field_text(v, sep=sep) for v in value]
        return sep.join(p for p in parts if p)
    if isinstance(value, dict):
        return field_text(value.get("text"), sep=sep) if "text" in value else ""
    if hasattr(value, "text"):
        return field_text(getattr(value, "text", None), sep=sep)
    s = str(value)
    return s if s else ""
