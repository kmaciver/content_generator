"""M5-03 — the archive itself.

Pure: entries in, bytes out. Every claim here is about the *file* a person
downloads, which is the one artifact that leaves the system entirely — after
this point nothing in the pipeline can correct it.

Two properties carry the ticket. The archive is **deterministic**, so a package
is an ordinary content-addressed object and "did anything change?" has an
answer. And the manifest **verifies** rather than describes: a zip already
lists its own entries, so a manifest that only repeated the names would earn
nothing.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import pytest

from videoforge_shared.hashing import sha256_bytes
from videoforge_workers.packaging import (
    MANIFEST_NAME,
    PackageEntry,
    build_package,
    manifest_for,
)

_ENTRIES = [
    PackageEntry("video.mp4", b"\x00\x01moov"),
    PackageEntry("cover.png", b"\x89PNG\r\n"),
    PackageEntry("caption.txt", b"Most budgets fail in week two."),
    PackageEntry("scenes/scene-002.png", b"two"),
    PackageEntry("scenes/scene-001.png", b"one"),
]


def _manifest() -> dict[str, Any]:
    return manifest_for(
        _ENTRIES,
        project={"id": "01PROJECT", "topic": "budgets", "title": None},
        video={"duration_ms": 22600, "width": 1080, "height": 1920, "scenes": 4},
        caption={"hook": "Why budgets fail", "characters": 30, "hashtags": ["money"]},
    )


def _open(archive: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(archive))


class TestArchive:
    def test_every_entry_is_present_and_intact(self) -> None:
        with _open(build_package(_ENTRIES, manifest=_manifest())) as zf:
            for entry in _ENTRIES:
                assert zf.read(entry.path) == entry.data

    def test_entries_are_sorted_by_path(self) -> None:
        """Insertion order would make the archive depend on the order the
        stage happened to read storage in, which is not a property anyone
        intends to depend on."""
        with _open(build_package(_ENTRIES, manifest=_manifest())) as zf:
            names = [n for n in zf.namelist() if n != MANIFEST_NAME]
        assert names == sorted(names)

    def test_the_archive_is_valid(self) -> None:
        """``testzip`` walks every CRC. A packager that wrote a corrupt member
        would otherwise be discovered by whoever downloaded it."""
        with _open(build_package(_ENTRIES, manifest=_manifest())) as zf:
            assert zf.testzip() is None

    def test_a_duplicate_path_is_refused(self) -> None:
        """Most zip readers silently keep one of two same-named entries, so a
        package built with a collision would ship a video missing a scene with
        no error anywhere."""
        with pytest.raises(ValueError, match="two package entries"):
            build_package(
                [PackageEntry("a.png", b"1"), PackageEntry("a.png", b"2")],
                manifest=_manifest(),
            )

    def test_the_manifest_name_is_reserved(self) -> None:
        with pytest.raises(ValueError, match="written by the packager"):
            build_package([PackageEntry(MANIFEST_NAME, b"{}")], manifest=_manifest())


class TestDeterminism:
    def test_the_same_inputs_give_the_same_bytes(self) -> None:
        """What makes a package a content-addressed artifact like everything
        else — and what makes deduplication work rather than storing a fresh
        copy of an unchanged archive on every run."""
        assert build_package(_ENTRIES, manifest=_manifest()) == build_package(
            _ENTRIES, manifest=_manifest()
        )

    def test_input_order_does_not_change_the_bytes(self) -> None:
        """The stage reads storage in whatever order the scene rows come back.
        That must not reach the archive."""
        assert build_package(_ENTRIES, manifest=_manifest()) == build_package(
            list(reversed(_ENTRIES)), manifest=_manifest()
        )

    def test_no_clock_reaches_the_archive(self) -> None:
        """``ZipInfo(path)`` defaults to ``time.localtime()``, which puts both
        the moment *and* the packaging machine's timezone into every entry."""
        with _open(build_package(_ENTRIES, manifest=_manifest())) as zf:
            stamps = {info.date_time for info in zf.infolist()}
        assert stamps == {(1980, 1, 1, 0, 0, 0)}

    def test_changed_content_changes_the_bytes(self) -> None:
        """The other half: identity that ignored its input would make every
        package in the workspace the same object."""
        other = [*_ENTRIES[:-1], PackageEntry("scenes/scene-001.png", b"different")]
        assert build_package(_ENTRIES, manifest=_manifest()) != build_package(
            other, manifest=_manifest()
        )


class TestManifest:
    def test_it_hashes_every_entry(self) -> None:
        """**The reason the manifest exists.** A zip lists its own names; what
        it cannot tell a recipient is whether the bytes are the ones that were
        packaged."""
        manifest = _manifest()
        by_path = {f["path"]: f for f in manifest["files"]}

        assert set(by_path) == {e.path for e in _ENTRIES}
        for entry in _ENTRIES:
            assert by_path[entry.path]["sha256"] == sha256_bytes(entry.data)
            assert by_path[entry.path]["bytes"] == len(entry.data)

    def test_it_does_not_describe_itself(self) -> None:
        """A file cannot carry its own hash, and a placeholder for it would be
        a field that is always wrong."""
        assert MANIFEST_NAME not in {f["path"] for f in _manifest()["files"]}

    def test_files_are_listed_in_archive_order(self) -> None:
        """A manifest whose order differed from the zip's would invite a reader
        to assume one from the other."""
        manifest = _manifest()
        paths = [f["path"] for f in manifest["files"]]
        assert paths == sorted(paths)

    def test_the_manifest_in_the_archive_is_the_one_returned(self) -> None:
        """The stage stores this dict in ``meta`` so a reviewer can read the
        contents without downloading. If the two could differ, the review
        screen would be describing a different file."""
        manifest = _manifest()
        with _open(build_package(_ENTRIES, manifest=manifest)) as zf:
            assert json.loads(zf.read(MANIFEST_NAME)) == manifest

    def test_the_hashes_match_the_bytes_actually_archived(self) -> None:
        """End to end, and the assertion that would catch a packager that
        hashed one thing and wrote another."""
        manifest = _manifest()
        with _open(build_package(_ENTRIES, manifest=manifest)) as zf:
            for record in manifest["files"]:
                assert sha256_bytes(zf.read(record["path"])) == record["sha256"]
