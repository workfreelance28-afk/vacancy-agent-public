import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
from vacancy_agent.persistence.sqlite_repository import SqliteVacancyRepository

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
        "description": "Внедрение AI-агентов",
        "requirements": "Python, LLM",
        "key_skills": ["Python", "LLM"],
        "experience": "От 1 года до 3 лет",
        "employment": "Полная занятость",
        "work_format": ["Удалённая работа"],
        "area": "Москва",
        "url": "https://hh.ru/vacancy/12345",
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


@pytest.fixture
def repo() -> Iterator[SqliteVacancyRepository]:
    with SqliteVacancyRepository(":memory:") as repository:
        yield repository


def test_exists_is_false_for_unknown_hh_id(repo: SqliteVacancyRepository) -> None:
    assert repo.exists("does-not-exist") is False


def test_save_then_exists_is_true(repo: SqliteVacancyRepository) -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)

    assert repo.exists("12345") is True


def test_save_round_trips_all_vacancy_fields(repo: SqliteVacancyRepository) -> None:
    vacancy = _vacancy()
    filter_result = _filter_result(reasons=["target_title_match"], rejected_by=[])

    repo.save(vacancy, filter_result, VacancyStatus.NEW)
    stored = repo.get("12345")

    assert stored is not None
    assert stored.vacancy == vacancy
    assert stored.filter_result == filter_result
    assert stored.status == VacancyStatus.NEW
    assert stored.assessment is None  # фильтр отклонил бы/принял без Qwen — не передан save()
    assert stored.qwen_model is None
    assert stored.qwen_min_score is None
    assert stored.qwen_passed is None


def test_save_round_trips_none_and_empty_optional_fields(repo: SqliteVacancyRepository) -> None:
    vacancy = _vacancy(
        company=None,
        salary_from=None,
        salary_to=None,
        salary_currency=None,
        salary_gross=None,
        description=None,
        requirements=None,
        key_skills=[],
        experience=None,
        employment=None,
        work_format=[],
        area=None,
    )

    repo.save(vacancy, _filter_result(), VacancyStatus.NEW)
    stored = repo.get("12345")

    assert stored is not None
    assert stored[0] == vacancy


def test_save_round_trips_qwen_assessment(repo: SqliteVacancyRepository) -> None:
    """Найдено ревью TASK-007 (`docs/DECISIONS.md`, ADR-022, P1): без этого
    решение Qwen нельзя проверить или использовать в MVP 3, а отклонённая
    Qwen вакансия неотличима в хранилище от прошедшей его."""

    vacancy = _vacancy()
    assessment = _assessment(score=91, relevant=True, recommendation="strong_match")

    repo.save(
        vacancy,
        _filter_result(),
        VacancyStatus.NEW,
        assessment=assessment,
        qwen_model="deepseek-r1",
        qwen_min_score=70,
        qwen_passed=True,
    )
    stored = repo.get("12345")

    assert stored is not None
    assert stored.assessment == assessment
    assert stored.qwen_model == "deepseek-r1"
    assert stored.qwen_min_score == 70
    assert stored.qwen_passed is True


def test_save_round_trips_qwen_assessment_with_empty_lists(repo: SqliteVacancyRepository) -> None:
    vacancy = _vacancy()
    assessment = _assessment(strengths=[], gaps=[], risks=[])

    repo.save(vacancy, _filter_result(), VacancyStatus.NEW, assessment=assessment, qwen_model="m")
    stored = repo.get("12345")

    assert stored is not None
    assert stored.assessment == assessment


