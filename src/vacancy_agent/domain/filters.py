"""Жёсткие детерминированные фильтры вакансий (см. `docs/ARCHITECTURE.md`,
таблица «Ответственность компонентов»: Python-фильтр отвечает за
детерминированные критерии, не за смысловой LLM-анализ).

Критерии, пороги и списки слов зафиксированы во владельческом профиле
`docs/FILTER_PROFILE.md` (ADR-008 в `docs/DECISIONS.md`). Этот модуль
реализует правила из профиля как чистые функции без побочных эффектов и
без обращения к конфигурации/окружению — `FilterCriteria` собирается
вызывающим кодом (сборка из `vacancy_agent.config.settings.FilterSettings`
не входит в TASK-003 и появится вместе с задачей, которая реально
запускает конвейер, см. ROADMAP TASK-005).

Здесь — только логика (`FilterCriteria`, `filter_vacancy` и приватные
проверки). Конкретные значения профиля (списки фраз, пороги первого
запуска) вынесены в `vacancy_agent.domain.default_filter_profile`, чтобы
правки данных профиля не требовали трогать модуль с алгоритмом (см.
`docs/DECISIONS.md`, ADR-010).
"""

import re
from collections.abc import Iterable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.vacancy import Vacancy

_YO_TRANSLATION = str.maketrans({"ё": "е", "Ё": "Е"})
_AI_TOKEN_RE = re.compile(r"\bai\b", re.IGNORECASE)

_INTERNSHIP_EMPLOYMENT_MARKER = "стажир"
_EXPERIENCE_MORE_THAN_6 = "Более 6 лет"
_COUNTRYWIDE_AREA = "россия"

# Каждая проверка — чистая функция `(vacancy, ...) -> (reasons, rejected_by)`,
# без мутации общего состояния; `filter_vacancy` агрегирует результаты.
_CheckOutcome = tuple[list[str], list[str]]


class FilterCriteria(BaseModel):
    """Параметры жёсткой фильтрации. Форма и значения — из `docs/FILTER_PROFILE.md`."""

    model_config = ConfigDict(frozen=True)

    target_titles: list[str] = Field(default_factory=list)
    secondary_titles: list[str] = Field(default_factory=list)
    include_phrases: list[str] = Field(default_factory=list)
    exclude_title_phrases: list[str] = Field(default_factory=list)
    internship_phrases: list[str] = Field(default_factory=list)
    exclude_anywhere_phrases: list[str] = Field(default_factory=list)
    salary_min: int | None = None
    allow_missing_salary: bool = True
    allowed_areas: list[str] = Field(default_factory=list)
    allowed_work_formats: list[str] = Field(default_factory=list)
    allowed_experience: list[str] = Field(default_factory=list)
    max_age_days: int | None = None


def filter_vacancy(
    vacancy: Vacancy, criteria: FilterCriteria, *, now: datetime | None = None
) -> FilterResult:
    """Применяет жёсткие фильтры к одной вакансии.

    Каждая проверка выполняется независимо от исхода остальных — причины
    собираются полностью, как требует `docs/FILTER_PROFILE.md` («Порядок
    применения фильтров»: не останавливаться на первой причине).
    `reasons` — контекстные коды (в т.ч. по прошедшим проверкам),
    `rejected_by` — коды, которые реально привели к отклонению.
    """

    outcomes: list[_CheckOutcome] = [
        _check_age(vacancy, criteria, now),
        _check_exclude_keywords(vacancy, criteria),
        _check_internship_employment(vacancy),
        _check_location(vacancy, criteria),
        _check_experience(vacancy, criteria),
        _check_salary(vacancy, criteria),
        _check_relevance(vacancy, criteria),
    ]

    reasons: list[str] = []
    rejected_by: list[str] = []
    for check_reasons, check_rejected_by in outcomes:
        for code in check_reasons:
            _append_unique(reasons, code)
        for code in check_rejected_by:
            _append_unique(rejected_by, code)

    return FilterResult(accepted=not rejected_by, reasons=reasons, rejected_by=rejected_by)


def _normalize(text: str) -> str:
    return text.translate(_YO_TRANSLATION).lower()


def _phrase_in_text(text: str, phrase: str) -> bool:
    if not text:
        return False
    if _normalize(phrase) == "ai":
        return bool(_AI_TOKEN_RE.search(text))
    return _normalize(phrase) in _normalize(text)


def _any_phrase_in_texts(texts: Iterable[str], phrases: Iterable[str]) -> bool:
    texts = [t for t in texts if t]
    return any(_phrase_in_text(text, phrase) for text in texts for phrase in phrases)


def _append_unique(codes: list[str], code: str) -> None:
    if code not in codes:
        codes.append(code)


def _check_age(vacancy: Vacancy, criteria: FilterCriteria, now: datetime | None) -> _CheckOutcome:
    if criteria.max_age_days is None:
        return [], []
    reference = now or datetime.now(UTC)
    age = reference - vacancy.published_at
    if age.days > criteria.max_age_days:
        return [], ["published_too_old"]
    return [], []


