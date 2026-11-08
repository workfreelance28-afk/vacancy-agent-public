import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from vacancy_agent.api.app import create_app
from vacancy_agent.application.application_drafts import (
    ApplicationDraftGeneratorFactory,
    ApplicationDraftModelResolver,
)
from vacancy_agent.domain.application_draft_errors import (
    ApplicationDraftConcurrencyError,
    ApplicationDraftNetworkError,
    ApplicationDraftResponseError,
)
from vacancy_agent.domain.application_draft_plan import (
    ApplicationDraftPlan,
    RequirementEvidenceSelection,
)
from vacancy_agent.domain.assessment import Gap, Strength, VacancyAssessment
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.master_profile import MASTER_PROFILE
from vacancy_agent.domain.scan_job import ScanJob, ScanJobStatus
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.stored_application_draft import StoredApplicationDraft
from vacancy_agent.domain.stored_vacancy import StoredVacancy
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.observability.json_logging import JsonFormatter

_KNOWN_FACT_ID = MASTER_PROFILE.facts[0].id

_NOW = datetime(2026, 8, 27, 9, 0, 0, tzinfo=UTC)


def _vacancy(hh_id: str, **overrides: object) -> Vacancy:
    fields: dict[str, object] = {
        "hh_id": hh_id,
        "title": "AI-интегратор",
        "company": "Example",
        "salary_from": 200_000,
        "salary_to": None,
        "salary_currency": "RUR",
        "salary_gross": True,
        "description": "Описание",
        "requirements": "Требования",
        "key_skills": ["Python"],
        "experience": "От 1 года до 3 лет",
        "employment": "Полная занятость",
        "work_format": ["Удалённая работа"],
        "area": "Москва",
        "url": f"https://example.test/vacancy/{hh_id}",
        "published_at": _NOW,
        "fetched_at": _NOW,
    }
    fields.update(overrides)
    return Vacancy(**fields)


def _stored(hh_id: str, **overrides: object) -> StoredVacancy:
    fields: dict[str, object] = {
        "vacancy": _vacancy(hh_id),
        "filter_result": FilterResult(accepted=True, reasons=["target_title_match"]),
        "status": VacancyStatus.NEW,
        "assessment": None,
        "qwen_model": None,
        "qwen_min_score": None,
        "qwen_passed": None,
    }
    fields.update(overrides)
    return StoredVacancy(**fields)


class _FakeRepository:
    """Фейк, реализующий и `VacancyReadRepository` (TASK-009), и
    `ApplicationDraftRepository` (TASK-011) — как и настоящий
    `SqliteVacancyRepository`, структурно удовлетворяет обоим протоколам
    одновременно. `create_app` принимает фабрику как параметр, поэтому
    подмена не требует `app.dependency_overrides`."""

    def __init__(self, items: list[StoredVacancy]) -> None:
        self._items = items
        self.drafts: dict[str, StoredApplicationDraft] = {}
        self.closed = False
        self.ping_error: Exception | None = None
        self.close_error: Exception | None = None

    def ping(self) -> None:
        if self.ping_error is not None:
            raise self.ping_error

    def get(self, hh_id: str) -> StoredVacancy | None:
        for item in self._items:
            if item.vacancy.hh_id == hh_id:
                return item
        return None

    def get_vacancy(self, vacancy_id: str) -> StoredVacancy | None:
        return self.get(vacancy_id)

    def list_vacancies(self, *, limit: int, offset: int) -> list[StoredVacancy]:
        return self._items[offset : offset + limit]

    def count_vacancies(self) -> int:
        return len(self._items)

    def get_application_draft(self, vacancy_id: str) -> StoredApplicationDraft | None:
        return self.drafts.get(vacancy_id)

    def save_application_draft(self, stored: StoredApplicationDraft) -> bool:
        if stored.draft.vacancy_id in self.drafts:
            return False
        self.drafts[stored.draft.vacancy_id] = stored
        return True

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _FakeGenerator:
    """`generate` возвращает `ApplicationDraftPlan` (TASK-012) — реальный
    `validate_plan`/`render_application_draft_content` внутри
    `prepare_application_draft` превращает его в `ApplicationDraftContent`,
    эта заглушка content не строит."""

    def __init__(
        self,
        plan: ApplicationDraftPlan | None = None,
        *,
        model: str = "fake-model",
        fail_with: Exception | None = None,
    ) -> None:
        self.model = model
        self._plan = plan
        self._fail_with = fail_with
        self.generate_calls = 0
        self.closed = False

    def generate(self, vacancy: object, assessment: object, profile: object) -> object:
        self.generate_calls += 1
        if self._fail_with is not None:
            raise self._fail_with
        assert self._plan is not None
        return self._plan

    def close(self) -> None:
        self.closed = True


def _valid_plan() -> ApplicationDraftPlan:
    """`requirement_quote="Требования"` — точная, дословная подстрока
    `_vacancy`'s default `requirements="Требования"` (TASK-012)."""

    return ApplicationDraftPlan(
        requirements=[
            RequirementEvidenceSelection(
                requirement_quote="Требования", evidence_fact_ids=[_KNOWN_FACT_ID]
            )
        ],
        resume_fact_ids=[],
        cover_letter_fact_ids=[_KNOWN_FACT_ID],
    )


