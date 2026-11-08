"""Application service TASK-011 (ADR-034/ADR-035), TASK-012 (ADR-052/053) —
синхронный local-Qwen vertical slice подготовки проверяемого черновика
отклика.

Зависит только от фабрик портов (`ApplicationDraftRepository`/
`ApplicationDraftGenerator`), не от FastAPI/SQLite/LM Studio/httpx напрямую —
тот же принцип, что `application/vacancy_queries.py` (TASK-009) и
`application/wiring.py` (TASK-008).

Порядок `prepare_application_draft` (см. `docs/TASK-011.md`, «Application
service»):

1. открыть repository, проверить уже существующий черновик;
2. если он создан той же версией Master Profile, промпта И той же моделью
   (`model_resolver()`, ADR-049) — вернуть его с `reused=True` до любых
   проверок Qwen settings/сети; `model_resolver()` вызывается только когда
   profile/prompt уже совпали (Python `and` замыкает накоротко) — не создаёт
   generator и не трогает сеть; устаревший по любому из трёх компонентов
   черновик не переиспользуется (ADR-042/ADR-049);
3. иначе прочитать вакансию и проверить предусловия (существует, принята
   hard filters, оценена Qwen, прошла порог, статус не `skipped`, и —
   TASK-012 — `vacancy.requirements` непуст после trim: без него плану
   нечего цитировать, `_require_eligible`, HTTP `409`);
4. закрыть repository ДО вызова LM Studio — вызов может занимать до 300
   секунд (`docs/TASK-011.md`), удерживать SQLite-соединение всё это время
   не нужно и мешало бы параллельному GET;
5. создать generator лениво, вызвать и закрыть его в `finally` — с TASK-012
   `generator.generate` возвращает `ApplicationDraftPlan`, не готовый
   контент;
6. проверить план (`domain/application_draft_plan.py: validate_plan` —
   цитаты дословны, evidence ID существуют в Master Profile) и построить
   `ApplicationDraftContent` детерминированным renderer'ом
   (`domain/application_draft_renderer.py`), затем собрать Python-owned
   envelope;
7. открыть новый repository, попытаться сохранить и закрыть его;
8. при проигранной локальной гонке (`save_application_draft` вернул
   `False`) прочитать уже сохранённый черновик; если его
   `profile_version`/`prompt_version`/`model` — все три — совпадают с
   этой генерацией, вернуть его с `reused=True`; если хотя бы один
   компонент другой (конкурент сохранил результат другой версии,
   ADR-049/ADR-050) — не выдавать чужой результат за актуальный, а
   повторить попытку сохранить свой один раз; если и повтор проиграл
   гонку с несовпадающим результатом — поднять
   `ApplicationDraftConcurrencyError`, а не молча вернуть чужую версию
   (`_resolve_after_lost_race`).

Ошибка `close()` (repository или generator) логируется без секретов и не
маскирует первичную generation/application-ошибку — та же политика, что
`application.wiring._close_all` (`docs/DECISIONS.md`, ADR-026)."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NamedTuple, Protocol

from vacancy_agent.domain.application_draft import ApplicationDraft, MasterProfile
from vacancy_agent.domain.application_draft_errors import (
    ApplicationDraftConcurrencyError,
    VacancyNotEligibleError,
)
from vacancy_agent.domain.application_draft_plan import validate_plan
from vacancy_agent.domain.application_draft_renderer import render_application_draft_content
from vacancy_agent.domain.ports import ApplicationDraftGenerator, ApplicationDraftRepository
from vacancy_agent.domain.repository_errors import VacancyNotFoundError
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.stored_application_draft import StoredApplicationDraft
from vacancy_agent.domain.stored_vacancy import StoredVacancy

logger = logging.getLogger("vacancy_agent.application_drafts")

DRAFT_SCHEMA_VERSION = "2"

ApplicationDraftRepositoryFactory = Callable[[], ApplicationDraftRepository]
ApplicationDraftGeneratorFactory = Callable[[], ApplicationDraftGenerator]
# Дешёвая проверка сконфигурированного имени модели — читает только
# настройки, не создаёт `ApplicationDraftGenerator` и не открывает сеть
# (TASK-011, ADR-049). Реальная реализация (`api/main.py`) читает только
# `QwenSettings().model`; полноценный generator строится отдельно, только
# когда `generator_factory()` реально вызывается для генерации.
ApplicationDraftModelResolver = Callable[[], str]


class _Closeable(Protocol):
    def close(self) -> None: ...


class ApplicationDraftResult(NamedTuple):
    stored: StoredApplicationDraft
    reused: bool


class ApplicationDraftPreflight(NamedTuple):
    """Безопасная сводка перед запросом human approval."""

    vacancy_id: str
    title: str
    company: str | None


def preflight_application_draft(
    repository_factory: ApplicationDraftRepositoryFactory,
    vacancy_id: str,
) -> ApplicationDraftPreflight:
    """Проверить вакансию без создания generator и вызова LLM."""

    repository = repository_factory()
    try:
        stored_vacancy = repository.get_vacancy(vacancy_id)
        if stored_vacancy is None:
            raise VacancyNotFoundError(f"Вакансия {vacancy_id!r} не найдена")

        _require_eligible(stored_vacancy)

        return ApplicationDraftPreflight(
            vacancy_id=stored_vacancy.vacancy.hh_id,
            title=stored_vacancy.vacancy.title,
            company=stored_vacancy.vacancy.company,
        )
    finally:
        _safe_close(repository)


def prepare_application_draft(
    repository_factory: ApplicationDraftRepositoryFactory,
    generator_factory: ApplicationDraftGeneratorFactory,
    model_resolver: ApplicationDraftModelResolver,
    profile: MasterProfile,
    prompt_version: str,
    vacancy_id: str,
) -> ApplicationDraftResult:
    repository = repository_factory()
    try:
        existing = repository.get_application_draft(vacancy_id)
        if (
            existing is not None
            and existing.draft.profile_version == profile.version
            and existing.prompt_version == prompt_version
            # `and` замыкает накоротко: `model_resolver()` вызывается,
            # только когда profile/prompt уже совпали — иначе устаревший по
            # ним черновик и так не переиспользуется, а лишний вызов
            # `model_resolver()` был бы ненужным чтением настроек (TASK-011,
            # ADR-049).
            and existing.model == model_resolver()
        ):
            return ApplicationDraftResult(stored=existing, reused=True)

        stored_vacancy = repository.get_vacancy(vacancy_id)
        if stored_vacancy is None:
            raise VacancyNotFoundError(f"Вакансия {vacancy_id!r} не найдена")
        _require_eligible(stored_vacancy)
    finally:
        _safe_close(repository)

    # `_require_eligible` уже гарантировала `assessment is not None` выше —
    # явный `assert` только сужает тип для mypy, не добавляет новую проверку.
    assert stored_vacancy.assessment is not None

    generator = generator_factory()
    try:
        # Найдено ревью TASK-011: `generator.model` раньше читался ДО этого
        # `try` — если бы чтение атрибута само по себе подняло исключение,
        # `_safe_close(generator)` не выполнился бы вовсе. Теперь чтение и
        # вызов внутри одного `try/finally`, как и требует гарантия
        # закрытия «при любой ошибке».
        model_name = generator.model
        plan = generator.generate(stored_vacancy.vacancy, stored_vacancy.assessment, profile)
    finally:
        _safe_close(generator)

    # TASK-012: `generator.generate` вернула только план (цитаты требований
    # + evidence ID), не готовый контент. `_require_eligible` уже
    # гарантировала, что `vacancy.requirements` непуст — явный `assert`
    # только сужает тип для mypy, не добавляет новую проверку.
    assert stored_vacancy.vacancy.requirements is not None
    validate_plan(plan, stored_vacancy.vacancy.requirements, profile)
    content = render_application_draft_content(plan, profile)

    draft = ApplicationDraft(
        schema_version=DRAFT_SCHEMA_VERSION,
        vacancy_id=vacancy_id,
        profile_version=profile.version,
        content=content,
    )
    stored = StoredApplicationDraft(
        draft=draft,
        model=model_name,
        prompt_version=prompt_version,
        created_at=datetime.now(UTC),
    )

    repository = repository_factory()
    try:
        inserted = repository.save_application_draft(stored)
        if inserted:
            return ApplicationDraftResult(stored=stored, reused=False)
        # Проиграна локальная гонка (unique conflict на vacancy_id) — не
        # ошибка сама по себе: другой вызов уже успешно сохранил черновик.
        # Чья версия победила (profile/prompt/model, все три) — отдельный
        # вопрос (ADR-049/ADR-050, см. `_resolve_after_lost_race`).
        return _resolve_after_lost_race(repository, stored, vacancy_id)
    finally:
        _safe_close(repository)


def _matches_this_generation(
    candidate: StoredApplicationDraft, stored: StoredApplicationDraft
) -> bool:
    """Полная тройка reuse identity (TASK-011, ADR-049/ADR-050) —
    `profile_version`, `prompt_version` и `model` — не только `model`."""

    return (
        candidate.draft.profile_version == stored.draft.profile_version
        and candidate.prompt_version == stored.prompt_version
        and candidate.model == stored.model
    )


def _resolve_after_lost_race(
    repository: ApplicationDraftRepository,
    stored: StoredApplicationDraft,
    vacancy_id: str,
) -> ApplicationDraftResult:
    """Вызывается только после проигранной локальной гонки за вставку
    (TASK-011, ADR-049, ужесточено ADR-050): читает уже сохранённый
    победившей стороной черновик и решает, можно ли отдать его как
    `reused=True`.

    Сверяет ПОЛНУЮ тройку `profile_version`/`prompt_version`/`model`
    (`_matches_this_generation`), не только `model` — Codex review-fix
    ADR-050: прежняя версия сравнивала одну модель, поэтому могла отдать
    как `reused=True` черновик с чужим `profile_version`. Если тройка
    совпадает — конкурент сгенерировал ровно то же самое, результат
    равноценен нашему; отдаём его. Если хотя бы один компонент другой —
    конкурент мог быть более ранним in-flight вызовом с уже сменившейся
    версией (A/B по модели, либо смена Master Profile/промпта между
    запросами); выдавать его результат за `reused=True` значило бы
    представить чужую версию как актуальную для запроса, ожидавшего
    другую. Вместо этого повторяем `save_application_draft` со своим
    `stored` ровно один раз — UPSERT-условие в `persistence/
    sqlite_repository.py` учитывает все три поля, поэтому повтор должен
    заменить устаревшую строку.

    Если и повторная попытка проиграла гонку, читаем результат СНОВА и
    ТАК ЖЕ сверяем полную тройку (ADR-050: прежняя версия возвращала его
    безусловно, без проверки вообще). Совпадает — отдаём как раньше. Не
    совпадает — значит минимум два конкурентных сохранения подряд
    выиграли с версией, отличной от нашей; неограниченный retry-цикл не
    нужен для однопользовательского локального сценария
    (`docs/TASK-011.md`), поэтому поднимаем `ApplicationDraftConcurrencyError`
    вместо того, чтобы молча выдать несовпадающий черновик за
    актуальный."""

    raced = repository.get_application_draft(vacancy_id)
    if raced is None:
        raise AssertionError("save_application_draft вернул False, но черновика нет в хранилище")
    if _matches_this_generation(raced, stored):
        return ApplicationDraftResult(stored=raced, reused=True)

    if repository.save_application_draft(stored):
        return ApplicationDraftResult(stored=stored, reused=False)

    raced_again = repository.get_application_draft(vacancy_id)
    if raced_again is None:
        raise AssertionError("save_application_draft вернул False, но черновика нет в хранилище")
    if _matches_this_generation(raced_again, stored):
        return ApplicationDraftResult(stored=raced_again, reused=True)

    raise ApplicationDraftConcurrencyError(
        "Конкурентная запись помешала сохранить черновик этой версии — повторите запрос"
    )


def get_application_draft(
    repository_factory: ApplicationDraftRepositoryFactory, vacancy_id: str
) -> StoredApplicationDraft | None:
    repository = repository_factory()
    try:
        return repository.get_application_draft(vacancy_id)
    finally:
        _safe_close(repository)


def _require_eligible(stored: StoredVacancy) -> None:
    if not stored.filter_result.accepted:
        raise VacancyNotEligibleError("вакансия не принята hard filters")
    if stored.assessment is None:
        raise VacancyNotEligibleError("вакансия ещё не оценена Qwen")
    if not stored.qwen_passed:
        raise VacancyNotEligibleError("вакансия не прошла порог Qwen")
    if stored.status == VacancyStatus.SKIPPED:
        raise VacancyNotEligibleError("вакансия помечена как пропущенная (skipped)")
    # TASK-012 («Детерминированные проверки», пункт 1): без непустого
    # `vacancy.requirements` плану нечего дословно цитировать — это
    # безопасный `409` ДО создания generator, не `502` после реального
    # вызова LM Studio.
    if not (stored.vacancy.requirements or "").strip():
        raise VacancyNotEligibleError("вакансия не содержит текста требований (requirements)")


def _safe_close(resource: _Closeable) -> None:
    try:
        resource.close()
    except Exception:
        logger.exception("Не удалось закрыть ресурс %r", resource)
