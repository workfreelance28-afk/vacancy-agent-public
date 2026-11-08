"""Публичная read-модель сохранённой вакансии (TASK-009, ADR-028/ADR-029).

Заменяет приватный `_StoredVacancy` из `persistence/sqlite_repository.py`:
`application/vacancy_queries.py` и HTTP-слой (`api/`) не должны зависеть от
модуля персистентности ради одного типа возврата чтения. Поля и их порядок
не изменились — существующий код и тесты, читающие результат `get()` по
позиции (`stored[0]`, ...), продолжают работать (см. ADR-022)."""

from typing import NamedTuple

from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.vacancy import Vacancy


class StoredVacancy(NamedTuple):
    vacancy: Vacancy
    filter_result: FilterResult
    status: VacancyStatus
    assessment: VacancyAssessment | None
    qwen_model: str | None
    qwen_min_score: int | None
    qwen_passed: bool | None
