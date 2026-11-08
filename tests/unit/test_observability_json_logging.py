import json
import logging
import sys

import pytest

from vacancy_agent.observability.json_logging import (
    JsonFormatter,
    bind_request_id,
    configure_json_logging,
    get_request_id,
    is_valid_request_id,
)


def _logger_with_json_handler(name: str) -> tuple[logging.Logger, list[str]]:
    """A logger wired to `JsonFormatter`, capturing formatted output into
    a plain list instead of stdout — isolated from pytest's own logging
    capture, which reads `LogRecord`s, not formatted text."""

    lines: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    logger = logging.getLogger(name)
    logger.handlers = [_ListHandler()]
    logger.handlers[0].setFormatter(JsonFormatter())
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, lines


def test_json_formatter_produces_one_parseable_json_object_with_fixed_fields() -> None:
    logger, lines = _logger_with_json_handler("test.json_logging.fixed_fields")

    logger.info("scan_job.claimed", extra={"job_id": "abc-123", "attempt": 2})

    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "scan_job.claimed"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.json_logging.fixed_fields"
    assert "timestamp" in payload
    assert payload["job_id"] == "abc-123"
    assert payload["attempt"] == 2


def test_json_formatter_includes_request_id_while_bound() -> None:
    logger, lines = _logger_with_json_handler("test.json_logging.request_id")

    token = bind_request_id("req-xyz")
    try:
        logger.info("http.request")
    finally:
        token.reset()
    logger.info("after_unbind")

    with_id = json.loads(lines[0])
    without_id = json.loads(lines[1])
    assert with_id["request_id"] == "req-xyz"
    assert "request_id" not in without_id


def test_passing_request_id_via_extra_raises_instead_of_silently_colliding() -> None:
    """Documents a real stdlib constraint discovered while building this
    module (an earlier version of this test assumed `extra` would
    silently win over the ambient value — it doesn't): once the record
    factory has set `record.request_id`, `Logger.makeRecord` raises
    `KeyError` if `extra` tries to set that same key again. Call sites in
    this codebase must never pass `request_id` via `extra=` — it's always
    already there ambiently while a request/response cycle is in
    progress."""

    logger, _lines = _logger_with_json_handler("test.json_logging.extra_collision")

    token = bind_request_id("ambient-request-id")
    try:
        with pytest.raises(KeyError):
            logger.info("scan_job.enqueued", extra={"request_id": "explicit-request-id"})
    finally:
        token.reset()


def test_json_formatter_includes_exc_type_and_trace_but_never_the_exception_message() -> None:
    """`str(exc)` can carry a DSN, a header value, or other secret-shaped
    data the exception's raiser never sanitized (TASK-S2.5, «Не входит»).
    Only the exception's class name and frame locations are safe to log
    unconditionally — never `formatException`'s exception-message line."""

    logger, lines = _logger_with_json_handler("test.json_logging.exc_info")

    try:
        raise ValueError("boom sk-live-should-never-appear")
    except ValueError:
        logger.exception("something.failed")

    payload = json.loads(lines[0])
    assert payload["exc_type"] == "ValueError"
    assert "boom" not in json.dumps(payload)
    assert "sk-live-should-never-appear" not in json.dumps(payload)
    assert any("test_observability_json_logging.py" in frame for frame in payload["exc_trace"])


def test_get_request_id_reflects_currently_bound_value() -> None:
    assert get_request_id() is None
    token = bind_request_id("req-1")
    try:
        assert get_request_id() == "req-1"
    finally:
        token.reset()
    assert get_request_id() is None


def test_is_valid_request_id_accepts_typical_correlation_ids() -> None:
    assert is_valid_request_id("a" * 36) is True  # UUID-length
    assert is_valid_request_id("trace-123.abc:def") is True


def test_is_valid_request_id_rejects_none_empty_and_oversized() -> None:
    assert is_valid_request_id(None) is False
    assert is_valid_request_id("") is False
    assert is_valid_request_id("a" * 201) is False


def test_is_valid_request_id_rejects_control_characters_and_whitespace() -> None:
    assert is_valid_request_id("has space") is False
    assert is_valid_request_id("has\nnewline") is False


def test_configure_json_logging_writes_to_stdout_not_stderr() -> None:
    """Codex review: `logging.StreamHandler()` with no argument defaults to
    `sys.stderr`, contradicting TASK-S2.5's "JSON stdout logs" and the
    usual log-aggregator split (structured stdout, unstructured stderr)."""

    try:
        configure_json_logging()
        (handler,) = logging.getLogger().handlers
        assert handler.stream is sys.stdout  # type: ignore[attr-defined]
    finally:
        logging.getLogger().handlers = []
