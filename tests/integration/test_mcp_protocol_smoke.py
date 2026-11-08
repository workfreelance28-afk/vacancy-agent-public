"""MCP protocol smoke (TASK-S2.6, критерий №6): `mcp_main._repository_factory`
поверх настоящего `SqliteVacancyRepository` (временный файл), не fake, как в
`tests/unit/test_mcp_server.py` — доказывает, что реальная точка входа
(`mcp_main.py`) и настоящий MCP-протокол (`Client(..., mode="legacy")`,
полноценный handshake) работают вместе: discovery, преflight поверх
реальной БД, human-in-the-loop approval и fail-closed пути.

Намеренно без LLM (`generator_factory`/`model_resolver` не переданы) — этот
smoke доказывает только safety-механизм (schema → preflight → approval →
fail-closed), не полный happy-path генерации, который уже отдельно покрыт
unit-тестом с fake generator (`test_prepare_application_draft_accept_
generates_and_saves_draft`)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp import Client, types

from vacancy_agent.config.settings import DatabaseSettings
from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.mcp_main import _repository_factory
from vacancy_agent.mcp_server import create_mcp_server
from vacancy_agent.persistence.sqlite_repository import SqliteVacancyRepository

_NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def _vacancy(hh_id: str, **overrides: object) -> Vacancy:
    fields: dict[str, object] = {
        "hh_id": hh_id,
        "title": "AI Integration Engineer",
        "company": "Example",
        "requirements": "Опыт Python.",
        "url": f"https://superjob.ru/vakansii/{hh_id}.html",
        "published_at": _NOW,
        "fetched_at": _NOW,
    }
    fields.update(overrides)
    return Vacancy(**fields)


@pytest.fixture
def mcp_over_real_sqlite(tmp_path: Path) -> Iterator[tuple[object, str]]:
    db_path = tmp_path / "vacancies.db"

    with SqliteVacancyRepository(str(db_path)) as seed:
        seed.save(
            _vacancy("superjob:eligible"),
            FilterResult(accepted=True, reasons=["target_title_match"]),
            VacancyStatus.NEW,
            assessment=VacancyAssessment(
                schema_version="1", score=90, relevant=True, recommendation="strong_match"
            ),
            qwen_model="test-model",
            qwen_min_score=70,
            qwen_passed=True,
        )
        seed.save(
            _vacancy("superjob:ineligible", published_at=_NOW - timedelta(days=1)),
            FilterResult(accepted=False, rejected_by=["hard_filter"]),
            VacancyStatus.NEW,
        )

    database = DatabaseSettings(url=f"sqlite:///{db_path}")
    mcp = create_mcp_server(_repository_factory(database))
    yield mcp, str(db_path)


@pytest.mark.asyncio
async def test_tool_is_discoverable_with_strict_schema_over_real_sqlite(
    mcp_over_real_sqlite: tuple[object, str],
) -> None:
    mcp, _ = mcp_over_real_sqlite

    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.list_tools()

    tool = next(item for item in result.tools if item.name == "prepare_application_draft")
    request_schema = tool.input_schema["$defs"]["PrepareApplicationDraftInput"]
    assert request_schema["additionalProperties"] is False
    assert set(request_schema["properties"]) == {"vacancy_id"}


@pytest.mark.asyncio
async def test_decline_over_real_sqlite_creates_no_draft(
    mcp_over_real_sqlite: tuple[object, str],
) -> None:
    mcp, db_path = mcp_over_real_sqlite

    async def decline(context: object, params: object) -> types.ElicitResult:
        return types.ElicitResult(action="decline")

    async with Client(
        mcp, raise_exceptions=True, mode="legacy", elicitation_callback=decline
    ) as client:
        result = await client.call_tool(
            "prepare_application_draft",
            {"request": {"vacancy_id": "superjob:eligible"}},
        )

    assert result.is_error is False
    assert result.structured_content == {
        "status": "declined",
        "vacancy_id": "superjob:eligible",
        "reused": None,
    }
    with SqliteVacancyRepository(db_path) as check:
        assert check.get_application_draft("superjob:eligible") is None


@pytest.mark.asyncio
async def test_no_elicitation_capability_over_real_sqlite_is_unsupported(
    mcp_over_real_sqlite: tuple[object, str],
) -> None:
    mcp, _ = mcp_over_real_sqlite

    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepare_application_draft",
            {"request": {"vacancy_id": "superjob:eligible"}},
        )

    assert result.is_error is False
    assert result.structured_content == {
        "status": "unsupported",
        "vacancy_id": "superjob:eligible",
        "reused": None,
    }


@pytest.mark.asyncio
async def test_ineligible_vacancy_over_real_sqlite_fails_closed(
    mcp_over_real_sqlite: tuple[object, str],
) -> None:
    """Реальный `preflight_application_draft` поверх настоящей БД отклоняет
    неподходящую вакансию ещё до попытки спросить approval — elicitation
    вообще не должен вызываться (fake без callback это доказал бы падением,
    но здесь достаточно проверить, что клиент без callback тоже не спрашивают
    и это не отличимо от decline по побочным эффектам)."""

    mcp, _ = mcp_over_real_sqlite

    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepare_application_draft",
            {"request": {"vacancy_id": "superjob:ineligible"}},
        )

    assert result.is_error is True
    assert result.structured_content is None
    error_text = "".join(getattr(item, "text", "") for item in result.content)
    assert error_text.endswith("вакансия не принята hard filters")


@pytest.mark.asyncio
async def test_missing_vacancy_over_real_sqlite_fails_closed(
    mcp_over_real_sqlite: tuple[object, str],
) -> None:
    mcp, _ = mcp_over_real_sqlite

    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepare_application_draft",
            {"request": {"vacancy_id": "superjob:unknown"}},
        )

    assert result.is_error is True
    assert result.structured_content is None
    error_text = "".join(getattr(item, "text", "") for item in result.content)
    assert error_text.endswith("Вакансия не найдена")


@pytest.mark.asyncio
async def test_accept_without_configured_generator_fails_closed_over_real_sqlite(
    mcp_over_real_sqlite: tuple[object, str],
) -> None:
    """`mcp_main.main()` не передаёт `generator_factory`/`model_resolver` в
    этом smoke — как если бы `QWEN_MODEL` не был настроен. Даже настоящий
    accept не должен создать черновик."""

    mcp, db_path = mcp_over_real_sqlite

    async def accept(context: object, params: object) -> types.ElicitResult:
        return types.ElicitResult(action="accept", content={})

    async with Client(
        mcp, raise_exceptions=True, mode="legacy", elicitation_callback=accept
    ) as client:
        result = await client.call_tool(
            "prepare_application_draft",
            {"request": {"vacancy_id": "superjob:eligible"}},
        )

    assert result.is_error is True
    assert result.structured_content is None
    error_text = "".join(getattr(item, "text", "") for item in result.content)
    assert error_text.endswith("Генератор черновиков не настроен")
    with SqliteVacancyRepository(db_path) as check:
        assert check.get_application_draft("superjob:eligible") is None