def test_save_round_trips_qwen_threshold_decision_below_score(
    repo: SqliteVacancyRepository,
) -> None:
    """Найдено ревью TASK-007 (ADR-022, второе уточнение): `qwen_score`
    один по себе не позволяет исторически восстановить, прошла ли вакансия
    порог в момент решения — `QWEN_MIN_SCORE` конфигурируем и может
    измениться позже. `qwen_min_score`/`qwen_passed` — снимок на момент
    обработки, не текущее значение настройки."""

    vacancy = _vacancy()
    assessment = _assessment(score=65, relevant=True, recommendation="review")

    repo.save(
        vacancy,
        _filter_result(),
        VacancyStatus.NEW,
        assessment=assessment,
        qwen_model="deepseek-r1",
        qwen_min_score=70,
        qwen_passed=False,
    )
    stored = repo.get("12345")

    assert stored is not None
    assert stored.assessment is not None
    assert stored.assessment.score == 65
    assert stored.qwen_min_score == 70
    assert stored.qwen_passed is False


def test_save_without_assessment_stores_no_qwen_data(repo: SqliteVacancyRepository) -> None:
    """Вакансия, отклонённая жёсткими фильтрами (или ещё не дошедшая до
    Qwen), не должна получать оценку из ниоткуда."""

    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)
    stored = repo.get("12345")

    assert stored is not None
    assert stored.assessment is None
    assert stored.qwen_model is None
    assert stored.qwen_min_score is None
    assert stored.qwen_passed is None


def test_save_is_idempotent_for_repeated_hh_id(repo: SqliteVacancyRepository) -> None:
    first = _vacancy(title="First save")
    second = _vacancy(title="Second save")

    repo.save(first, _filter_result(), VacancyStatus.NEW)
    repo.save(second, _filter_result(), VacancyStatus.NEW)

    stored = repo.get("12345")
    assert stored is not None
    assert stored[0].title == "First save"


def test_save_does_not_raise_or_duplicate_on_repeated_call(
    repo: SqliteVacancyRepository,
) -> None:
    vacancy = _vacancy()
    for _ in range(3):
        repo.save(vacancy, _filter_result(), VacancyStatus.NEW)

    # Прямой доступ к соединению — только для проверки отсутствия дублей строк;
    # `get()` по PK не различил бы одну строку от нескольких совпадающих.
    row_count = repo._connection.execute(
        "SELECT COUNT(*) FROM vacancies WHERE hh_id = ?", ("12345",)
    ).fetchone()[0]
    assert row_count == 1


def test_update_status_changes_stored_status(repo: SqliteVacancyRepository) -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)

    repo.update_status("12345", VacancyStatus.VIEWED)

    stored = repo.get("12345")
    assert stored is not None
    assert stored[2] == VacancyStatus.VIEWED


def test_update_status_on_unknown_hh_id_raises(repo: SqliteVacancyRepository) -> None:
    with pytest.raises(VacancyNotFoundError):
        repo.update_status("does-not-exist", VacancyStatus.VIEWED)


def test_update_status_allows_viewed_to_skipped(repo: SqliteVacancyRepository) -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)
    repo.update_status("12345", VacancyStatus.VIEWED)

    repo.update_status("12345", VacancyStatus.SKIPPED)

    stored = repo.get("12345")
    assert stored is not None
    assert stored[2] == VacancyStatus.SKIPPED


def test_update_status_allows_same_status_as_a_no_op(repo: SqliteVacancyRepository) -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)

    repo.update_status("12345", VacancyStatus.NEW)

    stored = repo.get("12345")
    assert stored is not None
    assert stored[2] == VacancyStatus.NEW


def test_update_status_rejects_backward_transition(repo: SqliteVacancyRepository) -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)
    repo.update_status("12345", VacancyStatus.VIEWED)

    with pytest.raises(ValueError, match="недопустим"):
        repo.update_status("12345", VacancyStatus.NEW)

    # Статус не должен был поменяться при отклонённом переходе.
    stored = repo.get("12345")
    assert stored is not None
    assert stored[2] == VacancyStatus.VIEWED


def test_update_status_rejects_transition_out_of_terminal_skipped(
    repo: SqliteVacancyRepository,
) -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)
    repo.update_status("12345", VacancyStatus.SKIPPED)

    with pytest.raises(ValueError, match="недопустим"):
        repo.update_status("12345", VacancyStatus.VIEWED)


