import asyncio
import time
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime

from vacancy_agent.application.daily_scan import run_daily_scan
from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.filters import FilterCriteria
from vacancy_agent.domain.notifier_errors import TelegramSendError
from vacancy_agent.domain.qwen_errors import QwenNetworkError
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.vacancy import Vacancy

_NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
_MIN_SCORE = 70


def _vacancy(**overrides: object) -> Vacancy:
    fields: dict[str, object] = {
        "hh_id": "1",
        "title": "AI-интегратор",
        "url": "https://hh.ru/vacancy/1",
        "published_at": _NOW,
        "fetched_at": _NOW,
    }
    fields.update(overrides)
    return Vacancy(**fields)


def _criteria(**overrides: object) -> FilterCriteria:
    fields: dict[str, object] = {"target_titles": ["AI-интегратор"]}
    fields.update(overrides)
    return FilterCriteria(**fields)


def _assessment(**overrides: object) -> VacancyAssessment:
    fields: dict[str, object] = {
        "schema_version": "1",
        "score": 100,
        "relevant": True,
        "recommendation": "strong_match",
    }
    fields.update(overrides)
    return VacancyAssessment(**fields)


class _FakeHHClient:
    def __init__(self, vacancies: Iterable[Vacancy]) -> None:
        self._vacancies = list(vacancies)
        self.received_params: Mapping[str, str] | None = None

    def search_vacancies(self, params: Mapping[str, str]) -> Iterator[Vacancy]:
        self.received_params = params
        return iter(self._vacancies)


class _FakeRepository:
    def __init__(self, existing_hh_ids: Iterable[str] = ()) -> None:
        self._existing = set(existing_hh_ids)
        self.saved: list[tuple[Vacancy, FilterResult, VacancyStatus]] = []
        self.saved_assessments: list[
            tuple[VacancyAssessment | None, str | None, int | None, bool | None]
        ] = []

    def exists(self, hh_id: str) -> bool:
        return hh_id in self._existing

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
    ) -> None:
        self.saved.append((vacancy, filter_result, status))
        self.saved_assessments.append((assessment, qwen_model, qwen_min_score, qwen_passed))
        self._existing.add(vacancy.hh_id)

    def update_status(self, hh_id: str, status: VacancyStatus) -> None:
        raise NotImplementedError


class _FakeNotifier:
    def __init__(self, fail_hh_ids: Iterable[str] = ()) -> None:
        self._fail_hh_ids = set(fail_hh_ids)
        self.sent: list[Vacancy] = []

    async def send_vacancy_card(self, vacancy: Vacancy) -> None:
        if vacancy.hh_id in self._fail_hh_ids:
            raise TelegramSendError("boom")
        self.sent.append(vacancy)

    async def handle_callback(self, chat_id: int, payload: str) -> None:
        raise NotImplementedError


class _FakeQwenClient:
    """По умолчанию оценивает любую вакансию как явно релевантную (score=100)
    — большинству тестов в этом файле не важна Qwen-специфика, только то,
    что она пропускает вакансию дальше по конвейеру."""

    def __init__(
        self,
        assessments: dict[str, VacancyAssessment] | None = None,
        fail_hh_ids: Iterable[str] = (),
    ) -> None:
        self._assessments = assessments or {}
        self._fail_hh_ids = set(fail_hh_ids)
        self.assessed: list[str] = []
        self.model = "fake-qwen-model"

    def assess(self, vacancy: Vacancy) -> VacancyAssessment:
        self.assessed.append(vacancy.hh_id)
        if vacancy.hh_id in self._fail_hh_ids:
            raise QwenNetworkError("boom")
        return self._assessments.get(vacancy.hh_id, _assessment())

    def close(self) -> None:
        pass


async def test_skips_already_known_vacancy() -> None:
    hh_client = _FakeHHClient([_vacancy(hh_id="1")])
    repository = _FakeRepository(existing_hh_ids=["1"])
    notifier = _FakeNotifier()
    qwen_client = _FakeQwenClient()

    summary = await run_daily_scan(
        hh_client,
        repository,
        notifier,
        qwen_client,
        _criteria(),
        {},
        qwen_min_score=_MIN_SCORE,
        now=_NOW,
    )

    assert summary.fetched == 1
    assert summary.already_known == 1
    assert repository.saved == []
    assert notifier.sent == []
    assert qwen_client.assessed == []


async def test_saves_rejected_vacancy_without_notifying() -> None:
    hh_client = _FakeHHClient([_vacancy(hh_id="1", title="Бухгалтер")])
    repository = _FakeRepository()
    notifier = _FakeNotifier()
    qwen_client = _FakeQwenClient()

    summary = await run_daily_scan(
        hh_client,
        repository,
        notifier,
        qwen_client,
        _criteria(),
        {},
        qwen_min_score=_MIN_SCORE,
        now=_NOW,
    )

    assert summary.filtered_out == 1
    assert summary.notified == 0
    assert len(repository.saved) == 1
    assert repository.saved[0][1].accepted is False
    assert notifier.sent == []
    assert qwen_client.assessed == []  # фильтр отклонил раньше, чем дошло до Qwen
    assert repository.saved_assessments[0] == (None, None, None, None)


