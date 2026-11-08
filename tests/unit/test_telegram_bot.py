from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter

from vacancy_agent.config.settings import TelegramSettings
from vacancy_agent.domain.notifier_errors import TelegramSendError
from vacancy_agent.domain.repository_errors import VacancyNotFoundError
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.integrations.telegram_bot import AiogramTelegramNotifier

_NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def _vacancy(**overrides: object) -> Vacancy:
    fields: dict[str, object] = {
        "hh_id": "12345",
        "title": "AI-интегратор",
        "company": "HeadHunter",
        "salary_from": 200_000,
        "salary_to": 250_000,
        "salary_currency": "RUR",
        "salary_gross": True,
        "area": "Москва",
        "work_format": ["Удалённая работа"],
        "experience": "От 1 года до 3 лет",
        "url": "https://hh.ru/vacancy/12345",
        "published_at": _NOW - timedelta(days=1),
        "fetched_at": _NOW,
    }
    fields.update(overrides)
    return Vacancy(**fields)


def _settings(**overrides: object) -> TelegramSettings:
    fields: dict[str, object] = {
        "allowed_chat_ids": [111],
        "max_retries": 2,
        "retry_backoff_seconds": 0.001,
        "request_timeout_seconds": 15.0,
    }
    fields.update(overrides)
    return TelegramSettings(**fields)


class _FakeBot:
    def __init__(self, fail_times: int = 0, exception: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._fail_times = fail_times
        self._exception = exception or TelegramNetworkError(method=None, message="boom")  # type: ignore[arg-type]

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: object = None,
        parse_mode: object = None,
        request_timeout: object = None,
    ) -> None:
        self.calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
                "request_timeout": request_timeout,
            }
        )
        if len(self.calls) <= self._fail_times:
            raise self._exception


class _FakeRepository:
    def __init__(self) -> None:
        self.updates: list[tuple[str, VacancyStatus]] = []
        self.raise_on_update: Exception | None = None

    def exists(self, hh_id: str) -> bool:
        raise NotImplementedError

    def save(self, vacancy: object, filter_result: object, status: object) -> None:
        raise NotImplementedError

    def update_status(self, hh_id: str, status: VacancyStatus) -> None:
        if self.raise_on_update is not None:
            raise self.raise_on_update
        self.updates.append((hh_id, status))


# --- карточка ---


async def test_card_contains_title_company_and_url() -> None:
    bot = _FakeBot()
    notifier = AiogramTelegramNotifier(_settings(), _FakeRepository(), bot=bot)

    await notifier.send_vacancy_card(_vacancy())

    text = str(bot.calls[0]["text"])
    assert "AI-интегратор" in text
    assert "HeadHunter" in text
    assert "https://hh.ru/vacancy/12345" in text


async def test_card_link_caption_is_provider_neutral() -> None:
    """Найдено эксплуатационной приёмкой TASK-008: подпись ссылки была
    захардкожена как «Открыть на HH», хотя `vacancy.url` может вести и на
    SuperJob (см. ADR-013/ADR-014) — подпись должна быть нейтральной, сам
    `url` не меняется."""

    bot = _FakeBot()
    notifier = AiogramTelegramNotifier(_settings(), _FakeRepository(), bot=bot)

    await notifier.send_vacancy_card(
        _vacancy(url="https://superjob.ru/vacancy/ai-integrator-12345.html")
    )

    text = str(bot.calls[0]["text"])
    assert "https://superjob.ru/vacancy/ai-integrator-12345.html" in text
    assert "Открыть вакансию" in text
    assert "Открыть на HH" not in text


async def test_card_escapes_untrusted_vacancy_text() -> None:
    bot = _FakeBot()
    notifier = AiogramTelegramNotifier(_settings(), _FakeRepository(), bot=bot)

    await notifier.send_vacancy_card(_vacancy(title="<script>alert(1)</script>"))

    text = str(bot.calls[0]["text"])
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


async def test_card_keyboard_encodes_hh_id_in_callback_data() -> None:
    bot = _FakeBot()
    notifier = AiogramTelegramNotifier(_settings(), _FakeRepository(), bot=bot)

    await notifier.send_vacancy_card(_vacancy(hh_id="99999"))

    markup = bot.calls[0]["reply_markup"]
    callback_data = [button.callback_data for row in markup.inline_keyboard for button in row]  # type: ignore[union-attr]
    assert "viewed:99999" in callback_data
    assert "skipped:99999" in callback_data


async def test_card_reports_missing_salary() -> None:
    bot = _FakeBot()
    notifier = AiogramTelegramNotifier(_settings(), _FakeRepository(), bot=bot)

    await notifier.send_vacancy_card(_vacancy(salary_from=None, salary_to=None))

    assert "не указана" in str(bot.calls[0]["text"])


async def test_sends_to_every_allowed_chat_id() -> None:
    bot = _FakeBot()
    settings = _settings(allowed_chat_ids=[111, 222])
    notifier = AiogramTelegramNotifier(settings, _FakeRepository(), bot=bot)

    await notifier.send_vacancy_card(_vacancy())

    assert [call["chat_id"] for call in bot.calls] == [111, 222]