def _eligible_stored(hh_id: str, **overrides: object) -> StoredVacancy:
    fields: dict[str, object] = {
        "assessment": VacancyAssessment(
            schema_version="1", score=90, relevant=True, recommendation="strong_match"
        ),
        "qwen_model": "test-model",
        "qwen_min_score": 70,
        "qwen_passed": True,
    }
    fields.update(overrides)
    return _stored(hh_id, **fields)


def _forbidden_generator_factory() -> object:
    pytest.fail("generator_factory не должен вызываться при ошибке пользовательского ввода")


def _client(
    items: list[StoredVacancy],
    *,
    generator_factory: ApplicationDraftGeneratorFactory | None = None,
    model_resolver: ApplicationDraftModelResolver | None = None,
) -> TestClient:
    # Одна и та же фейковая инстанция на все вызовы фабрики — так же, как
    # реальный SQLite-файл переживает открытие/закрытие отдельных
    # соединений: без этого `drafts`, сохранённые в save-фазе одного
    # запроса, не были бы видны следующему запросу или read-фазе того же
    # `prepare_application_draft` (найдено при первом прогоне этих тестов).
    repository = _FakeRepository(items)
    # TASK-011 ADR-049: большинству тестов достаточно того, что
    # `model_resolver` вернёт `.model` того же fake-generator'а, который уже
    # передан как `generator_factory` — явно переопределять его нужно
    # только тестам, которые проверяют смену модели или отсутствие
    # generator_factory/model_resolver. Читать `.model` у фейка бесплатно
    # (в отличие от реального `LMStudioApplicationDraftGenerator`, где это
    # означало бы лишний httpx.Client — там `api/main.py` использует
    # отдельный дешёвый `_resolve_model_name`, не этот приём).
    if model_resolver is None and generator_factory is not None:
        model_resolver = _model_resolver_from_generator_factory(generator_factory)
    return TestClient(
        create_app(
            lambda: repository,
            generator_factory=generator_factory,
            model_resolver=model_resolver,
        )
    )


def _model_resolver_from_generator_factory(
    generator_factory: ApplicationDraftGeneratorFactory,
) -> ApplicationDraftModelResolver:
    def resolve() -> str:
        return generator_factory().model

    return resolve


def test_health_returns_ok_without_touching_repository() -> None:
    calls: list[str] = []

    class _ExplodingRepository:
        def get(self, hh_id: str) -> StoredVacancy | None:
            calls.append("get")
            raise AssertionError("health не должен обращаться к репозиторию")

        def list_vacancies(self, *, limit: int, offset: int) -> list[StoredVacancy]:
            calls.append("list")
            raise AssertionError("health не должен обращаться к репозиторию")

        def count_vacancies(self) -> int:
            calls.append("count")
            raise AssertionError("health не должен обращаться к репозиторию")

        def close(self) -> None:
            calls.append("close")

    client = TestClient(create_app(lambda: _ExplodingRepository()))

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert calls == []


# --- /ready (TASK-S2.1) ---


def test_ready_returns_200_when_repository_ping_succeeds() -> None:
    client = _client([])

    response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_returns_503_when_repository_ping_fails() -> None:
    """Инвариант TASK-S2.1: `/ready` не должен пересказывать raw-исключение
    адаптера (DSN/SQL) в HTTP-ответе клиенту."""

    repository = _FakeRepository([])
    repository.ping_error = RuntimeError("connection to server at 'db-secret-host' failed")
    client = TestClient(create_app(lambda: repository))

    response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert "db-secret-host" not in response.text


def test_ready_returns_503_when_repository_factory_raises() -> None:
    def _factory() -> _FakeRepository:
        raise RuntimeError("postgresql://user:pass@host/db unreachable")

    client = TestClient(create_app(_factory))

    response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert "user:pass" not in response.text


def test_ready_closes_repository_after_ping() -> None:
    repository = _FakeRepository([])
    client = TestClient(create_app(lambda: repository))

    client.get("/api/v1/ready")

    assert repository.closed is True


def test_ready_returns_200_even_when_close_after_successful_ping_fails() -> None:
    """Review-fix TASK-S2.1 [P2]: если `ping()` прошёл, но `close()` после
    него бросает (например, адаптер уже разорвал соединение), это не
    должно превратить успешный readiness-check в необработанный `500`."""

    repository = _FakeRepository([])
    repository.close_error = RuntimeError("connection to server at 'db-secret-host' lost")
    client = TestClient(create_app(lambda: repository))

    response = client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_returns_503_not_500_when_ping_and_close_both_fail() -> None:
    """Review-fix TASK-S2.1 [P2]: `close()`, вызванный из `finally` после
    неудачного `ping()`, не должен подменить собой уже подготовленный
    `503` — иначе клиент увидел бы необработанный `500` вместо
    задокументированного контракта readiness."""

    repository = _FakeRepository([])
    repository.ping_error = RuntimeError("ping failed: db-secret-host")
    repository.close_error = RuntimeError("close failed too: db-secret-host")
    client = TestClient(create_app(lambda: repository))

    response = client.get("/api/v1/ready")

    assert response.status_code == 503
    assert "db-secret-host" not in response.text


def test_list_vacancies_default_pagination() -> None:
    client = _client([_stored("1"), _stored("2")])

    response = client.get("/api/v1/vacancies")

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == ["1", "2"]


def test_list_vacancies_custom_pagination() -> None:
    client = _client([_stored(str(i)) for i in range(5)])

    response = client.get("/api/v1/vacancies", params={"limit": 2, "offset": 2})

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 2
    assert body["offset"] == 2
    assert body["total"] == 5
    assert [item["id"] for item in body["items"]] == ["2", "3"]


