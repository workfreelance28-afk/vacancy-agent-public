"""Выбор backend-адаптера `VacancyRepository` по схеме `DATABASE_URL`
(TASK-S2.1).

Единственное место, которое знает про оба конкретных класса
(`SqliteVacancyRepository`, `PostgresVacancyRepository`) — `api/main.py` (и
любой будущий вызывающий код) собирает `RepositoryFactory` через
`create_vacancy_repository`, не выбирая класс адаптера сам. Неизвестная
схема отклоняется здесь явно, до попытки что-либо открыть/мигрировать —
опечатка в `DATABASE_URL` (например, `postgres://` вместо `postgresql://`,
или вообще незнакомая СУБД) не должна тихо провалиться в SQLite-путь по
умолчанию или дойти до драйвера с непонятной ошибкой."""

from vacancy_agent.config.settings import DatabaseSettings
from vacancy_agent.domain.ports import ScanJobQueue, VacancyRepositoryBackend
from vacancy_agent.persistence.postgres_repository import PostgresVacancyRepository
from vacancy_agent.persistence.postgres_scan_queue import PostgresScanJobQueue
from vacancy_agent.persistence.sqlite_repository import SqliteVacancyRepository

_SQLITE_PREFIX = "sqlite:///"
_POSTGRES_PREFIX = "postgresql://"


def create_vacancy_repository(settings: DatabaseSettings) -> VacancyRepositoryBackend:
    """Создаёт и мигрирует репозиторий для `settings.url`.

    `sqlite:///...` → `SqliteVacancyRepository` (поведение не изменилось).
    `postgresql://...` → `PostgresVacancyRepository`. Любая другая схема —
    явный `ValueError` с самим `settings.url` НЕ включённым в сообщение,
    только его префикс до `://` — DSN может содержать credentials
    (invariant TASK-S2.1: raw DSN/credentials не должны попасть в логи или
    HTTP-ошибки, а исключение отсюда может всплыть в них)."""

    if settings.url.startswith(_SQLITE_PREFIX):
        return SqliteVacancyRepository.from_settings(settings)
    if settings.url.startswith(_POSTGRES_PREFIX):
        return PostgresVacancyRepository.from_settings(settings)

    scheme = settings.url.split("://", 1)[0] if "://" in settings.url else settings.url
    raise ValueError(
        f"Неизвестная схема DATABASE_URL: {scheme!r} "
        f"(поддерживаются только {_SQLITE_PREFIX!r} и {_POSTGRES_PREFIX!r})"
    )


def create_scan_job_queue(settings: DatabaseSettings) -> ScanJobQueue:
    """Создаёт durable-очередь фонового scan (TASK-S2.2). В отличие от
    `create_vacancy_repository`, у очереди нет SQLite-варианта — «Queue API
    доступен только с PostgreSQL» (`docs/TASK-S2.2.md`, «Архитектурная
    граница»): `sqlite:///...` и любая другая схема отклоняются одним и тем
    же явным `ValueError` без утечки credentials (сообщение содержит только
    схему до `://`, как и `create_vacancy_repository`). Вызывающий HTTP-код
    (`api/app.py`) превращает это в безопасный `503`, не останавливая
    остальные routes."""

    if settings.url.startswith(_POSTGRES_PREFIX):
        return PostgresScanJobQueue.from_settings(settings)

    scheme = settings.url.split("://", 1)[0] if "://" in settings.url else settings.url
    raise ValueError(
        f"Очередь сканирования доступна только для {_POSTGRES_PREFIX!r}, "
        f"текущая схема DATABASE_URL: {scheme!r}"
    )