async def test_send_uses_configured_request_timeout() -> None:
    bot = _FakeBot()
    settings = _settings(request_timeout_seconds=7.0)
    notifier = AiogramTelegramNotifier(settings, _FakeRepository(), bot=bot)

    await notifier.send_vacancy_card(_vacancy())

    assert bot.calls[0]["request_timeout"] == 7


async def test_send_vacancy_card_raises_when_no_allowed_chat_ids() -> None:
    bot = _FakeBot()
    notifier = AiogramTelegramNotifier(_settings(allowed_chat_ids=[]), _FakeRepository(), bot=bot)

    with pytest.raises(TelegramSendError):
        await notifier.send_vacancy_card(_vacancy())

    assert bot.calls == []


# --- отправка: повторы ---


async def test_send_retries_on_network_error_then_succeeds() -> None:
    bot = _FakeBot(fail_times=1)
    notifier = AiogramTelegramNotifier(_settings(), _FakeRepository(), bot=bot)

    await notifier.send_vacancy_card(_vacancy())

    assert len(bot.calls) == 2


async def test_send_raises_telegram_send_error_after_exhausting_retries() -> None:
    bot = _FakeBot(fail_times=99)
    notifier = AiogramTelegramNotifier(_settings(max_retries=1), _FakeRepository(), bot=bot)

    with pytest.raises(TelegramSendError):
        await notifier.send_vacancy_card(_vacancy())

    assert len(bot.calls) == 2  # исходная попытка + 1 повтор


async def test_send_does_not_retry_on_non_retryable_error() -> None:
    exc = TelegramForbiddenError(method=None, message="bot was blocked")  # type: ignore[arg-type]
    bot = _FakeBot(fail_times=99, exception=exc)
    notifier = AiogramTelegramNotifier(_settings(max_retries=3), _FakeRepository(), bot=bot)

    with pytest.raises(TelegramSendError) as exc_info:
        await notifier.send_vacancy_card(_vacancy())

    assert len(bot.calls) == 1  # без повтора
    assert exc_info.value.__cause__ is exc  # исходная ошибка не потеряна


async def test_send_waits_at_least_retry_after_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "vacancy_agent.integrations.telegram_bot.asyncio.sleep",
        lambda seconds: sleeps.append(seconds) or _immediate(),
    )

    exc = TelegramRetryAfter(method=None, message="slow down", retry_after=5)  # type: ignore[arg-type]
    bot = _FakeBot(fail_times=1, exception=exc)
    notifier = AiogramTelegramNotifier(
        _settings(retry_backoff_seconds=0.001), _FakeRepository(), bot=bot
    )

    await notifier.send_vacancy_card(_vacancy())

    assert sleeps == [5.0]


async def _immediate() -> None:
    return None


# --- callback ---


async def test_handle_callback_updates_status_for_allowed_chat() -> None:
    repository = _FakeRepository()
    notifier = AiogramTelegramNotifier(_settings(), repository, bot=_FakeBot())

    await notifier.handle_callback(111, "viewed:12345")

    assert repository.updates == [("12345", VacancyStatus.VIEWED)]


async def test_handle_callback_maps_skipped_action() -> None:
    repository = _FakeRepository()
    notifier = AiogramTelegramNotifier(_settings(), repository, bot=_FakeBot())

    await notifier.handle_callback(111, "skipped:12345")

    assert repository.updates == [("12345", VacancyStatus.SKIPPED)]


async def test_handle_callback_ignores_disallowed_chat_id() -> None:
    repository = _FakeRepository()
    notifier = AiogramTelegramNotifier(_settings(), repository, bot=_FakeBot())

    await notifier.handle_callback(999, "viewed:12345")

    assert repository.updates == []


async def test_handle_callback_ignores_malformed_payload() -> None:
    repository = _FakeRepository()
    notifier = AiogramTelegramNotifier(_settings(), repository, bot=_FakeBot())

    await notifier.handle_callback(111, "not-a-valid-payload")

    assert repository.updates == []


async def test_handle_callback_ignores_unknown_action() -> None:
    repository = _FakeRepository()
    notifier = AiogramTelegramNotifier(_settings(), repository, bot=_FakeBot())

    await notifier.handle_callback(111, "prepare:12345")

    assert repository.updates == []


async def test_handle_callback_does_not_raise_when_vacancy_not_found() -> None:
    repository = _FakeRepository()
    repository.raise_on_update = VacancyNotFoundError("not found")
    notifier = AiogramTelegramNotifier(_settings(), repository, bot=_FakeBot())

    await notifier.handle_callback(111, "viewed:does-not-exist")  # не должно бросить


async def test_handle_callback_does_not_raise_on_invalid_transition() -> None:
    repository = _FakeRepository()
    repository.raise_on_update = ValueError("недопустим")
    notifier = AiogramTelegramNotifier(_settings(), repository, bot=_FakeBot())

    await notifier.handle_callback(111, "viewed:12345")  # не должно бросить
