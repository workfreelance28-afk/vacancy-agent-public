from datetime import UTC, datetime, timedelta

from vacancy_agent.domain.default_filter_profile import DEFAULT_FILTER_CRITERIA
from vacancy_agent.domain.filters import FilterCriteria, filter_vacancy
from vacancy_agent.domain.vacancy import Vacancy

_NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def _vacancy(**overrides: object) -> Vacancy:
    fields: dict[str, object] = {
        "hh_id": "1",
        "title": "AI-интегратор",
        "url": "https://hh.ru/vacancy/1",
        "published_at": _NOW,
        "fetched_at": _NOW,
    }
    fields.update(overrides)
    return Vacancy(**fields)


def _criteria(**overrides: object) -> FilterCriteria:
    fields: dict[str, object] = {
        "target_titles": ["AI-интегратор", "AI Integration Engineer"],
        "secondary_titles": ["Python-разработчик"],
        "include_phrases": ["AI-интеграция", "LLM", "Qwen"],
        "exclude_title_phrases": ["менеджер по продажам", "графический дизайнер"],
        "internship_phrases": ["стажёр", "стажер", "intern"],
        "exclude_anywhere_phrases": ["только процент", "без оклада"],
        "salary_min": 180_000,
        "allow_missing_salary": True,
        "allowed_areas": ["Москва", "Московская область", "Россия"],
        "allowed_work_formats": ["удалённо", "гибрид", "офис"],
        "allowed_experience": ["От 1 года до 3 лет", "От 3 до 6 лет"],
        "max_age_days": 7,
    }
    fields.update(overrides)
    return FilterCriteria(**fields)


def _base_kwargs(**overrides: object) -> dict[str, object]:
    """Вакансия, которая проходит все проверки, кроме той, что тестируется."""

    fields: dict[str, object] = {
        "title": "AI-интегратор",
        "salary_from": 200_000,
        "salary_to": 250_000,
        "salary_currency": "RUR",
        "work_format": ["Удалённая работа"],
        "area": "Москва",
        "experience": "От 1 года до 3 лет",
        "employment": "Полная занятость",
        "published_at": _NOW,
    }
    fields.update(overrides)
    return fields


def test_accepts_a_fully_matching_vacancy() -> None:
    result = filter_vacancy(_vacancy(**_base_kwargs()), _criteria(), now=_NOW)

    assert result.accepted is True
    assert result.rejected_by == []


# --- давность ---


def test_rejects_vacancy_older_than_max_age_days() -> None:
    old_date = _NOW - timedelta(days=8)
    vacancy = _vacancy(**_base_kwargs(published_at=old_date))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "published_too_old" in result.rejected_by


def test_accepts_vacancy_within_max_age_days() -> None:
    recent_date = _NOW - timedelta(days=6)
    vacancy = _vacancy(**_base_kwargs(published_at=recent_date))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "published_too_old" not in result.rejected_by


def test_max_age_days_none_disables_age_check() -> None:
    old_date = _NOW - timedelta(days=365)
    vacancy = _vacancy(**_base_kwargs(published_at=old_date))

    result = filter_vacancy(vacancy, _criteria(max_age_days=None), now=_NOW)

    assert "published_too_old" not in result.rejected_by


# --- исключающие слова ---


def test_rejects_excluded_title_keyword() -> None:
    vacancy = _vacancy(**_base_kwargs(title="Менеджер по продажам AI-продукта"))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert result.accepted is False
    assert "excluded_title_keyword" in result.rejected_by


def test_exclude_keyword_only_in_description_does_not_reject() -> None:
    vacancy = _vacancy(
        **_base_kwargs(description="Ранее работал графический дизайнер, теперь AI-интегратор.")
    )

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "excluded_title_keyword" not in result.rejected_by


def test_internship_phrase_in_description_rejects_even_without_title_match() -> None:
    vacancy = _vacancy(**_base_kwargs(description="Ищем стажёра в AI-команду."))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "internship_not_allowed" in result.rejected_by


def test_internship_marker_in_employment_field_rejects() -> None:
    vacancy = _vacancy(**_base_kwargs(employment="Стажировка"))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "internship_not_allowed" in result.rejected_by


def test_ai_product_designer_title_is_not_excluded_as_pure_design() -> None:
    vacancy = _vacancy(**_base_kwargs(title="AI Product Designer"))

    result = filter_vacancy(vacancy, _criteria(target_titles=[]), now=_NOW)

    assert "excluded_title_keyword" not in result.rejected_by


