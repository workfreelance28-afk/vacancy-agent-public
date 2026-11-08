from datetime import UTC, datetime

import pytest
from mcp import Client, types

from vacancy_agent.domain.application_draft_plan import (
    ApplicationDraftPlan,
    RequirementEvidenceSelection,
)
from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.master_profile import MASTER_PROFILE
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.stored_application_draft import StoredApplicationDraft
from vacancy_agent.domain.stored_vacancy import StoredVacancy
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.mcp_server import create_mcp_server

_KNOWN_FACT_ID = MASTER_PROFILE.facts[0].id

_NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


def _vacancy(**overrides: object) -> Vacancy:
    fields: dict[str, object] = {
        "hh_id": "superjob:12345",
        "title": "AI Integration Engineer",
        "company": "Example",
        "requirements": "Опыт Python.",
        "url": "https://superjob.ru/vakansii/12345.html",
        "published_at": _NOW,
        "fetched_at": _NOW,
    }
    fields.update(overrides)
    return Vacancy(**fields)


def _eligible_stored_vacancy(**overrides: object) -> StoredVacancy:
    fields: dict[str, object] = {
        "vacancy": _vacancy(),
        "filter_result": FilterResult(accepted=True, reasons=["target_title_match"]),
        "status": VacancyStatus.NEW,
        "assessment": VacancyAssessment(
            schema_version="1", score=90, relevant=True, recommendation="strong_match"
        ),
        "qwen_model": "test-model",
        "qwen_min_score": 70,
        "qwen_passed": True,
    }
    fields.update(overrides)
    return StoredVacancy(**fields)  # type: ignore[arg-type]


class _FakeRepository:
    """Фейковый `ApplicationDraftRepository`. По умолчанию
    `get_application_draft`/`save_application_draft` не должны вызываться
    вовсе (approval ещё не пройден) — `allow_draft_operations=True`
    включает их реальное поведение для теста на `accept`, где генерация
    должна дойти до сохранения черновика."""

    def __init__(
        self, vacancy: StoredVacancy | None, *, allow_draft_operations: bool = False
    ) -> None:
        self._vacancy = vacancy
        self._allow_draft_operations = allow_draft_operations
        self.drafts: dict[str, StoredApplicationDraft] = {}
        self.closed_count = 0

    def get_vacancy(self, vacancy_id: str) -> StoredVacancy | None:
        return self._vacancy

    def get_application_draft(self, vacancy_id: str) -> StoredApplicationDraft | None:
        if not self._allow_draft_operations:
            pytest.fail("get_application_draft не должен вызываться до approval")
        return self.drafts.get(vacancy_id)

    def save_application_draft(self, stored: StoredApplicationDraft) -> bool:
        if not self._allow_draft_operations:
            pytest.fail("save_application_draft не должен вызываться без approval")
        self.drafts[stored.draft.vacancy_id] = stored
        return True

    def close(self) -> None:
        self.closed_count += 1


class _FakeGenerator:
    """Возвращает `ApplicationDraftPlan`, ссылающийся на факты РЕАЛЬНОГО
    `MASTER_PROFILE` — `mcp_server.py` использует боевой профиль, не
    тестовый, и `validate_plan` реально проверяет и цитаты требований
    (точная подстрока `vacancy.requirements`), и существование ID фактов."""

    def __init__(self) -> None:
        self.model = "fake-model"

    def generate(
        self, vacancy: object, assessment: object, profile: object
    ) -> ApplicationDraftPlan:
        return ApplicationDraftPlan(
            requirements=(
                RequirementEvidenceSelection(
                    requirement_quote="Опыт Python", evidence_fact_ids=(_KNOWN_FACT_ID,)
                ),
            ),
            resume_fact_ids=(),
            cover_letter_fact_ids=(_KNOWN_FACT_ID,),
        )

    def close(self) -> None:
        pass


def _forbidden_repository_factory() -> object:
    pytest.fail("repository_factory не должен вызываться при ошибке схемы запроса")


