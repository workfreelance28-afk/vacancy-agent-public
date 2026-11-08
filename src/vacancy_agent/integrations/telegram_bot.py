"""Telegram-бот на `aiogram`. Реализует `vacancy_agent.domain.ports.TelegramNotifier`.

Единственный получатель карточек — список `TelegramSettings.allowed_chat_ids`
(система однопользовательская в MVP 1, см. `docs/PRODUCT.md` — «сложная
многопользовательская система» явно вне границ). `handle_callback`
проверяет `chat_id` по этому же списку — колбэки из чужих чатов
игнорируются, а не обрабатываются (см. `docs/ARCHITECTURE.md`,
«Надёжность и безопасность»: «Telegram callback проверяется по допустимым
действиям и вакансии»).

Текст вакансии (заголовок, компания, регион, ...) — недоверенные внешние
данные (см. `docs/AI_ROLES.md`): всё, что попадает в HTML-разметку
сообщения, экранируется через `html.escape`.

Отправка использует тайм-аут/повторы `TelegramSettings` — та же схема,
что и HH-клиент (см. `docs/DECISIONS.md`, ADR-007/ADR-011): повтор на
сетевые и серверные ошибки, `TelegramRetryAfter` — не короче
`retry_after` от Telegram. Прочие ошибки `aiogram` (например,
`TelegramForbiddenError`, если пользователь заблокировал бота, или
`TelegramBadRequest` — чат не найден) не ретраятся, но всё равно
становятся `TelegramSendError` — вызывающий код (`application/daily_scan.py`)
знает только про `domain.notifier_errors`, не про `aiogram`. Пустой
`allowed_chat_ids` — тоже `TelegramSendError`, а не тихий успех без
единого отправленного сообщения (см. `docs/DECISIONS.md`, ADR-012,
раздел «Уточнения по итогам ревью»).
"""

import asyncio
import html
import logging
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from vacancy_agent.config.settings import TelegramSettings
from vacancy_agent.domain.notifier_errors import TelegramSendError
from vacancy_agent.domain.ports import VacancyRepository
from vacancy_agent.domain.repository_errors import PersistenceError
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.vacancy import Vacancy

logger = logging.getLogger("vacancy_agent.telegram")

_ACTION_TO_STATUS: dict[str, VacancyStatus] = {
    "viewed": VacancyStatus.VIEWED,
    "skipped": VacancyStatus.SKIPPED,
}


