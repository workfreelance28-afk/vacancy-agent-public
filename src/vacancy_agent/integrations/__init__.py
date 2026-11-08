"""Конкретные реализации портов из `vacancy_agent.domain.ports`.

Источники вакансий: `HttpxHHClient` (TASK-002; переключаемый, не
основной с ADR-013/ADR-014) и `SuperJobClient` (основной с ADR-014,
после закрытия анонимного доступа к HH API) — выбор между ними задаётся
`Settings.vacancy_source` (`application/wiring.py`). Telegram-бот
реализован в TASK-005. `LMStudioQwenClient` (ADR-015, подключён к
`application/daily_scan.py` в TASK-007/ADR-021) — первый AI-этап (MVP 2),
локальная модель через LM Studio; его ошибки — в `domain/qwen_errors.py`,
не здесь (та же причина, что и `domain/notifier_errors.py`, ADR-012).
Claude-клиент не реализуется раньше MVP 3.
"""

from vacancy_agent.integrations.hh_client import HttpxHHClient
from vacancy_agent.integrations.hh_errors import (
    HHApiError,
    HHClientError,
    HHNetworkError,
    HHNormalizationError,
)
from vacancy_agent.integrations.hh_normalize import normalize_vacancy as normalize_hh_vacancy
from vacancy_agent.integrations.qwen_client import LMStudioQwenClient
from vacancy_agent.integrations.superjob_client import SuperJobClient
from vacancy_agent.integrations.superjob_errors import (
    SuperJobApiError,
    SuperJobClientError,
    SuperJobNetworkError,
    SuperJobNormalizationError,
)
from vacancy_agent.integrations.superjob_normalize import (
    normalize_vacancy as normalize_superjob_vacancy,
)

__all__ = [
    "HHApiError",
    "HHClientError",
    "HHNetworkError",
    "HHNormalizationError",
    "HttpxHHClient",
    "LMStudioQwenClient",
    "SuperJobApiError",
    "SuperJobClient",
    "SuperJobClientError",
    "SuperJobNetworkError",
    "SuperJobNormalizationError",
    "normalize_hh_vacancy",
    "normalize_superjob_vacancy",
]