def test_repository_recovers_after_a_rejected_update(repo: SqliteVacancyRepository) -> None:
    """Отклонённый update_status не должен оставлять соединение в состоянии,
    которое блокирует последующие операции (см. review-fix TASK-004)."""

    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)
    repo.update_status("12345", VacancyStatus.SKIPPED)

    with pytest.raises(ValueError, match="недопустим"):
        repo.update_status("12345", VacancyStatus.VIEWED)

    # Следующая операция на том же соединении должна пройти как ни в чём не бывало.
    repo.save(_vacancy(hh_id="99999"), _filter_result(), VacancyStatus.NEW)
    assert repo.exists("99999") is True


def test_save_rejects_status_outside_mvp1(repo: SqliteVacancyRepository) -> None:
    with pytest.raises(ValueError, match="MVP 1"):
        repo.save(_vacancy(), _filter_result(), VacancyStatus.APPLIED)


def test_update_status_rejects_status_outside_mvp1(repo: SqliteVacancyRepository) -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)

    with pytest.raises(ValueError, match="MVP 1"):
        repo.update_status("12345", VacancyStatus.OFFER)


def test_get_returns_none_for_unknown_hh_id(repo: SqliteVacancyRepository) -> None:
    assert repo.get("does-not-exist") is None


def test_ping_succeeds_on_open_connection(repo: SqliteVacancyRepository) -> None:
    repo.ping()


def test_ping_raises_after_close(repo: SqliteVacancyRepository) -> None:
    repo.close()

    with pytest.raises(sqlite3.ProgrammingError):
        repo.ping()


def test_reopening_same_file_database_does_not_fail(tmp_path: Path) -> None:
    db_path = str(tmp_path / "vacancies.db")

    with SqliteVacancyRepository(db_path) as first:
        first.save(_vacancy(), _filter_result(), VacancyStatus.NEW)

    with SqliteVacancyRepository(db_path) as second:
        assert second.exists("12345") is True
        second.save(_vacancy(hh_id="67890"), _filter_result(), VacancyStatus.NEW)
        assert second.exists("67890") is True


def test_creates_parent_directory_for_file_database(tmp_path: Path) -> None:
    db_path = str(tmp_path / "nested" / "dir" / "vacancies.db")

    with SqliteVacancyRepository(db_path):
        pass

    assert (tmp_path / "nested" / "dir" / "vacancies.db").exists()


def test_from_settings_parses_sqlite_url(tmp_path: Path) -> None:
    db_path = tmp_path / "vacancies.db"
    settings = DatabaseSettings(url=f"sqlite:///{db_path}")

    with SqliteVacancyRepository.from_settings(settings) as repository:
        repository.save(_vacancy(), _filter_result(), VacancyStatus.NEW)

    assert db_path.exists()


def test_from_settings_rejects_non_sqlite_url() -> None:
    settings = DatabaseSettings(url="postgresql://localhost/db")

    with pytest.raises(ValueError, match="sqlite"):
        SqliteVacancyRepository.from_settings(settings)


# --- list_vacancies/count_vacancies (TASK-009, ADR-028/ADR-029) ---


def test_list_vacancies_empty_when_no_rows(repo: SqliteVacancyRepository) -> None:
    assert repo.list_vacancies(limit=20, offset=0) == []
    assert repo.count_vacancies() == 0


def test_list_vacancies_orders_by_published_at_desc(repo: SqliteVacancyRepository) -> None:
    older = _vacancy(hh_id="1", published_at=_NOW - timedelta(days=2))
    newer = _vacancy(hh_id="2", published_at=_NOW)
    repo.save(older, _filter_result(), VacancyStatus.NEW)
    repo.save(newer, _filter_result(), VacancyStatus.NEW)

    items = repo.list_vacancies(limit=20, offset=0)

    assert [item.vacancy.hh_id for item in items] == ["2", "1"]