class AiogramTelegramNotifier:
    """Показ карточек вакансий и обработка кнопок. `repository` нужен, чтобы
    применить результат нажатия — сама сигнатура `handle_callback` (порт из
    `domain/ports.py`) не принимает репозиторий, поэтому он внедряется
    через конструктор."""

    def __init__(
        self,
        settings: TelegramSettings,
        repository: VacancyRepository,
        bot: Bot | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._bot = bot or Bot(token=settings.bot_token.get_secret_value())

    async def send_vacancy_card(self, vacancy: Vacancy) -> None:
        if not self._settings.allowed_chat_ids:
            # Пустой список получателей — не «отправили нулю адресатов
            # успешно», а невозможность выполнить задачу метода: карточку
            # никто не увидит. `daily_scan.py` должен считать это отказом
            # отправки (не сохранять вакансию, повторить на следующем
            # прогоне), а не тихим успехом.
            raise TelegramSendError(
                "TELEGRAM_ALLOWED_CHAT_IDS пуст — отправлять карточку некому"
            )

        text = _format_card(vacancy)
        keyboard = _build_keyboard(vacancy.hh_id)
        for chat_id in self._settings.allowed_chat_ids:
            await self._send_with_retry(chat_id, text, keyboard)

    async def _send_with_retry(
        self, chat_id: int, text: str, keyboard: InlineKeyboardMarkup
    ) -> None:
        for attempt in range(self._settings.max_retries + 1):
            try:
                await self._bot.send_message(
                    chat_id,
                    text,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                    request_timeout=int(self._settings.request_timeout_seconds),
                )
            except TelegramRetryAfter as exc:
                error: Exception = exc
                delay = max(
                    self._settings.retry_backoff_seconds * (attempt + 1), float(exc.retry_after)
                )
            except (TelegramNetworkError, TelegramServerError) as exc:
                error = exc
                delay = self._settings.retry_backoff_seconds * (attempt + 1)
            except TelegramAPIError as exc:
                # Не ретраится (например, `TelegramForbiddenError` — бот
                # заблокирован, `TelegramBadRequest` — чат не найден), но
                # всё равно должна стать `TelegramSendError`, а не утечь как
                # сырое исключение `aiogram` — иначе `daily_scan.py` (и
                # любой другой вызывающий код, знающий только про
                # `domain.notifier_errors`) не сможет её поймать.
                raise TelegramSendError(
                    f"Telegram отклонил отправку в chat_id={chat_id}: {exc}"
                ) from exc
            else:
                return

            if attempt == self._settings.max_retries:
                raise TelegramSendError(
                    f"Не удалось отправить сообщение в chat_id={chat_id}: {error}"
                ) from error
            await asyncio.sleep(delay)

    async def handle_callback(self, chat_id: int, payload: str) -> None:
        if chat_id not in self._settings.allowed_chat_ids:
            logger.warning("Callback от неразрешённого chat_id=%s проигнорирован", chat_id)
            return

        parsed = _parse_payload(payload)
        if parsed is None:
            logger.warning("Не удалось разобрать callback payload=%r", payload)
            return

        action, hh_id = parsed
        status = _ACTION_TO_STATUS.get(action)
        if status is None:
            logger.warning("Неизвестное действие в callback payload=%r", payload)
            return

        try:
            self._repository.update_status(hh_id, status)
        except (PersistenceError, ValueError):
            logger.warning("Не удалось применить callback payload=%r", payload, exc_info=True)


def create_dispatcher(notifier: AiogramTelegramNotifier) -> Dispatcher:
    """Собирает `Dispatcher`, который передаёт входящие нажатия кнопок в
    `notifier.handle_callback`. Используется точкой входа, которая реально
    запускает long polling (см. `application/wiring.py`) — сам этот модуль
    сетевых вызовов не делает и тестируется без обращения к Telegram."""

    dispatcher = Dispatcher()

    @dispatcher.callback_query()
    async def _on_callback_query(callback: CallbackQuery) -> None:
        if callback.data is not None and callback.message is not None:
            await notifier.handle_callback(callback.message.chat.id, callback.data)
        await callback.answer()

    return dispatcher


def _parse_payload(payload: str) -> tuple[str, str] | None:
    parts = payload.split(":", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def _build_keyboard(hh_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👀 Просмотрено", callback_data=f"viewed:{hh_id}"),
                InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"skipped:{hh_id}"),
            ]
        ]
    )


def _format_card(vacancy: Vacancy) -> str:
    lines = [f"<b>{html.escape(vacancy.title)}</b>"]
    if vacancy.company:
        lines.append(html.escape(vacancy.company))
    lines.append(html.escape(_format_salary(vacancy)))

    location_bits = [bit for bit in (vacancy.area, ", ".join(vacancy.work_format)) if bit]
    if location_bits:
        lines.append(html.escape(" · ".join(location_bits)))
    if vacancy.experience:
        lines.append(html.escape(vacancy.experience))

    lines.append(f'<a href="{html.escape(vacancy.url)}">Открыть вакансию</a>')
    lines.append(html.escape(_format_age(vacancy.published_at)))
    return "\n".join(lines)


def _format_age(published_at: datetime) -> str:
    days = (datetime.now(UTC) - published_at).days
    if days <= 0:
        return "Опубликована сегодня"
    if days == 1:
        return "Опубликована вчера"
    return f"Опубликована {days} дн. назад"


def _format_salary(vacancy: Vacancy) -> str:
    if vacancy.salary_from is None and vacancy.salary_to is None:
        return "Зарплата не указана"

    currency = vacancy.salary_currency or ""
    suffix = ""
    if vacancy.salary_gross is True:
        suffix = " до вычета налогов"
    elif vacancy.salary_gross is False:
        suffix = " на руки"

    if vacancy.salary_from is not None and vacancy.salary_to is not None:
        body = f"{_format_amount(vacancy.salary_from)} – {_format_amount(vacancy.salary_to)}"
    elif vacancy.salary_from is not None:
        body = f"от {_format_amount(vacancy.salary_from)}"
    else:
        assert vacancy.salary_to is not None
        body = f"до {_format_amount(vacancy.salary_to)}"

    return f"{body} {currency}{suffix}".strip()


def _format_amount(amount: float) -> str:
    return f"{int(amount):,}".replace(",", " ")
