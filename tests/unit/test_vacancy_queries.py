from datetime import UTC, datetime

import pytest

from vacancy_agent.application.vacancy_queries import get_vacancy, list_vacancies
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.stored_vacancy import StoredVacancy
from vacancy_agent.domain.vacancy import Vacancy

_NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


def _stored(hh_id: str) -> StoredVacancy:
    vacancy = Vacancy(
        hh_id=hh_id,
        title="AI-интегратор",
        url=f"https://example.test/vacancy/{hh_id}",
        published_at=_NOW,
        fetched_at=_NOW,
    )
    return StoredVacancy(
        vacancy=vacancy,
        filter_result=FilterResult(accepted=True),
        status=VacancyStatus.NEW,
        assessment=None,
        qwen_model=None,
        qwen_min_score=None,
        qwen_passed=None,
    )


class _FakeReadRepository:
    """Фейк `VacancyReadRepository` — фиксирует, что `close()` вызван ровно
    один раз за операцию (TASK-009: репозиторий не должен жить дольше
    одной query-операции)."""

    def __init__(self, items: list[StoredVacancy], *, fail_on: str | None = None) -> None:
        self._items = items
        self._fail_on = fail_on
        self.closed = False

    def get(self, hh_id: str) -> StoredVacancy | None:
        if self._fail_on == "get":
            raise RuntimeError("boom")
        for item in self._items:
            if item.vacancy.hh_id == hh_id:
                return item
        return None

    def list_vacancies(self, *, limit: int, offset: int) -> list[StoredVacancy]:
        if self._fail_on == "list":
            raise RuntimeError("boom")
        return self._items[offset : offset + limit]

    def count_vacancies(self) -> int:
        return len(self._items)

    def close(self) -> None:
        self.closed = True


def test_list_vacancies_returns_page_with_total() -> None:
    items = [_stored("1"), _stored("2"), _stored("3")]
    repository = _FakeReadRepository(items)

    page = list_vacancies(lambda: repository, limit=2, offset=0)

    assert [item.vacancy.hh_id for item in page.items] == ["1", "2"]
    assert page.total == 3
    assert page.limit == 2
    assert page.offset == 0


def test_list_vacancies_closes_repository() -> None:
    repository = _FakeReadRepository([])

    list_vacancies(lambda: repository, limit=20, offset=0)

    assert repository.closed is True


def test_list_vacancies_closes_repository_even_if_list_raises() -> None:
    repository = _FakeReadRepository([], fail_on="list")

    with pytest.raises(RuntimeError):
        list_vacancies(lambda: repository, limit=20, offset=0)

    assert repository.closed is True


def test_get_vacancy_returns_none_for_unknown_id() -> None:
    repository = _FakeReadRepository([_stored("1")])

    assert get_vacancy(lambda: repository, "unknown") is None


def test_get_vacancy_returns_stored_vacancy_by_id() -> None:
    stored = _stored("42")
    repository = _FakeReadRepository([stored])

    result = get_vacancy(lambda: repository, "42")

    assert result == stored


def test_get_vacancy_closes_repository_even_if_get_raises() -> None:
    repository = _FakeReadRepository([], fail_on="get")

    with pytest.raises(RuntimeError):
        get_vacancy(lambda: repository, "1")

    assert repository.closed is True
