"""JSON stdout logging + `request_id` correlation (TASK-S2.5).

One JSON object per line, with fixed top-level fields (`timestamp`,
`level`, `logger`, `event`) plus whatever safe fields a call site adds via
`logger.info(event, extra={...})` — callers never format vacancy text,
prompts, LLM answers, DSNs, headers, API keys or chat IDs into `extra`
(TASK-S2.5, «Не входит»); this module has no way to enforce that, it only
provides the mechanism.

`request_id` is set once per HTTP request (`api/app.py`'s middleware) into
a `ContextVar` and attached to every `LogRecord` as it's *created* — via
`logging.setLogRecordFactory`, installed once as an import-time side
effect of this module, not by `configure_json_logging()`. This
distinction matters: the record factory must be active before ANY log
call happens, including in tests that never call `configure_json_logging`
(which only replaces the root logger's *handler*, something a naked
import must not do — it would clobber `pytest`'s own log-capture
handler). A logging `Filter` attached only to our own handler would have
been the more obvious approach, but it would silently do nothing for a
record observed through a *different* handler (exactly what `caplog`
uses) — the record factory runs once per record, before any handler ever
sees it, so it works the same way in production and under `caplog`.
`anyio.to_thread.run_sync` (which FastAPI/Starlette use to run sync `def`
routes, see `api/app.py`'s docstring) copies the current
`contextvars.Context`, so this also works for the project's sync routes,
not just `async def` ones.

Call sites must never pass `request_id` via `extra=` — the factory
already sets it on every record, and stdlib `Logger.makeRecord` raises
`KeyError` if `extra` tries to set a key the record already has (verified
live, not assumed: this is exactly how the first version of this module's
own test suite caught the mistake before it shipped)."""

import json
import logging
import re
import sys
import traceback
from contextvars import ContextVar
from datetime import UTC, datetime

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# What a fresh `logging.LogRecord` already carries — anything else on
# `record.__dict__` came from a caller's `extra={...}` (or our own record
# factory below) and belongs in the JSON payload as its own field.
_STANDARD_RECORD_ATTRS = frozenset(
    logging.LogRecord(
        name="", level=logging.INFO, pathname="", lineno=0, msg="", args=(), exc_info=None
    ).__dict__
) | {"message"}


def is_valid_request_id(value: str | None) -> bool:
    """A safe, bounded shape for a client-supplied `X-Request-ID` — short,
    printable ASCII. An oversized or oddly-encoded header becomes a fresh
    UUID instead of being trusted verbatim into every log line for that
    request."""

    return value is not None and bool(_REQUEST_ID_PATTERN.fullmatch(value))


def get_request_id() -> str | None:
    return _request_id_var.get()


def bind_request_id(request_id: str) -> "_RequestIdToken":
    return _RequestIdToken(_request_id_var.set(request_id))


class _RequestIdToken:
    """Thin wrapper so callers don't need to import `contextvars` — a
    `with`-free `reset()` handle, used from `api/app.py`'s middleware in
    its own `finally` block."""

    def __init__(self, token: object) -> None:
        self._token = token

    def reset(self) -> None:
        _request_id_var.reset(self._token)  # type: ignore[arg-type]


def _install_request_id_record_factory() -> None:
    """Wraps whatever record factory is currently installed so every new
    `LogRecord` picks up the ambient `request_id`, if any — idempotent:
    checks a marker attribute so re-importing this module (or an
    explicit second call) never stacks the wrapper more than once."""

    current_factory = logging.getLogRecordFactory()
    if getattr(current_factory, "_vacancy_agent_request_id_factory", False):
        return

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = current_factory(*args, **kwargs)
        request_id = _request_id_var.get()
        if request_id is not None:
            record.request_id = request_id
        return record

    factory._vacancy_agent_request_id_factory = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


_install_request_id_record_factory()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            # Только тип исключения и расположение фреймов (file:line in
            # function) — НИКОГДА `str(exc)`/полный `formatException`.
            # `str(exc)` у некоторых исключений содержит DSN, заголовки
            # или содержимое запроса (например, driver connection errors,
            # httpx `HTTPStatusError`) — TASK-S2.5, «Не входит» запрещает
            # секретам попадать в логи, и этого нельзя гарантировать для
            # исключения, которое этот модуль не контролирует. Тот же
            # принцип, что и в `api/app.py`'s `ready()`: логируем
            # `type(exc).__name__`, не `exc`.
            exc_type, exc_value, exc_tb = record.exc_info
            payload["exc_type"] = exc_type.__name__ if exc_type is not None else None
            payload["exc_trace"] = [
                f"{frame.filename}:{frame.lineno} in {frame.name}"
                for frame in traceback.extract_tb(exc_tb)
            ]
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_json_logging(level: int = logging.INFO) -> None:
    """Replaces the root logger's handlers with a single stdout handler
    using `JsonFormatter` — called once from each process entrypoint
    (`api/main.py`, `worker.py`) instead of `logging.basicConfig(format=...)`.
    Idempotent: safe to call more than once (e.g. in tests). Does NOT
    install the `request_id` record factory — that happens unconditionally
    at import time, see the module docstring."""

    # Явно `sys.stdout` — `logging.StreamHandler()` без аргумента пишет в
    # `sys.stderr`, что противоречило бы TASK-S2.5 (stdout JSON logs) и
    # обычным ожиданиям log-агрегаторов (structured stdout, unstructured
    # stderr).
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