@pytest.mark.asyncio
async def test_prepare_application_draft_is_exposed_with_strict_schema() -> None:
    mcp = create_mcp_server(_forbidden_repository_factory)

    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.list_tools()

    tool = next(
        item for item in result.tools if item.name == "prepare_application_draft"
    )

    assert set(tool.input_schema["properties"]) == {"request"}

    request_schema = tool.input_schema["$defs"]["PrepareApplicationDraftInput"]
    assert request_schema["additionalProperties"] is False
    assert set(request_schema["properties"]) == {"vacancy_id"}


@pytest.mark.asyncio
async def test_prepare_application_draft_with_no_elicitation_callback_is_unsupported() -> (
    None
):
    """Клиент, не зарегистрировавший `elicitation_callback`, честно не
    объявляет capability при `initialize` — сервер обязан увидеть это и не
    пытаться спросить вовсе, а не просто получить отказ по факту запроса."""

    repository = _FakeRepository(_eligible_stored_vacancy())
    mcp = create_mcp_server(lambda: repository)

    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepare_application_draft",
            {"request": {"vacancy_id": "superjob:12345"}},
        )

    assert result.is_error is False
    assert result.structured_content == {
        "status": "unsupported",
        "vacancy_id": "superjob:12345",
        "reused": None,
    }
    assert repository.closed_count == 1


@pytest.mark.asyncio
async def test_prepare_application_draft_accept_generates_and_saves_draft() -> None:
    """`mode="legacy"` — единственный in-memory режим с настоящим дуплексным
    каналом; без него сервер физически не может отправить elicitation-запрос
    клиенту, даже если тот его поддерживает."""

    repository = _FakeRepository(_eligible_stored_vacancy(), allow_draft_operations=True)
    generator = _FakeGenerator()
    mcp = create_mcp_server(
        lambda: repository,
        generator_factory=lambda: generator,
        model_resolver=lambda: generator.model,
    )

    async def accept(context: object, params: object) -> types.ElicitResult:
        return types.ElicitResult(action="accept", content={})

    async with Client(
        mcp, raise_exceptions=True, mode="legacy", elicitation_callback=accept
    ) as client:
        result = await client.call_tool(
            "prepare_application_draft",
            {"request": {"vacancy_id": "superjob:12345"}},
        )

    assert result.is_error is False
    assert result.structured_content == {
        "status": "approved",
        "vacancy_id": "superjob:12345",
        "reused": False,
    }
    assert len(repository.drafts) == 1


@pytest.mark.asyncio
async def test_prepare_application_draft_decline_does_not_error() -> None:
    repository = _FakeRepository(_eligible_stored_vacancy())
    mcp = create_mcp_server(lambda: repository)

    async def decline(context: object, params: object) -> types.ElicitResult:
        return types.ElicitResult(action="decline")

    async with Client(
        mcp, raise_exceptions=True, mode="legacy", elicitation_callback=decline
    ) as client:
        result = await client.call_tool(
            "prepare_application_draft",
            {"request": {"vacancy_id": "superjob:12345"}},
        )

    assert result.is_error is False
    assert result.structured_content == {
        "status": "declined",
        "vacancy_id": "superjob:12345",
        "reused": None,
    }


@pytest.mark.asyncio
async def test_prepare_application_draft_cancel_does_not_error() -> None:
    repository = _FakeRepository(_eligible_stored_vacancy())
    mcp = create_mcp_server(lambda: repository)

    async def cancel(context: object, params: object) -> types.ElicitResult:
        return types.ElicitResult(action="cancel")

    async with Client(
        mcp, raise_exceptions=True, mode="legacy", elicitation_callback=cancel
    ) as client:
        result = await client.call_tool(
            "prepare_application_draft",
            {"request": {"vacancy_id": "superjob:12345"}},
        )

    assert result.is_error is False
    assert result.structured_content == {
        "status": "cancelled",
        "vacancy_id": "superjob:12345",
        "reused": None,
    }