def test_list_vacancies_tie_breaks_by_hh_id_ascending(repo: SqliteVacancyRepository) -> None:
    """`docs/TASK-009.md` требует детерминированный порядок при одинаковом
    `published_at` — иначе страница могла бы отдавать строки в разном
    порядке между запросами (SQLite не гарантирует стабильный порядок без
    явного `ORDER BY` по уникальному полю)."""

    repo.save(_vacancy(hh_id="b", published_at=_NOW), _filter_result(), VacancyStatus.NEW)
    repo.save(_vacancy(hh_id="a", published_at=_NOW), _filter_result(), VacancyStatus.NEW)

    items = repo.list_vacancies(limit=20, offset=0)

    assert [item.vacancy.hh_id for item in items] == ["a", "b"]


def test_list_vacancies_respects_limit_and_offset(repo: SqliteVacancyRepository) -> None:
    for i in range(5):
        repo.save(
            _vacancy(hh_id=str(i), published_at=_NOW - timedelta(days=i)),
            _filter_result(),
            VacancyStatus.NEW,
        )

    page1 = repo.list_vacancies(limit=2, offset=0)
    page2 = repo.list_vacancies(limit=2, offset=2)
    page3 = repo.list_vacancies(limit=2, offset=4)

    assert [item.vacancy.hh_id for item in page1] == ["0", "1"]
    assert [item.vacancy.hh_id for item in page2] == ["2", "3"]
    assert [item.vacancy.hh_id for item in page3] == ["4"]
    assert repo.count_vacancies() == 5


def test_list_vacancies_pagination_does_not_duplicate_or_skip_rows(
    repo: SqliteVacancyRepository,
) -> None:
    for i in range(7):
        repo.save(
            _vacancy(hh_id=str(i), published_at=_NOW - timedelta(days=i)),
            _filter_result(),
            VacancyStatus.NEW,
        )

    all_ids: list[str] = []
    offset = 0
    while True:
        page = repo.list_vacancies(limit=3, offset=offset)
        if not page:
            break
        all_ids.extend(item.vacancy.hh_id for item in page)
        offset += 3

    assert all_ids == [str(i) for i in range(7)]


# --- application_drafts (TASK-011, ADR-034/ADR-035) ---


def _draft_content(**overrides: object) -> ApplicationDraftContent:
    fields: dict[str, object] = {
        "match_summary": "Соответствие хорошее.",
        "requirements": [
            RequirementMatch(
                requirement="Python",
                coverage="full",
                explanation="Есть опыт",
                evidence_fact_ids=["skill.python"],
            )
        ],
        "resume_suggestions": [],
        "cover_letter_paragraphs": [
            EvidenceParagraph(text="Письмо.", evidence_fact_ids=["skill.python"])
        ],
        "gaps": [],
        "questions_for_user": [],
        "warnings": [],
    }
    fields.update(overrides)
    return ApplicationDraftContent(**fields)


def _stored_draft(vacancy_id: str = "12345", **overrides: object) -> StoredApplicationDraft:
    fields: dict[str, object] = {
        "draft": ApplicationDraft(
            schema_version="1",
            vacancy_id=vacancy_id,
            profile_version="test-v1",
            content=_draft_content(),
        ),
        "model": "deepseek-r1-0528-qwen3-8b",
        "prompt_version": "1",
        "created_at": _NOW,
    }
    fields.update(overrides)
    return StoredApplicationDraft(**fields)  # type: ignore[arg-type]


def test_get_vacancy_is_an_alias_for_get(repo: SqliteVacancyRepository) -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)

    assert repo.get_vacancy("12345") == repo.get("12345")


def test_get_application_draft_returns_none_when_missing(repo: SqliteVacancyRepository) -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)

    assert repo.get_application_draft("12345") is None


