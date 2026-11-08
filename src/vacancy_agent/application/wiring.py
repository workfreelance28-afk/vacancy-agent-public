"""Сборка конкретных реализаций портов из `Settings` для реального запуска.

В отличие от `daily_scan.py` (принимает уже собранные зависимости,
тестируется на фейках), этот модуль знает про конкретные интеграции
(`httpx`, SQLite, `aiogram`) и переменные окружения — используется только
точкой входа, которая реально запускает процесс (`scheduler.py`), а не
доменной/сценарной логикой.

TASK-015 (combined SuperJob+Habr прогон) добавлен ЦЕЛИКОМ здесь, не в
`daily_scan.py`: `run_daily_scan` остаётся source-agnostic примитивом
«один источник → SQLite/Telegram», не знающим о комбинированном режиме —
`RuntimeComponents` собирает один или несколько описателей запуска
(`_SourceRun`), а `_run_source_scans`/`run_scan_once` вызывают
`run_daily_scan` по одному разу на каждый, агрегируя результат."""

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import NamedTuple, Protocol

from aiogram import Bot
from pydantic import Field

from vacancy_agent.application.daily_scan import DailyScanSummary, run_daily_scan
from vacancy_agent.config.settings import (
    FilterSettings,
    HabrCareerSettings,
    HHSettings,
    Settings,
    SuperJobSettings,
)
from vacancy_agent.domain.default_filter_profile import DEFAULT_FILTER_CRITERIA
from vacancy_agent.domain.filters import FilterCriteria
from vacancy_agent.domain.ports import (
    QwenClient,
    TelegramNotifier,
    VacancyRepository,
    VacancySource,
)
from vacancy_agent.domain.vacancy_source_errors import VacancySourceError
from vacancy_agent.integrations.habr_client import HabrCareerSource
from vacancy_agent.integrations.hh_client import HttpxHHClient
from vacancy_agent.integrations.qwen_client import LMStudioQwenClient
from vacancy_agent.integrations.superjob_client import SuperJobClient
from vacancy_agent.integrations.telegram_bot import AiogramTelegramNotifier
from vacancy_agent.persistence.factory import create_vacancy_repository

logger = logging.getLogger("vacancy_agent.wiring")

_SUMMARY_FIELDS = tuple(DailyScanSummary.model_fields.keys())
"""Читается из `DailyScanSummary` динамически, а не хардкодится — если
набор счётчиков `daily_scan.py` изменится, агрегация здесь не разойдётся
с ним молча (`daily_scan.py` в остальном не тронут, TASK-015 scope)."""


class ScanRunSummary(DailyScanSummary):
    """Итог `run_scan_once` (TASK-015) — тот же контракт, что
    `DailyScanSummary` (суммарные счётчики), плюс разбивка по источникам и
    список источников, не завершившихся успешно. Обычный однопоточный
    режим (`hh`/`superjob`/`habr`) тоже возвращает именно этот тип —
    `per_source` в нём просто содержит один элемент, `failed_sources`
    обычно пуст — CLI/scheduler используют один и тот же контракт
    независимо от режима."""

    per_source: Mapping[str, DailyScanSummary] = Field(default_factory=dict)
    failed_sources: tuple[str, ...] = Field(default_factory=tuple)


class _Closeable(Protocol):
    def close(self) -> None: ...


def _close_all(resources: Iterable[_Closeable]) -> None:
    """Закрывает каждый ресурс, логируя (а не поднимая) ошибку закрытия —
    иначе один сломанный `resource.close()` останавливал бы цикл и оставлял
    остальные ресурсы открытыми (найдено ревью TASK-008/ADR-025: и в
    unwind-ветке `RuntimeComponents.__init__`, где важнее не потерять
    исходное исключение конструирования, и в `close()`/`main.py`, где
    ошибка закрытия одного ресурса не должна мешать закрыть остальные,
    включая `bot.session`)."""

    for resource in resources:
        try:
            resource.close()
        except Exception:
            logger.exception("Не удалось закрыть ресурс %r", resource)