@pytest.mark.asyncio
async def test_prepare_application_draft_rejects_invalid_vacancy_id() -> None:
    mcp = create_mcp_server(_forbidden_repository_factory)

    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepare_application_draft",
            {"request": {"vacancy_id": "id with spaces"}},
        )

    assert result.is_error is True
    assert result.structured_content is None


@pytest.mark.asyncio
async def test_prepare_application_draft_rejects_fake_approval() -> None:
    mcp = create_mcp_server(_forbidden_repository_factory)

    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepare_application_draft",
            {"request": {"vacancy_id": "superjob:12345", "approved": True}},
        )

    assert result.is_error is True
    assert result.structured_content is None


@pytest.mark.asyncio
async def test_prepare_application_draft_fails_closed_for_missing_vacancy() -> None:
    """Taxonomy: `VacancyNotFoundError` получает свою фиксированную фразу,
    отличную от других причин отказа — не общий "недоступна". SDK всегда
    добавляет спереди `"Error executing tool <name>: "` к сообщению
    `ToolError` (`tools/base.py`: "An anticipated failure keeps its own
    text after the prefix") — проверяем наш текст как suffix, не точное
    совпадение всей строки."""

    repository = _FakeRepository(None)
    mcp = create_mcp_server(lambda: repository)

    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepare_application_draft",
            {"request": {"vacancy_id": "unknown-vacancy"}},
        )

    assert result.is_error is True
    assert result.structured_content is None
    assert repository.closed_count == 1
    error_text = "".join(getattr(item, "text", "") for item in result.content)
    assert error_text.endswith("Вакансия не найдена")


@pytest.mark.asyncio
async def test_prepare_application_draft_fails_closed_for_ineligible_vacancy() -> None:
    """TASK-S2.6, критерий №5: сообщение об ошибке безопасно — по нему нельзя
    узнать название вакансии или компании, даже если сам tool call эту
    вакансию нашёл и отклонил по eligibility. Taxonomy: `VacancyNotEligibleError`
    показывается как есть (`str(exc)`), потому что её текст — всегда одна из
    нескольких статических безопасных фраз, а не произвольная строка."""

    repository = _FakeRepository(
        _eligible_stored_vacancy(
            vacancy=_vacancy(title="Секретный проект X", company="Тайный Работодатель"),
            filter_result=FilterResult(accepted=False, rejected_by=["hard_filter"]),
        )
    )
    mcp = create_mcp_server(lambda: repository)

    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepare_application_draft",
            {"request": {"vacancy_id": "superjob:12345"}},
        )

    assert result.is_error is True
    assert result.structured_content is None
    error_text = "".join(getattr(item, "text", "") for item in result.content)
    assert error_text.endswith("вакансия не принята hard filters")
    assert "Секретный проект X" not in error_text
    assert "Тайный Работодатель" not in error_text


@pytest.mark.asyncio
async def test_prepare_application_draft_accept_fails_closed_when_generator_not_configured() -> (
    None
):
    """Taxonomy: отсутствие сконфигурированного generator/model_resolver —
    отдельная, отличимая от остальных причина отказа (как и `503` в
    `api/app.py` для того же случая), а не общее сообщение о генерации."""

    repository = _FakeRepository(_eligible_stored_vacancy(), allow_draft_operations=True)
    mcp = create_mcp_server(lambda: repository)

    async def accept(context: object, params: object) -> types.ElicitResult:
        return types.ElicitResult(action="accept", content={})

    async with Client(
        mcp, raise_exceptions=True, mode="legacy", elicitation_callback=accept
    ) as client:
        result = await client.call_tool(
            "prepare_application_draft",
            {"request": {"vacancy_id": "superjob:12345"}},
        )

    assert result.is_error is True
    assert result.structured_content is None
    error_text = "".join(getattr(item, "text", "") for item in result.content)
    assert error_text.endswith("Генератор черновиков не настроен")
    assert repository.drafts == {}
