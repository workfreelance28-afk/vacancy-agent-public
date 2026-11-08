from datetime import datetime

import pytest

from vacancy_agent.integrations.hh_errors import HHNormalizationError
from vacancy_agent.integrations.hh_normalize import normalize_vacancy


def _raw_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": "7760476",
        "name": "Python Developer",
        "alternate_url": "https://hh.ru/vacancy/7760476",
        "url": "https://api.hh.ru/vacancies/7760476",
        "published_at": "2026-08-20T13:27:16+0300",
        "employer": {"name": "HeadHunter"},
        "salary": {"from": 100000, "to": 150000, "currency": "RUR", "gross": True},
        "area": {"name": "Москва"},
        "experience": {"name": "От 1 года до 3 лет"},
        "employment": {"name": "Полная занятость"},
        "snippet": {
            "responsibility": "Писать код на <highlighttext>Python</highlighttext>.",
            "requirement": "Знание <highlighttext>Python</highlighttext> и SQL.",
        },
        "schedule": {"name": "Удалённая работа"},
    }
    item.update(overrides)
    return item


def test_normalize_maps_all_known_fields() -> None:
    vacancy = normalize_vacancy(_raw_item())

    assert vacancy.hh_id == "7760476"
    assert vacancy.title == "Python Developer"
    assert vacancy.company == "HeadHunter"
    assert vacancy.salary_from == 100000
    assert vacancy.salary_to == 150000
    assert vacancy.salary_currency == "RUR"
    assert vacancy.salary_gross is True
    assert vacancy.area == "Москва"
    assert vacancy.experience == "От 1 года до 3 лет"
    assert vacancy.employment == "Полная занятость"
    assert vacancy.url == "https://hh.ru/vacancy/7760476"
    assert vacancy.published_at == datetime.fromisoformat("2026-08-20T13:27:16+0300")


def test_normalize_strips_highlight_tags_from_snippet() -> None:
    vacancy = normalize_vacancy(_raw_item())

    assert vacancy.description == "Писать код на Python."
    assert vacancy.requirements == "Знание Python и SQL."
    assert "highlighttext" not in (vacancy.description or "")


def test_normalize_falls_back_to_schedule_when_work_format_missing() -> None:
    vacancy = normalize_vacancy(_raw_item())

    assert vacancy.work_format == ["Удалённая работа"]


def test_normalize_prefers_explicit_work_format_over_schedule() -> None:
    item = _raw_item(work_format=[{"id": "REMOTE", "name": "Удалённо"}])

    vacancy = normalize_vacancy(item)

    assert vacancy.work_format == ["Удалённо"]


def test_normalize_hh_id_is_preserved_verbatim_as_string() -> None:
    vacancy = normalize_vacancy(_raw_item(id="00042"))

    assert vacancy.hh_id == "00042"


def test_normalize_handles_missing_optional_sections() -> None:
    item = _raw_item()
    del item["employer"]
    del item["salary"]
    del item["snippet"]
    del item["schedule"]
    del item["experience"]
    del item["employment"]
    del item["area"]

    vacancy = normalize_vacancy(item)

    assert vacancy.company is None
    assert vacancy.salary_from is None
    assert vacancy.description is None
    assert vacancy.requirements is None
    assert vacancy.work_format == []
    assert vacancy.experience is None
    assert vacancy.employment is None
    assert vacancy.area is None
    assert vacancy.key_skills == []


def test_normalize_falls_back_to_api_url_when_alternate_url_missing() -> None:
    item = _raw_item(alternate_url=None)

    vacancy = normalize_vacancy(item)

    assert vacancy.url == "https://api.hh.ru/vacancies/7760476"


def test_normalize_raises_when_no_url_available() -> None:
    item = _raw_item(alternate_url=None, url=None)

    with pytest.raises(HHNormalizationError):
        normalize_vacancy(item)


def test_normalize_raises_on_missing_required_field() -> None:
    item = _raw_item()
    del item["published_at"]

    with pytest.raises(HHNormalizationError):
        normalize_vacancy(item)


def test_normalize_raises_hh_normalization_error_on_malformed_key_skills() -> None:
    item = _raw_item(key_skills=["not-a-dict"])

    with pytest.raises(HHNormalizationError):
        normalize_vacancy(item)


def test_normalize_raises_hh_normalization_error_on_malformed_work_format() -> None:
    item = _raw_item(work_format=["not-a-dict"])

    with pytest.raises(HHNormalizationError):
        normalize_vacancy(item)
