from enum import StrEnum


class VacancyStatus(StrEnum):
    """Полный набор статусов из `docs/DATA_CONTRACTS.md`.

    MVP 1 использует только NEW, VIEWED и SKIPPED. Остальные значения
    зарезервированы контрактом для MVP 3-4 и пока нигде не присваиваются.
    Полная таблица допустимых переходов утверждается в MVP 4.
    """

    NEW = "new"
    VIEWED = "viewed"
    SKIPPED = "skipped"
    PREPARING = "preparing"
    APPLIED = "applied"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"


MVP1_STATUSES: frozenset[VacancyStatus] = frozenset(
    {VacancyStatus.NEW, VacancyStatus.VIEWED, VacancyStatus.SKIPPED}
)
