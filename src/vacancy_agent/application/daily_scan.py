"""Ежедневный сценарий (см. `docs/ARCHITECTURE.md`, «Поток обработки
вакансии»; TASK-007/ADR-021 подключила Qwen как AI-этап после жёстких
фильтров и перед Telegram — до этого, в MVP 1, вакансия уходила в
Telegram сразу после фильтров).

Для каждой вакансии из источника (SuperJob/HH):

1. пропустить уже известные по `hh_id` (дедупликация, `exists()`);
2. применить `filter_vacancy`;
3. если фильтр отклонил — сохранить с причинами и перейти к следующей
   (причины отбраковки должны быть наблюдаемы, см. «Выход из MVP 1»);
4. если принял — оценить через `QwenClient.assess`. Техническая ошибка
   (`QwenClientError` — сеть, API, невалидный ответ) — fail-closed:
   вакансия НЕ отправляется и НЕ сохраняется, обработка остальных
   вакансий продолжается (ADR-020/021: «не должен окончательно
   дедуплицировать проблемную вакансию — она повторяется в следующем
   запуске»). Если оценка получена, но `passes_relevance_threshold`
   не пройден — это не ошибка, а завершённое решение: вакансия
   сохраняется (не будет переоцениваться каждый день), но не
   отправляется. В обоих случаях (ниже порога и после отправки) оценка
   (`score`/`relevant`/`recommendation`/`strengths`/`gaps`/`risks`),
   имя модели и действовавшие на момент решения порог/итог
   (`qwen_min_score`/`qwen_passed`) сохраняются вместе с вакансией
   (`repository.save(..., assessment=..., qwen_model=..., qwen_min_score=...,
   qwen_passed=...)`) — без оценки решение Qwen нельзя было бы проверить
   или использовать в MVP 3, а отклонённая Qwen вакансия в хранилище
   была бы неотличима от прошедшей его; без снимка порога/итога решение
   было бы непроверяемо задним числом после смены `QWEN_MIN_SCORE`
   (найдено ревью TASK-007, `docs/DECISIONS.md`, ADR-022).
5. если Qwen счёл релевантной — отправить карточку в Telegram и только
   при успешной отправке сохранить. Если отправка не удалась даже
   после повторов внутри `TelegramNotifier` (`TelegramNotifierError`),
   вакансия НЕ сохраняется — на следующем прогоне она будет обработана
   заново (включая повторную оценку Qwen), а не потеряна молча.

Чистая оркестрация уже собранных зависимостей (портов из `domain/ports.py`
и `FilterCriteria`) — без обращения к `config`/окружению, тестируется на
фейках. Сборка конкретных реализаций — в `application/wiring.py`.

`VacancySource.search_vacancies` и `QwenClient.assess` — синхронные
(см. `domain/ports.py`), делают блокирующие сетевые запросы.
`run_daily_scan` — сопрограмма, которая в реальном процессе
(`scheduler.py`) делит один event loop с Telegram long polling: прямой
вызов любого из них внутри `async def` заблокировал бы цикл событий
(для Qwen — на десятки секунд на вакансию, см. калибровку TASK-006),
из-за чего нажатия кнопок в Telegram не обрабатывались бы до
завершения (тот же класс бага, что нашло ревью TASK-005 для HH).
Поэтому оба вызова уходят в отдельный поток через `asyncio.to_thread`.
"""

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from vacancy_agent.domain.assessment import passes_relevance_threshold
from vacancy_agent.domain.filters import FilterCriteria, filter_vacancy
from vacancy_agent.domain.notifier_errors import TelegramNotifierError
from vacancy_agent.domain.ports import (
    QwenClient,
    TelegramNotifier,
    VacancyRepository,
    VacancySource,
)
from vacancy_agent.domain.qwen_errors import QwenClientError
from vacancy_agent.domain.status import VacancyStatus

logger = logging.getLogger("vacancy_agent.daily_scan")


class DailyScanSummary(BaseModel):
    """Наблюдаемый результат прогона — сколько вакансий на каждом шаге."""

    model_config = ConfigDict(frozen=True)

    fetched: int = 0
    already_known: int = 0
    filtered_out: int = 0
    accepted: int = 0
    qwen_failed: int = 0
    qwen_rejected: int = 0
    notified: int = 0
    send_failed: int = 0


async def run_daily_scan(
    vacancy_client: VacancySource,
    repository: VacancyRepository,
    notifier: TelegramNotifier,
    qwen_client: QwenClient,
    criteria: FilterCriteria,
    search_params: Mapping[str, str],
    *,
    qwen_min_score: int,
    now: datetime | None = None,
) -> DailyScanSummary:
    fetched = already_known = filtered_out = accepted = 0
    qwen_failed = qwen_rejected = notified = send_failed = 0

    # `search_vacancies` блокирует (синхронный HTTP) — выносим в отдельный
    # поток, чтобы не заморозить event loop, который в реальном процессе
    # (`scheduler.py`) также обслуживает Telegram long polling.
    vacancies = await asyncio.to_thread(
        lambda: list(vacancy_client.search_vacancies(search_params))
    )

    for vacancy in vacancies:
        fetched += 1

        if repository.exists(vacancy.hh_id):
            already_known += 1
            continue

        filter_result = filter_vacancy(vacancy, criteria, now=now)

        if not filter_result.accepted:
            repository.save(vacancy, filter_result, VacancyStatus.NEW)
            filtered_out += 1
            continue

        accepted += 1

        try:
            assessment = await asyncio.to_thread(qwen_client.assess, vacancy)
        except QwenClientError:
            logger.warning(
                "Qwen не смог оценить hh_id=%s — повтор на следующем прогоне",
                vacancy.hh_id,
                exc_info=True,
            )
            qwen_failed += 1
            continue

        qwen_passed = passes_relevance_threshold(assessment, qwen_min_score)
        if not qwen_passed:
            repository.save(
                vacancy,
                filter_result,
                VacancyStatus.NEW,
                assessment=assessment,
                qwen_model=qwen_client.model,
                qwen_min_score=qwen_min_score,
                qwen_passed=qwen_passed,
            )
            qwen_rejected += 1
            continue

        try:
            await notifier.send_vacancy_card(vacancy)
        except TelegramNotifierError:
            logger.warning(
                "Не удалось отправить карточку hh_id=%s — повтор на следующем прогоне",
                vacancy.hh_id,
                exc_info=True,
            )
            send_failed += 1
            continue

        repository.save(
            vacancy,
            filter_result,
            VacancyStatus.NEW,
            assessment=assessment,
            qwen_model=qwen_client.model,
            qwen_min_score=qwen_min_score,
            qwen_passed=qwen_passed,
        )
        notified += 1

    return DailyScanSummary(
        fetched=fetched,
        already_known=already_known,
        filtered_out=filtered_out,
        accepted=accepted,
        qwen_failed=qwen_failed,
        qwen_rejected=qwen_rejected,
        notified=notified,
        send_failed=send_failed,
    )
