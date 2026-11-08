"""Unit-тесты `worker.py` wiring-логики (TASK-S2.2 review-fix, round 2):

- [P1] «Реальные временные сбои обходят retry очереди» — `_make_runner`
  должен заметить непустой `ScanRunSummary.failed_sources` (частичный сбой
  источника, который `application/wiring.py: _run_source_scans` перехватывает
  сам и никогда не пробрасывает наружу `run_scan_once`) и поднять
  `TransientScanJobError`, а не тихо вернуть его как success.
- [P2] «Ошибка создания queue оставляет ресурсы открытыми» — `run()` должен
  закрыть уже открытый `RuntimeComponents`, если `create_scan_job_queue`
  после него бросает исключение.

Тот же приём monkeypatch, что `tests/unit/test_scheduler.py` для
`scheduler._run_scan_job` — не открывает реальный PostgreSQL/SuperJob/Habr/
Qwen/Telegram."""

from dataclasses import dataclass
from typing import Any

import pytest

import vacancy_agent.worker as worker_module
from vacancy_agent.application.wiring import ScanRunSummary
from vacancy_agent.domain.scan_job_errors import TransientScanJobError


@dataclass
class _FakeSettings:
    """Достаточно `run()`, что читает только `settings.database` (аргумент
    для `RuntimeComponents`/`create_scan_job_queue`) — не настоящий
    `config.settings.Settings`."""

    database: object = None


async def test_runner_raises_transient_when_a_source_partially_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_scan_once(components: object) -> ScanRunSummary:
        return ScanRunSummary(fetched=2, notified=1, failed_sources=("superjob",))

    monkeypatch.setattr(worker_module, "run_scan_once", fake_run_scan_once)
    runner = worker_module._make_runner(object())  # type: ignore[arg-type]

    with pytest.raises(TransientScanJobError) as exc_info:
        await runner()

    assert "superjob" in exc_info.value.error_code


async def test_runner_raises_transient_listing_all_failed_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Несколько неуспешных источников (combined `superjob_habr`, TASK-015)
    — `error_code` должен называть все, не только первый."""

    async def fake_run_scan_once(components: object) -> ScanRunSummary:
        return ScanRunSummary(failed_sources=("superjob", "habr"))

    monkeypatch.setattr(worker_module, "run_scan_once", fake_run_scan_once)
    runner = worker_module._make_runner(object())  # type: ignore[arg-type]

    with pytest.raises(TransientScanJobError) as exc_info:
        await runner()

    assert "superjob" in exc_info.value.error_code
    assert "habr" in exc_info.value.error_code


async def test_runner_raises_transient_when_qwen_failed_is_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_daily_scan` (`application/daily_scan.py`) перехватывает
    `QwenClientError` само на уровне одной вакансии и продолжает —
    `qwen_failed` растёт, но ошибка не пробрасывается наружу
    `run_scan_once`. Без явной проверки этот транзиентный сбой тихо
    засчитывался бы success (review-fix TASK-S2.2 round 2 [P1])."""

    async def fake_run_scan_once(components: object) -> ScanRunSummary:
        return ScanRunSummary(fetched=5, accepted=3, qwen_failed=2, notified=1)

    monkeypatch.setattr(worker_module, "run_scan_once", fake_run_scan_once)
    runner = worker_module._make_runner(object())  # type: ignore[arg-type]

    with pytest.raises(TransientScanJobError) as exc_info:
        await runner()

    assert "qwen_failed=2" in exc_info.value.error_code


async def test_runner_raises_transient_when_send_failed_is_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Тот же провал на шаге отправки в Telegram
    (`TelegramNotifierError`/`send_failed`)."""

    async def fake_run_scan_once(components: object) -> ScanRunSummary:
        return ScanRunSummary(fetched=5, accepted=3, send_failed=1)

    monkeypatch.setattr(worker_module, "run_scan_once", fake_run_scan_once)
    runner = worker_module._make_runner(object())  # type: ignore[arg-type]

    with pytest.raises(TransientScanJobError) as exc_info:
        await runner()

    assert "send_failed=1" in exc_info.value.error_code


async def test_runner_returns_result_when_no_source_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_scan_once(components: object) -> ScanRunSummary:
        return ScanRunSummary(fetched=3, notified=2)

    monkeypatch.setattr(worker_module, "run_scan_once", fake_run_scan_once)
    runner = worker_module._make_runner(object())  # type: ignore[arg-type]

    result = await runner()

    assert result == {
        "fetched": 3,
        "already_known": 0,
        "filtered_out": 0,
        "accepted": 0,
        "qwen_failed": 0,
        "qwen_rejected": 0,
        "notified": 2,
        "send_failed": 0,
        "per_source": {},
        "failed_sources": [],
    }


async def test_run_closes_components_when_queue_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []

    class _FakeComponents:
        def close(self) -> None:
            closed.append(True)

    def fake_runtime_components(settings: object) -> _FakeComponents:
        return _FakeComponents()

    def fake_create_scan_job_queue(settings: object) -> Any:
        raise RuntimeError("postgresql://user:pass@host/db unreachable")

    monkeypatch.setattr(worker_module, "load_settings", lambda: _FakeSettings())
    monkeypatch.setattr(worker_module, "RuntimeComponents", fake_runtime_components)
    monkeypatch.setattr(worker_module, "create_scan_job_queue", fake_create_scan_job_queue)

    with pytest.raises(RuntimeError, match="unreachable"):
        await worker_module.run()

    assert closed == [True]


async def test_run_closes_components_even_when_queue_close_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review-fix TASK-S2.2 round 2 [P2] «Queue close can prevent
    component cleanup»: `queue.close()` бросает (например, соединение уже
    разорвано) — `components.close()` всё равно должен быть вызван, а не
    пропущен из-за необработанного исключения первого `close()` в одном
    `finally`."""

    closed: list[str] = []

    class _FakeComponents:
        def close(self) -> None:
            closed.append("components")

    class _RaisingQueue:
        def close(self) -> None:
            raise RuntimeError("connection already closed")

    async def fake_worker_loop(components: object, queue: object, metrics: object) -> None:
        return None

    monkeypatch.setattr(worker_module, "load_settings", lambda: _FakeSettings())
    monkeypatch.setattr(worker_module, "RuntimeComponents", lambda settings: _FakeComponents())
    monkeypatch.setattr(worker_module, "create_scan_job_queue", lambda settings: _RaisingQueue())
    monkeypatch.setattr(worker_module, "_worker_loop", fake_worker_loop)

    await worker_module.run()

    assert closed == ["components"]


async def test_run_closes_both_components_and_queue_on_clean_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Успешный путь тоже должен закрывать оба ресурса — регрессия не
    должна проявляться только на failure-ветке P2-фикса."""

    closed: list[str] = []

    class _FakeComponents:
        def close(self) -> None:
            closed.append("components")

    class _FakeQueue:
        def close(self) -> None:
            closed.append("queue")

    async def fake_worker_loop(components: object, queue: object, metrics: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(worker_module, "load_settings", lambda: _FakeSettings())
    monkeypatch.setattr(worker_module, "RuntimeComponents", lambda settings: _FakeComponents())
    monkeypatch.setattr(worker_module, "create_scan_job_queue", lambda settings: _FakeQueue())
    monkeypatch.setattr(worker_module, "_worker_loop", fake_worker_loop)

    with pytest.raises(KeyboardInterrupt):
        await worker_module.run()

    assert closed == ["queue", "components"]