def _check_exclude_keywords(vacancy: Vacancy, criteria: FilterCriteria) -> _CheckOutcome:
    """Проверяет исключающие слова.

    Большинство фраз (продажи, дизайн, разметка данных, ...) отклоняют
    вакансию только по совпадению в заголовке — совпадение только в
    описании не всегда означает нерелевантность (см.
    `docs/FILTER_PROFILE.md`, «Исключающие ключевые слова»). Фразы про
    стажировку и оплату только процентом — исключение из этого правила:
    они проверяются и в заголовке, и в описании/требованиях.
    """

    text_fields = [vacancy.title, vacancy.description or "", vacancy.requirements or ""]
    rejected_by: list[str] = []

    if _any_phrase_in_texts(text_fields, criteria.internship_phrases):
        rejected_by.append("internship_not_allowed")

    if any(_phrase_in_text(vacancy.title, phrase) for phrase in criteria.exclude_title_phrases):
        rejected_by.append("excluded_title_keyword")
        return [], rejected_by

    if _any_phrase_in_texts(text_fields, criteria.exclude_anywhere_phrases):
        rejected_by.append("excluded_title_keyword")

    return [], rejected_by


def _check_internship_employment(vacancy: Vacancy) -> _CheckOutcome:
    if vacancy.employment and _INTERNSHIP_EMPLOYMENT_MARKER in _normalize(vacancy.employment):
        return [], ["internship_not_allowed"]
    return [], []


def _check_location(vacancy: Vacancy, criteria: FilterCriteria) -> _CheckOutcome:
    bucket = _work_format_bucket(vacancy.work_format)
    if bucket is None:
        return ["work_format_unknown"], []

    if bucket == "remote":
        if _format_allowed(criteria, "удалённо"):
            return [], []
        return [], ["work_format_not_allowed"]

    canonical = "гибрид" if bucket == "hybrid" else "офис"
    if not _format_allowed(criteria, canonical):
        return [], ["work_format_not_allowed"]

    # Для гибрида/офиса профиль допускает только Москву/МО — общероссийская
    # запись в `allowed_areas` («Россия») относится только к удалённой работе
    # (которая до этой ветки не доходит: см. ранний `return` выше для remote).
    specific_areas = [a for a in criteria.allowed_areas if _normalize(a) != _COUNTRYWIDE_AREA]
    if not _area_allowed(vacancy.area, specific_areas):
        return [], ["area_not_allowed"]
    return [], []


def _work_format_bucket(work_format: list[str]) -> str | None:
    """Сводит формат работы HH к одной из трёх категорий профиля.

    Нераспознанный, но непустой формат (например, «Вахтовый метод»,
    «Сменный график») намеренно возвращает `None` — так же, как отсутствие
    формата — и не проверяется по географии: профиль требует не отклонять
    неоднозначные случаи жёстко (см. `docs/FILTER_PROFILE.md`, «Принцип
    профиля»).
    """

    normalized = " ".join(_normalize(item) for item in work_format)
    if "удал" in normalized:
        return "remote"
    if "гибрид" in normalized or "комбинир" in normalized:
        return "hybrid"
    if "офис" in normalized or "полный день" in normalized:
        return "office"
    return None


def _format_allowed(criteria: FilterCriteria, canonical: str) -> bool:
    return any(_normalize(fmt) == _normalize(canonical) for fmt in criteria.allowed_work_formats)


def _area_allowed(area: str | None, allowed_areas: list[str]) -> bool:
    if not area:
        return False
    normalized_area = _normalize(area)
    return any(
        _normalize(allowed) in normalized_area or normalized_area in _normalize(allowed)
        for allowed in allowed_areas
    )


def _check_experience(vacancy: Vacancy, criteria: FilterCriteria) -> _CheckOutcome:
    if vacancy.experience is None:
        return ["experience_unknown"], []

    normalized_experience = _normalize(vacancy.experience)
    allowed = {_normalize(exp) for exp in criteria.allowed_experience}
    if normalized_experience in allowed:
        if normalized_experience == _normalize(_EXPERIENCE_MORE_THAN_6):
            return ["experience_above_range_allowed"], []
        return [], []

    return [], ["experience_not_allowed"]


def _check_salary(vacancy: Vacancy, criteria: FilterCriteria) -> _CheckOutcome:
    if criteria.salary_min is None:
        return [], []

    if vacancy.salary_from is None and vacancy.salary_to is None:
        if criteria.allow_missing_salary:
            return ["salary_missing_allowed"], []
        return [], ["salary_missing_not_allowed"]

    currency = vacancy.salary_currency
    if currency is not None and currency.upper() not in {"RUR", "RUB"}:
        return ["salary_currency_unchecked"], []

    if vacancy.salary_to is None:
        return ["salary_open_upper_bound"], []

    if vacancy.salary_to < criteria.salary_min:
        return [], ["salary_below_minimum"]

    return ["salary_meets_minimum"], []


def _check_relevance(vacancy: Vacancy, criteria: FilterCriteria) -> _CheckOutcome:
    if any(_phrase_in_text(vacancy.title, phrase) for phrase in criteria.target_titles):
        return ["target_title_match"], []

    text_fields = [
        vacancy.title,
        vacancy.description or "",
        vacancy.requirements or "",
        *vacancy.key_skills,
    ]

    is_secondary_title = any(
        _phrase_in_text(vacancy.title, phrase) for phrase in criteria.secondary_titles
    )
    has_include_keyword = _any_phrase_in_texts(text_fields, criteria.include_phrases)

    if is_secondary_title and has_include_keyword:
        return ["target_title_match"], []

    return [], ["no_relevant_keyword"]
