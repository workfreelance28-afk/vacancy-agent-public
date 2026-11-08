"""Structured logging + minimal metrics (TASK-S2.5).

`json_logging.py` — one-JSON-object-per-line stdout formatter plus the
`request_id` correlation mechanism (contextvar + logging filter).
`metrics.py` — a small process-local, thread-safe counters/latency
registry with a bounded, fixed set of metric names and label values (no
vacancy text, prompts, DSNs, secrets, or unbounded IDs).

Both `api/main.py` and `worker.py` call `configure_json_logging()` once at
startup; each process holds its own `MetricsRegistry` instance — there is
no cross-process aggregation (no Prometheus/collector in this stage, see
`docs/TASK-S2.5.md`)."""
