"""Unit tests for structured logging (M0-05)."""

from __future__ import annotations

import io
import json
import logging

import pytest

from videoforge_shared.correlation import correlation_context
from videoforge_shared.logging import configure_logging


@pytest.fixture()
def capture() -> tuple[io.StringIO, logging.Logger]:
    stream = io.StringIO()
    configure_logging(level="DEBUG", fmt="json", stream=stream)
    return stream, logging.getLogger("videoforge.test")


def _last_line(stream: io.StringIO) -> dict[str, object]:
    lines = [line for line in stream.getvalue().splitlines() if line]
    return dict(json.loads(lines[-1]))


class TestJsonFormat:
    def test_basic_shape(self, capture: tuple[io.StringIO, logging.Logger]) -> None:
        stream, logger = capture
        logger.info("hello world")
        payload = _last_line(stream)
        assert payload["msg"] == "hello world"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "videoforge.test"
        assert "ts" in payload
        assert "correlation_id" not in payload  # unbound context

    def test_extra_fields_pass_through(
        self, capture: tuple[io.StringIO, logging.Logger]
    ) -> None:
        stream, logger = capture
        logger.info("job started", extra={"job_id": "01ABC", "queue": "llm"})
        payload = _last_line(stream)
        assert payload["job_id"] == "01ABC"
        assert payload["queue"] == "llm"

    def test_correlation_id_injected_from_context(
        self, capture: tuple[io.StringIO, logging.Logger]
    ) -> None:
        stream, logger = capture
        with correlation_context("cid-log-test"):
            logger.info("inside")
        logger.info("outside")
        lines = [json.loads(line) for line in stream.getvalue().splitlines() if line]
        assert lines[0]["correlation_id"] == "cid-log-test"
        assert "correlation_id" not in lines[1]

    def test_unserialisable_extra_degrades_to_str(
        self, capture: tuple[io.StringIO, logging.Logger]
    ) -> None:
        stream, logger = capture
        logger.info("odd", extra={"payload": {1, 2}})  # sets are not JSON
        assert "payload" in _last_line(stream)

    def test_exception_included(
        self, capture: tuple[io.StringIO, logging.Logger]
    ) -> None:
        stream, logger = capture
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("failed")
        payload = _last_line(stream)
        exc_text = str(payload["exc"])
        assert "ValueError" in exc_text
        assert "boom" in exc_text


class TestConfiguration:
    def test_reconfigure_does_not_stack_handlers(self) -> None:
        first = configure_logging(stream=io.StringIO())
        second = configure_logging(stream=io.StringIO())
        root = logging.getLogger()
        ours = [h for h in root.handlers if getattr(h, "_videoforge_handler", False)]
        assert ours == [second]
        assert first not in root.handlers

    def test_pretty_format_is_line_oriented(self) -> None:
        stream = io.StringIO()
        configure_logging(level="INFO", fmt="pretty", stream=stream)
        with correlation_context("cid-pretty"):
            logging.getLogger("videoforge.test").info(
                "readable", extra={"job_id": "01X"}
            )
        line = stream.getvalue().strip()
        assert "readable" in line
        assert "cid=cid-pretty" in line
        assert "job_id=01X" in line
        assert not line.startswith("{")  # decidedly not JSON