def test_list_vacancies_summary_omits_full_assessment() -> None:
    stored = _stored(
        "1",
        assessment=VacancyAssessment(
            schema_version="1",
            score=82,
            relevant=True,
            strengths=[Strength(skill="Python", evidence="3 года")],
            gaps=[Gap(skill="Docker", importance="medium")],
            risks=[],
            recommendation="review",
        ),
        qwen_model="deepseek-r1-0528-qwen3-8b",
        qwen_min_score=70,
        qwen_passed=True,
    )
    client = _client([stored])

    item = client.get("/api/v1/vacancies").json()["items"][0]

    assert item["qwen_score"] == 82
    assert item["qwen_passed"] is True
    assert "assessment" not in item
    assert "qwen_model" not in item


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
def test_list_vacancies_rejects_invalid_pagination(params: dict[str, int]) -> None:
    client = _client([])

    response = client.get("/api/v1/vacancies", params=params)

    assert response.status_code == 422


def test_get_vacancy_detail_includes_filter_and_qwen_metadata() -> None:
    stored = _stored(
        "1",
        assessment=VacancyAssessment(
            schema_version="1",
            score=82,
            relevant=True,
            strengths=[],
            gaps=[],
            risks=["требуется релокация"],
            recommendation="review",
        ),
        qwen_model="deepseek-r1-0528-qwen3-8b",
        qwen_min_score=70,
        qwen_passed=True,
    )
    client = _client([stored])

    response = client.get("/api/v1/vacancies/1")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "1"
    assert body["filter_result"]["accepted"] is True
    assert body["assessment"]["score"] == 82
    assert body["qwen_model"] == "deepseek-r1-0528-qwen3-8b"
    assert body["qwen_min_score"] == 70
    assert body["qwen_passed"] is True


def test_get_vacancy_detail_returns_404_for_unknown_id() -> None:
    client = _client([])

    response = client.get("/api/v1/vacancies/unknown")

    assert response.status_code == 404
    assert response.json() == {"detail": "Вакансия не найдена"}


def test_openapi_declares_only_the_declared_routes() -> None:
    """Явная проверка требований TASK-009/TASK-011/TASK-S2.2: `POST /scan` не
    появляется в API даже случайно (например, если бы кто-то позже
    импортировал router из другого модуля), а из новых routes есть ровно
    один `POST` и один `GET` по заявленным путям (обратите внимание: `POST`
    — на `application-drafts` во множественном числе, `GET` — на
    `application-draft` в единственном, ровно как в `docs/TASK-011.md`;
    `POST /api/v1/jobs/scan` и `GET /api/v1/jobs/{job_id}` — TASK-S2.2;
    `GET /api/v1/metrics` — TASK-S2.5)."""

    client = _client([])

    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths) == {
        "/api/v1/health",
        "/api/v1/ready",
        "/api/v1/metrics",
        "/api/v1/vacancies",
        "/api/v1/vacancies/{vacancy_id}",
        "/api/v1/vacancies/{vacancy_id}/application-drafts",
        "/api/v1/vacancies/{vacancy_id}/application-draft",
        "/api/v1/jobs/scan",
        "/api/v1/jobs/{job_id}",
    }
    assert set(paths["/api/v1/health"]) == {"get"}
    assert set(paths["/api/v1/ready"]) == {"get"}
    assert set(paths["/api/v1/metrics"]) == {"get"}
    assert set(paths["/api/v1/vacancies"]) == {"get"}
    assert set(paths["/api/v1/vacancies/{vacancy_id}"]) == {"get"}
    assert set(paths["/api/v1/vacancies/{vacancy_id}/application-drafts"]) == {"post"}
    assert set(paths["/api/v1/vacancies/{vacancy_id}/application-draft"]) == {"get"}
    assert set(paths["/api/v1/jobs/scan"]) == {"post"}
    assert set(paths["/api/v1/jobs/{job_id}"]) == {"get"}


def test_responses_do_not_leak_secrets_or_db_path() -> None:
    client = _client([_stored("1")])

    for response in (
        client.get("/api/v1/health"),
        client.get("/api/v1/vacancies"),
        client.get("/api/v1/vacancies/1"),
    ):
        text = response.text
        assert "TELEGRAM_BOT_TOKEN" not in text
        assert "SUPERJOB_API_KEY" not in text
        assert "sqlite:///" not in text
        assert ".env" not in text


def test_list_and_detail_and_health_work_with_an_exploding_generator_factory() -> None:
    """`docs/TASK-011.md`: health/list/detail не читают Qwen settings и не
    создают generator — доказательство, что даже если `generator_factory`
    сконфигурирована, но упала бы при вызове, эти routes её не трогают."""

    client = _client([_stored("1")], generator_factory=_forbidden_generator_factory)

    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/vacancies").status_code == 200
    assert client.get("/api/v1/vacancies/1").status_code == 200


# --- POST/GET application-drafts (TASK-011, ADR-034/ADR-035) ---


