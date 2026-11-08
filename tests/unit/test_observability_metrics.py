import pytest

from vacancy_agent.observability.metrics import MetricsRegistry, error_category


def test_increment_counts_by_label_combination() -> None:
    registry = MetricsRegistry()

    registry.increment("scan_jobs_total", {"outcome": "succeeded"})
    registry.increment("scan_jobs_total", {"outcome": "succeeded"})
    registry.increment("scan_jobs_total", {"outcome": "failed"})

    snapshot = registry.snapshot()
    assert snapshot["counters"] == {
        "scan_jobs_total": {"outcome=succeeded": 2, "outcome=failed": 1}
    }


def test_increment_label_key_is_order_independent() -> None:
    registry = MetricsRegistry()

    registry.increment("http_requests_total", {"method": "GET", "path": "/x", "status_code": "200"})
    registry.increment("http_requests_total", {"status_code": "200", "path": "/x", "method": "GET"})

    snapshot = registry.snapshot()
    assert snapshot["counters"]["http_requests_total"] == {
        "method=GET,path=/x,status_code=200": 2
    }


def test_increment_without_labels() -> None:
    registry = MetricsRegistry()

    registry.increment("scan_jobs_enqueued_total")
    registry.increment("scan_jobs_enqueued_total")

    assert registry.snapshot()["counters"] == {"scan_jobs_enqueued_total": {"": 2}}


def test_observe_latency_reports_count_avg_and_max() -> None:
    registry = MetricsRegistry()

    registry.observe_latency("scan_job_attempt_duration_seconds", 1.0)
    registry.observe_latency("scan_job_attempt_duration_seconds", 3.0)

    snapshot = registry.snapshot()
    assert snapshot["latencies"] == {
        "scan_job_attempt_duration_seconds": {"count": 2, "avg_seconds": 2.0, "max_seconds": 3.0}
    }


def test_snapshot_is_empty_for_a_fresh_registry() -> None:
    assert MetricsRegistry().snapshot() == {"counters": {}, "latencies": {}}


@pytest.mark.parametrize(
    ("error_code", "expected_category"),
    [
        ("OSError", "OSError"),
        ("vacancy_source_error:superjob", "vacancy_source_error"),
        (
            "vacancy_processing_failed:qwen_failed=2,send_failed=0",
            "vacancy_processing_failed",
        ),
    ],
)
def test_error_category_strips_the_unbounded_suffix(
    error_code: str, expected_category: str
) -> None:
    assert error_category(error_code) == expected_category


def test_registry_methods_do_not_raise_when_internal_state_is_corrupted() -> None:
    """TASK-S2.5, criterion #4: a bug in the observability code itself
    must not propagate into the caller (an HTTP route, a worker job
    outcome) — every public method catches its own internal errors."""

    registry = MetricsRegistry()
    registry._counters = None  # type: ignore[assignment]  # simulate a corrupted internal state
    registry._latencies = None  # type: ignore[assignment]

    registry.increment("scan_jobs_total", {"outcome": "succeeded"})  # must not raise
    registry.observe_latency("scan_job_attempt_duration_seconds", 1.0)  # must not raise
    assert registry.snapshot() == {"counters": {}, "latencies": {}}  # must not raise
