"""Contract-тесты `PostgresVacancyRepository` на реальной тестовой PostgreSQL
(TASK-S2.1, критерий готовности «одинаковый contract suite проходит для
SQLite и реальной PostgreSQL»).

Не mock/fake — реальное `psycopg`-соединение к тестовой БД, заданной
`TEST_DATABASE_URL` (`postgresql://...`). Явно пропускается, если эта
переменная не задана: пользователь Vacancy Agent отдельно разрешает
установку/запуск PostgreSQL (`docs/TASK-S2.1.md`, «Стоп-условия») — этот
файл не поднимает PostgreSQL сам и не считается тихо пройденным при
отсутствии тестовой БД (критерий готовности TASK-S2.1 №8): пока
`TEST_DATABASE_URL` не задан локально хотя бы один раз с зелёным результатом,
задача остаётся незавершённой.

Review-fix TASK-S2.1 [P1]: фикстура `repo` выполняет `DELETE FROM` на
`vacancies`/`application_drafts` перед каждым тестом, поэтому одного
`TEST_DATABASE_URL` недостаточно, чтобы тесты начали выполняться —
`_require_safe_test_dsn` ниже требует ЕЩЁ и явный
`TEST_DATABASE_URL_CONFIRM_DESTRUCTIVE=1`, и `test` в имени БД. Оба
условия — намеренно fail-closed: опечатка в DSN, указывающая на рабочую
или чужую БД, должна упасть с понятной ошибкой, а не тихо очистить чужие
данные.

Покрывает тот же минимум, что явно требует TASK-S2.1: save/get/list/count,
duplicate save, status update, draft insert/reuse/replace, foreign key и
rollback при ошибке — не дублирует один-в-один весь
`tests/unit/test_sqlite_repository.py` (около 40 тестов), а проверяет, что
`PostgresVacancyRepository` даёт те же наблюдаемые результаты, что
`SqliteVacancyRepository`, для этого минимального набора сценариев."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from vacancy_agent.persistence.postgres_repository import PostgresVacancyRepository

from vacancy_agent.config.settings import DatabaseSettings
from vacancy_agent.domain.application_draft import (
    ApplicationDraft,
    ApplicationDraftContent,
    EvidenceParagraph,
    RequirementMatch,
)
from vacancy_agent.domain.assessment import Gap, Strength, VacancyAssessment
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.repository_errors import VacancyNotFoundError
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.stored_application_draft import StoredApplicationDraft
from vacancy_agent.domain.vacancy import Vacancy

psycopg = pytest.importorskip("psycopg")

_TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")
_CONFIRM_ENV_VAR = "TEST_DATABASE_URL_CONFIRM_DESTRUCTIVE"
_CONFIRMED = os.environ.get(_CONFIRM_ENV_VAR, "") == "1"

pytestmark = pytest.mark.skipif(
    not _TEST_DSN,
    reason=(
        "TEST_DATABASE_URL не задан — PostgreSQL contract-тесты пропущены "
        "локально (TASK-S2.1, критерий готовности №8 остаётся открытым до "
        "зафиксированного прогона на реальной тестовой PostgreSQL)"
    ),
)


def _require_safe_test_dsn(dsn: str) -> None:
    """Fail-closed guard (review-fix TASK-S2.1 [P1]): фикстура `repo` ниже
    выполняет `DELETE FROM application_drafts`/`vacancies` перед КАЖДЫМ
    тестом. Опечатка в `TEST_DATABASE_URL` (например, случайно указанный
    на рабочую/чужую БД) не должна тихо очистить чужие данные — поэтому
    здесь требуются ОБА независимых явных сигнала, а не один:

    1. отдельная переменная окружения `TEST_DATABASE_URL_CONFIRM_DESTRUCTIVE=1`
       (сам факт наличия `TEST_DATABASE_URL` не считается согласием на
       разрушительные операции);
    2. имя базы данных в DSN должно содержать `test` — простая, но
       дешёвая вторая линия защиты от той же опечатки.

    Отсутствие любого из двух — жёсткий `pytest.fail()`, а не тихий
    `skip`: скрытая ошибка конфигурации, которая привела бы к DELETE в
    незащищённой БД, должна быть заметна, а не проигнорирована."""

    if not _CONFIRMED:
        pytest.fail(
            f"TEST_DATABASE_URL задан, но {_CONFIRM_ENV_VAR}=1 — нет. Эти тесты "
            "выполняют DELETE FROM vacancies/application_drafts перед каждым "
            "тестом (fail-closed guard, review-fix TASK-S2.1 [P1]) — установите "
            f"{_CONFIRM_ENV_VAR}=1, только если TEST_DATABASE_URL указывает на "
            "одноразовую тестовую PostgreSQL, данные которой не жалко потерять."
        )
    db_name = dsn.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in db_name.lower():
        pytest.fail(
            f"TEST_DATABASE_URL указывает на БД {db_name!r}, в имени которой нет "
            "'test' — fail-closed guard (review-fix TASK-S2.1 [P1]) отказывается "
            "выполнять DELETE FROM в базе, не похожей на одноразовую тестовую. "
            "Используйте отдельную тестовую БД с 'test' в имени."
        )

_NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)


def _vacancy(**overrides: object) -> Vacancy:
    fields: dict[str, object] = {
        "hh_id": "pg-12345",
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
        "url": "https://hh.ru/vacancy/pg-12345",
        "published_at": _NOW,
        "fetched_at": _NOW,
    }
    fields.update(overrides)
    return Vacancy(**fields)


def _filter_result(**overrides: object) -> FilterResult:
    fields: dict[str, object] = {
        "accepted": True,
        "reasons": ["target_title_match"],
        "rejected_by": [],
    }
    fields.update(overrides)
    return FilterResult(**fields)


def _assessment(**overrides: object) -> VacancyAssessment:
    fields: dict[str, object] = {
        "schema_version": "1",
        "score": 82,
        "relevant": True,
        "strengths": [Strength(skill="Python", evidence="3 года опыта")],
        "gaps": [Gap(skill="Docker", importance="medium")],
        "risks": ["требуется релокация"],
        "recommendation": "review",
    }
    fields.update(overrides)
    return VacancyAssessment(**fields)


def _stored_draft(vacancy_id: str = "pg-12345", **overrides: object) -> StoredApplicationDraft:
    fields: dict[str, object] = {
        "draft": ApplicationDraft(
            schema_version="1",
            vacancy_id=vacancy_id,
            profile_version="test-v1",
            content=ApplicationDraftContent(
                match_summary="Соответствие хорошее.",
                requirements=[
                    RequirementMatch(
                        requirement="Python",
                        coverage="full",
                        explanation="Есть опыт",
                        evidence_fact_ids=["skill.python"],
                    )
                ],
                resume_suggestions=[],
                cover_letter_paragraphs=[
                    EvidenceParagraph(text="Письмо.", evidence_fact_ids=["skill.python"])
                ],
                gaps=[],
                questions_for_user=[],
                warnings=[],
            ),
        ),
        "model": "deepseek-r1-0528-qwen3-8b",
        "prompt_version": "1",
        "created_at": _NOW,
    }
    fields.update(overrides)
    return StoredApplicationDraft(**fields)  # type: ignore[arg-type]


@pytest.fixture
def repo() -> Iterator["PostgresVacancyRepository"]:
    from vacancy_agent.persistence.postgres_repository import PostgresVacancyRepository

    _require_safe_test_dsn(_TEST_DSN)
    settings = DatabaseSettings(url=_TEST_DSN)
    repository = PostgresVacancyRepository.from_settings(settings)
    # Каждый тест начинает с пустых таблиц — контрактные тесты не должны
    # зависеть от порядка запуска или состояния, оставленного прошлым
    # прогоном на той же тестовой БД.
    with repository._connection.cursor() as cursor:
        cursor.execute("DELETE FROM application_drafts")
        cursor.execute("DELETE FROM vacancies")
    repository._connection.commit()
    try:
        yield repository
    finally:
        repository.close()


def test_ping_succeeds_on_live_connection(repo: "PostgresVacancyRepository") -> None:
    repo.ping()


def test_exists_is_false_for_unknown_hh_id(repo: "PostgresVacancyRepository") -> None:
    assert repo.exists("does-not-exist") is False


def test_save_round_trips_vacancy_and_assessment(repo: "PostgresVacancyRepository") -> None:
    vacancy = _vacancy()
    assessment = _assessment()

    repo.save(
        vacancy,
        _filter_result(),
        VacancyStatus.NEW,
        assessment=assessment,
        qwen_model="deepseek-r1",
        qwen_min_score=70,
        qwen_passed=True,
    )
    stored = repo.get("pg-12345")

    assert stored is not None
    assert stored.vacancy == vacancy
    assert stored.assessment == assessment
    assert stored.qwen_model == "deepseek-r1"
    assert stored.qwen_passed is True


def test_save_is_idempotent_for_repeated_hh_id(repo: "PostgresVacancyRepository") -> None:
    repo.save(_vacancy(title="First"), _filter_result(), VacancyStatus.NEW)
    repo.save(_vacancy(title="Second"), _filter_result(), VacancyStatus.NEW)

    stored = repo.get("pg-12345")
    assert stored is not None
    assert stored.vacancy.title == "First"
    assert repo.count_vacancies() == 1


def test_list_vacancies_and_count(repo: "PostgresVacancyRepository") -> None:
    older = _vacancy(hh_id="a", published_at=_NOW - timedelta(days=1))
    newer = _vacancy(hh_id="b", published_at=_NOW)
    repo.save(older, _filter_result(), VacancyStatus.NEW)
    repo.save(newer, _filter_result(), VacancyStatus.NEW)

    items = repo.list_vacancies(limit=10, offset=0)

    assert [item.vacancy.hh_id for item in items] == ["b", "a"]
    assert repo.count_vacancies() == 2


def test_update_status_happy_path_and_not_found(repo: "PostgresVacancyRepository") -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)

    repo.update_status("pg-12345", VacancyStatus.VIEWED)
    stored = repo.get("pg-12345")
    assert stored is not None
    assert stored.status == VacancyStatus.VIEWED

    with pytest.raises(VacancyNotFoundError):
        repo.update_status("does-not-exist", VacancyStatus.VIEWED)


def test_update_status_rejects_backward_transition_and_recovers(
    repo: "PostgresVacancyRepository",
) -> None:
    """Отклонённый переход откатывает транзакцию, но не оставляет
    соединение в состоянии, которое блокирует следующую операцию — тот же
    инвариант, что `tests/unit/test_sqlite_repository.py:
    test_repository_recovers_after_a_rejected_update`."""

    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)
    repo.update_status("pg-12345", VacancyStatus.VIEWED)

    with pytest.raises(ValueError, match="недопустим"):
        repo.update_status("pg-12345", VacancyStatus.NEW)

    stored = repo.get("pg-12345")
    assert stored is not None
    assert stored.status == VacancyStatus.VIEWED

    # Соединение остаётся рабочим после отката.
    repo.save(_vacancy(hh_id="pg-99999"), _filter_result(), VacancyStatus.NEW)
    assert repo.exists("pg-99999") is True


def test_save_and_get_application_draft_round_trips(repo: "PostgresVacancyRepository") -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)
    stored = _stored_draft()

    inserted = repo.save_application_draft(stored)
    fetched = repo.get_application_draft("pg-12345")

    assert inserted is True
    assert fetched == stored


def test_save_application_draft_reuses_same_version(repo: "PostgresVacancyRepository") -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)

    first = repo.save_application_draft(_stored_draft(model="same-model"))
    second = repo.save_application_draft(_stored_draft(model="same-model"))

    assert first is True
    assert second is False


def test_save_application_draft_replaces_stale_model(repo: "PostgresVacancyRepository") -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)
    repo.save_application_draft(_stored_draft(model="old-model"))

    replaced = repo.save_application_draft(_stored_draft(model="new-model"))

    assert replaced is True
    fetched = repo.get_application_draft("pg-12345")
    assert fetched is not None
    assert fetched.model == "new-model"


def test_save_application_draft_rejects_orphan_vacancy_id(
    repo: "PostgresVacancyRepository",
) -> None:
    orphan = _stored_draft(vacancy_id="does-not-exist")

    with pytest.raises(psycopg.Error):
        repo.save_application_draft(orphan)


def test_migrations_are_safe_to_reapply(repo: "PostgresVacancyRepository") -> None:
    """Повторное создание репозитория для той же БД не падает на уже
    применённых миграциях (TASK-S2.1, критерий готовности №4)."""

    from vacancy_agent.persistence.postgres_repository import PostgresVacancyRepository

    settings = DatabaseSettings(url=_TEST_DSN)
    second = PostgresVacancyRepository.from_settings(settings)
    try:
        second.save(_vacancy(hh_id="pg-reopen"), _filter_result(), VacancyStatus.NEW)
        assert second.exists("pg-reopen") is True
    finally:
        second.close()