def test_post_application_draft_happy_path_returns_exact_response() -> None:
    stored = _eligible_stored("1")
    generator = _FakeGenerator(_valid_plan(), model="deepseek-r1-0528-qwen3-8b")
    client = _client([stored], generator_factory=lambda: generator)

    response = client.post(
        "/api/v1/vacancies/1/application-drafts",
        json={"confirm_local_generation": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["vacancy_id"] == "1"
    assert body["draft"]["profile_version"] == MASTER_PROFILE.version
    assert body["draft"]["content"]["requirements"][0]["requirement"] == "Требования"
    assert body["draft"]["content"]["requirements"][0]["coverage"] == "partial"
    assert body["model"] == "deepseek-r1-0528-qwen3-8b"
    assert body["prompt_version"] == "3"
    assert body["reused"] is False
    assert generator.generate_calls == 1


def test_post_application_draft_second_call_is_idempotent() -> None:
    stored = _eligible_stored("1")
    generator = _FakeGenerator(_valid_plan())
    client = _client([stored], generator_factory=lambda: generator)

    first = client.post(
        "/api/v1/vacancies/1/application-drafts", json={"confirm_local_generation": True}
    )
    second = client.post(
        "/api/v1/vacancies/1/application-drafts", json={"confirm_local_generation": True}
    )

    assert first.json()["reused"] is False
    assert second.json()["reused"] is True
    assert second.json()["draft"] == first.json()["draft"]
    assert generator.generate_calls == 1


def test_post_application_draft_returns_404_for_unknown_vacancy() -> None:
    generator = _FakeGenerator(_valid_plan())
    client = _client([], generator_factory=lambda: generator)

    response = client.post(
        "/api/v1/vacancies/unknown/application-drafts",
        json={"confirm_local_generation": True},
    )

    assert response.status_code == 404
    assert generator.generate_calls == 0


def test_post_application_draft_returns_409_when_not_eligible() -> None:
    stored = _stored("1", filter_result=FilterResult(accepted=False, rejected_by=["x"]))
    generator = _FakeGenerator(_valid_plan())
    client = _client([stored], generator_factory=lambda: generator)

    response = client.post(
        "/api/v1/vacancies/1/application-drafts", json={"confirm_local_generation": True}
    )

    assert response.status_code == 409
    assert generator.generate_calls == 0


def test_post_application_draft_returns_409_on_concurrency_conflict() -> None:
    """Codex review-fix (ADR-050): `ApplicationDraftConcurrencyError`
    (проигранная гонка дважды подряд с несовпадающей версией) должна
    отображаться в `409`, не приводить к сырому `500`. Здесь ошибка
    поднята напрямую генератором — сама race-логика проверена в
    `test_application_drafts_service.py`; этот тест проверяет только
    HTTP-отображение."""

    stored = _eligible_stored("1")
    generator = _FakeGenerator(
        fail_with=ApplicationDraftConcurrencyError(
            "Конкурентная запись помешала сохранить черновик этой версии — повторите запрос"
        )
    )
    client = _client([stored], generator_factory=lambda: generator)

    response = client.post(
        "/api/v1/vacancies/1/application-drafts", json={"confirm_local_generation": True}
    )

    assert response.status_code == 409


def test_post_application_draft_returns_503_when_generator_factory_missing() -> None:
    client = _client([_eligible_stored("1")])  # generator_factory не передана

    response = client.post(
        "/api/v1/vacancies/1/application-drafts", json={"confirm_local_generation": True}
    )

    assert response.status_code == 503


def test_post_application_draft_returns_503_on_network_error() -> None:
    generator = _FakeGenerator(fail_with=ApplicationDraftNetworkError("LM Studio недоступен"))
    client = _client([_eligible_stored("1")], generator_factory=lambda: generator)

    response = client.post(
        "/api/v1/vacancies/1/application-drafts", json={"confirm_local_generation": True}
    )

    assert response.status_code == 503


def test_post_application_draft_returns_502_on_invalid_model_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_marker = "PRIVATE_MODEL_TEXT_MUST_NOT_LEAK"
    generator = _FakeGenerator(
        fail_with=ApplicationDraftResponseError(
            secret_marker, category="pydantic_cross_field"
        )
    )
    client = _client([_eligible_stored("1")], generator_factory=lambda: generator)

    response = client.post(
        "/api/v1/vacancies/1/application-drafts", json={"confirm_local_generation": True}
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Ответ модели не прошёл валидацию"}
    assert "category=pydantic_cross_field" in caplog.text
    assert secret_marker not in caplog.text


def test_post_application_draft_logs_json_shape_diagnostics_without_leaking_raw_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ADR-044: `diagnostics` (безопасные числа/позиции/booleans о форме
    content) должен попасть в локальный лог рядом с `category`, но HTTP
    `502`-ответ пользователю остаётся тем же generic текстом, и raw content
    модели нигде не просачивается — ни в JSON-ответ, ни в `caplog`."""

    secret_marker = "PRIVATE_MODEL_TEXT_MUST_NOT_LEAK"
    generator = _FakeGenerator(
        fail_with=ApplicationDraftResponseError(
            secret_marker,
            category="content_json_invalid",
            diagnostics={
                "json_error_code": "expecting_value",
                "content_length": 42,
                "error_pos": 0,
                "distance_from_end": 42,
                "starts_with_brace": False,
                "ends_with_brace": True,
                "has_markdown_fence": False,
                "has_think_tags": True,
                "has_leading_text": True,
                "has_trailing_text": False,
            },
        )
    )
    client = _client([_eligible_stored("1")], generator_factory=lambda: generator)

    response = client.post(
        "/api/v1/vacancies/1/application-drafts", json={"confirm_local_generation": True}
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Ответ модели не прошёл валидацию"}
    assert "category=content_json_invalid" in caplog.text
    assert "has_think_tags" in caplog.text
    assert "expecting_value" in caplog.text
    assert secret_marker not in caplog.text
    assert secret_marker not in response.text


def test_post_application_draft_rejects_marker_placed_directly_in_json_error_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Codex review-fix (ADR-046): предыдущий тест кладёт маркер в
    сообщение исключения, не в само поле `json_error_code` — этот
    воспроизводит находку буквально: маркер передан КАК ЗНАЧЕНИЕ
    `json_error_code`, единственного строкового ключа в allow-list
    `diagnostics`. Граница на конструкторе `ApplicationDraftResponseError`
    (`domain/application_draft_errors.py: JSON_ERROR_CODES`) должна
    отклонить его как значение вне замкнутого списка нормализованных
    кодов — не пропустить только потому, что это строка. Ни HTTP-ответ,
    ни `caplog` не должны содержать маркер."""

    secret_marker = "PRIVATE_MODEL_TEXT_MUST_NOT_LEAK"
    generator = _FakeGenerator(
        fail_with=ApplicationDraftResponseError(
            "Ответ модели — не валидный JSON",
            category="content_json_invalid",
            diagnostics={
                "json_error_code": secret_marker,
                "content_length": 42,
            },
        )
    )
    client = _client([_eligible_stored("1")], generator_factory=lambda: generator)

    response = client.post(
        "/api/v1/vacancies/1/application-drafts", json={"confirm_local_generation": True}
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Ответ модели не прошёл валидацию"}
    assert "category=content_json_invalid" in caplog.text
    assert "content_length" in caplog.text
    assert secret_marker not in caplog.text
    assert secret_marker not in response.text


def test_post_application_draft_returns_502_and_does_not_save_on_unknown_evidence_id() -> None:
    bad_plan = ApplicationDraftPlan(
        requirements=[
            RequirementEvidenceSelection(
                requirement_quote="Требования", evidence_fact_ids=["does.not.exist.in.profile"]
            )
        ],
        resume_fact_ids=[],
        cover_letter_fact_ids=["does.not.exist.in.profile"],
    )
    generator = _FakeGenerator(bad_plan)
    client = _client([_eligible_stored("1")], generator_factory=lambda: generator)

    response = client.post(
        "/api/v1/vacancies/1/application-drafts", json={"confirm_local_generation": True}
    )

    assert response.status_code == 502
    assert client.get("/api/v1/vacancies/1/application-draft").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"confirm_local_generation": None},
        {"confirm_local_generation": False},
        {"confirm_local_generation": "true"},
        {"confirm_local_generation": "false"},
        {"confirm_local_generation": 1},
        {"confirm_local_generation": 0},
        {"confirm_local_generation": True, "extra_field": "nope"},
    ],
)
def test_post_application_draft_rejects_invalid_confirmation(body: dict[str, object]) -> None:
    client = _client([_eligible_stored("1")], generator_factory=_forbidden_generator_factory)

    response = client.post("/api/v1/vacancies/1/application-drafts", json=body)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "vacancy_id",
    ["", " ", "a" * 256, "has spaces", "invalid/slash", "@starts-with-symbol"],
)
def test_post_application_draft_rejects_invalid_vacancy_id(vacancy_id: str) -> None:
    client = _client([], generator_factory=_forbidden_generator_factory)

    response = client.post(
        f"/api/v1/vacancies/{vacancy_id}/application-drafts",
        json={"confirm_local_generation": True},
    )

    # Пустой path segment вообще не совпадает с route (404 у самого
    # роутинга), остальные некорректные значения — 422 от валидации `Path`.
    assert response.status_code in (404, 422)


