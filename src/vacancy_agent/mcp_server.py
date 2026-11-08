"""Локальный MCP server Vacancy Agent (TASK-S2.6).

`create_mcp_server` — тонкий adapter поверх `application/application_drafts.py`:
сам MCP-слой не хранит состояние БД и не решает, какая вакансия допустима —
он получает `repository_factory`/`generator_factory`/`model_resolver` через
dependency injection (тот же принцип, что `api/app.py`'s `create_app`) и
вызывает уже существующие `preflight_application_draft`/
`prepare_application_draft`. `generator_factory`/`model_resolver`
опциональны (по умолчанию `None`) — как и в `api/app.py`: если LLM не
сконфигурирован, tool безопасно отказывается на этапе `accept`, не пытаясь
что-то создать вслепую.

Human approval реализован через MCP elicitation (`ctx.elicit`), а не через
поле в аргументах tool — модель не может сама себя авторизовать: единственный
сигнал согласия — `result.action`, пришедший отдельным запросом сервер→клиент,
на который клиент отвечает от лица человека (или явно отказывается). Если
клиент не поддерживает elicitation (capability не объявлена при `initialize`)
или сам запрос падает — операция завершается `unsupported`, fail-closed.

Только после `accept` вызывается `prepare_application_draft` — та же
функция, что использует `POST /api/v1/vacancies/{id}/application-drafts`
в API: она повторно валидирует вакансию (могла измениться между preflight и
approval) и результат самой LLM. MCP-слой не дублирует ни одно из этих
правил."""

from typing import Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from vacancy_agent.application.application_drafts import (
    ApplicationDraftGeneratorFactory,
    ApplicationDraftModelResolver,
    ApplicationDraftRepositoryFactory,
    preflight_application_draft,
)
from vacancy_agent.application.application_drafts import (
    prepare_application_draft as generate_application_draft,
)
from vacancy_agent.domain.application_draft_errors import (
    ApplicationDraftApiError,
    ApplicationDraftConcurrencyError,
    ApplicationDraftNetworkError,
    ApplicationDraftResponseError,
    EvidenceValidationError,
    RequirementQuoteError,
    VacancyNotEligibleError,
)
from vacancy_agent.domain.master_profile import MASTER_PROFILE
from vacancy_agent.domain.repository_errors import VacancyNotFoundError
from vacancy_agent.integrations.application_draft_prompt import (
    APPLICATION_DRAFT_PROMPT_VERSION,
)

_GENERATION_ERRORS = (
    VacancyNotFoundError,
    VacancyNotEligibleError,
    ApplicationDraftNetworkError,
    ApplicationDraftApiError,
    ApplicationDraftResponseError,
    EvidenceValidationError,
    RequirementQuoteError,
    ApplicationDraftConcurrencyError,
)


def _safe_error_message(exc: Exception) -> str:
    """Единственное место, решающее, что безопасно показать наружу при сбое
    preflight/generation — те же формулировки, что уже проверены и приняты в
    `api/app.py` для тех же исключений, не новые слова с новым риском
    утечки. Neизвестное исключение (в т.ч. будущее, ещё не заведённое сюда)
    получает самый общий и самый безопасный fallback, а не свой `str(exc)`.

    `VacancyNotEligibleError.args[0]` — единственное исключение, которое
    можно показать как есть: оно всегда одна из нескольких статических фраз
    (`_require_eligible` в `application_drafts.py`), никогда не включает
    текст вакансии."""

    if isinstance(exc, VacancyNotFoundError):
        return "Вакансия не найдена"
    if isinstance(exc, VacancyNotEligibleError):
        return str(exc)
    if isinstance(exc, ApplicationDraftNetworkError | ApplicationDraftApiError):
        return "LM Studio недоступен или не настроен"
    if isinstance(
        exc, ApplicationDraftResponseError | EvidenceValidationError | RequirementQuoteError
    ):
        return "Ответ модели не прошёл валидацию"
    if isinstance(exc, ApplicationDraftConcurrencyError):
        return "Конкурентная запись — повторите запрос"
    return "Не удалось подготовить черновик для этой вакансии"