def test_save_and_get_application_draft_round_trips(repo: SqliteVacancyRepository) -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)
    stored = _stored_draft()

    inserted = repo.save_application_draft(stored)
    fetched = repo.get_application_draft("12345")

    assert inserted is True
    assert fetched == stored


def test_save_application_draft_returns_false_on_duplicate_vacancy_id(
    repo: SqliteVacancyRepository,
) -> None:
    """Настоящий дубликат — те же profile/prompt/model version, что уже
    сохранены (TASK-011, ADR-049: `model` теперь тоже часть identity, см.
    `test_save_application_draft_replaces_stale_model` для случая, когда
    отличается только модель)."""

    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)
    first = repo.save_application_draft(_stored_draft(model="same-model"))
    second = repo.save_application_draft(_stored_draft(model="same-model"))

    assert first is True
    assert second is False
    # Второй вызов не подменил и не продублировал строку — хранится ровно
    # первая версия.
    assert repo.get_application_draft("12345") is not None
    assert repo.get_application_draft("12345").model == "same-model"  # type: ignore[union-attr]


def test_save_application_draft_replaces_stale_model(repo: SqliteVacancyRepository) -> None:
    """TASK-011 ADR-049: смена только `model` (profile/prompt version те
    же) должна атомарно заменить строку — до этой правки WHERE-условие
    UPSERT не сравнивало `model`, и `QWEN_MODEL` можно было сменить, не
    затронув сохранённый draft вовсе."""

    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)
    repo.save_application_draft(_stored_draft(model="old-model"))
    replacement = _stored_draft(
        model="new-model",
        draft=ApplicationDraft(
            schema_version="1",
            vacancy_id="12345",
            profile_version="test-v1",
            content=_draft_content(match_summary="Другая модель"),
        ),
    )

    replaced = repo.save_application_draft(replacement)

    assert replaced is True
    assert repo.get_application_draft("12345") == replacement


def test_save_application_draft_replaces_stale_profile_version(
    repo: SqliteVacancyRepository,
) -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)
    repo.save_application_draft(_stored_draft(model="old-model"))
    replacement = _stored_draft(
        draft=ApplicationDraft(
            schema_version="1",
            vacancy_id="12345",
            profile_version="test-v2",
            content=_draft_content(match_summary="Новая версия"),
        ),
        model="new-model",
    )

    replaced = repo.save_application_draft(replacement)

    assert replaced is True
    assert repo.get_application_draft("12345") == replacement


def test_save_application_draft_replaces_stale_prompt_version(
    repo: SqliteVacancyRepository,
) -> None:
    repo.save(_vacancy(), _filter_result(), VacancyStatus.NEW)
    repo.save_application_draft(_stored_draft(model="old-model", prompt_version="1"))
    replacement = _stored_draft(model="new-model", prompt_version="2")

    replaced = repo.save_application_draft(replacement)

    assert replaced is True
    assert repo.get_application_draft("12345") == replacement


def test_save_application_draft_rejects_orphan_vacancy_id(repo: SqliteVacancyRepository) -> None:
    """`PRAGMA foreign_keys = ON` (см. `__init__`) должен отклонять черновик
    для вакансии, которой нет в `vacancies` — без него orphan-строка
    сохранилась бы молча."""

    orphan = _stored_draft(vacancy_id="does-not-exist")

    with pytest.raises(sqlite3.IntegrityError):
        repo.save_application_draft(orphan)


def test_application_drafts_migration_is_idempotent_across_reopens(tmp_path: Path) -> None:
    db_path = str(tmp_path / "vacancies.db")

    with SqliteVacancyRepository(db_path) as first:
        first.save(_vacancy(), _filter_result(), VacancyStatus.NEW)
        first.save_application_draft(_stored_draft())

    with SqliteVacancyRepository(db_path) as second:
        # Реоткрытие того же файла не падает на повторном применении
        # 0003_application_drafts.sql и видит ранее сохранённый черновик.
        assert second.get_application_draft("12345") is not None
