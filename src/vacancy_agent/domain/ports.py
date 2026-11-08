"""Интерфейсы интеграций (порты), которые реализует слой `integrations/`.

Домен и будущий слой `application/` зависят только от этих протоколов,
а не от конкретных клиентов — это соответствует таблице ответственности
компонентов в `docs/ARCHITECTURE.md`. Конкретные реализации появляются
по мере задач: HH-клиент — TASK-002, репозиторий — TASK-004,
Telegram-бот — TASK-005, `QwenClient` — MVP 2. `ClaudeClient`
(отклонённый Claude vertical slice, TASK-010/ADR-032/ADR-033) удалён в
TASK-011 вместе с остальными неиспользуемыми placeholders — местный Qwen
и его собственные порты ниже (`ApplicationDraftGenerator`,
`ApplicationDraftRepository`) заменили этот план (ADR-034/ADR-035).
"""

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Protocol
from uuid import UUID

from vacancy_agent.domain.application_draft import MasterProfile
from vacancy_agent.domain.application_draft_plan import ApplicationDraftPlan
from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.scan_job import ScanJob
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.stored_application_draft import StoredApplicationDraft
from vacancy_agent.domain.stored_vacancy import StoredVacancy
from vacancy_agent.domain.vacancy import Vacancy


class VacancySource(Protocol):
    """Получение вакансий из внешнего источника (HH, SuperJob, ...). Не отвечает за их оценку.

    Синхронный протокол намеренно: `search_vacancies` — генератор с
    блокирующими HTTP-запросами (`httpx.Client`, не `AsyncClient`,
    см. `docs/DECISIONS.md`, ADR-007/ADR-010). Реальный вызывающий код
    (`application/daily_scan.py`) делит event loop с Telegram long
    polling и оборачивает вызов в `asyncio.to_thread`, а не делает
    протокол асинхронным (ADR-012, review-fix P2 «синхронный HH-клиент
    блокировал Telegram polling») — переход на `async def` здесь
    потребовал бы пересмотра уже проверенного тестом фикса без причины.
    """

    def search_vacancies(self, params: Mapping[str, str]) -> Iterable[Vacancy]: ...

    def close(self) -> None: ...


class VacancyRepository(Protocol):
    """Хранение вакансий, фильтрации и статусов. Обеспечивает идемпотентность по `hh_id`.

    `assessment`/`qwen_model`/`qwen_min_score`/`qwen_passed` — необязательные
    (по умолчанию `None`): вакансия, отклонённая жёсткими фильтрами, никогда
    не доходит до Qwen, и для неё оценки не будет. Если оценка есть, она
    должна быть сохранена вместе с вакансией — иначе решение Qwen нельзя
    проверить или использовать в MVP 3, а вакансия, отклонённая Qwen, в
    хранилище неотличима от прошедшей его (найдено ревью TASK-007,
    `docs/DECISIONS.md`, ADR-022). `qwen_min_score`/`qwen_passed` —
    действовавший порог и итог порогового решения НА МОМЕНТ обработки:
    порог конфигурируем и может измениться позже, поэтому по одному
    `assessment.score` нельзя восстановить, прошла бы вакансия порог
    в момент решения (тоже найдено ревью TASK-007, ADR-022)."""

    def exists(self, hh_id: str) -> bool: ...

    def save(
        self,
        vacancy: Vacancy,
        filter_result: FilterResult,
        status: VacancyStatus,
        *,
        assessment: VacancyAssessment | None = None,
        qwen_model: str | None = None,
        qwen_min_score: int | None = None,
        qwen_passed: bool | None = None,
    ) -> None: ...

    def update_status(self, hh_id: str, status: VacancyStatus) -> None: ...


class VacancyReadRepository(Protocol):
    """Только чтение сохранённых вакансий (TASK-009, ADR-028/ADR-029).

    Отдельный протокол от `VacancyRepository`, а не расширение его новыми
    методами: `VacancyRepository` уже используется `application/daily_scan.py`
    и его тестовыми фейками (`exists`/`save`/`update_status`), которые не
    реализуют чтение страниц — добавление `list_vacancies`/`count_vacancies`
    туда потребовало бы бессмысленно реализовывать их в каждом write-фейке
    pipeline. `SqliteVacancyRepository` реализует оба протокола одновременно
    (структурная типизация, явное наследование не нужно).

    Каждый вызов создаёт, использует и закрывает собственное SQLite-
    соединение в одном синхронном worker thread — `close()` обязателен в
    протоколе именно поэтому, а не потому что чтение владеет ресурсом
    дольше одной операции (см. `application/vacancy_queries.py`)."""

    def get(self, hh_id: str) -> StoredVacancy | None: ...

    def list_vacancies(self, *, limit: int, offset: int) -> list[StoredVacancy]: ...

    def count_vacancies(self) -> int: ...

    def close(self) -> None: ...


