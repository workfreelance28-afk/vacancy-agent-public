from datetime import UTC, datetime

import pytest

from vacancy_agent.domain.default_filter_profile import DEFAULT_FILTER_CRITERIA
from vacancy_agent.domain.filters import filter_vacancy
from vacancy_agent.integrations.superjob_errors import SuperJobNormalizationError
from vacancy_agent.integrations.superjob_normalize import normalize_vacancy

_DATE_PUBLISHED = 1755673200  # 2025-08-20T09:00:00+00:00


def _raw_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": 45_678_901,
        "profession": "AI Integration Engineer",
        "work": "Интеграция LLM в продуктовые сценарии, поддержка API.",
        "candidat": "Опыт с Python и Docker от 2 лет.",
        "payment_from": 200_000,
        "payment_to": 300_000,
        "currency": "rub",
        "town": {"title": "Москва"},
        "firm_name": "OOO Ромашка",
        "client": {"title": "Ромашка (клиент)"},
        "link": "https://superjob.ru/vakansii/ai-integration-engineer-45678901.html",
        "experience": {"title": "От 1 года"},
        "type_of_work": {"title": "Полная занятость"},
        "place_of_work": {"title": "Удаленная работа"},
        "date_published": _DATE_PUBLISHED,
    }
    item.update(overrides)
    return item


def test_normalize_maps_all_known_fields() -> None:
    vacancy = normalize_vacancy(_raw_item())

    assert vacancy.hh_id == "superjob:45678901"
    assert vacancy.title == "AI Integration Engineer"
    assert vacancy.company == "OOO Ромашка"
    assert vacancy.salary_from == 200_000
    assert vacancy.salary_to == 300_000
    assert vacancy.salary_currency == "rub"
    assert vacancy.salary_gross is None
    assert vacancy.description == "Интеграция LLM в продуктовые сценарии, поддержка API."
    assert vacancy.requirements == "Опыт с Python и Docker от 2 лет."
    assert vacancy.area == "Москва"
    assert vacancy.experience == "От 1 года до 3 лет"
    assert vacancy.employment == "Полная занятость"
    assert vacancy.key_skills == []
    assert vacancy.work_format == ["Удаленная работа"]
    assert vacancy.url == "https://superjob.ru/vakansii/ai-integration-engineer-45678901.html"
    assert vacancy.published_at == datetime.fromtimestamp(_DATE_PUBLISHED, tz=UTC)


def test_normalize_maps_work_to_description_and_candidat_to_requirements() -> None:
    """Официальная документация SuperJob (api.superjob.ru): `work` —
    «Должностные обязанности», `candidat` — «Требования к кандидату»
    (найдено ревью, см. `docs/DECISIONS.md`, ADR-016) — не наоборот и не
    оба в одно поле."""

    item = _raw_item(work="Обязанности: интеграция API.", candidat="Требования: Python 3+.")

    vacancy = normalize_vacancy(item)

    assert vacancy.description == "Обязанности: интеграция API."
    assert vacancy.requirements == "Требования: Python 3+."


def test_normalize_prefixes_hh_id_with_source_to_avoid_cross_source_collision() -> None:
    """`Vacancy.hh_id` общий для всех источников (ADR-014) — без префикса
    числовой SuperJob-ID мог бы совпасть с числовым HH-ID и молча
    признаться дубликатом чужой вакансии (найдено ревью, ADR-016)."""

    vacancy = normalize_vacancy(_raw_item(id=123))

    assert vacancy.hh_id == "superjob:123"


@pytest.mark.parametrize(
    ("raw_title", "canonical"),
    [
        ("Без опыта", "Нет опыта"),
        ("От 1 года", "От 1 года до 3 лет"),
        ("От 3 лет", "От 3 до 6 лет"),
        ("От 6 лет", "Более 6 лет"),
    ],
)
def test_normalize_canonicalizes_known_superjob_experience_values(
    raw_title: str, canonical: str
) -> None:
    """SuperJob и HH используют разные формулировки для одного и того же
    смысла опыта — `domain/filters.py` source-agnostic и ожидает
    HH-канонические строки (найдено живой диагностикой TASK-006A, см.
    `docs/DECISIONS.md`, ADR-019): без канонизации все 4 реальных
    значения SuperJob отклонялись `experience_not_allowed`."""

    vacancy = normalize_vacancy(_raw_item(experience={"title": raw_title}))

    assert vacancy.experience == canonical


def test_normalize_passes_through_unknown_experience_value_unchanged() -> None:
    """Неизвестное значение НЕ подгоняется под ближайшую категорию —
    passthrough человекочитаемой строки как есть, чтобы фильтр дал явный
    `experience_not_allowed`, а не тихую неверную категоризацию
    (`docs/DECISIONS.md`, ADR-019)."""

    vacancy = normalize_vacancy(_raw_item(experience={"title": "От 10 лет"}))

    assert vacancy.experience == "От 10 лет"


