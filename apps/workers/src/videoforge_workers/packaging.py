"""The publishing package: a deterministic zip and its manifest (M5-03).

Pure — entries in, archive bytes out — for the reason ``cards`` and ``cover``
are: the properties worth asserting are *of the archive*, and a function that
also read from MinIO could only be tested against a stack.

**Why the manifest is not optional decoration.** F10 says the package is a zip
of the video, thumbnail, caption, hashtags, metadata and assets. A zip already
lists its entries, so a manifest that merely repeated the names would earn
nothing. This one carries a **sha256 per entry**, which makes the package the
one artifact a recipient can *verify* rather than trust — the same rule ADR-004
applies to every object in storage, carried across the boundary where the bytes
leave the system.

**Deterministic, and it takes work.** Python's ``ZipFile`` writes the current
clock into every entry and preserves insertion order, so the same inputs
produce different bytes on every run — which would give the package a new
content hash each time, defeat deduplication, and make "did anything actually
change?" unanswerable. Entries are therefore sorted by path and stamped with a
fixed timestamp. This is the same property M4-02 established for cards and
M5-02 for covers, for the same reason.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any

from videoforge_shared.hashing import sha256_bytes

__all__ = [
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "PackageEntry",
    "build_package",
    "manifest_for",
]

MANIFEST_NAME = "manifest.json"

#: Bumped when the manifest's *shape* changes, so a reader can tell a v1
#: package from a v2 one without guessing. Mirrors the timeline's own
#: ``SCHEMA_VERSION``.
MANIFEST_SCHEMA_VERSION = 1

#: 1980-01-01, the earliest a zip entry can express. A constant rather than the
#: real mtime: the clock is the thing that makes two identical packages differ.
#: Zip's DOS timestamps cannot represent anything earlier, so this is the
#: floor rather than an arbitrary choice.
_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class PackageEntry:
    """One file destined for the archive."""

    #: Path inside the zip, using forward slashes. Sorted on, so it also
    #: decides the archive's byte order.
    path: str
    data: bytes


def manifest_for(
    entries: list[PackageEntry] | tuple[PackageEntry, ...],
    *,
    project: dict[str, Any],
    video: dict[str, Any],
    caption: dict[str, Any],
) -> dict[str, Any]:
    """The manifest, as a plain dict.

    Separate from :func:`build_package` so the stage can store it in the
    version's ``meta`` — a reviewer should be able to see what is in the package
    without downloading and unzipping it, which is the whole point of M5-04.

    ``files`` is sorted by path, matching the archive: a manifest whose order
    differed from the zip's would invite a reader to assume one from the other.
    """
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "project": project,
        "video": video,
        "caption": caption,
        "files": [
            {
                "path": entry.path,
                "sha256": sha256_bytes(entry.data),
                "bytes": len(entry.data),
            }
            for entry in sorted(entries, key=lambda e: e.path)
        ],
    }


def build_package(
    entries: list[PackageEntry] | tuple[PackageEntry, ...],
    *,
    manifest: dict[str, Any],
) -> bytes:
    """Zip ``entries`` plus ``manifest``, deterministically.

    The manifest is written **last** but describes only the other entries: a
    file cannot carry its own hash, and including a placeholder for it would be
    a field that is always wrong.

    Raises on a duplicate path. A zip can hold two entries with one name and
    most readers silently keep the last, so a packager that produced one would
    ship a video missing a scene with no error anywhere.
    """
    ordered = sorted(entries, key=lambda e: e.path)

    seen: set[str] = set()
    for entry in ordered:
        if entry.path in seen:
            raise ValueError(
                f"two package entries claim {entry.path!r}; most zip readers "
                "would silently keep one and the package would be wrong"
            )
        seen.add(entry.path)
    if MANIFEST_NAME in seen:
        raise ValueError(f"{MANIFEST_NAME} is written by the packager, not supplied")

    buffer = io.BytesIO()
    # ``ZIP_DEFLATED`` at a pinned level. zlib's output for a given level is
    # stable within a build, which is what keeps the archive byte-identical;
    # leaving the level implicit would tie the bytes to a default that can move
    # between Python versions.
    with zipfile.ZipFile(
        buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for entry in ordered:
            archive.writestr(_info(entry.path), entry.data)
        archive.writestr(
            _info(MANIFEST_NAME),
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )
    return buffer.getvalue()


def _info(path: str) -> zipfile.ZipInfo:
    """A ``ZipInfo`` with the clock taken out of it.

    ``ZipInfo(path)`` alone defaults to ``time.localtime()``, so it carries
    both the moment *and* the packaging machine's timezone into the archive.
    ``external_attr`` is set explicitly for the same reason: left unset it is
    0, which some tools read as mode ``000`` and refuse to extract.
    """
    info = zipfile.ZipInfo(path, date_time=_FIXED_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info
