"""Сценарии использования (use cases).

`daily_scan` — ежедневный прогон (HH → дедупликация → фильтры →
хранилище → Telegram), реализован в TASK-005. `wiring` собирает
конкретные реализации портов из `Settings` для реального запуска.
"""

from vacancy_agent.application.daily_scan import DailyScanSummary, run_daily_scan
from vacancy_agent.application.wiring import (
    RuntimeComponents,
    build_filter_criteria,
    build_hh_search_params,
)

__all__ = [
    "DailyScanSummary",
    "RuntimeComponents",
    "build_filter_criteria",
    "build_hh_search_params",
    "run_daily_scan",
]