def build_filter_criteria(settings: FilterSettings) -> FilterCriteria:
    """Переопределяет `DEFAULT_FILTER_CRITERIA` тем, что явно задано в
    `FilterSettings` — пустой список/`None` означает «взять из профиля»,
    а не «пустое значение» (см. `FilterSettings` docstring, ADR-012)."""

    defaults = DEFAULT_FILTER_CRITERIA
    return FilterCriteria(
        target_titles=settings.target_titles or defaults.target_titles,
        secondary_titles=settings.secondary_titles or defaults.secondary_titles,
        include_phrases=settings.include_phrases or defaults.include_phrases,
        exclude_title_phrases=settings.exclude_title_phrases or defaults.exclude_title_phrases,
        internship_phrases=settings.internship_phrases or defaults.internship_phrases,
        exclude_anywhere_phrases=(
            settings.exclude_anywhere_phrases or defaults.exclude_anywhere_phrases
        ),
        salary_min=settings.salary_min if settings.salary_min is not None else defaults.salary_min,
        allow_missing_salary=settings.allow_missing_salary,
        allowed_areas=settings.allowed_areas or defaults.allowed_areas,
        allowed_work_formats=settings.allowed_work_formats or defaults.allowed_work_formats,
        allowed_experience=settings.allowed_experience or defaults.allowed_experience,
        max_age_days=(
            settings.max_age_days if settings.max_age_days is not None else defaults.max_age_days
        ),
    )


def build_hh_search_params(settings: HHSettings) -> dict[str, str]:
    """Параметры запроса к `GET /vacancies` (см. `VacancySource.search_vacancies`)."""

    params: dict[str, str] = {}
    if settings.search_text:
        params["text"] = settings.search_text
    if settings.area:
        params["area"] = settings.area
    if settings.period_days is not None:
        params["period"] = str(settings.period_days)
    return params


def build_superjob_search_params(settings: SuperJobSettings) -> dict[str, str]:
    """Параметры запроса к `GET /2.0/vacancies/` (см. `VacancySource.search_vacancies`).

    Несколько направлений поиска (`SuperJobSettings.keywords`) передаются
    как один `keywords[0]`-блок официального SuperJob-синтаксиса (см.
    `api.superjob.ru`, раздел про параметры поиска), а не как
    самодельный comma-joined `keyword=...` (устаревший подход, найден
    ревью — синтаксис не был подтверждён вживую и оказался неверным,
    см. `docs/DECISIONS.md`, ADR-016):

    - `keywords[0][keys]` — список фраз через запятую;
    - `keywords[0][srws]=10` — искать по всему тексту вакансии
      (должность + обязанности + требования + условия), не только по
      названию — иначе часть релевантных вакансий не находилась бы;
    - `keywords[0][skwc]=or` — «хотя бы одно слово/фраза», не «все сразу».

    Живым запросом (`docs/DECISIONS.md`, ADR-016) подтверждено: этот
    набор параметров возвращает `200` и реально фильтрует результаты
    (осмысленная выборка на реальных ключевых словах, ноль результатов
    на заведомо бессмысленном ключевом слове).
    """

    params: dict[str, str] = {}
    if settings.keywords:
        params["keywords[0][keys]"] = ", ".join(settings.keywords)
        params["keywords[0][srws]"] = "10"
        params["keywords[0][skwc]"] = "or"
    if settings.town:
        params["town"] = settings.town
    return params


def build_habr_search_params(settings: HabrCareerSettings) -> dict[str, str]:
    """Параметры запроса к `GET /vacancies` career.habr.com (см.
    `VacancySource.search_vacancies`). Пустой `search_query` передаётся
    как есть — `HabrCareerSource.search_vacancies` отклоняет его сама, до
    первого сетевого запроса (TASK-013), не эта функция: она вызывается
    и когда `habr` вообще не выбран текущим `VACANCY_SOURCE`."""

    return {"q": settings.search_query}