class RepositoryHealthCheck(Protocol):
    """Дешёвая проверка доступности хранилища для `GET /api/v1/ready`
    (TASK-S2.1). Отдельный минимальный протокол, не расширение
    `VacancyReadRepository`/`VacancyRepository` — по той же причине, что и
    `VacancyReadRepository`/`ApplicationDraftRepository` (ADR-028): `/health`
    (liveness) не должен требовать readiness-возможностей, а readiness не
    должен зависеть от полного read/write контракта репозитория. Оба
    backend-адаптера (`SqliteVacancyRepository`, `PostgresVacancyRepository`)
    реализуют его структурно наравне с остальными протоколами — тот же
    объект, что `RepositoryFactory`/`ApplicationDraftRepositoryFactory`
    возвращают для своих нужд.

    `ping()` бросает любое исключение адаптера (например, `sqlite3.Error`
    или `psycopg.Error`) при недоступной/повреждённой БД — HTTP-слой
    (`api/app.py`) ловит это как generic `503`, не пересказывая исключение
    клиенту (invariant TASK-S2.1: raw DSN/SQL не попадают в HTTP-ошибки)."""

    def ping(self) -> None: ...

    def close(self) -> None: ...


class TelegramNotifier(Protocol):
    """Показ карточек вакансий и приём команд пользователя. Не отвечает за поиск вакансий.

    Асинхронный протокол: `aiogram` (см. ADR-006 в `docs/DECISIONS.md`) —
    asyncio-only библиотека без синхронного API.
    """

    async def send_vacancy_card(self, vacancy: Vacancy) -> None: ...

    async def handle_callback(self, chat_id: int, payload: str) -> None: ...


class QwenClient(Protocol):
    """Локальная массовая оценка вакансии (MVP 2). Реализация —
    `integrations.qwen_client.LMStudioQwenClient`, подключена к
    `application/daily_scan.py` в TASK-007 (`docs/DECISIONS.md`, ADR-021).

    Синхронный протокол намеренно — та же причина, что у `VacancySource`:
    `assess` делает блокирующий HTTP-запрос (десятки секунд на локальной
    модели, см. калибровку TASK-006), вызывающий код оборачивает его в
    `asyncio.to_thread`, а не делает протокол асинхронным.

    `assess` бросает `domain.qwen_errors.QwenClientError` (или подкласс)
    на любую техническую ошибку — сеть, HTTP-ошибку API, невалидный/
    неструктурный ответ модели. `application/daily_scan.py` ловит этот
    домейн-уровневый тип, не заботясь о конкретной реализации клиента —
    та же причина, по которой `TelegramNotifierError` живёт в
    `domain/notifier_errors.py`, а не в `integrations/` (ADR-012).

    `model` — имя модели, фактически выполнившей оценку (например,
    `deepseek-r1-0528-qwen3-8b`, см. калибровку TASK-006, ADR-020).
    `VacancyAssessment` его не содержит — это не то, что модель сообщает
    о себе в ответе, а то, что знает вызывающая сторона (конфигурация
    клиента), нужно для сохранения вместе с оценкой (ADR-022)."""

    model: str

    def assess(self, vacancy: Vacancy) -> VacancyAssessment: ...

    def close(self) -> None: ...


class ApplicationDraftGenerator(Protocol):
    """Генерация проверяемого черновика отклика через локальный Qwen (TASK-011,
    ADR-034/ADR-035) для ОДНОЙ уже отфильтрованной и оценённой вакансии.

    Отдельный от `QwenClient` протокол, не расширение `assess`: массовая
    оценка (`QwenClient`) и генерация черновика — разные use case с разным
    промптом, output schema и стоимостью по времени (`docs/TASK-011.md`,
    «Порты и границы») — смешивание сделало бы существующие fake-клиенты
    `QwenClient` в тестах scan обязанными реализовывать генерацию черновика,
    которая им не нужна.

    С TASK-012 (ADR-052/053) `generate` возвращает `ApplicationDraftPlan`, не
    готовый `ApplicationDraftContent` — модель выбирает только цитаты
    требований вакансии и evidence facts; итоговый проверяемый контент
    строит доверенный Python-renderer (`domain/application_draft_renderer.py`)
    уже после `generate` (`application/application_drafts.py`).

    `model` — так же, как у `QwenClient`, имя модели, фактически выполнившей
    генерацию — вызывающая сторона (`application/application_drafts.py`)
    сохраняет его вместе с черновиком."""

    model: str

    def generate(
        self,
        vacancy: Vacancy,
        assessment: VacancyAssessment,
        profile: MasterProfile,
    ) -> ApplicationDraftPlan: ...

    def close(self) -> None: ...


