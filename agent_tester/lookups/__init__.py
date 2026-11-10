"""Deterministic (no-LLM) record lookups for prep/find and contact matching."""
from .record import (
    LookupResult,
    detail_field,
    find_contact_by_email,
    find_records,
    lookup_from_pack,
    read_wallet_fields,
)

__all__ = [
    "LookupResult", "detail_field", "find_contact_by_email", "find_records",
    "lookup_from_pack", "read_wallet_fields",
]
