"""Общие правила допустимых статусов и переходов MVP 1 (TASK-S2.1).

Извлечено из `persistence/sqlite_repository.py`, где эта логика жила до
появления второго backend-адаптера (`persistence/postgres_repository.py`).
Оба адаптера `VacancyRepository` обязаны использовать ровно одну реализацию
допустимых статусов и переходов между ними — раздельные копии в двух
файлах рано или поздно разошлись бы (например, один backend разрешил бы
переход, который другой отклоняет), и такой баг было бы трудно поймать
тестами одного конкретного backend'а.

Поведение и текст сообщений об ошибках не изменились по сравнению с
исходным `sqlite_repository.py` — существующие SQLite-тесты
(`tests/unit/test_sqlite_repository.py`), проверяющие эти сообщения через
`pytest.raises(..., match=...)`, продолжают проходить без изменений.
"""

from vacancy_agent.domain.status import MVP1_STATUSES, VacancyStatus


def require_mvp1_status(status: VacancyStatus) -> None:
    if status not in MVP1_STATUSES:
        allowed = ", ".join(s.value for s in MVP1_STATUSES)
        raise ValueError(
            f"Статус {status.value!r} ещё не используется в MVP 1 (допустимы: {allowed})"
        )


# Допустимые переходы внутри MVP 1. Полная таблица переходов утверждается
# в MVP 4 (`docs/DATA_CONTRACTS.md`) — до этого действует минимальная
# однонаправленная схема: NEW → VIEWED → SKIPPED, без отката назад.
# Переход в тот же статус — не-операция, разрешена явно (безопасный повтор).
_ALLOWED_TRANSITIONS: dict[VacancyStatus, frozenset[VacancyStatus]] = {
    VacancyStatus.NEW: frozenset(
        {VacancyStatus.NEW, VacancyStatus.VIEWED, VacancyStatus.SKIPPED}
    ),
    VacancyStatus.VIEWED: frozenset({VacancyStatus.VIEWED, VacancyStatus.SKIPPED}),
    VacancyStatus.SKIPPED: frozenset({VacancyStatus.SKIPPED}),
}


def require_allowed_transition(current: VacancyStatus, target: VacancyStatus) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ValueError(f"Переход {current.value!r} → {target.value!r} недопустим для MVP 1")
