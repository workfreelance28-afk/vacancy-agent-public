import logging
from pathlib import Path

import pytest

import vacancy_agent.main as main_module
from vacancy_agent.application.wiring import ScanRunSummary
from vacancy_agent.config.settings import Settings
from vacancy_agent.main import run

_OPERATIONAL_SCAN_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "SUPERJOB_API_KEY",
    "QWEN_MODEL",
)


def test_run_succeeds_without_secrets() -> None:
    assert run([]) == 0


def test_run_on_invalid_config_does_not_leak_secret_into_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("QWEN_MIN_SCORE", "not-a-number")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret-value")

    with caplog.at_level(logging.ERROR):
        exit_code = run([])

    assert exit_code == 1
    assert "super-secret-value" not in caplog.text


def test_run_rejects_unknown_command() -> None:
    assert run(["not-a-real-command"]) == 1


def test_run_scan_rejects_extra_arguments() -> None:
    assert run(["scan", "--extra"]) == 1


class _FakeBotSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeBot:
    def __init__(self) -> None:
        self.session = _FakeBotSession()


class _FakeComponents:
    """Замена `RuntimeComponents` для тестов CLI — не открывает реальных
    HTTP/SQLite/Telegram-ресурсов, только считает вызовы `close()`."""

    def __init__(self, settings: Settings, *, close_fails: bool = False) -> None:
        self.settings = settings
        self.bot = _FakeBot()
        self.closed = False
        self._close_fails = close_fails

    def close(self) -> None:
        self.closed = True
        if self._close_fails:
            raise RuntimeError("components.close() boom")


def _set_valid_scan_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Изолирует от реального `.env` разработчика и задаёт минимальный
    набор operational-настроек, которых `scan` требует по fail-fast
    проверке — без них тест ушёл бы в `_missing_scan_settings`, а не в
    проверяемый код."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-not-a-real-secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "[123]")
    monkeypatch.setenv("SUPERJOB_API_KEY", "fake-superjob-key")
    monkeypatch.setenv("QWEN_MODEL", "fake-model")


def test_scan_command_fails_fast_without_constructing_components(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Найдено требованием TASK-008 (ADR-025): невалидная operational-
    конфигурация должна быть обнаружена до попытки открыть реальные
    HTTP/SQLite/Telegram-ресурсы, а не провалиться где-то в середине
    `RuntimeComponents.__init__` с менее понятной ошибкой."""

    monkeypatch.chdir(tmp_path)
    for var in _OPERATIONAL_SCAN_VARS:
        monkeypatch.delenv(var, raising=False)

    def _fail_if_constructed(settings: Settings) -> _FakeComponents:
        pytest.fail("RuntimeComponents не должен собираться при невалидном operational-конфиге")

    monkeypatch.setattr(main_module, "RuntimeComponents", _fail_if_constructed)

    with caplog.at_level(logging.ERROR):
        exit_code = run(["scan"])

    assert exit_code == 1
    assert "TELEGRAM_BOT_TOKEN" in caplog.text


def test_scan_command_with_habr_source_fails_fast_on_empty_search_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Codex review-fix [P2] (TASK-013): пустой `HABR_SEARCH_QUERY` при
    `VACANCY_SOURCE=habr` раньше обнаруживался только внутри
    `HabrCareerSource.search_vacancies`, вызванного из `run_daily_scan` —
    то есть уже ПОСЛЕ того, как `RuntimeComponents.__init__` успевал
    создать SQLite/HTTP-клиенты/`LMStudioQwenClient`/Telegram `Bot`.
    Критерий требует понятную ошибку до создания HTTP-клиента, не после —
    `RuntimeComponents` вообще не должен собираться."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VACANCY_SOURCE", "habr")
    monkeypatch.delenv("HABR_SEARCH_QUERY", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-not-a-real-secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "[123]")
    monkeypatch.setenv("QWEN_MODEL", "fake-model")
    # `SUPERJOB_API_KEY` намеренно не задан — не обязателен при `habr`
    # (тот же приём условной проверки, что уже есть для `superjob`).
    monkeypatch.delenv("SUPERJOB_API_KEY", raising=False)

    def _fail_if_constructed(settings: Settings) -> _FakeComponents:
        pytest.fail("RuntimeComponents не должен собираться при пустом HABR_SEARCH_QUERY")

    monkeypatch.setattr(main_module, "RuntimeComponents", _fail_if_constructed)

    with caplog.at_level(logging.ERROR):
        exit_code = run(["scan"])

    assert exit_code == 1
    assert "HABR_SEARCH_QUERY" in caplog.text


def test_scan_command_with_superjob_habr_source_fails_fast_on_missing_superjob_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """TASK-015: «Для VACANCY_SOURCE=superjob_habr до создания ресурсов и
    сетевых запросов проверить одновременно: SUPERJOB_API_KEY; непустой
    HABR_SEARCH_QUERY» — здесь `HABR_SEARCH_QUERY` задан, а
    `SUPERJOB_API_KEY` — нет; `RuntimeComponents` не должен собираться."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VACANCY_SOURCE", "superjob_habr")
    monkeypatch.setenv("HABR_SEARCH_QUERY", "AI интегратор")
    monkeypatch.delenv("SUPERJOB_API_KEY", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-not-a-real-secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "[123]")
    monkeypatch.setenv("QWEN_MODEL", "fake-model")

    def _fail_if_constructed(settings: Settings) -> _FakeComponents:
        pytest.fail("RuntimeComponents не должен собираться при отсутствующем SUPERJOB_API_KEY")

    monkeypatch.setattr(main_module, "RuntimeComponents", _fail_if_constructed)

    with caplog.at_level(logging.ERROR):
        exit_code = run(["scan"])

    assert exit_code == 1
    assert "SUPERJOB_API_KEY" in caplog.text


