"""Минимальный integration-тест TASK-008 (`docs/DECISIONS.md`, ADR-025):
`application/daily_scan.run_daily_scan` вместе с настоящим
`SqliteVacancyRepository` (временный файл), а не с фейком-заглушкой
репозитория, как в `tests/unit/test_daily_scan.py`. Источник вакансий,
Qwen и Telegram остаются фейковыми портами — их собственное поведение
уже покрыто соответствующими unit-тестами; здесь важно только то, что
`application`-слой и настоящая persistence-реализация работают вместе
корректно (сохраняют/читают ту же оценку, которую вычислил сценарий), а
не то, что каждый компонент верен по отдельности."""

from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

from vacancy_agent.application.daily_scan import run_daily_scan
from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.domain.filters import FilterCriteria
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.persistence.sqlite_repository import SqliteVacancyRepository

_NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
_MIN_SCORE = 70


def _vacancy(**overrides: object) -> Vacancy:
    fields: dict[str, object] = {
        "hh_id": "superjob:1",
        "title": "AI-интегратор",
        "url": "https://superjob.ru/vakansii/1.html",
        "published_at": _NOW,
        "fetched_at": _NOW,
    }
    fields.update(overrides)
    return Vacancy(**fields)


class _FakeVacancyClient:
    def __init__(self, vacancies: Iterable[Vacancy]) -> None:
        self._vacancies = list(vacancies)

    def search_vacancies(self, params: Mapping[str, str]) -> Iterator[Vacancy]:
        return iter(self._vacancies)

    def close(self) -> None:
        pass


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[Vacancy] = []

    async def send_vacancy_card(self, vacancy: Vacancy) -> None:
        self.sent.append(vacancy)

    async def handle_callback(self, chat_id: int, payload: str) -> None:
        raise NotImplementedError


class _FakeQwenClient:
    def __init__(self, assessment: VacancyAssessment) -> None:
        self._assessment = assessment
        self.model = "fake-integration-model"

    def assess(self, vacancy: Vacancy) -> VacancyAssessment:
        return self._assessment

    def close(self) -> None:
        pass


async def test_accepted_vacancy_is_persisted_with_qwen_assessment_in_real_sqlite(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "integration.db")
    repository = SqliteVacancyRepository(db_path)
    vacancy_client = _FakeVacancyClient([_vacancy()])
    notifier = _FakeNotifier()
    assessment = VacancyAssessment(
        schema_version="1",
        score=85,
        relevant=True,
        recommendation="strong_match",
    )
    qwen_client = _FakeQwenClient(assessment)
    criteria = FilterCriteria(target_titles=["AI-интегратор"])

    try:
        summary = await run_daily_scan(
            vacancy_client,
            repository,
            notifier,
            qwen_client,
            criteria,
            {},
            qwen_min_score=_MIN_SCORE,
            now=_NOW,
        )

        assert summary.notified == 1
        assert [v.hh_id for v in notifier.sent] == ["superjob:1"]

        stored = repository.get("superjob:1")
        assert stored is not None
        assert stored.status == VacancyStatus.NEW
        assert stored.assessment == assessment
        assert stored.qwen_model == "fake-integration-model"
        assert stored.qwen_min_score == _MIN_SCORE
        assert stored.qwen_passed is True

        # Второй прогон с тем же источником не должен повторно отправлять
        # уже известную (дедуплицированную) вакансию — реальная SQLite, не фейк.
        notifier.sent.clear()
        second_summary = await run_daily_scan(
            vacancy_client,
            repository,
            notifier,
            qwen_client,
            criteria,
            {},
            qwen_min_score=_MIN_SCORE,
            now=_NOW,
        )

        assert second_summary.already_known == 1
        assert notifier.sent == []
    finally:
        repository.close()


async def test_vacancy_below_qwen_threshold_is_persisted_but_not_notified(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "integration_below_threshold.db")
    repository = SqliteVacancyRepository(db_path)
    vacancy_client = _FakeVacancyClient([_vacancy(hh_id="superjob:2")])
    notifier = _FakeNotifier()
    assessment = VacancyAssessment(
        schema_version="1", score=40, relevant=True, recommendation="skip"
    )
    qwen_client = _FakeQwenClient(assessment)
    criteria = FilterCriteria(target_titles=["AI-интегратор"])

    try:
        summary = await run_daily_scan(
            vacancy_client,
            repository,
            notifier,
            qwen_client,
            criteria,
            {},
            qwen_min_score=_MIN_SCORE,
            now=_NOW,
        )

        assert summary.qwen_rejected == 1
        assert notifier.sent == []

        stored = repository.get("superjob:2")
        assert stored is not None
        assert stored.assessment is not None
        assert stored.assessment.score == 40
        assert stored.qwen_passed is False
    finally:
        repository.close()
