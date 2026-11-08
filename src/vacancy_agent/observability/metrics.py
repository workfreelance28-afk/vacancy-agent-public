"""Process-local metrics registry (TASK-S2.5).

`MetricsRegistry` holds two kinds of series, both with a **fixed, small
set of metric names** and **bounded label values** (HTTP method, route
template, status code, scan-job outcome, a coarse error category derived
from `error_code` — never a raw vacancy ID, job UUID, idempotency key, or
exception message): a counter map and a latency summary map. There is no
cross-process aggregation — the API and the worker each hold their own
instance; only the API's is reachable, via `GET /api/v1/metrics`.

Every public method swallows its own internal errors (logs a warning,
never raises) — TASK-S2.5 criterion #4: a bug in the observability code
itself must not change the HTTP status a route returns or the outcome a
worker records for a job. This is the one place that guarantee needs to
be enforced, rather than wrapping every call site."""

import logging
import threading
from dataclasses import dataclass, field

logger = logging.getLogger("vacancy_agent.observability.metrics")

Labels = dict[str, str]


def _label_key(labels: Labels) -> str:
    """Canonical, sorted `name=value,...` string — the same labels always
    produce the same key regardless of the order they were passed in."""

    return ",".join(f"{name}={value}" for name, value in sorted(labels.items()))


def error_category(error_code: str) -> str:
    """Collapses a `TransientScanJobError`/`PermanentScanJobError`
    `error_code` (`application/scan_worker.py`) to the bounded prefix
    before its first `:` — e.g. `"vacancy_processing_failed:qwen_failed=2,
    send_failed=0"` (TASK-S2.2 review-fix, embeds live counters) becomes
    `"vacancy_processing_failed"`. Plain exception-class codes like
    `"OSError"` pass through unchanged. Using the raw `error_code` as a
    metric label would be an unbounded-cardinality label — safe for a log
    field, not for a metric (TASK-S2.5, criterion #3)."""

    return error_code.split(":", 1)[0]


@dataclass
class _LatencySummary:
    count: int = 0
    total_seconds: float = 0.0
    max_seconds: float = 0.0

    def record(self, seconds: float) -> None:
        self.count += 1
        self.total_seconds += seconds
        self.max_seconds = max(self.max_seconds, seconds)

    def to_dict(self) -> dict[str, float | int]:
        avg = self.total_seconds / self.count if self.count else 0.0
        return {
            "count": self.count,
            "avg_seconds": round(avg, 4),
            "max_seconds": round(self.max_seconds, 4),
        }


@dataclass
class MetricsRegistry:
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _counters: dict[str, dict[str, int]] = field(default_factory=dict)
    _latencies: dict[str, _LatencySummary] = field(default_factory=dict)

    def increment(self, name: str, labels: Labels | None = None) -> None:
        try:
            key = _label_key(labels or {})
            with self._lock:
                series = self._counters.setdefault(name, {})
                series[key] = series.get(key, 0) + 1
        except Exception:
            logger.warning("metrics.increment failed for %s", name, exc_info=True)

    def observe_latency(self, name: str, seconds: float) -> None:
        try:
            with self._lock:
                summary = self._latencies.setdefault(name, _LatencySummary())
                summary.record(seconds)
        except Exception:
            logger.warning("metrics.observe_latency failed for %s", name, exc_info=True)

    def snapshot(self) -> dict[str, object]:
        try:
            with self._lock:
                counters = {name: dict(series) for name, series in self._counters.items()}
                latencies = {name: summary.to_dict() for name, summary in self._latencies.items()}
            return {"counters": counters, "latencies": latencies}
        except Exception:
            logger.warning("metrics.snapshot failed", exc_info=True)
            return {"counters": {}, "latencies": {}}
