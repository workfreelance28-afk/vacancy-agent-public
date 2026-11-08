"""Общая база ожидаемых ошибок источника вакансий (HH/SuperJob/Habr Career).

В домене, а не в `integrations/`, по той же причине, что `qwen_errors.py`/
`notifier_errors.py`/`repository_errors.py` (`docs/DECISIONS.md`, ADR-012):
вызывающий код должен различать «штатный отказ конкретного источника» от
программной ошибки/сбоя SQLite/Qwen/Telegram, не зная, каким клиентом
реализован источник.

TASK-015: combined-режим (`application/wiring.py`) ловит сбой ОДНОГО
источника этим единственным типом — не перечисляя `HHClientError`/
`SuperJobClientError`/`HabrCareerClientError` по отдельности — и продолжает
обрабатывать остальные источники. Любое исключение, НЕ являющееся
`VacancySourceError` (SQLite, Qwen wiring, Telegram, программная ошибка),
обязано останавливать прогон целиком, как и раньше."""


class VacancySourceError(Exception):
    """Базовая ошибка любого `VacancySource` (`domain/ports.py`) — ожидаемый
    сбой конкретного источника вакансий (сеть, API, разбор ответа/HTML), не
    программная ошибка."""