async def test_saves_and_notifies_vacancy_accepted_by_qwen() -> None:
    hh_client = _FakeHHClient([_vacancy(hh_id="1", title="AI-интегратор")])
    repository = _FakeRepository()
    notifier = _FakeNotifier()
    qwen_client = _FakeQwenClient({"1": _assessment(score=85, relevant=True)})

    summary = await run_daily_scan(
        hh_client,
        repository,
        notifier,
        qwen_client,
        _criteria(),
        {},
        qwen_min_score=_MIN_SCORE,
        now=_NOW,
    )

    assert summary.accepted == 1
    assert summary.qwen_rejected == 0
    assert summary.qwen_failed == 0
    assert summary.notified == 1
    assert len(repository.saved) == 1
    assert repository.saved[0][1].accepted is True
    assert repository.saved[0][2] == VacancyStatus.NEW
    assert [v.hh_id for v in notifier.sent] == ["1"]
    assert qwen_client.assessed == ["1"]
    saved_assessment, saved_model, saved_min_score, saved_passed = repository.saved_assessments[0]
    assert saved_assessment is not None
    assert saved_assessment.score == 85
    assert saved_model == "fake-qwen-model"
    assert saved_min_score == _MIN_SCORE
    assert saved_passed is True


async def test_saves_vacancy_below_qwen_threshold_without_notifying() -> None:
    """Ниже порога — не ошибка, а завершённое решение: вакансия сохраняется
    (не переоценивается каждый день), но не отправляется (`docs/DECISIONS.md`,
    ADR-020/021)."""

    hh_client = _FakeHHClient([_vacancy(hh_id="1", title="AI-интегратор")])
    repository = _FakeRepository()
    notifier = _FakeNotifier()
    qwen_client = _FakeQwenClient({"1": _assessment(score=50, relevant=True)})

    summary = await run_daily_scan(
        hh_client,
        repository,
        notifier,
        qwen_client,
        _criteria(),
        {},
        qwen_min_score=_MIN_SCORE,
        now=_NOW,
    )

    assert summary.accepted == 1
    assert summary.qwen_rejected == 1
    assert summary.notified == 0
    assert len(repository.saved) == 1
    assert notifier.sent == []
    assert repository.exists("1") is True  # не будет переоцениваться на следующем прогоне
    saved_assessment, saved_model, saved_min_score, saved_passed = repository.saved_assessments[0]
    assert saved_assessment is not None  # найдено ревью: решение Qwen должно быть проверяемо
    assert saved_assessment.score == 50
    assert saved_model == "fake-qwen-model"
    assert saved_min_score == _MIN_SCORE  # действовавший порог на момент решения (ADR-022)
    assert saved_passed is False


async def test_saves_vacancy_not_relevant_per_qwen_even_with_high_score() -> None:
    hh_client = _FakeHHClient([_vacancy(hh_id="1", title="AI-интегратор")])
    repository = _FakeRepository()
    notifier = _FakeNotifier()
    qwen_client = _FakeQwenClient(
        {"1": _assessment(score=95, relevant=False, recommendation="skip")}
    )

    summary = await run_daily_scan(
        hh_client,
        repository,
        notifier,
        qwen_client,
        _criteria(),
        {},
        qwen_min_score=_MIN_SCORE,
        now=_NOW,
    )

    assert summary.qwen_rejected == 1
    assert summary.notified == 0
    assert notifier.sent == []


async def test_does_not_save_vacancy_when_qwen_assessment_fails() -> None:
    """Fail-closed и retryable (`docs/DECISIONS.md`, ADR-020/021): техническая
    ошибка Qwen — вакансия не отправляется и не сохраняется, будет оценена
    заново на следующем прогоне, а не потеряна и не пропущена молча."""

    hh_client = _FakeHHClient([_vacancy(hh_id="1", title="AI-интегратор")])
    repository = _FakeRepository()
    notifier = _FakeNotifier()
    qwen_client = _FakeQwenClient(fail_hh_ids=["1"])

    summary = await run_daily_scan(
        hh_client,
        repository,
        notifier,
        qwen_client,
        _criteria(),
        {},
        qwen_min_score=_MIN_SCORE,
        now=_NOW,
    )

    assert summary.qwen_failed == 1
    assert summary.notified == 0
    assert repository.saved == []
    assert notifier.sent == []
    assert repository.exists("1") is False  # будет оценена заново на следующем прогоне


async def test_does_not_save_vacancy_when_notification_fails() -> None:
    hh_client = _FakeHHClient([_vacancy(hh_id="1", title="AI-интегратор")])
    repository = _FakeRepository()
    notifier = _FakeNotifier(fail_hh_ids=["1"])
    qwen_client = _FakeQwenClient()

    summary = await run_daily_scan(
        hh_client,
        repository,
        notifier,
        qwen_client,
        _criteria(),
        {},
        qwen_min_score=_MIN_SCORE,
        now=_NOW,
    )

    assert summary.send_failed == 1
    assert summary.notified == 0
    assert repository.saved == []
    assert repository.exists("1") is False  # будет обработана заново на следующем прогоне