class _SourceSpec(NamedTuple):
    """Как построить один источник — ДО его фактического создания
    (TASK-015). `build_client` — отложенный конструктор (не готовый
    клиент): `RuntimeComponents.__init__` вызывает его и сразу добавляет
    результат в `opened`, ПРЕЖДЕ чем строить следующий источник — иначе
    сбой конструктора второго источника (например, `Habr` в
    `superjob_habr`) оставил бы уже созданный первый клиент
    незарегистрированным для закрытия при unwind."""

    name: str
    build_client: Callable[[], VacancySource]
    search_params: Mapping[str, str]


class _SourceRun(NamedTuple):
    """Один уже построенный источник — то, что реально исполняет
    `_run_source_scans`."""

    name: str
    client: VacancySource
    search_params: Mapping[str, str]


def _build_source_specs(settings: Settings) -> list[_SourceSpec]:
    """Порядок в `superjob_habr` фиксирован: SuperJob, затем Habr
    (TASK-015). HH в combined-режим не входит — остаётся отдельным
    резервным вариантом (`docs/TASK-013.md`/`docs/TASK-015.md`)."""

    if settings.vacancy_source == "superjob_habr":
        return [
            _SourceSpec(
                "superjob",
                lambda: SuperJobClient(settings.superjob),
                build_superjob_search_params(settings.superjob),
            ),
            _SourceSpec(
                "habr",
                lambda: HabrCareerSource(settings.habr),
                build_habr_search_params(settings.habr),
            ),
        ]
    if settings.vacancy_source == "superjob":
        return [
            _SourceSpec(
                "superjob",
                lambda: SuperJobClient(settings.superjob),
                build_superjob_search_params(settings.superjob),
            )
        ]
    if settings.vacancy_source == "habr":
        return [
            _SourceSpec(
                "habr",
                lambda: HabrCareerSource(settings.habr),
                build_habr_search_params(settings.habr),
            )
        ]
    return [
        _SourceSpec("hh", lambda: HttpxHHClient(settings.hh), build_hh_search_params(settings.hh))
    ]


def _open_source_runs(specs: Sequence[_SourceSpec], opened: list[_Closeable]) -> list[_SourceRun]:
    """Строит каждый источник по его `_SourceSpec` и СРАЗУ добавляет
    построенный клиент в `opened` (список ресурсов, которые вызывающий код
    закроет через `_close_all`, включая случай, когда построение
    ПОСЛЕДУЮЩЕГО источника упадёт) — TASK-015: «все source-клиенты
    закрываются... при partial construction failure». Вынесена отдельной
    функцией, а не инлайн в `RuntimeComponents.__init__`, специально ради
    прямой проверяемости этой гарантии юнит-тестом без реального SQLite/
    Telegram/HTTP."""

    source_runs: list[_SourceRun] = []
    for spec in specs:
        client = spec.build_client()
        opened.append(client)
        source_runs.append(_SourceRun(spec.name, client, spec.search_params))
    return source_runs


