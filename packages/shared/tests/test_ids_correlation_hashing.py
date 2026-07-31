"""Unit tests for ids, correlation, and hashing (M0-05)."""

from __future__ import annotations

import hashlib
import io
import time

from videoforge_shared.correlation import (
    CORRELATION_HEADER,
    correlation_context,
    correlation_headers,
    ensure_correlation_id,
    extract_correlation_id,
    get_correlation_id,
)
from videoforge_shared.hashing import (
    content_key,
    safe_filename,
    sha256_bytes,
    sha256_from_key,
    sha256_stream,
)
from videoforge_shared.ids import ULID_ALPHABET, ULID_LENGTH, is_ulid, new_ulid


class TestUlids:
    def test_shape(self) -> None:
        ulid = new_ulid()
        assert len(ulid) == ULID_LENGTH
        assert all(c in ULID_ALPHABET for c in ulid)
        assert is_ulid(ulid)

    def test_uniqueness(self) -> None:
        batch = {new_ulid() for _ in range(1000)}
        assert len(batch) == 1000

    def test_time_ordering_across_ticks(self) -> None:
        """The timestamp prefix must make later ULIDs sort later."""
        first = new_ulid()
        time.sleep(0.002)  # > 1ms so the timestamp component advances
        second = new_ulid()
        assert first < second

    def test_is_ulid_rejects_junk(self) -> None:
        assert not is_ulid("")
        assert not is_ulid("not-a-ulid")
        assert not is_ulid("I" * ULID_LENGTH)  # I is not in Crockford base32


class TestCorrelation:
    def test_unbound_by_default(self) -> None:
        assert get_correlation_id() is None
        assert correlation_headers() == {}

    def test_context_binds_and_restores(self) -> None:
        with correlation_context("cid-outer") as outer:
            assert outer == "cid-outer"
            assert get_correlation_id() == "cid-outer"
            with correlation_context("cid-inner"):
                assert get_correlation_id() == "cid-inner"
            # Restore matters: long-lived workers must not leak one job's id
            # into the next job's logs.
            assert get_correlation_id() == "cid-outer"
        assert get_correlation_id() is None

    def test_context_mints_when_not_given(self) -> None:
        with correlation_context() as cid:
            assert is_ulid(cid)

    def test_ensure_creates_once(self) -> None:
        with correlation_context("preset"):
            assert ensure_correlation_id() == "preset"

    def test_headers_round_trip(self) -> None:
        with correlation_context("cid-123"):
            headers = correlation_headers()
        assert headers == {CORRELATION_HEADER: "cid-123"}
        # WSGI-style lowercase on the receiving side must still resolve.
        lowered = {k.lower(): v for k, v in headers.items()}
        assert extract_correlation_id(lowered) == "cid-123"

    def test_extract_ignores_empty_values(self) -> None:
        assert extract_correlation_id({"X-Request-Id": ""}) is None
        assert extract_correlation_id({}) is None


class TestHashing:
    def test_known_vector(self) -> None:
        # sha256("abc") is a published test vector (FIPS 180-2).
        assert sha256_bytes(b"abc") == (
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )

    def test_stream_equals_bytes(self) -> None:
        data = b"x" * (3 * 1024 * 1024 + 17)  # spans multiple chunks, odd tail
        assert sha256_stream(io.BytesIO(data)) == sha256_bytes(data)

    def test_content_key_shape(self) -> None:
        digest = hashlib.sha256(b"payload").hexdigest()
        key = content_key(digest, "scene0.png")
        assert key == f"{digest[:2]}/{digest}/scene0.png"

    def test_content_key_rejects_non_digest(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="sha256"):
            content_key("nope", "f.png")

    def test_filename_sanitised(self) -> None:
        digest = hashlib.sha256(b"p").hexdigest()
        key = content_key(digest, "../../etc/passwd name?.png")
        assert key.endswith("/etc_passwd_name_.png")
        assert ".." not in key

    def test_safe_filename_never_empty(self) -> None:
        assert safe_filename("???") == "file"

    def test_sha_recoverable_from_key(self) -> None:
        digest = hashlib.sha256(b"p").hexdigest()
        assert sha256_from_key(content_key(digest, "a.mp4")) == digest
        assert sha256_from_key("music/calm-01.mp3") is None