# --- география и формат ---


def test_remote_vacancy_accepted_regardless_of_employer_area() -> None:
    vacancy = _vacancy(**_base_kwargs(work_format=["Удалённая работа"], area="Новосибирск"))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "area_not_allowed" not in result.rejected_by
    assert "work_format_not_allowed" not in result.rejected_by


def test_hybrid_in_moscow_accepted() -> None:
    vacancy = _vacancy(**_base_kwargs(work_format=["Гибридный график"], area="Москва"))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert result.accepted is True


def test_office_outside_moscow_rejected_by_area() -> None:
    vacancy = _vacancy(**_base_kwargs(work_format=["Полный день"], area="Новосибирск"))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "area_not_allowed" in result.rejected_by


def test_office_in_russia_but_not_moscow_is_rejected_by_area() -> None:
    """"Россия" в allowed_areas относится к удалённой работе, а не офису/гибриду."""

    vacancy = _vacancy(**_base_kwargs(work_format=["Полный день"], area="Россия"))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "area_not_allowed" in result.rejected_by
    assert result.accepted is False


def test_unrecognized_work_format_is_not_hard_rejected() -> None:
    """Нераспознанный формат (например, вахтовый метод) — не «офис» по умолчанию."""

    vacancy = _vacancy(
        **_base_kwargs(work_format=["Вахтовый метод"], area="Новосибирск")
    )

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "work_format_unknown" in result.reasons
    assert "area_not_allowed" not in result.rejected_by
    assert "work_format_not_allowed" not in result.rejected_by


def test_missing_work_format_is_not_rejected() -> None:
    vacancy = _vacancy(**_base_kwargs(work_format=[]))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "work_format_unknown" in result.reasons
    assert "work_format_not_allowed" not in result.rejected_by
    assert "area_not_allowed" not in result.rejected_by


def test_remote_rejected_when_remote_not_in_allowed_formats() -> None:
    vacancy = _vacancy(**_base_kwargs(work_format=["Удалённая работа"]))

    result = filter_vacancy(vacancy, _criteria(allowed_work_formats=["офис"]), now=_NOW)

    assert "work_format_not_allowed" in result.rejected_by


# --- опыт ---


def test_no_experience_is_rejected() -> None:
    vacancy = _vacancy(**_base_kwargs(experience="Нет опыта"))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "experience_not_allowed" in result.rejected_by


def test_no_experience_is_accepted_when_profile_allows_it() -> None:
    vacancy = _vacancy(**_base_kwargs(experience="Нет опыта"))

    result = filter_vacancy(
        vacancy,
        _criteria(allowed_experience=["Нет опыта", "От 1 года до 3 лет"]),
        now=_NOW,
    )

    assert "experience_not_allowed" not in result.rejected_by


def test_experience_between_1_and_3_is_accepted() -> None:
    vacancy = _vacancy(**_base_kwargs(experience="От 1 года до 3 лет"))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "experience_not_allowed" not in result.rejected_by


def test_more_than_6_years_is_rejected_when_profile_does_not_allow_it() -> None:
    vacancy = _vacancy(**_base_kwargs(experience="Более 6 лет"))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "experience_not_allowed" in result.rejected_by


def test_more_than_6_years_can_be_explicitly_allowed() -> None:
    vacancy = _vacancy(**_base_kwargs(experience="Более 6 лет"))

    result = filter_vacancy(
        vacancy,
        _criteria(allowed_experience=["Более 6 лет"]),
        now=_NOW,
    )

    assert "experience_not_allowed" not in result.rejected_by
    assert "experience_above_range_allowed" in result.reasons


def test_missing_experience_is_not_rejected() -> None:
    vacancy = _vacancy(**_base_kwargs(experience=None))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "experience_unknown" in result.reasons
    assert "experience_not_allowed" not in result.rejected_by


# --- зарплата ---


def test_missing_salary_allowed_by_default() -> None:
    vacancy = _vacancy(**_base_kwargs(salary_from=None, salary_to=None))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "salary_missing_allowed" in result.reasons
    assert result.accepted is True


def test_missing_salary_rejected_when_not_allowed() -> None:
    vacancy = _vacancy(**_base_kwargs(salary_from=None, salary_to=None))

    result = filter_vacancy(vacancy, _criteria(allow_missing_salary=False), now=_NOW)

    assert "salary_missing_not_allowed" in result.rejected_by


