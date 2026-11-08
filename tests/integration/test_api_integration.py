"""Integration-тест TASK-009 (ADR-028/ADR-029) и TASK-011 (ADR-034/ADR-035):
FastAPI поверх настоящего `SqliteVacancyRepository` (временный файл), не
фейка, как в `tests/unit/test_api.py` — доказывает, что HTTP-слой,
application-слой (`vacancy_queries.py`/`application_drafts.py`) и persistence
реально работают вместе, включая то, что каждый запрос открывает и закрывает
своё SQLite-соединение (см. `docs/TASK-009.md`, «SQLite и синхронные routes»)
и что `application_drafts`-таблица (TASK-011, миграция `0003`) реально
сохраняет и читает validated черновик через тот же файл. `create_app` здесь
получает только фабрику репозитория и фейковый generator — SuperJob/LM
Studio/Telegram-клиенты не собираются вовсе, TestClient не может случайно до
них достучаться."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from vacancy_agent.api.app import create_app
from vacancy_agent.config.settings import QwenSettings
from vacancy_agent.domain.application_draft import (
    ApplicationDraft,
    ApplicationDraftContent,
    EvidenceParagraph,
    RequirementMatch,
)
from vacancy_agent.domain.application_draft_plan import (
    ApplicationDraftPlan,
    RequirementEvidenceSelection,
)
from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.master_profile import MASTER_PROFILE
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.stored_application_draft import StoredApplicationDraft
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.integrations.application_draft_client import LMStudioApplicationDraftGenerator
from vacancy_agent.persistence.sqlite_repository import SqliteVacancyRepository

_NOW = datetime(2026, 8, 27, 9, 0, 0, tzinfo=UTC)
_KNOWN_FACT_ID = MASTER_PROFILE.facts[0].id
_KNOWN_FACT_STATEMENT = MASTER_PROFILE.facts[0].statement
_ELIGIBLE_REQUIREMENTS_TEXT = "Требуется: опыт Python, опыт SQL."


class _FakeGenerator:
    """Фейковый `ApplicationDraftGenerator` — реальный LM Studio здесь не
    нужен и не должен вызываться (TASK-011 ограничение на fake/MockTransport
    в тестах); реальность этого теста — в настоящей SQLite вокруг него.
    `generate` возвращает `ApplicationDraftPlan` (TASK-012) — итоговый
    `ApplicationDraftContent` строит настоящий детерминированный renderer
    внутри `prepare_application_draft`, не эта заглушка."""

    def __init__(self) -> None:
        self.model = "deepseek-r1-0528-qwen3-8b"
        self.generate_calls = 0
        self.closed = False

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
        self.closed = True


def _vacancy(hh_id: str, **overrides: object) -> Vacancy:
    fields: dict[str, object] = {
        "hh_id": hh_id,
        "title": "AI-интегратор",
        "url": f"https://superjob.ru/vakansii/{hh_id}.html",
        "published_at": _NOW,
        "fetched_at": _NOW,
    }
    fields.update(overrides)
    return Vacancy(**fields)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "vacancies.db"

    with SqliteVacancyRepository(str(db_path)) as seed:
        seed.save(
            _vacancy("2", published_at=_NOW),
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
            _vacancy("1", published_at=_NOW - timedelta(days=1)),
            FilterResult(
                accepted=False,
                reasons=["salary_below_minimum"],
                rejected_by=["salary_below_minimum"],
            ),
            VacancyStatus.NEW,
        )

    def factory() -> SqliteVacancyRepository:
        return SqliteVacancyRepository(str(db_path))

    yield TestClient(create_app(factory))


@pytest.fixture
def draft_setup(tmp_path: Path) -> Iterator[tuple[TestClient, _FakeGenerator, str]]:
    """Отдельная от `client` фикстура: одна eligible-вакансия (принята hard
    filters, оценена Qwen, порог пройден) плюс фейковый generator, поверх
    того же реального временного файла SQLite."""

    db_path = tmp_path / "drafts.db"

    with SqliteVacancyRepository(str(db_path)) as seed:
        seed.save(
            _vacancy(
                "superjob:eligible", published_at=_NOW, requirements=_ELIGIBLE_REQUIREMENTS_TEXT
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

    def repository_factory() -> SqliteVacancyRepository:
        return SqliteVacancyRepository(str(db_path))

    generator = _FakeGenerator()

    def generator_factory() -> _FakeGenerator:
        return generator

    app = create_app(
        repository_factory,
        generator_factory=generator_factory,
        model_resolver=lambda: generator.model,
    )
    yield TestClient(app), generator, str(db_path)


def test_health_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_vacancies_reads_real_sqlite(client: TestClient) -> None:
    response = client.get("/api/v1/vacancies")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == ["2", "1"]
    assert body["items"][0]["qwen_score"] == 82
    assert body["items"][0]["qwen_passed"] is True
    assert body["items"][1]["qwen_score"] is None
    assert body["items"][1]["filter_accepted"] is False


def test_list_vacancies_pagination_across_real_rows(client: TestClient) -> None:
    response = client.get("/api/v1/vacancies", params={"limit": 1, "offset": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["limit"] == 1
    assert body["offset"] == 1
    assert [item["id"] for item in body["items"]] == ["1"]


def test_get_vacancy_detail_reads_real_sqlite(client: TestClient) -> None:
    response = client.get("/api/v1/vacancies/2")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "2"
    assert body["filter_result"]["accepted"] is True
    assert body["assessment"]["score"] == 82
    assert body["qwen_model"] == "deepseek-r1-0528-qwen3-8b"


def test_get_vacancy_detail_404_for_unknown_id(client: TestClient) -> None:
    response = client.get("/api/v1/vacancies/does-not-exist")

    assert response.status_code == 404


def test_repeated_requests_each_open_and_close_their_own_connection(
    client: TestClient,
) -> None:
    """Несколько последовательных запросов к одному файлу SQLite не должны
    конфликтовать («database is locked») — каждый запрос открывает и
    закрывает собственное соединение, ни одно не остаётся висеть."""

    for _ in range(5):
        assert client.get("/api/v1/vacancies").status_code == 200
        assert client.get("/api/v1/vacancies/2").status_code == 200


# --- TASK-011: POST/GET application-drafts поверх реальной SQLite ---


def test_post_application_draft_persists_to_real_sqlite(
    draft_setup: tuple[TestClient, _FakeGenerator, str],
) -> None:
    client, generator, db_path = draft_setup

    response = client.post(
        "/api/v1/vacancies/superjob:eligible/application-drafts",
        json={"confirm_local_generation": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["vacancy_id"] == "superjob:eligible"
    assert body["reused"] is False
    assert generator.generate_calls == 1

    # Проверка независимым, свежим соединением к тому же файлу — не тем,
    # которым только что писал сам сервис.
    with SqliteVacancyRepository(db_path) as verify:
        stored = verify.get_application_draft("superjob:eligible")
    assert stored is not None
    assert stored.model == "deepseek-r1-0528-qwen3-8b"
    assert stored.draft.schema_version == "2"
    assert stored.draft.content.requirements[0].requirement == "опыт Python"
    assert stored.draft.content.requirements[0].coverage == "partial"
    assert _KNOWN_FACT_STATEMENT in stored.draft.content.requirements[0].explanation


def test_second_post_reuses_stored_draft_without_calling_generator_again(
    draft_setup: tuple[TestClient, _FakeGenerator, str],
) -> None:
    client, generator, _db_path = draft_setup

    client.post(
        "/api/v1/vacancies/superjob:eligible/application-drafts",
        json={"confirm_local_generation": True},
    )
    second = client.post(
        "/api/v1/vacancies/superjob:eligible/application-drafts",
        json={"confirm_local_generation": True},
    )

    assert second.status_code == 200
    assert second.json()["reused"] is True
    assert generator.generate_calls == 1


def test_post_replaces_draft_in_real_sqlite_when_only_model_changes(
    draft_setup: tuple[TestClient, _FakeGenerator, str],
) -> None:
    """TASK-011 ADR-049: «real temporary SQLite заменяет строку при смене
    model» — через реальный API поверх настоящего временного файла SQLite
    (не `_FakeRepository` напрямую, как в `test_application_drafts_
    service.py`). Меняем только `generator.model` между двумя POST —
    profile/prompt version не трогаем; так же, как в production
    `_resolve_model_name`/`_generator_factory` перечитывают `QwenSettings()`
    на каждый вызов, если `QWEN_MODEL` изменился между запросами."""

    client, generator, db_path = draft_setup

    first = client.post(
        "/api/v1/vacancies/superjob:eligible/application-drafts",
        json={"confirm_local_generation": True},
    )
    assert first.status_code == 200
    assert first.json()["reused"] is False

    generator.model = "qwen/qwen3-8b"

    second = client.post(
        "/api/v1/vacancies/superjob:eligible/application-drafts",
        json={"confirm_local_generation": True},
    )

    assert second.status_code == 200
    assert second.json()["reused"] is False
    assert generator.generate_calls == 2

    with SqliteVacancyRepository(db_path) as verify:
        stored = verify.get_application_draft("superjob:eligible")
    assert stored is not None
    assert stored.model == "qwen/qwen3-8b"


def test_post_replaces_stale_profile_and_prompt_version_in_real_sqlite(
    draft_setup: tuple[TestClient, _FakeGenerator, str],
) -> None:
    """TASK-012: старый черновик schema `1`/prompt `2` (до детерминированного
    plan-слоя) заменяется schema `2`/prompt `3` (`APPLICATION_DRAFT_PROMPT_
    VERSION`/`DRAFT_SCHEMA_VERSION`) — тот же механизм version-aware
    idempotency (ADR-049), что заменял устаревший `profile_version`/`model`,
    отдельная миграция не нужна."""

    client, generator, db_path = draft_setup
    stale = StoredApplicationDraft(
        draft=ApplicationDraft(
            schema_version="1",
            vacancy_id="superjob:eligible",
            profile_version="2026-08-27-v1",
            content=ApplicationDraftContent(
                match_summary="Старый черновик с неподтверждёнными утверждениями.",
                requirements=[
                    RequirementMatch(
                        requirement="Python",
                        coverage="full",
                        explanation="Старая версия",
                        evidence_fact_ids=[_KNOWN_FACT_ID],
                    )
                ],
                cover_letter_paragraphs=[
                    EvidenceParagraph(text="Старое письмо.", evidence_fact_ids=[_KNOWN_FACT_ID])
                ],
            ),
        ),
        model="old-model",
        prompt_version="2",
        created_at=_NOW,
    )
    with SqliteVacancyRepository(db_path) as seed:
        assert seed.save_application_draft(stale) is True

    response = client.post(
        "/api/v1/vacancies/superjob:eligible/application-drafts",
        json={"confirm_local_generation": True},
    )

    assert response.status_code == 200
    assert response.json()["reused"] is False
    assert response.json()["draft"]["profile_version"] == MASTER_PROFILE.version
    assert response.json()["prompt_version"] == "3"
    assert response.json()["draft"]["schema_version"] == "2"
    assert response.json()["draft"]["content"]["requirements"][0]["requirement"] == "опыт Python"
    assert generator.generate_calls == 1

    with SqliteVacancyRepository(db_path) as verify:
        stored = verify.get_application_draft("superjob:eligible")
    assert stored is not None
    assert stored.draft.profile_version == MASTER_PROFILE.version
    assert stored.prompt_version == "3"
    assert stored.draft.schema_version == "2"


def test_get_application_draft_reads_real_sqlite_without_generator(
    draft_setup: tuple[TestClient, _FakeGenerator, str],
) -> None:
    client, generator, _db_path = draft_setup

    client.post(
        "/api/v1/vacancies/superjob:eligible/application-drafts",
        json={"confirm_local_generation": True},
    )
    generate_calls_after_post = generator.generate_calls

    response = client.get("/api/v1/vacancies/superjob:eligible/application-draft")

    assert response.status_code == 200
    assert response.json()["reused"] is True
    assert generator.generate_calls == generate_calls_after_post


def test_get_application_draft_404_when_not_yet_generated(
    draft_setup: tuple[TestClient, _FakeGenerator, str],
) -> None:
    client, _generator, _db_path = draft_setup

    response = client.get("/api/v1/vacancies/superjob:eligible/application-draft")

    assert response.status_code == 404


def test_post_application_draft_404_for_vacancy_not_in_this_database(
    draft_setup: tuple[TestClient, _FakeGenerator, str],
) -> None:
    client, generator, _db_path = draft_setup

    response = client.post(
        "/api/v1/vacancies/does-not-exist/application-drafts",
        json={"confirm_local_generation": True},
    )

    assert response.status_code == 404
    assert generator.generate_calls == 0


def test_post_returns_502_and_does_not_persist_when_lm_studio_returns_empty_cover_letter_ids(
    tmp_path: Path,
) -> None:
    """TASK-012 аналог живой находки TASK-011 (ADR-039), перенесённый на план:
    настоящий `LMStudioApplicationDraftGenerator` (не фейк, как в
    `draft_setup` выше) поверх `httpx.MockTransport` и настоящая SQLite. LM
    Studio отвечает `HTTP 200` с `cover_letter_fact_ids: []` — план требует
    минимум один ID (`docs/TASK-012.md`, «Доменный контракт плана»: "1..10"),
    тем самым ответом, который прошёл бы менее строгую схему, но не проходит
    Pydantic. API должен вернуть `502`, ничего не сохранить, и последующий
    `GET` — `404`. Реальный LM Studio здесь не вызывается — `httpx.
    MockTransport` подменяет сеть целиком."""

    db_path = tmp_path / "empty_evidence.db"
    with SqliteVacancyRepository(str(db_path)) as seed:
        seed.save(
            _vacancy(
                "superjob:52004689", published_at=_NOW, requirements=_ELIGIBLE_REQUIREMENTS_TEXT
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

    def repository_factory() -> SqliteVacancyRepository:
        return SqliteVacancyRepository(str(db_path))

    bad_plan = {
        "requirements": [
            {
                "requirement_quote": "опыт Python",
                "evidence_fact_ids": [_KNOWN_FACT_ID],
            }
        ],
        "resume_fact_ids": [],
        "cover_letter_fact_ids": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(bad_plan, ensure_ascii=False)}}]},
        )

    qwen_settings = QwenSettings(
        base_url="http://localhost:1234/v1",
        model="qwen-test-model",
        api_key=SecretStr("test-key-should-never-appear-in-a-url-or-log"),
        draft_request_timeout_seconds=1.0,
        max_retries=0,
    )

    def generator_factory() -> LMStudioApplicationDraftGenerator:
        return LMStudioApplicationDraftGenerator(
            qwen_settings, transport=httpx.MockTransport(handler)
        )

    client = TestClient(
        create_app(
            repository_factory,
            generator_factory=generator_factory,
            model_resolver=lambda: qwen_settings.model,
        )
    )

    response = client.post(
        "/api/v1/vacancies/superjob:52004689/application-drafts",
        json={"confirm_local_generation": True},
    )

    assert response.status_code == 502

    with SqliteVacancyRepository(str(db_path)) as verify:
        assert verify.get_application_draft("superjob:52004689") is None

    assert (
        client.get("/api/v1/vacancies/superjob:52004689/application-draft").status_code == 404
    )


def test_post_returns_502_and_does_not_persist_when_lm_studio_uses_unknown_evidence_id(
    tmp_path: Path,
) -> None:
    """TASK-012 аналог живой находки TASK-011 (ADR-041), перенесённый на
    план: настоящий `LMStudioApplicationDraftGenerator` поверх `httpx.
    MockTransport` и настоящая SQLite. Grammar (ADR-040) скомпилировалась,
    Pydantic-структура ответа валидна (цитата — дословная подстрока
    requirements), но модель использовала `evidence_fact_id`, которого нет
    в переданном Master Profile — `validate_plan`
    (`domain/application_draft_plan.py`) отклоняет это уже после реального
    вызова. Схема ограничивает допустимые ID через `enum`
    (`build_response_json_schema`), но эта проверка остаётся обязательной
    второй линией защиты — фейковый обработчик здесь намеренно игнорирует
    enum схемы и возвращает недопустимый ID, как реально сделала модель до
    исправления ADR-041. API должен вернуть `502`, ничего не сохранить, и
    последующий `GET` — `404`."""

    db_path = tmp_path / "unknown_evidence.db"
    with SqliteVacancyRepository(str(db_path)) as seed:
        seed.save(
            _vacancy(
                "superjob:52004689", published_at=_NOW, requirements=_ELIGIBLE_REQUIREMENTS_TEXT
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

    def repository_factory() -> SqliteVacancyRepository:
        return SqliteVacancyRepository(str(db_path))

    bad_plan = {
        "requirements": [
            {
                "requirement_quote": "опыт Python",
                "evidence_fact_ids": ["does.not.exist.in.master.profile"],
            }
        ],
        "resume_fact_ids": [],
        "cover_letter_fact_ids": [_KNOWN_FACT_ID],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(bad_plan, ensure_ascii=False)}}]},
        )

    qwen_settings = QwenSettings(
        base_url="http://localhost:1234/v1",
        model="qwen-test-model",
        api_key=SecretStr("test-key-should-never-appear-in-a-url-or-log"),
        draft_request_timeout_seconds=1.0,
        max_retries=0,
    )

    def generator_factory() -> LMStudioApplicationDraftGenerator:
        return LMStudioApplicationDraftGenerator(
            qwen_settings, transport=httpx.MockTransport(handler)
        )

    client = TestClient(
        create_app(
            repository_factory,
            generator_factory=generator_factory,
            model_resolver=lambda: qwen_settings.model,
        )
    )

    response = client.post(
        "/api/v1/vacancies/superjob:52004689/application-drafts",
        json={"confirm_local_generation": True},
    )

    assert response.status_code == 502

    with SqliteVacancyRepository(str(db_path)) as verify:
        assert verify.get_application_draft("superjob:52004689") is None

    assert (
        client.get("/api/v1/vacancies/superjob:52004689/application-draft").status_code == 404
    )



def test_post_returns_502_and_does_not_persist_when_quote_is_not_exact_substring(
    tmp_path: Path,
) -> None:
    """TASK-012, «Детерминированные проверки», пункт 2: `requirement_quote`
    должен быть точной, дословной подстрокой `vacancy.requirements` —
    пересказ отклоняется `validate_plan` уже после реального вызова.
    Реальный LM Studio здесь не вызывается — `httpx.MockTransport`
    подменяет сеть целиком."""

    db_path = tmp_path / "paraphrased_quote.db"
    with SqliteVacancyRepository(str(db_path)) as seed:
        seed.save(
            _vacancy(
                "superjob:52004689", published_at=_NOW, requirements=_ELIGIBLE_REQUIREMENTS_TEXT
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

    def repository_factory() -> SqliteVacancyRepository:
        return SqliteVacancyRepository(str(db_path))

    bad_plan = {
        "requirements": [
            {
                "requirement_quote": "нужен разработчик со знанием Python и SQL",
                "evidence_fact_ids": [_KNOWN_FACT_ID],
            }
        ],
        "resume_fact_ids": [],
        "cover_letter_fact_ids": [_KNOWN_FACT_ID],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(bad_plan, ensure_ascii=False)}}]},
        )

    qwen_settings = QwenSettings(
        base_url="http://localhost:1234/v1",
        model="qwen-test-model",
        api_key=SecretStr("test-key-should-never-appear-in-a-url-or-log"),
        draft_request_timeout_seconds=1.0,
        max_retries=0,
    )

    def generator_factory() -> LMStudioApplicationDraftGenerator:
        return LMStudioApplicationDraftGenerator(
            qwen_settings, transport=httpx.MockTransport(handler)
        )

    client = TestClient(
        create_app(
            repository_factory,
            generator_factory=generator_factory,
            model_resolver=lambda: qwen_settings.model,
        )
    )

    response = client.post(
        "/api/v1/vacancies/superjob:52004689/application-drafts",
        json={"confirm_local_generation": True},
    )

    assert response.status_code == 502

    with SqliteVacancyRepository(str(db_path)) as verify:
        assert verify.get_application_draft("superjob:52004689") is None

    assert (
        client.get("/api/v1/vacancies/superjob:52004689/application-draft").status_code == 404
    )


def test_post_returns_409_when_vacancy_requirements_is_missing(tmp_path: Path) -> None:
    """TASK-012, «Детерминированные проверки», пункт 1: без непустого
    `vacancy.requirements` плану нечего дословно цитировать — безопасный
    `409` ДО создания generator (`generator_factory` здесь вообще не должен
    быть вызван)."""

    db_path = tmp_path / "missing_requirements.db"
    with SqliteVacancyRepository(str(db_path)) as seed:
        seed.save(
            _vacancy("superjob:no-requirements", published_at=_NOW, requirements=None),
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

    def repository_factory() -> SqliteVacancyRepository:
        return SqliteVacancyRepository(str(db_path))

    def generator_factory() -> LMStudioApplicationDraftGenerator:
        pytest.fail("generator_factory не должен вызываться без vacancy.requirements")

    client = TestClient(
        create_app(
            repository_factory,
            generator_factory=generator_factory,
            model_resolver=lambda: "unused-model",
        )
    )

    response = client.post(
        "/api/v1/vacancies/superjob:no-requirements/application-drafts",
        json={"confirm_local_generation": True},
    )

    assert response.status_code == 409