def test_post_application_draft_accepts_255_char_vacancy_id_boundary() -> None:
    vacancy_id = "a" * 255
    generator = _FakeGenerator(_valid_plan())
    client = _client([_eligible_stored(vacancy_id)], generator_factory=lambda: generator)

    response = client.post(
        f"/api/v1/vacancies/{vacancy_id}/application-drafts",
        json={"confirm_local_generation": True},
    )

    assert response.status_code == 200


def test_get_application_draft_returns_stored_draft() -> None:
    stored_vacancy = _eligible_stored("1")
    generator = _FakeGenerator(_valid_plan())
    client = _client([stored_vacancy], generator_factory=lambda: generator)
    client.post("/api/v1/vacancies/1/application-drafts", json={"confirm_local_generation": True})

    response = client.get("/api/v1/vacancies/1/application-draft")

    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["vacancy_id"] == "1"
    assert body["reused"] is True


def test_get_application_draft_returns_404_when_missing() -> None:
    client = _client([_eligible_stored("1")])

    response = client.get("/api/v1/vacancies/1/application-draft")

    assert response.status_code == 404


def test_post_and_get_application_draft_do_not_leak_secrets_or_raw_model_hints() -> None:
    generator = _FakeGenerator(_valid_plan())
    client = _client([_eligible_stored("1")], generator_factory=lambda: generator)

    post_response = client.post(
        "/api/v1/vacancies/1/application-drafts", json={"confirm_local_generation": True}
    )
    get_response = client.get("/api/v1/vacancies/1/application-draft")

    for response in (post_response, get_response):
        text = response.text
        assert "TELEGRAM_BOT_TOKEN" not in text
        assert "SUPERJOB_API_KEY" not in text
        assert "sqlite:///" not in text
        assert ".env" not in text


# --- /api/v1/jobs (TASK-S2.2) ---