def test_salary_open_upper_bound_accepted_even_if_from_below_minimum() -> None:
    vacancy = _vacancy(**_base_kwargs(salary_from=50_000, salary_to=None))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "salary_open_upper_bound" in result.reasons
    assert "salary_below_minimum" not in result.rejected_by


def test_salary_to_below_minimum_rejected() -> None:
    vacancy = _vacancy(**_base_kwargs(salary_from=None, salary_to=150_000))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "salary_below_minimum" in result.rejected_by


def test_salary_range_straddling_minimum_accepted() -> None:
    vacancy = _vacancy(**_base_kwargs(salary_from=150_000, salary_to=200_000))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "salary_below_minimum" not in result.rejected_by


def test_net_salary_at_minimum_is_not_penalized() -> None:
    vacancy = _vacancy(
        **_base_kwargs(salary_from=None, salary_to=180_000, salary_gross=False)
    )

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "salary_below_minimum" not in result.rejected_by


def test_foreign_currency_salary_is_unchecked_not_rejected() -> None:
    vacancy = _vacancy(**_base_kwargs(salary_from=None, salary_to=1_000, salary_currency="USD"))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "salary_currency_unchecked" in result.reasons
    assert "salary_below_minimum" not in result.rejected_by


def test_salary_check_skipped_when_criteria_has_no_minimum() -> None:
    vacancy = _vacancy(**_base_kwargs(salary_from=None, salary_to=10_000))

    result = filter_vacancy(vacancy, _criteria(salary_min=None), now=_NOW)

    assert "salary_below_minimum" not in result.rejected_by


# --- целевые названия и ключевые слова ---


def test_target_title_match_accepted_without_keyword_elsewhere() -> None:
    vacancy = _vacancy(**_base_kwargs(title="AI-интегратор", description="", requirements=""))

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "target_title_match" in result.reasons
    assert result.accepted is True


def test_secondary_title_without_keyword_is_rejected() -> None:
    vacancy = _vacancy(
        **_base_kwargs(title="Python-разработчик", description="", requirements="")
    )

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "no_relevant_keyword" in result.rejected_by


def test_secondary_title_with_keyword_is_accepted() -> None:
    vacancy = _vacancy(
        **_base_kwargs(title="Python-разработчик", description="Интеграция LLM в продукт.")
    )

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "target_title_match" in result.reasons
    assert result.accepted is True


def test_include_keyword_with_secondary_title_is_accepted() -> None:
    vacancy = _vacancy(
        **_base_kwargs(
            title="Инженер по автоматизации процессов",
            requirements="Опыт работы с Qwen и LLM.",
        )
    )

    result = filter_vacancy(
        vacancy,
        _criteria(
            target_titles=[],
            secondary_titles=["Инженер по автоматизации процессов"],
        ),
        now=_NOW,
    )

    assert "target_title_match" in result.reasons


def test_include_keyword_without_approved_title_is_rejected() -> None:
    vacancy = _vacancy(
        **_base_kwargs(
            title="Преподаватель",
            requirements="Использование Qwen и LLM.",
        )
    )

    result = filter_vacancy(vacancy, _criteria(target_titles=[]), now=_NOW)

    assert "no_relevant_keyword" in result.rejected_by


def test_no_relevant_keyword_rejects_when_nothing_matches() -> None:
    vacancy = _vacancy(
        **_base_kwargs(title="Бухгалтер", description="", requirements="", key_skills=[])
    )

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert "no_relevant_keyword" in result.rejected_by


def test_bare_ai_keyword_requires_word_boundary() -> None:
    vacancy = _vacancy(
        **_base_kwargs(title="Специалист по продвижению", description="Работа с сайтами")
    )

    result = filter_vacancy(
        vacancy, _criteria(target_titles=[], include_phrases=["AI"]), now=_NOW
    )

    assert "no_relevant_keyword" in result.rejected_by


def test_bare_ai_keyword_matches_as_standalone_token() -> None:
    vacancy = _vacancy(**_base_kwargs(title="AI Engineer", description="", requirements=""))

    result = filter_vacancy(
        vacancy,
        _criteria(
            target_titles=[],
            secondary_titles=["AI Engineer"],
            include_phrases=["AI"],
        ),
        now=_NOW,
    )

    assert "target_title_match" in result.reasons


