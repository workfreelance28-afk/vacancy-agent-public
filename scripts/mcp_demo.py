"""Короткое воспроизводимое демо TASK-S2.6.

Показывает safety-механизм MCP tool `prepare_application_draft` —
`schema → preflight → human-in-the-loop approval → fail-closed` — БЕЗ
реальной LLM-генерации и без какой-либо внешней отправки. Использует
временный SQLite-файл (создаётся и удаляется в рамках запуска), боевую БД
не трогает.

Запуск:
    .venv/bin/python scripts/mcp_demo.py

Сценарий 1 — интерактивный: MCP elicitation реально спросит ВАС, человека
за клавиатурой, подтвердить ли операцию (`y`/иначе). Это тот самый
human-in-the-loop, который модель не может подделать полем в аргументах.
Сценарии 2 и 3 автоматические — показывают unsupported и fail-closed до
попытки approval."""

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path

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


def _vacancy(hh_id: str) -> Vacancy:
    return Vacancy(
        hh_id=hh_id,
        title="AI Integration Engineer (demo)",
        company="Example",
        requirements="Опыт Python.",
        url=f"https://superjob.ru/vakansii/{hh_id}.html",
        published_at=_NOW,
        fetched_at=_NOW,
    )


def _seed(db_path: Path) -> None:
    with SqliteVacancyRepository(str(db_path)) as seed:
        seed.save(
            _vacancy("demo:eligible"),
            FilterResult(accepted=True, reasons=["target_title_match"]),
            VacancyStatus.NEW,
            assessment=VacancyAssessment(
                schema_version="1", score=90, relevant=True, recommendation="strong_match"
            ),
            qwen_model="demo-model",
            qwen_min_score=70,
            qwen_passed=True,
        )
        seed.save(
            _vacancy("demo:ineligible"),
            FilterResult(accepted=False, rejected_by=["hard_filter"]),
            VacancyStatus.NEW,
        )


async def _ask_human(context: object, params: types.ElicitRequestParams) -> types.ElicitResult:
    print(f"\n[MCP elicitation] {params.message}")
    answer = input("Подтвердить? [y/N] ").strip().lower()
    if answer == "y":
        return types.ElicitResult(action="accept", content={})
    return types.ElicitResult(action="decline")


def _print_result(label: str, result: types.CallToolResult) -> None:
    print(f"\n=== {label} ===")
    if result.is_error:
        text = "".join(getattr(item, "text", "") for item in result.content)
        print(f"is_error=True: {text}")
    else:
        print(f"is_error=False: {result.structured_content}")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "vacancies.db"
        _seed(db_path)

        # Намеренно без generator_factory/model_resolver — этот demo
        # доказывает только safety-механизм, не полную генерацию через
        # реальную LM Studio (отдельное решение, TASK-S2.6).
        database = DatabaseSettings(url=f"sqlite:///{db_path}")
        mcp = create_mcp_server(_repository_factory(database))

        print("Сценарий 1: eligible-вакансия, реальный human-in-the-loop approval.")
        async with Client(
            mcp, raise_exceptions=True, mode="legacy", elicitation_callback=_ask_human
        ) as client:
            result = await client.call_tool(
                "prepare_application_draft",
                {"request": {"vacancy_id": "demo:eligible"}},
            )
        _print_result("Сценарий 1 (ваш ответ)", result)

        print("\nСценарий 2: клиент без elicitation capability -> unsupported.")
        async with Client(mcp, raise_exceptions=True) as client:
            result = await client.call_tool(
                "prepare_application_draft",
                {"request": {"vacancy_id": "demo:eligible"}},
            )
        _print_result("Сценарий 2 (без capability)", result)

        print("\nСценарий 3: неподходящая вакансия -> fail-closed до approval.")
        async with Client(mcp, raise_exceptions=True) as client:
            result = await client.call_tool(
                "prepare_application_draft",
                {"request": {"vacancy_id": "demo:ineligible"}},
            )
        _print_result("Сценарий 3 (ineligible)", result)


if __name__ == "__main__":
    asyncio.run(main())