class PrepareApplicationDraftInput(BaseModel):
    """Строгий входной контракт MCP tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vacancy_id: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        description="ID уже сохранённой вакансии",
    )


class DraftApprovalRequest(BaseModel):
    """Пустая elicitation-схема: единственный сигнал согласия — `result.action`
    (`accept`/`decline`/`cancel`) самого elicitation-протокола, не поле внутри
    данных. Поле вроде `approve: bool` здесь было бы тем же анти-паттерном,
    который явно запрещён в аргументах tool (TASK-S2.6, «Главная граница
    безопасности»)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class PrepareApplicationDraftResult(BaseModel):
    """Итог approval+generation шага. Сам контент черновика намеренно не
    возвращается здесь — отдельный следующий шаг; `approved` подтверждает,
    что генерация прошла, `reused` — была ли она повторным использованием
    уже сохранённого черновика. Безопасная taxonomy ошибок (`_safe_error_message`)
    отвечает за все fail-пути — они приходят через `is_error=True` с текстом
    сообщения, не через это поле."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["approved", "declined", "cancelled", "unsupported"]
    vacancy_id: str
    reused: bool | None = None


def create_mcp_server(
    repository_factory: ApplicationDraftRepositoryFactory,
    generator_factory: ApplicationDraftGeneratorFactory | None = None,
    model_resolver: ApplicationDraftModelResolver | None = None,
) -> MCPServer:
    """Собрать MCP server с внедрёнными фабриками: тесты передают fake,
    реальный entrypoint — SQLite/PostgreSQL repository и LM Studio
    generator. `generator_factory`/`model_resolver` опциональны — их
    отсутствие означает, что генерация недоступна (как и в `api/app.py`)."""

    mcp = MCPServer("Vacancy Agent")

    @mcp.tool()
    async def prepare_application_draft(
        request: PrepareApplicationDraftInput,
        ctx: Context,
    ) -> PrepareApplicationDraftResult:
        """Подготовить локальный черновик отклика для сохранённой вакансии.

        Требует явное подтверждение человека через MCP elicitation. Tool
        никогда не отправляет отклик работодателю.
        """

        try:
            preflight = preflight_application_draft(
                repository_factory, request.vacancy_id
            )
        except (VacancyNotFoundError, VacancyNotEligibleError) as exc:
            # `ToolError`, не голый `ValueError` — SDK стирает сообщение
            # любого другого исключения, заменяя его на generic "Error
            # executing tool <name>" (`tools/base.py`'s `UnexpectedToolError`).
            # Только `ToolError` доносит наш текст до клиента как есть.
            raise ToolError(_safe_error_message(exc)) from exc

        if (
            ctx.client_capabilities is None
            or ctx.client_capabilities.elicitation is None
        ):
            return PrepareApplicationDraftResult(
                status="unsupported", vacancy_id=preflight.vacancy_id
            )

        try:
            elicitation = await ctx.elicit(
                f"Подготовить черновик отклика на вакансию {preflight.title!r}?",
                DraftApprovalRequest,
            )
        except Exception:
            # Клиент солгал про capability, либо канал пропал в процессе —
            # тот же fail-closed итог, что и явное отсутствие поддержки, а не
            # сырое исключение протокола наружу.
            return PrepareApplicationDraftResult(
                status="unsupported", vacancy_id=preflight.vacancy_id
            )

        if elicitation.action == "decline":
            return PrepareApplicationDraftResult(
                status="declined", vacancy_id=preflight.vacancy_id
            )
        if elicitation.action != "accept":
            return PrepareApplicationDraftResult(
                status="cancelled", vacancy_id=preflight.vacancy_id
            )

        if generator_factory is None or model_resolver is None:
            raise ToolError("Генератор черновиков не настроен")

        try:
            result = generate_application_draft(
                repository_factory,
                generator_factory,
                model_resolver,
                MASTER_PROFILE,
                APPLICATION_DRAFT_PROMPT_VERSION,
                preflight.vacancy_id,
            )
        except _GENERATION_ERRORS as exc:
            raise ToolError(_safe_error_message(exc)) from exc

        return PrepareApplicationDraftResult(
            status="approved", vacancy_id=preflight.vacancy_id, reused=result.reused
        )

    return mcp