# --- сбор нескольких причин одновременно ---


def test_multiple_failed_checks_are_all_collected() -> None:
    old_date = _NOW - timedelta(days=30)
    vacancy = _vacancy(
        **_base_kwargs(
            title="Менеджер по продажам",
            published_at=old_date,
            experience="Нет опыта",
        )
    )

    result = filter_vacancy(vacancy, _criteria(), now=_NOW)

    assert result.accepted is False
    assert "published_too_old" in result.rejected_by
    assert "excluded_title_keyword" in result.rejected_by
    assert "experience_not_allowed" in result.rejected_by


# --- дефолтный профиль ---


def test_default_filter_criteria_has_neutral_public_search_parameters() -> None:
    assert DEFAULT_FILTER_CRITERIA.salary_min == 0
    assert DEFAULT_FILTER_CRITERIA.allow_missing_salary is True
    assert DEFAULT_FILTER_CRITERIA.max_age_days == 7
    assert DEFAULT_FILTER_CRITERIA.allowed_areas == []
    assert DEFAULT_FILTER_CRITERIA.allowed_work_formats == ["удалённо", "гибрид", "офис"]
    assert DEFAULT_FILTER_CRITERIA.allowed_experience == ["Нет опыта", "От 1 года до 3 лет"]


def test_default_filter_criteria_accepts_realistic_target_vacancy() -> None:
    vacancy = _vacancy(
        **_base_kwargs(
            title="AI-интегратор",
            description="Внедрение AI-агентов и LLM в продуктовые процессы.",
        )
    )

    result = filter_vacancy(vacancy, DEFAULT_FILTER_CRITERIA, now=_NOW)

    assert result.accepted is True


def test_default_filter_criteria_rejects_clearly_irrelevant_vacancy() -> None:
    vacancy = _vacancy(
        **_base_kwargs(title="Менеджер по продажам", description="", requirements="")
    )

    result = filter_vacancy(vacancy, DEFAULT_FILTER_CRITERIA, now=_NOW)

    assert result.accepted is False


def test_default_filter_criteria_accepts_junior_ai_automation_role() -> None:
    vacancy = _vacancy(
        **_base_kwargs(
            title="Junior специалист по AI-автоматизации",
            description="Интеграция готовых моделей через API и n8n.",
            experience="Нет опыта",
            salary_from=80_000,
            salary_to=100_000,
        )
    )

    result = filter_vacancy(vacancy, DEFAULT_FILTER_CRITERIA, now=_NOW)

    assert result.accepted is True


def test_default_filter_criteria_rejects_senior_ai_role() -> None:
    vacancy = _vacancy(
        **_base_kwargs(
            title="Senior AI Integration Engineer",
            experience="От 3 до 6 лет",
        )
    )

    result = filter_vacancy(vacancy, DEFAULT_FILTER_CRITERIA, now=_NOW)

    assert "excluded_title_keyword" in result.rejected_by
    assert "experience_not_allowed" in result.rejected_by


def test_default_filter_criteria_rejects_ml_engineer_title() -> None:
    vacancy = _vacancy(
        **_base_kwargs(
            title="Junior ML Engineer",
            description="Обучение и валидация моделей.",
            experience="Нет опыта",
        )
    )

    result = filter_vacancy(vacancy, DEFAULT_FILTER_CRITERIA, now=_NOW)

    assert "excluded_title_keyword" in result.rejected_by


def test_default_filter_criteria_rejects_teacher_even_with_llm_keyword() -> None:
    vacancy = _vacancy(
        **_base_kwargs(
            title="Преподаватель по нейросетям",
            requirements="Обучение работе с LLM и AI.",
            experience="Нет опыта",
        )
    )

    result = filter_vacancy(vacancy, DEFAULT_FILTER_CRITERIA, now=_NOW)

    assert "excluded_title_keyword" in result.rejected_by


def test_default_filter_criteria_rejects_manager_even_with_ai_keyword() -> None:
    vacancy = _vacancy(
        **_base_kwargs(
            title="Junior project manager",
            requirements="Проект по внедрению AI.",
            experience="Нет опыта",
        )
    )

    result = filter_vacancy(vacancy, DEFAULT_FILTER_CRITERIA, now=_NOW)

    assert "excluded_title_keyword" in result.rejected_by