_JOB_NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def _job(**overrides: object) -> ScanJob:
    fields: dict[str, object] = {
        "id": uuid4(),
        "idempotency_key": "key-1",
        "status": ScanJobStatus.QUEUED,
        "attempt": 0,
        "max_attempts": 3,
        "available_at": _JOB_NOW,
        "lease_expires_at": None,
        "created_at": _JOB_NOW,
        "updated_at": _JOB_NOW,
        "result": None,
        "error_code": None,
    }
    fields.update(overrides)
    return ScanJob(**fields)


class _FakeScanJobQueue:
    """Фейк `domain.ports.ScanJobQueue` — держит job'ы в памяти,
    `enqueue` — та же UPSERT-по-idempotency_key семантика, что реальный
    `PostgresScanJobQueue` (TASK-S2.2)."""

    def __init__(self, jobs: dict[str, ScanJob] | None = None) -> None:
        self._by_key: dict[str, ScanJob] = dict(jobs or {})
        self._by_id: dict[UUID, ScanJob] = {job.id: job for job in self._by_key.values()}
        self.closed = False
        self.enqueue_error: Exception | None = None
        self.get_error: Exception | None = None

    def enqueue(self, idempotency_key: str) -> tuple[ScanJob, bool]:
        if self.enqueue_error is not None:
            raise self.enqueue_error
        existing = self._by_key.get(idempotency_key)
        if existing is not None:
            return existing, True
        job = _job(idempotency_key=idempotency_key)
        self._by_key[idempotency_key] = job
        self._by_id[job.id] = job
        return job, False

    def claim_one(self, *, now: datetime, lease_seconds: int) -> ScanJob | None:
        raise NotImplementedError

    def mark_succeeded(self, job_id: UUID, *, attempt: int, result: Mapping[str, object]) -> None:
        raise NotImplementedError

    def mark_for_retry(
        self, job_id: UUID, *, attempt: int, available_at: datetime, error_code: str
    ) -> None:
        raise NotImplementedError

    def mark_failed(self, job_id: UUID, *, attempt: int, error_code: str) -> None:
        raise NotImplementedError

    def get(self, job_id: UUID) -> ScanJob | None:
        if self.get_error is not None:
            raise self.get_error
        return self._by_id.get(job_id)

    def close(self) -> None:
        self.closed = True


def _client_with_scan_job_queue(queue: _FakeScanJobQueue | None) -> TestClient:
    repository = _FakeRepository([])
    factory = None if queue is None else (lambda: queue)
    return TestClient(create_app(lambda: repository, scan_job_queue_factory=factory))


def test_enqueue_scan_job_returns_202_and_new_job() -> None:
    queue = _FakeScanJobQueue()
    client = _client_with_scan_job_queue(queue)

    response = client.post("/api/v1/jobs/scan", json={"idempotency_key": "run-2026-09-20"})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["reused"] is False
    assert UUID(body["job_id"]) in queue._by_id


def test_enqueue_scan_job_is_idempotent_for_the_same_key() -> None:
    queue = _FakeScanJobQueue()
    client = _client_with_scan_job_queue(queue)

    first = client.post("/api/v1/jobs/scan", json={"idempotency_key": "same-key"})
    second = client.post("/api/v1/jobs/scan", json={"idempotency_key": "same-key"})

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert first.json()["reused"] is False
    assert second.json()["reused"] is True


def test_enqueue_scan_job_rejects_extra_fields() -> None:
    client = _client_with_scan_job_queue(_FakeScanJobQueue())

    response = client.post(
        "/api/v1/jobs/scan", json={"idempotency_key": "k", "unexpected": "x"}
    )

    assert response.status_code == 422


def test_enqueue_scan_job_returns_503_when_queue_not_configured() -> None:
    client = _client_with_scan_job_queue(None)

    response = client.post("/api/v1/jobs/scan", json={"idempotency_key": "k"})

    assert response.status_code == 503


def test_enqueue_scan_job_returns_503_without_leaking_raw_error_when_factory_fails() -> None:
    def _factory() -> _FakeScanJobQueue:
        raise RuntimeError("postgresql://user:pass@host/db unreachable")

    repository = _FakeRepository([])
    client = TestClient(create_app(lambda: repository, scan_job_queue_factory=_factory))

    response = client.post("/api/v1/jobs/scan", json={"idempotency_key": "k"})

    assert response.status_code == 503
    assert "user:pass" not in response.text


def test_enqueue_scan_job_returns_503_when_backend_rejects_non_postgres_scheme() -> None:
    """TASK-S2.2, «Архитектурная граница»: SQLite backend отвечает
    безопасным `503` на очереди, не 500/раскрытием деталей."""

    def _factory() -> _FakeScanJobQueue:
        raise ValueError("Очередь сканирования доступна только для 'postgresql://'")

    repository = _FakeRepository([])
    client = TestClient(create_app(lambda: repository, scan_job_queue_factory=_factory))

    response = client.post("/api/v1/jobs/scan", json={"idempotency_key": "k"})

    assert response.status_code == 503


def test_enqueue_scan_job_closes_queue() -> None:
    queue = _FakeScanJobQueue()
    client = _client_with_scan_job_queue(queue)

    client.post("/api/v1/jobs/scan", json={"idempotency_key": "k"})

    assert queue.closed is True