def test_end_to_end_junior_superjob_experience_is_allowed() -> None:
    """Сквозной тест TASK-006A: нормализация → фильтр. До канонизации ВСЕ
    вакансии SuperJob отклонялись `experience_not_allowed` (живая
    диагностика, `docs/DECISIONS.md`, ADR-019)."""

    vacancy = normalize_vacancy(_raw_item(experience={"title": "От 1 года"}))

    result = filter_vacancy(vacancy, DEFAULT_FILTER_CRITERIA)

    assert "experience_not_allowed" not in result.rejected_by


def test_end_to_end_three_year_superjob_experience_is_rejected_by_junior_profile() -> None:
    vacancy = normalize_vacancy(_raw_item(experience={"title": "От 3 лет"}))

    result = filter_vacancy(vacancy, DEFAULT_FILTER_CRITERIA)

    assert "experience_not_allowed" in result.rejected_by


def test_end_to_end_no_experience_superjob_value_is_allowed_by_junior_profile() -> None:
    vacancy = normalize_vacancy(_raw_item(experience={"title": "Без опыта"}))

    result = filter_vacancy(vacancy, DEFAULT_FILTER_CRITERIA)

    assert "experience_not_allowed" not in result.rejected_by


def test_end_to_end_place_of_work_not_applicable_stays_unknown_not_office() -> None:
    """`"Не имеет значения"` — частое значение на реальных вакансиях
    (живая диагностика, ADR-019) — должно оставаться `work_format_unknown`
    (не блокирующая причина), не превращаться в «офис»."""

    vacancy = normalize_vacancy(
        _raw_item(
            experience={"title": "От 3 лет"},
            place_of_work={"id": 0, "title": "Не имеет значения"},
        )
    )

    result = filter_vacancy(vacancy, DEFAULT_FILTER_CRITERIA)

    assert "work_format_unknown" in result.reasons
    assert "work_format_unknown" not in result.rejected_by
    assert "work_format_not_allowed" not in result.rejected_by


def test_normalize_falls_back_to_client_title_when_firm_name_missing() -> None:
    item = _raw_item()
    del item["firm_name"]

    vacancy = normalize_vacancy(item)

    assert vacancy.company == "Ромашка (клиент)"


def test_normalize_treats_zero_payment_as_missing_salary() -> None:
    vacancy = normalize_vacancy(_raw_item(payment_from=0, payment_to=0))

    assert vacancy.salary_from is None
    assert vacancy.salary_to is None


def test_normalize_treats_zero_payment_from_as_missing_only_that_bound() -> None:
    """`payment_from=0` — «нижняя граница не указана», а не «зарплата от 0»,
    даже если верхняя граница задана (SuperJob-соглашение, см. модуль
    `superjob_normalize.py`)."""

    vacancy = normalize_vacancy(_raw_item(payment_from=0, payment_to=250_000))

    assert vacancy.salary_from is None
    assert vacancy.salary_to == 250_000


def test_normalize_hh_id_is_preserved_as_string() -> None:
    vacancy = normalize_vacancy(_raw_item(id=42))

    assert vacancy.hh_id == "superjob:42"


def test_normalize_handles_missing_optional_sections() -> None:
    item = _raw_item()
    del item["firm_name"]
    del item["client"]
    del item["town"]
    del item["experience"]
    del item["type_of_work"]
    del item["place_of_work"]
    del item["work"]
    del item["candidat"]
    item["payment_from"] = None
    item["payment_to"] = None
    del item["currency"]

    vacancy = normalize_vacancy(item)

    assert vacancy.company is None
    assert vacancy.salary_from is None
    assert vacancy.salary_to is None
    assert vacancy.salary_currency is None
    assert vacancy.description is None
    assert vacancy.requirements is None
    assert vacancy.area is None
    assert vacancy.experience is None
    assert vacancy.employment is None
    assert vacancy.work_format == []


def test_normalize_maps_place_of_work_title_into_work_format() -> None:
    vacancy = normalize_vacancy(
        _raw_item(place_of_work={"id": 1, "title": "На территории работодателя"})
    )

    assert vacancy.work_format == ["На территории работодателя"]


def test_normalize_raises_when_link_missing() -> None:
    item = _raw_item(link=None)

    with pytest.raises(SuperJobNormalizationError):
        normalize_vacancy(item)


def test_normalize_raises_on_missing_required_field() -> None:
    item = _raw_item()
    del item["date_published"]

    with pytest.raises(SuperJobNormalizationError):
        normalize_vacancy(item)


def test_normalize_raises_on_malformed_date_published() -> None:
    item = _raw_item(date_published="not-a-timestamp")

    with pytest.raises(SuperJobNormalizationError):
        normalize_vacancy(item)
