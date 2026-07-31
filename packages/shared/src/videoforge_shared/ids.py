"""ULID generation — every primary key in the system (SADD §10.2).

ULIDs over UUIDs because the 48-bit timestamp prefix makes them
lexicographically sortable by creation time (index-friendly, log-friendly)
while staying 26 chars of URL-safe Crockford base32.
"""

from __future__ import annotations

from ulid import ULID

#: Alphabet ULIDs draw from — Crockford base32 (no I, L, O, U).
ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_LENGTH = 26


def new_ulid() -> str:
    """A fresh ULID as its canonical 26-character string."""
    return str(ULID())


def is_ulid(value: str) -> bool:
    """Cheap shape check — length and alphabet, no timestamp validation."""
    return len(value) == ULID_LENGTH and all(c in ULID_ALPHABET for c in value)