def test_get_scan_job_returns_status_for_known_job() -> None:
    job = _job(
        status=ScanJobStatus.SUCCEEDED,
        attempt=1,
        result={"fetched": 3, "notified": 1},
    )
    queue = _FakeScanJobQueue({job.idempotency_key: job})
    client = _client_with_scan_job_queue(queue)

    response = client.get(f"/api/v1/jobs/{job.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == str(job.id)
    assert body["status"] == "succeeded"
    assert body["attempt"] == 1
    assert body["result"] == {"fetched": 3, "notified": 1}
    assert body["error_code"] is None


def test_get_scan_job_returns_404_for_unknown_uuid() -> None:
    client = _client_with_scan_job_queue(_FakeScanJobQueue())

    response = client.get(f"/api/v1/jobs/{uuid4()}")

    assert response.status_code == 404


def test_get_scan_job_returns_404_for_malformed_job_id() -> None:
    client = _client_with_scan_job_queue(_FakeScanJobQueue())

    response = client.get("/api/v1/jobs/not-a-uuid")

    assert response.status_code == 404


def test_get_scan_job_returns_503_when_queue_not_configured() -> None:
    client = _client_with_scan_job_queue(None)

    response = client.get(f"/api/v1/jobs/{uuid4()}")

    assert response.status_code == 503


def test_get_scan_job_does_not_leak_raw_error() -> None:
    job = _job()
    queue = _FakeScanJobQueue({job.idempotency_key: job})
    queue.get_error = RuntimeError("connection to server at 'db-secret-host' failed")
    client = _client_with_scan_job_queue(queue)

    response = client.get(f"/api/v1/jobs/{job.id}")

    assert response.status_code == 503
    assert "db-secret-host" not in response.text


def test_get_scan_job_reports_error_code_for_failed_job() -> None:
    job = _job(status=ScanJobStatus.FAILED, attempt=3, error_code="internal_error")
    queue = _FakeScanJobQueue({job.idempotency_key: job})
    client = _client_with_scan_job_queue(queue)

    response = client.get(f"/api/v1/jobs/{job.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_code"] == "internal_error"
    assert body["result"] is None


def test_get_scan_job_reports_retry_wait_status() -> None:
    job = _job(
        status=ScanJobStatus.RETRY_WAIT,
        attempt=1,
        available_at=_JOB_NOW + timedelta(seconds=30),
        error_code="network_error",
    )
    queue = _FakeScanJobQueue({job.idempotency_key: job})
    client = _client_with_scan_job_queue(queue)

    response = client.get(f"/api/v1/jobs/{job.id}")

    assert response.status_code == 200
    assert response.json()["status"] == "retry_wait"


# --- TASK-S2.5: request_id, logging, metrics ---


def test_response_echoes_a_valid_incoming_x_request_id() -> None:
    client = _client([])

    response = client.get("/api/v1/health", headers={"X-Request-ID": "client-supplied-id-1"})

    assert response.headers["X-Request-ID"] == "client-supplied-id-1"


def test_response_generates_a_request_id_when_none_supplied() -> None:
    client = _client([])

    response = client.get("/api/v1/health")

    assert response.headers["X-Request-ID"]


def test_response_generates_a_fresh_request_id_when_incoming_one_is_invalid() -> None:
    client = _client([])

    response = client.get("/api/v1/health", headers={"X-Request-ID": "has spaces, invalid"})

    assert response.headers["X-Request-ID"] != "has spaces, invalid"
    assert response.headers["X-Request-ID"]


def test_response_carries_x_request_id_even_on_an_unhandled_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Codex review (post-S2.5 commits e48ea1e, a413394): an unhandled
    exception used to propagate straight to Starlette's outer
    `ServerErrorMiddleware`, whose 500 response is built OUTSIDE
    `observability_middleware` — so it never got `X-Request-ID`, breaking
    criterion №2 ("each response"). The middleware must catch it itself,
    still echo the header, still log `http.request_failed`/`http.request`,
    and still match Starlette's own default 500 (`PlainTextResponse`,
    `text/plain; charset=utf-8`) — round 1 built a bare `Response(...)`
    instead, silently dropping `Content-Type`. The exception's own message
    (may contain a DSN/secret) must never reach ANY `LogRecord` at all —
    not even inside `record.exc_info`, which round 1 still populated via
    `exc_info=True` (a real leak vector: a second handler without our
    `JsonFormatter`, or the record simply living on in `caplog`, would have
    exposed the raw exception). Only its type name and frame locations,
    extracted before any logging call, are safe to attach via `extra=`."""

    class _ExplodingRepository:
        def list_vacancies(self, *, limit: int, offset: int) -> list[StoredVacancy]:
            raise RuntimeError("db dsn=postgresql://user:sk-live-should-never-appear@host/db")

        def count_vacancies(self) -> int:
            return 0

        def close(self) -> None:
            pass

    client = TestClient(create_app(lambda: _ExplodingRepository()))

    with caplog.at_level(logging.INFO):
        response = client.get(
            "/api/v1/vacancies", headers={"X-Request-ID": "corr-id-on-crash"}
        )

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "corr-id-on-crash"
    assert "sk-live-should-never-appear" not in response.text
    # Same as Starlette's own default `ServerErrorMiddleware` response —
    # a bare `Response(...)` (round 1) left this header unset entirely.
    assert response.headers["content-type"] == "text/plain; charset=utf-8"

    failed = next(r for r in caplog.records if r.getMessage() == "http.request_failed")
    http_request = next(r for r in caplog.records if r.getMessage() == "http.request")
    assert failed.request_id == "corr-id-on-crash"  # type: ignore[attr-defined]
    assert http_request.request_id == "corr-id-on-crash"  # type: ignore[attr-defined]
    assert http_request.status_code == 500  # type: ignore[attr-defined]

    # The raw exception must never touch a `LogRecord` at all — not even
    # as `record.exc_info`, which lives on in memory for as long as the
    # record does (e.g. this very `caplog` list) regardless of what any
    # one formatter chooses to print.
    assert failed.exc_info is None
    assert failed.exc_type == "RuntimeError"  # type: ignore[attr-defined]
    assert "sk-live-should-never-appear" not in json.dumps(failed.__dict__, default=str)

    # And, redundantly, what would actually have been written to stdout.
    formatted = JsonFormatter().format(failed)
    payload = json.loads(formatted)
    assert payload["exc_type"] == "RuntimeError"
    assert "sk-live-should-never-appear" not in formatted


def test_enqueue_scan_job_log_and_response_share_the_same_request_id_and_job_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TASK-S2.5, критерий №2: один `request_id` связывает HTTP-событие и
    `scan_job.enqueued`, и то же событие несёт `job_id`, которым worker
    помечает все свои дальнейшие события для этой job (TASK-S2.2)."""

    queue = _FakeScanJobQueue()
    client = _client_with_scan_job_queue(queue)

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/api/v1/jobs/scan",
            json={"idempotency_key": "daily-scan"},
            headers={"X-Request-ID": "corr-id-42"},
        )

    assert response.status_code == 202
    job_id = response.json()["job_id"]

    enqueued = next(r for r in caplog.records if r.getMessage() == "scan_job.enqueued")
    http_request = next(r for r in caplog.records if r.getMessage() == "http.request")
    assert enqueued.request_id == "corr-id-42"  # type: ignore[attr-defined]
    assert enqueued.job_id == job_id  # type: ignore[attr-defined]
    assert http_request.request_id == "corr-id-42"  # type: ignore[attr-defined]