class ApplicationDraftRepository(Protocol):
    """Чтение вакансии/черновика и сохранение одной актуальной versioned-версии
    validated-черновика на вакансию (TASK-011, ADR-034/ADR-035/ADR-042).

    Отдельный протокол, не расширение `VacancyRepository`/`VacancyReadRepository`
    — по той же причине, что и `VacancyReadRepository` (ADR-028): не заставлять
    существующие write-фейки pipeline и read-only GET реализовывать методы,
    которые им не нужны. `SqliteVacancyRepository` реализует все три протокола
    одновременно (структурная типизация).

    Как и `VacancyReadRepository`, каждый вызов создаёт, использует и
    закрывает собственное SQLite-соединение в одном синхронном worker
    thread — `application/application_drafts.py` явно закрывает repository
    до вызова `ApplicationDraftGenerator.generate` (который может занимать
    до 300 секунд) и открывает новый только для сохранения."""

    def get_vacancy(self, vacancy_id: str) -> StoredVacancy | None: ...

    def get_application_draft(self, vacancy_id: str) -> StoredApplicationDraft | None: ...

    def save_application_draft(self, stored: StoredApplicationDraft) -> bool:
        """True для insert/version-replace, False при гонке с той же версией."""
        ...

    def close(self) -> None: ...


class ScanJobQueue(Protocol):
    """Durable PostgreSQL-очередь фонового запуска scan (TASK-S2.2).

    Только для PostgreSQL (`persistence/factory.create_scan_job_queue`
    отклоняет SQLite явным `ValueError`, который HTTP-слой превращает в
    безопасный `503`, — «Queue API доступен только с PostgreSQL» из
    `docs/TASK-S2.2.md`) — в отличие от `VacancyRepository`/
    `RepositoryHealthCheck`, у этого протокола нет SQLite-реализации, и
    заводить её не планируется.

    Как и `VacancyReadRepository`, каждый вызывающий код (HTTP route,
    worker) создаёт, использует и закрывает собственный экземпляр —
    `close()` обязателен по той же причине (одно соединение на одну
    операцию/попытку)."""

    def enqueue(self, idempotency_key: str) -> tuple[ScanJob, bool]:
        """Создаёт job или возвращает существующую по тому же
        `idempotency_key`. Второй элемент — `True`, если job уже
        существовала (переиспользована), `False` — если создана только что."""
        ...

    def claim_one(self, *, now: datetime, lease_seconds: int) -> ScanJob | None:
        """Атомарно (`SELECT ... FOR UPDATE SKIP LOCKED`) забирает одну
        доступную job: `queued`/`retry_wait` с наступившим `available_at`,
        либо `running` с истёкшим lease (TASK-S2.2, «Истёкший lease
        восстанавливается»). Помечает её `running`, увеличивает `attempt`
        и продлевает lease до `now + lease_seconds`. `None`, если доступных
        job нет — вызывающий код (`application/scan_worker.py`) не должен
        расценивать это как ошибку.

        Истёкший lease на job, уже исчерпавшей `max_attempts` (worker упал
        во время последней разрешённой попытки, не вызвав `mark_*`) — не
        реанимируется ещё одной попыткой (review-fix TASK-S2.2 [P1]: раньше
        `attempt` рос без ограничения). Вместо этого такая job сразу
        переводится в `failed` этим же вызовом, `None` возвращается вызывающему
        коду для НЕЁ (job не выдаётся на исполнение) — следующий вызов
        `claim_one` уже не увидит её."""
        ...

    def mark_succeeded(self, job_id: UUID, *, attempt: int, result: Mapping[str, object]) -> None:
        """`attempt` — значение `ScanJob.attempt`, которое вызывающий код
        получил от `claim_one` для ЭТОЙ попытки (review-fix TASK-S2.2 [P1]).
        UPDATE применяется, только если в БД `attempt` всё ещё совпадает и
        job всё ещё `running` — иначе job уже была повторно забрана другим
        worker'ом (истёкший lease) или уже финализирована, и этот вызов
        пришёл от «зомби»-worker'а, чьё исполнение устарело: он должен молча
        не иметь эффекта, а не перезаписывать результат актуальной попытки."""
        ...

    def mark_for_retry(
        self, job_id: UUID, *, attempt: int, available_at: datetime, error_code: str
    ) -> None:
        """`attempt` — та же compare-and-swap защита от «зомби»-worker'а,
        что у `mark_succeeded` (см. его docstring)."""
        ...

    def mark_failed(self, job_id: UUID, *, attempt: int, error_code: str) -> None:
        """`attempt` — та же compare-and-swap защита от «зомби»-worker'а,
        что у `mark_succeeded` (см. его docstring)."""
        ...

    def get(self, job_id: UUID) -> ScanJob | None: ...

    def close(self) -> None: ...


class VacancyRepositoryBackend(
    VacancyRepository, VacancyReadRepository, ApplicationDraftRepository, RepositoryHealthCheck,
    Protocol,
):
    """Комбинированный протокол для persistence-backend-адаптеров
    (TASK-S2.1): `SqliteVacancyRepository` и `PostgresVacancyRepository`
    реализуют `VacancyRepository`/`VacancyReadRepository`/
    `ApplicationDraftRepository`/`RepositoryHealthCheck` одновременно на
    одном классе (структурная типизация, ADR-028/ADR-034) — этот alias
    нужен только там, где вызывающему коду (`persistence/factory.py`,
    `api/main.py`) требуется единый тип «репозитория, который умеет всё»,
    а не отдельный `Union` на каждом месте использования."""
