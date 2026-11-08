"""Доменные сущности, статусы и интерфейсы интеграций (порты).

Контракты полей соответствуют `docs/DATA_CONTRACTS.md`.
"""

from vacancy_agent.domain.assessment import (
    Gap,
    Strength,
    VacancyAssessment,
    passes_relevance_threshold,
)
from vacancy_agent.domain.candidate_profile import CANDIDATE_PROFILE
from vacancy_agent.domain.default_filter_profile import DEFAULT_FILTER_CRITERIA
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.filters import FilterCriteria, filter_vacancy
from vacancy_agent.domain.notifier_errors import TelegramNotifierError, TelegramSendError
from vacancy_agent.domain.ports import (
    QwenClient,
    TelegramNotifier,
    VacancyRepository,
    VacancySource,
)
from vacancy_agent.domain.qwen_errors import (
    QwenApiError,
    QwenClientError,
    QwenNetworkError,
    QwenResponseError,
)
from vacancy_agent.domain.repository_errors import PersistenceError, VacancyNotFoundError
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.vacancy import Vacancy

__all__ = [
    "CANDIDATE_PROFILE",
    "DEFAULT_FILTER_CRITERIA",
    "FilterCriteria",
    "FilterResult",
    "Gap",
    "PersistenceError",
    "QwenApiError",
    "QwenClient",
    "QwenClientError",
    "QwenNetworkError",
    "QwenResponseError",
    "Strength",
    "TelegramNotifier",
    "TelegramNotifierError",
    "TelegramSendError",
    "Vacancy",
    "VacancyAssessment",
    "VacancyNotFoundError",
    "VacancyRepository",
    "VacancySource",
    "VacancyStatus",
    "filter_vacancy",
    "passes_relevance_threshold",
]