def test_metrics_endpoint_reports_recorded_http_requests() -> None:
    client = _client([])

    client.get("/api/v1/health")
    client.get("/api/v1/health")
    client.get("/api/v1/vacancies/does-not-exist")

    response = client.get("/api/v1/metrics")

    assert response.status_code == 200
    body = response.json()
    counters = body["counters"]["http_requests_total"]
    assert counters["method=GET,path=/api/v1/health,status_code=200"] == 2
    assert counters["method=GET,path=/api/v1/vacancies/{vacancy_id},status_code=404"] == 1
    latency = body["latencies"]["http_request_duration_seconds"]
    assert latency["count"] >= 3


def test_metrics_endpoint_uses_route_templates_not_raw_path_values() -> None:
    """TASK-S2.5, критерий №3: путь с конкретным `vacancy_id` не должен
    заводить свою собственную метку — только объявленный route template,
    иначе кардинальность меток растёт без ограничения вместе с числом
    вакансий."""

    client = _client([])

    client.get("/api/v1/vacancies/some-specific-id-abc")
    client.get("/api/v1/vacancies/another-specific-id-xyz")

    counters = client.get("/api/v1/metrics").json()["counters"]["http_requests_total"]
    assert "method=GET,path=/api/v1/vacancies/{vacancy_id},status_code=404" in counters
    assert counters["method=GET,path=/api/v1/vacancies/{vacancy_id},status_code=404"] == 2
    assert not any("some-specific-id-abc" in key for key in counters)
    assert not any("another-specific-id-xyz" in key for key in counters)


def test_metrics_endpoint_counts_scan_job_enqueue_by_reused() -> None:
    client = _client_with_scan_job_queue(_FakeScanJobQueue())

    client.post("/api/v1/jobs/scan", json={"idempotency_key": "same-key"})
    client.post("/api/v1/jobs/scan", json={"idempotency_key": "same-key"})

    counters = client.get("/api/v1/metrics").json()["counters"]["scan_jobs_enqueued_total"]
    assert counters == {"reused=False": 1, "reused=True": 1}


def test_metrics_endpoint_response_contains_no_secrets_or_vacancy_data() -> None:
    generator = _FakeGenerator(_valid_plan())
    client = _client([_eligible_stored("1")], generator_factory=lambda: generator)

    client.get("/api/v1/vacancies/1")
    client.post(
        "/api/v1/vacancies/1/application-drafts", json={"confirm_local_generation": True}
    )

    text = client.get("/api/v1/metrics").text
    assert "TELEGRAM_BOT_TOKEN" not in text
    assert "SUPERJOB_API_KEY" not in text
    assert "sqlite:///" not in text
    assert _eligible_stored("1").vacancy.title not in text


def test_observability_failure_does_not_change_http_status_or_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TASK-S2.5, критерий №4: даже если сам observability-код сломан
    (здесь — `MetricsRegistry.increment` заменён на бросающий заглушку,
    в обход собственной защиты `MetricsRegistry`, чтобы проверить именно
    защиту в middleware), ответ API не должен измениться."""

    client = _client([])

    class _BrokenMetrics:
        def increment(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("metrics backend is on fire")

        def observe_latency(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("metrics backend is on fire")

        def snapshot(self) -> dict[str, object]:
            raise RuntimeError("metrics backend is on fire")

    client.app.state.metrics = _BrokenMetrics()  # type: ignore[attr-defined]

    with caplog.at_level(logging.WARNING):
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"]
    assert any("метрику" in r.getMessage() or "лог" in r.getMessage() for r in caplog.records)