async def test_passes_search_params_through_to_hh_client() -> None:
    hh_client = _FakeHHClient([])
    repository = _FakeRepository()
    notifier = _FakeNotifier()
    qwen_client = _FakeQwenClient()
    params = {"text": "AI", "period": "7"}

    await run_daily_scan(
        hh_client,
        repository,
        notifier,
        qwen_client,
        _criteria(),
        params,
        qwen_min_score=_MIN_SCORE,
        now=_NOW,
    )

    assert hh_client.received_params == params


async def test_summary_counts_across_multiple_vacancies() -> None:
    hh_client = _FakeHHClient(
        [
            _vacancy(hh_id="1", title="AI-интегратор"),  # accepted + notified
            _vacancy(hh_id="2", title="Бухгалтер"),  # filtered out
            _vacancy(hh_id="3", title="AI-интегратор"),  # already known
            _vacancy(hh_id="4", title="AI-интегратор"),  # accepted, qwen below threshold
            _vacancy(hh_id="5", title="AI-интегратор"),  # accepted, qwen technical error
            _vacancy(hh_id="6", title="AI-интегратор"),  # accepted, qwen ok, send fails
        ]
    )
    repository = _FakeRepository(existing_hh_ids=["3"])
    notifier = _FakeNotifier(fail_hh_ids=["6"])
    qwen_client = _FakeQwenClient(
        assessments={"4": _assessment(score=10, relevant=True)},
        fail_hh_ids=["5"],
    )

    summary = await run_daily_scan(
        hh_client,
        repository,
        notifier,
        qwen_client,
        _criteria(),
        {},
        qwen_min_score=_MIN_SCORE,
        now=_NOW,
    )

    assert summary.fetched == 6
    assert summary.already_known == 1
    assert summary.filtered_out == 1
    assert summary.accepted == 4
    assert summary.qwen_rejected == 1
    assert summary.qwen_failed == 1
    assert summary.notified == 1
    assert summary.send_failed == 1


class _BlockingHHClient:
    """Синхронный `time.sleep` внутри `search_vacancies` — как реальный
    `HttpxHHClient`, делающий блокирующий HTTP-запрос."""

    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds

    def search_vacancies(self, params: Mapping[str, str]) -> Iterator[Vacancy]:
        time.sleep(self._delay_seconds)
        return iter(())


async def test_hh_fetch_does_not_block_the_event_loop() -> None:
    """`run_daily_scan` делит event loop с Telegram long polling в реальном
    процессе (`scheduler.py`) — блокирующий вызов `search_vacancies` не
    должен останавливать обработку других сопрограмм (найдено ревью
    TASK-005, см. `docs/DECISIONS.md`, ADR-012). Если фикс снят (прямой
    вызов синхронного генератора внутри `async def`), `other_task`
    (нужно 0.05с event-loop-времени) не сможет выполниться, пока
    `time.sleep(0.2)` не отпустит поток — сценарий завершится первым."""

    events: list[str] = []

    async def other_task() -> None:
        await asyncio.sleep(0.05)
        events.append("other_task_done")

    async def scan_task() -> None:
        await run_daily_scan(
            _BlockingHHClient(0.2),
            _FakeRepository(),
            _FakeNotifier(),
            _FakeQwenClient(),
            _criteria(),
            {},
            qwen_min_score=_MIN_SCORE,
            now=_NOW,
        )
        events.append("scan_done")

    await asyncio.gather(other_task(), scan_task())

    assert events == ["other_task_done", "scan_done"]


class _BlockingQwenClient:
    """Синхронный `time.sleep` внутри `assess` — как реальный
    `LMStudioQwenClient`, делающий блокирующий HTTP-запрос (десятки секунд
    на локальной модели, см. калибровку TASK-006)."""

    def __init__(self, delay_seconds: float) -> None:
        self._delay_seconds = delay_seconds
        self.model = "fake-qwen-model"

    def assess(self, vacancy: Vacancy) -> VacancyAssessment:
        time.sleep(self._delay_seconds)
        return _assessment()

    def close(self) -> None:
        pass


async def test_qwen_assessment_does_not_block_the_event_loop() -> None:
    """Тот же класс бага, что и для HH-выборки (см. предыдущий тест), но для
    `QwenClient.assess` — на реальной локальной модели один вызов занимает
    десятки секунд (калибровка TASK-006); блокирующий вызов внутри
    `async def` заморозил бы Telegram long polling на всё это время."""

    events: list[str] = []

    async def other_task() -> None:
        await asyncio.sleep(0.05)
        events.append("other_task_done")

    async def scan_task() -> None:
        hh_client = _FakeHHClient([_vacancy(hh_id="1", title="AI-интегратор")])
        await run_daily_scan(
            hh_client,
            _FakeRepository(),
            _FakeNotifier(),
            _BlockingQwenClient(0.2),
            _criteria(),
            {},
            qwen_min_score=_MIN_SCORE,
            now=_NOW,
        )
        events.append("scan_done")

    await asyncio.gather(other_task(), scan_task())

    assert events == ["other_task_done", "scan_done"]
