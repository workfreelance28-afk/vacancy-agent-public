"""API-контракт поверх реальной PostgreSQL через полный HTTP-стек, а не
только через прямые вызовы репозитория (TASK-S2.1, review-fix round 2 [P1]:
"PostgreSQL API-контракт не проверяется" — `tests/integration/
test_postgres_repository_contract.py` покрывал только сам класс
`PostgresVacancyRepository`, критерий готовности TASK-S2.1 №5 "API
list/detail/draft работает с PostgreSQL через ту же application-логику"
оставался непроверенным).

Тот же принцип, что `tests/integration/test_api_integration.py` (SQLite) —
`create_app` получает `repository_factory`, который на каждый HTTP-запрос
открывает и закрывает СВОЁ соединение (то же "одно соединение на одну
операцию", что `api/main.py`/`persistence/factory.py`, TASK-S2.1), не
переиспользуя соединение между запросами. `TestClient` реально проходит
FastAPI routing, dependency injection и Pydantic-сериализацию поверх
настоящей тестовой PostgreSQL — ни один из этих слоёв не подменён.

Явно пропускается без `TEST_DATABASE_URL` — тот же fail-closed guard
(`_require_safe_test_dsn`), что и в `test_postgres_repository_contract.py`,
намеренно продублирован здесь, а не импортирован из другого test-модуля:
это единственная защита от `DELETE FROM` в чужой БД, и её независимая
надёжность в каждом файле важнее, чем экономия нескольких строк."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from vacancy_agent.api.app import create_app
from vacancy_agent.config.settings import DatabaseSettings
from vacancy_agent.domain.application_draft_plan import (
    ApplicationDraftPlan,
    RequirementEvidenceSelection,
)
from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.master_profile import MASTER_PROFILE
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.vacancy import Vacancy

psycopg = pytest.importorskip("psycopg")

_TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")
_CONFIRM_ENV_VAR = "TEST_DATABASE_URL_CONFIRM_DESTRUCTIVE"
_CONFIRMED = os.environ.get(_CONFIRM_ENV_VAR, "") == "1"

pytestmark = pytest.mark.skipif(
    not _TEST_DSN,
    reason=(
        "TEST_DATABASE_URL не задан — PostgreSQL API contract-тесты пропущены "
        "локально (TASK-S2.1, критерий готовности №5/№8 остаются открытыми до "
        "зафиксированного прогона на реальной тестовой PostgreSQL)"
    ),
)


def _require_safe_test_dsn(dsn: str) -> None:
    """Fail-closed guard, идентичный `test_postgres_repository_contract.
    _require_safe_test_dsn` (см. его docstring): фикстура `client` ниже
    тоже выполняет `DELETE FROM` перед каждым тестом, поэтому одного
    `TEST_DATABASE_URL` недостаточно — нужны ОБА явных сигнала:
    `TEST_DATABASE_URL_CONFIRM_DESTRUCTIVE=1` и `test` в имени БД."""

    if not _CONFIRMED:
        pytest.fail(
            f"TEST_DATABASE_URL задан, но {_CONFIRM_ENV_VAR}=1 — нет. Этот файл "
            "тоже выполняет DELETE FROM vacancies/application_drafts перед "
            f"каждым тестом — установите {_CONFIRM_ENV_VAR}=1, только если "
            "TEST_DATABASE_URL указывает на одноразовую тестовую PostgreSQL, "
            "данные которой не жалко потерять."
        )
    db_name = dsn.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in db_name.lower():
        pytest.fail(
            f"TEST_DATABASE_URL указывает на БД {db_name!r}, в имени которой нет "
            "'test' — отказ выполнять DELETE FROM в базе, не похожей на "
            "одноразовую тестовую. Используйте отдельную тестовую БД с 'test' "
            "в имени."
        )


_NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
_KNOWN_FACT_ID = MASTER_PROFILE.facts[0].id
_ELIGIBLE_REQUIREMENTS_TEXT = "Требуется: опыт Python, опыт SQL."


def _vacancy(hh_id: str, **overrides: object) -> Vacancy:
    fields: dict[str, object] = {
        "hh_id": hh_id,
        "title": "AI-интегратор",
        "company": "HeadHunter",
        "salary_from": 200_000,
        "salary_to": 250_000,
        "salary_currency": "RUR",
        "salary_gross": True,
        "description": "Внедрение AI-агентов",
        "requirements": "Python, LLM",
        "key_skills": ["Python", "LLM"],
        "experience": "От 1 года до 3 лет",
        "employment": "Полная занятость",
        "work_format": ["Удалённая работа"],
        "area": "Москва",
        "url": f"https://hh.ru/vacancy/{hh_id}",
        "published_at": _NOW,
        "fetched_at": _NOW,
    }
    fields.update(overrides)
    return Vacancy(**fields)


class _FakeGenerator:
    """Тот же фейк-generator, что `test_api_integration.py` (SQLite) —
    реальный LM Studio здесь не нужен: этот файл проверяет HTTP + application
    + реальную PostgreSQL, а не сеть до LLM."""

    def __init__(self) -> None:
        self.model = "deepseek-r1-0528-qwen3-8b"
        self.generate_calls = 0

    def generate(self, vacancy: object, assessment: object, profile: object) -> object:
        self.generate_calls += 1
        return ApplicationDraftPlan(
            requirements=[
                RequirementEvidenceSelection(
                    requirement_quote="опыт Python", evidence_fact_ids=[_KNOWN_FACT_ID]
                )
            ],
            resume_fact_ids=[],
            cover_letter_fact_ids=[_KNOWN_FACT_ID],
        )

    def close(self) -> None:
        pass


@pytest.fixture
def client() -> Iterator[TestClient]:
    from vacancy_agent.persistence.postgres_repository import PostgresVacancyRepository

    _require_safe_test_dsn(_TEST_DSN)
    settings = DatabaseSettings(url=_TEST_DSN)

    with PostgresVacancyRepository.from_settings(settings) as seed:
        with seed._connection.cursor() as cursor:
            cursor.execute("DELETE FROM application_drafts")
            cursor.execute("DELETE FROM vacancies")
        seed._connection.commit()
        seed.save(
            _vacancy("pg-api-2", published_at=_NOW),
            FilterResult(accepted=True, reasons=["target_title_match"]),
            VacancyStatus.NEW,
            assessment=VacancyAssessment(
                schema_version="1",
                score=82,
                relevant=True,
                strengths=[],
                gaps=[],
                risks=[],
                recommendation="review",
            ),
            qwen_model="deepseek-r1-0528-qwen3-8b",
            qwen_min_score=70,
            qwen_passed=True,
        )
        seed.save(
            _vacancy(
                "pg-api-eligible", published_at=_NOW, requirements=_ELIGIBLE_REQUIREMENTS_TEXT
            ),
            FilterResult(accepted=True, reasons=["target_title_match"]),
            VacancyStatus.NEW,
            assessment=VacancyAssessment(
                schema_version="1",
                score=90,
                relevant=True,
                strengths=[],
                gaps=[],
                risks=[],
                recommendation="strong_match",
            ),
            qwen_model="deepseek-r1-0528-qwen3-8b",
            qwen_min_score=70,
            qwen_passed=True,
        )

    def repository_factory() -> "PostgresVacancyRepository":
        return PostgresVacancyRepository.from_settings(settings)

    generator = _FakeGenerator()

    app = create_app(
        repository_factory,
        generator_factory=lambda: generator,
        model_resolver=lambda: generator.model,
    )
    yield TestClient(app)


def test_health_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_ok_against_real_postgres(client: TestClient) -> None:
    response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_list_vacancies_reads_real_postgres(client: TestClient) -> None:
    response = client.get("/api/v1/vacancies")

    assert response.status_code == 200
    body = response.json()
    ids = {item["id"] for item in body["items"]}
    assert {"pg-api-2", "pg-api-eligible"} <= ids
    assert body["total"] >= 2


def test_get_vacancy_detail_reads_real_postgres(client: TestClient) -> None:
    response = client.get("/api/v1/vacancies/pg-api-2")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "pg-api-2"
    assert body["assessment"]["score"] == 82
    assert body["qwen_model"] == "deepseek-r1-0528-qwen3-8b"


def test_get_vacancy_detail_404_for_unknown_id(client: TestClient) -> None:
    response = client.get("/api/v1/vacancies/does-not-exist")

    assert response.status_code == 404


def test_post_and_get_application_draft_persist_to_real_postgres(
    client: TestClient,
) -> None:
    posted = client.post(
        "/api/v1/vacancies/pg-api-eligible/application-drafts",
        json={"confirm_local_generation": True},
    )

    assert posted.status_code == 200
    body = posted.json()
    assert body["draft"]["vacancy_id"] == "pg-api-eligible"
    assert body["reused"] is False

    fetched = client.get("/api/v1/vacancies/pg-api-eligible/application-draft")

    assert fetched.status_code == 200
    assert fetched.json()["reused"] is True


def test_post_application_draft_404_for_vacancy_not_in_this_database(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/vacancies/does-not-exist/application-drafts",
        json={"confirm_local_generation": True},
    )

    assert response.status_code == 404