def test_scan_command_with_superjob_habr_source_fails_fast_on_empty_habr_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VACANCY_SOURCE", "superjob_habr")
    monkeypatch.setenv("SUPERJOB_API_KEY", "fake-superjob-key")
    monkeypatch.delenv("HABR_SEARCH_QUERY", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-not-a-real-secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "[123]")
    monkeypatch.setenv("QWEN_MODEL", "fake-model")

    def _fail_if_constructed(settings: Settings) -> _FakeComponents:
        pytest.fail("RuntimeComponents не должен собираться при пустом HABR_SEARCH_QUERY")

    monkeypatch.setattr(main_module, "RuntimeComponents", _fail_if_constructed)

    with caplog.at_level(logging.ERROR):
        exit_code = run(["scan"])

    assert exit_code == 1
    assert "HABR_SEARCH_QUERY" in caplog.text


def test_scan_command_returns_1_when_a_source_failed_partially(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """TASK-015: «если хотя бы один источник не завершился, итог помечается
    частично неуспешным; CLI... возвращает exit code 1» — даже когда
    другой источник обработан полностью и прогон в остальном успешен."""

    _set_valid_scan_env(monkeypatch, tmp_path)

    async def fake_run_scan_once(components: _FakeComponents) -> ScanRunSummary:
        return ScanRunSummary(fetched=2, notified=1, failed_sources=("habr",))

    monkeypatch.setattr(main_module, "RuntimeComponents", _FakeComponents)
    monkeypatch.setattr(main_module, "run_scan_once", fake_run_scan_once)

    with caplog.at_level(logging.ERROR):
        exit_code = run(["scan"])

    assert exit_code == 1
    assert "habr" in caplog.text


def test_scan_command_returns_0_when_no_source_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_valid_scan_env(monkeypatch, tmp_path)

    async def fake_run_scan_once(components: _FakeComponents) -> ScanRunSummary:
        return ScanRunSummary(fetched=2, notified=2)

    monkeypatch.setattr(main_module, "RuntimeComponents", _FakeComponents)
    monkeypatch.setattr(main_module, "run_scan_once", fake_run_scan_once)

    assert run(["scan"]) == 0


def test_scan_command_succeeds_without_real_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_valid_scan_env(monkeypatch, tmp_path)
    created: list[_FakeComponents] = []

    def fake_runtime_components(settings: Settings) -> _FakeComponents:
        components = _FakeComponents(settings)
        created.append(components)
        return components

    async def fake_run_scan_once(components: _FakeComponents) -> ScanRunSummary:
        return ScanRunSummary(fetched=3, accepted=1, notified=1)

    monkeypatch.setattr(main_module, "RuntimeComponents", fake_runtime_components)
    monkeypatch.setattr(main_module, "run_scan_once", fake_run_scan_once)

    exit_code = run(["scan"])

    assert exit_code == 0
    assert len(created) == 1


def test_scan_command_closes_resources_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_valid_scan_env(monkeypatch, tmp_path)
    created: list[_FakeComponents] = []

    def fake_runtime_components(settings: Settings) -> _FakeComponents:
        components = _FakeComponents(settings)
        created.append(components)
        return components

    async def fake_run_scan_once(components: _FakeComponents) -> ScanRunSummary:
        return ScanRunSummary(fetched=1)

    monkeypatch.setattr(main_module, "RuntimeComponents", fake_runtime_components)
    monkeypatch.setattr(main_module, "run_scan_once", fake_run_scan_once)

    assert run(["scan"]) == 0

    assert created[0].closed is True
    assert created[0].bot.session.closed is True


def test_scan_command_closes_resources_when_scan_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Найдено требованием TASK-008 (ADR-025): если сам прогон упал (не
    только конфигурация), уже открытые ресурсы всё равно должны
    закрываться — `finally` в `_scan_once_and_close`, а не только путь
    успеха."""

    _set_valid_scan_env(monkeypatch, tmp_path)
    created: list[_FakeComponents] = []

    def fake_runtime_components(settings: Settings) -> _FakeComponents:
        components = _FakeComponents(settings)
        created.append(components)
        return components

    async def failing_run_scan_once(components: _FakeComponents) -> ScanRunSummary:
        raise RuntimeError("boom")

    monkeypatch.setattr(main_module, "RuntimeComponents", fake_runtime_components)
    monkeypatch.setattr(main_module, "run_scan_once", failing_run_scan_once)

    exit_code = run(["scan"])

    assert exit_code == 1
    assert created[0].closed is True
    assert created[0].bot.session.closed is True


def test_scan_command_closes_bot_session_even_if_components_close_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Найдено ревью TASK-008 (ADR-025): `components.close()` и
    `bot.session.close()` шли последовательно в одном `finally` — если
    первый падал, второй не выполнялся и Telegram-сессия оставалась
    открытой."""

    _set_valid_scan_env(monkeypatch, tmp_path)
    created: list[_FakeComponents] = []

    def fake_runtime_components(settings: Settings) -> _FakeComponents:
        components = _FakeComponents(settings, close_fails=True)
        created.append(components)
        return components

    async def fake_run_scan_once(components: _FakeComponents) -> ScanRunSummary:
        return ScanRunSummary(fetched=1)

    monkeypatch.setattr(main_module, "RuntimeComponents", fake_runtime_components)
    monkeypatch.setattr(main_module, "run_scan_once", fake_run_scan_once)

    exit_code = run(["scan"])

    assert exit_code == 1
    assert created[0].bot.session.closed is True
