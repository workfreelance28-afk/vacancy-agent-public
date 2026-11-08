"""Read-сценарии для `GET /api/v1/vacancies` и `GET /api/v1/vacancies/{id}`
(TASK-009, ADR-028/ADR-029) — application-слой, не знает про FastAPI/HTTP.

Каждая функция принимает фабрику репозитория, а не готовый репозиторий:
`sqlite3`-соединение привязано к потоку и не должно жить дольше одной
операции (см. `docs/TASK-009.md`, «SQLite и синхронные routes») — фабрика
создаёт репозиторий, функция использует его и закрывает в `finally`,
целиком внутри одного вызова (в реальном приложении — одного worker
thread FastAPI-route). Так же, как `application/wiring.run_scan_once`
не создаёт `RuntimeComponents` сама, эти функции не создают репозиторий
напрямую — вызывающая сторона (HTTP-слой) решает, как именно его собрать.
"""

from collections.abc import Callable
from typing import NamedTuple

from vacancy_agent.domain.ports import VacancyReadRepository
from vacancy_agent.domain.stored_vacancy import StoredVacancy

RepositoryFactory = Callable[[], VacancyReadRepository]


class VacancyPage(NamedTuple):
    items: list[StoredVacancy]
    total: int
    limit: int
    offset: int


def list_vacancies(
    repository_factory: RepositoryFactory, *, limit: int, offset: int
) -> VacancyPage:
    repository = repository_factory()
    try:
        items = repository.list_vacancies(limit=limit, offset=offset)
        total = repository.count_vacancies()
    finally:
        repository.close()
    return VacancyPage(items=items, total=total, limit=limit, offset=offset)


def get_vacancy(repository_factory: RepositoryFactory, vacancy_id: str) -> StoredVacancy | None:
    repository = repository_factory()
    try:
        return repository.get(vacancy_id)
    finally:
        repository.close()
