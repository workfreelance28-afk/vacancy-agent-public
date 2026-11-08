import logging

import pytest

import vacancy_agent.scheduler as scheduler_module
from vacancy_agent.application.wiring import ScanRunSummary
from vacancy_agent.config.settings import ScheduleSettings
from vacancy_agent.scheduler import _run_scan_job, build_daily_trigger


def test_build_daily_trigger_uses_configured_timezone_explicitly() -> None:
    """`CronTrigger` без явного `timezone` резолвит часовой пояс хоста в
    момент создания — на сервере с другим системным TZ расписание тихо
    сдвинулось бы (см. `docs/DECISIONS.md`, ADR-012, «Уточнения по итогам
    ревью»). Проверяем, что часовой пояс триггера — это именно
    `ScheduleSettings.timezone`, а не что-то унаследованное неявно."""

    trigger = build_daily_trigger(ScheduleSettings(daily_run_hour=9, timezone="Europe/Moscow"))

    assert str(trigger.timezone) == "Europe/Moscow"


def test_build_daily_trigger_respects_a_different_timezone() -> None:
    trigger = build_daily_trigger(ScheduleSettings(daily_run_hour=9, timezone="UTC"))

    assert str(trigger.timezone) == "UTC"


def test_build_daily_trigger_uses_configured_hour() -> None:
    trigger = build_daily_trigger(ScheduleSettings(daily_run_hour=14, timezone="UTC"))

    hour_field = next(f for f in trigger.fields if f.name == "hour")
    assert str(hour_field) == "14"


async def test_run_scan_job_delegates_to_shared_run_scan_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`scheduler._run_scan_job` должен переиспользовать
    `application.wiring.run_scan_once` — ту же функцию, что и CLI-команда
    `vacancy-agent scan` (TASK-008, ADR-025) — а не собирать вызов
    `run_daily_scan` заново. Проверяем через monkeypatch, а не читая
    исходный код, чтобы тест ловил регресс, если кто-то вернёт дублирующий
    вызов `run_daily_scan` прямо в `scheduler.py`."""

    received: list[object] = []

    async def fake_run_scan_once(components: object) -> ScanRunSummary:
        received.append(components)
        return ScanRunSummary(fetched=1)

    monkeypatch.setattr(scheduler_module, "run_scan_once", fake_run_scan_once)

    sentinel_components = object()
    await _run_scan_job(sentinel_components)  # type: ignore[arg-type]

    assert received == [sentinel_components]


async def test_run_scan_job_logs_info_when_no_source_failed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_run_scan_once(components: object) -> ScanRunSummary:
        return ScanRunSummary(fetched=1, notified=1)

    monkeypatch.setattr(scheduler_module, "run_scan_once", fake_run_scan_once)

    with caplog.at_level(logging.INFO):
        await _run_scan_job(object())  # type: ignore[arg-type]

    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert any(record.levelno == logging.INFO for record in caplog.records)


async def test_run_scan_job_logs_error_when_a_source_failed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Codex review-fix [P2] (TASK-015): `run_scan_once` может вернуть
    непустой `failed_sources` (частичный успех combined-прогона), но
    `_run_scan_job` раньше логировал итог тем же `logger.info`, что и
    полный успех — планировщик не отличал бы частично неуспешный
    ежедневный прогон от обычного в логах/мониторинге, вопреки TASK-015.
    `main.py: _run_scan_command` уже делал это различие через exit code —
    здесь у job нет exit code, поэтому именно уровень лога обязан
    сигнализировать проблему."""

    async def fake_run_scan_once(components: object) -> ScanRunSummary:
        return ScanRunSummary(fetched=2, notified=1, failed_sources=("habr",))

    monkeypatch.setattr(scheduler_module, "run_scan_once", fake_run_scan_once)

    with caplog.at_level(logging.INFO):
        await _run_scan_job(object())  # type: ignore[arg-type]

    error_records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert error_records
    assert "habr" in error_records[0].getMessage()
    assert not any(record.levelno == logging.INFO for record in caplog.records)