class RuntimeComponents:
    """Держит живые ресурсы (HTTP/SQLite-соединения, Telegram `Bot`), которые
    нужно закрыть по завершении процесса — см. `close()`.

    Источник(и) вакансий (`source_runs`) выбираются через
    `Settings.vacancy_source` (`VACANCY_SOURCE`) — HH остаётся
    переключаемой реализацией, не удалён (см. `docs/DECISIONS.md`,
    ADR-013/ADR-014); `superjob_habr` (TASK-015) даёт больше одного
    элемента в `source_runs`, обрабатываемых последовательно
    `_run_source_scans`. `qwen_client`/`qwen_min_score` — модель, порог и
    политика ошибок утверждены калибровкой TASK-006 (ADR-020) и не
    переопределяются здесь — только читаются из `settings.qwen`
    (TASK-007, ADR-021)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Каждый уже открытый ресурс закрывается, если построение упадёт на
        # более позднем шаге (например, `Bot(token=...)` бросает
        # `TokenValidationError` на пустом токене) — иначе `repository`/
        # source-клиенты/`qwen_client` остались бы открытыми, а вызывающий
        # код не получил бы ссылку на них, чтобы закрыть (TASK-008/ADR-025:
        # прежде это было безопасно только потому, что процесс сразу падал
        # целиком; для одноразового `vacancy-agent scan`, который сам решает,
        # что делать дальше при ошибке, это уже не гарантия). TASK-015:
        # каждый source-клиент добавляется в `opened` СРАЗУ после своего
        # конструктора — сбой построения второго источника не оставляет
        # первый незарегистрированным.
        opened: list[_Closeable] = []
        try:
            self.repository = create_vacancy_repository(settings.database)
            opened.append(self.repository)

            self.source_runs = _open_source_runs(_build_source_specs(settings), opened)

            self.qwen_client = LMStudioQwenClient(settings.qwen)
            opened.append(self.qwen_client)
            self.qwen_min_score = settings.qwen.min_score
            self.bot = Bot(token=settings.telegram.bot_token.get_secret_value())
            self.notifier = AiogramTelegramNotifier(
                settings.telegram, self.repository, bot=self.bot
            )
            self.filter_criteria = build_filter_criteria(settings.filters)
        except Exception:
            _close_all(reversed(opened))
            raise

    def close(self) -> None:
        _close_all(
            (self.repository, *(run.client for run in self.source_runs), self.qwen_client)
        )


async def _run_source_scans(
    source_runs: Sequence[_SourceRun],
    repository: VacancyRepository,
    notifier: TelegramNotifier,
    qwen_client: QwenClient,
    criteria: FilterCriteria,
    *,
    qwen_min_score: int,
) -> ScanRunSummary:
    """Вызывает `run_daily_scan` по одному разу на каждый источник,
    последовательно, в переданном порядке (TASK-015: «без очереди и
    параллелизма»), и агрегирует результат.

    `VacancySourceError` (сеть/API/HTML одного источника) — ожидаемый,
    изолированный сбой: логируется безопасно (без текста исключения в
    возвращаемом summary), источник помечается в `failed_sources`, и
    обработка продолжается со следующего. Любое ДРУГОЕ исключение —
    SQLite, Qwen wiring, Telegram, программная ошибка — не перехватывается
    здесь и останавливает прогон целиком, как и раньше `run_daily_scan`
    делала для единственного источника."""

    per_source: dict[str, DailyScanSummary] = {}
    failed_sources: list[str] = []
    totals = dict.fromkeys(_SUMMARY_FIELDS, 0)

    for run in source_runs:
        try:
            summary = await run_daily_scan(
                run.client,
                repository,
                notifier,
                qwen_client,
                criteria,
                run.search_params,
                qwen_min_score=qwen_min_score,
            )
        except VacancySourceError:
            logger.warning(
                "Источник %r завершился ошибкой — остальные источники "
                "обрабатываются дальше",
                run.name,
                exc_info=True,
            )
            failed_sources.append(run.name)
            continue

        per_source[run.name] = summary
        for field in _SUMMARY_FIELDS:
            totals[field] += getattr(summary, field)

    return ScanRunSummary(**totals, per_source=per_source, failed_sources=tuple(failed_sources))


async def run_scan_once(components: RuntimeComponents) -> ScanRunSummary:
    """Один прогон всех источников `RuntimeComponents.source_runs`.

    Единственное место, где аргументы `run_daily_scan` собираются из
    `RuntimeComponents` — переиспользуется и `scheduler.py` (внутри
    постоянно работающего процесса), и CLI-командой `vacancy-agent scan`
    (одноразовый процесс, TASK-008/ADR-025), чтобы не дублировать этот
    список аргументов в обоих местах. Обычный однопоточный режим тоже идёт
    через `_run_source_scans` — один элемент в `source_runs` вместо
    нескольких (TASK-015: «Обычный режим с одним источником также
    использует этот контракт»)."""

    return await _run_source_scans(
        components.source_runs,
        components.repository,
        components.notifier,
        components.qwen_client,
        components.filter_criteria,
        qwen_min_score=components.qwen_min_score,
    )
