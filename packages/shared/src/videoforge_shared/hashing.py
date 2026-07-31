"""Content hashing and content-addressed key construction (SADD §18.2).

The object key IS the integrity check: ``{sha256[:2]}/{sha256}/{filename}``
means identical bytes always land on the same key (dedup for free), different
bytes can never collide with an existing key (immutability for free), and a
reader can verify what it fetched against the key it asked for (NF5).
"""

from __future__ import annotations

import hashlib
import re
from typing import BinaryIO

_CHUNK_SIZE = 1024 * 1024

#: Conservative filename charset — object keys travel through URLs, logs, and
#: FFmpeg command lines, so anything exotic is normalised away.
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def sha256_bytes(data: bytes) -> str:
    """Hex digest of an in-memory payload."""
    return hashlib.sha256(data).hexdigest()


def sha256_stream(stream: BinaryIO) -> str:
    """Hex digest of a stream, read in chunks. Consumes from the current
    position and does not rewind — the caller owns the file pointer."""
    digest = hashlib.sha256()
    while chunk := stream.read(_CHUNK_SIZE):
        digest.update(chunk)
    return digest.hexdigest()


def safe_filename(filename: str) -> str:
    """Collapse a filename to the safe charset. Never empty."""
    cleaned = _FILENAME_SAFE.sub("_", filename).strip("._") or "file"
    return cleaned


def content_key(sha256_hex: str, filename: str) -> str:
    """Object key for a payload: ``{sha[:2]}/{sha}/{filename}``.

    The two-char shard prefix keeps any single listing small; the full digest
    directory makes the key self-verifying; the filename keeps downloads and
    logs human-readable without affecting identity.
    """
    if not _SHA256_HEX.match(sha256_hex):
        raise ValueError(f"not a sha256 hex digest: {sha256_hex!r}")
    return f"{sha256_hex[:2]}/{sha256_hex}/{safe_filename(filename)}"


def sha256_from_key(key: str) -> str | None:
    """Recover the digest a content-addressed key claims, or None for keys
    outside the convention (e.g. music library assets)."""
    parts = key.split("/")
    if len(parts) >= 3 and _SHA256_HEX.match(parts[1]) and parts[0] == parts[1][:2]:
        return parts[1]
    return None
