from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime

import pytest

from vacancy_agent.application.wiring import (
    _build_source_specs,
    _close_all,
    _open_source_runs,
    _run_source_scans,
    _SourceRun,
    _SourceSpec,
    build_filter_criteria,
    build_habr_search_params,
    build_hh_search_params,
    build_superjob_search_params,
)
from vacancy_agent.config.settings import (
    FilterSettings,
    HabrCareerSettings,
    HHSettings,
    Settings,
    SuperJobSettings,
)
from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.domain.default_filter_profile import DEFAULT_FILTER_CRITERIA
from vacancy_agent.domain.filters import FilterCriteria
from vacancy_agent.domain.repository_errors import PersistenceError
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.domain.vacancy_source_errors import VacancySourceError
from vacancy_agent.integrations.habr_client import HabrCareerSource
from vacancy_agent.integrations.habr_errors import HabrCareerNetworkError
from vacancy_agent.integrations.hh_client import HttpxHHClient
from vacancy_agent.integrations.superjob_client import SuperJobClient
from vacancy_agent.integrations.superjob_errors import SuperJobNetworkError


class _RecordingResource:
    """Ресурс с `close()`, который фиксирует, что его вызвали, и может
    имитировать сбой закрытия — для проверки `_close_all`."""

    def __init__(self, name: str, calls: list[str], *, fails: bool = False) -> None:
        self._name = name
        self._calls = calls
        self._fails = fails

    def close(self) -> None:
        self._calls.append(self._name)
        if self._fails:
            raise RuntimeError(f"{self._name} close failed")


def test_close_all_closes_every_resource_even_if_one_raises() -> None:
    """Найдено ревью TASK-008 (ADR-025): падение `close()` одного ресурса
    раньше останавливало цикл — остальные оставались открытыми (был баг и
    в unwind-ветке `RuntimeComponents.__init__`, и в `close()`, откуда
    ненадёжно закрывался `bot.session` в `main.py`)."""

    calls: list[str] = []
    resources = [
        _RecordingResource("repository", calls, fails=True),
        _RecordingResource("vacancy_client", calls),
        _RecordingResource("qwen_client", calls),
    ]

    _close_all(resources)

    assert calls == ["repository", "vacancy_client", "qwen_client"]


def test_close_all_does_not_raise_on_failure() -> None:
    calls: list[str] = []
    resources = [_RecordingResource("only", calls, fails=True)]

    _close_all(resources)

    assert calls == ["only"]


def test_build_filter_criteria_falls_back_to_defaults_when_settings_empty() -> None:
    criteria = build_filter_criteria(FilterSettings())

    assert criteria == DEFAULT_FILTER_CRITERIA


def test_build_filter_criteria_overrides_only_explicitly_set_fields() -> None:
    settings = FilterSettings(salary_min=250_000)

    criteria = build_filter_criteria(settings)

    assert criteria.salary_min == 250_000
    assert criteria.target_titles == DEFAULT_FILTER_CRITERIA.target_titles
    assert criteria.include_phrases == DEFAULT_FILTER_CRITERIA.include_phrases


def test_build_filter_criteria_overrides_phrase_lists() -> None:
    settings = FilterSettings(target_titles=["Custom Title"])

    criteria = build_filter_criteria(settings)

    assert criteria.target_titles == ["Custom Title"]


def test_build_filter_criteria_respects_explicit_allow_missing_salary_false() -> None:
    settings = FilterSettings(allow_missing_salary=False)

    criteria = build_filter_criteria(settings)

    assert criteria.allow_missing_salary is False


def test_build_hh_search_params_includes_text_by_default() -> None:
    params = build_hh_search_params(HHSettings())

    assert "text" in params
    assert params["text"] == HHSettings().search_text


def test_build_hh_search_params_includes_period_when_set() -> None:
    params = build_hh_search_params(HHSettings(period_days=14))

    assert params["period"] == "14"


def test_build_hh_search_params_omits_area_when_unset() -> None:
    params = build_hh_search_params(HHSettings(area=None))

    assert "area" not in params


def test_build_hh_search_params_includes_area_when_set() -> None:
    params = build_hh_search_params(HHSettings(area="1"))

    assert params["area"] == "1"


def test_build_superjob_search_params_uses_official_keywords_or_syntax() -> None:
    """`keywords[0][keys]`/`srws`/`skwc` — официальный синтаксис SuperJob
    (api.superjob.ru), не самодельный comma-joined `keyword=...` (найдено
    ревью, см. `docs/DECISIONS.md`, ADR-016; синтаксис подтверждён живым
    запросом — `200` и реальная фильтрация результатов)."""

    settings = SuperJobSettings(keywords=["LLM", "AI engineer"])

    params = build_superjob_search_params(settings)

    assert params["keywords[0][keys]"] == "LLM, AI engineer"
    assert params["keywords[0][srws]"] == "10"
    assert params["keywords[0][skwc]"] == "or"


def test_build_superjob_search_params_omits_keywords_block_when_empty() -> None:
    params = build_superjob_search_params(SuperJobSettings(keywords=[]))

    assert "keywords[0][keys]" not in params
    assert "keywords[0][srws]" not in params
    assert "keywords[0][skwc]" not in params


def test_build_superjob_search_params_omits_town_when_unset() -> None:
    params = build_superjob_search_params(SuperJobSettings(town=None))

    assert "town" not in params


def test_build_superjob_search_params_includes_town_when_set() -> None:
    params = build_superjob_search_params(SuperJobSettings(town="Москва"))

    assert params["town"] == "Москва"


def test_build_habr_search_params_passes_query_through() -> None:
    params = build_habr_search_params(HabrCareerSettings(search_query="AI интегратор"))

    assert params["q"] == "AI интегратор"


def test_build_habr_search_params_passes_through_empty_query_unchanged() -> None:
    """`build_habr_search_params` сама не отклоняет пустой запрос — это
    делает `HabrCareerSource.search_vacancies` лениво, только когда `habr`
    реально выбран (TASK-013) — эта функция вызывается и когда
    `VACANCY_SOURCE` — не `habr` вовсе."""

    params = build_habr_search_params(HabrCareerSettings(search_query=""))

    assert params["q"] == ""


# --- _build_source_specs: source selection (TASK-013/TASK-015) ---


def test_build_source_specs_defaults_to_single_superjob() -> None:
    settings = Settings()

    specs = _build_source_specs(settings)

    assert [spec.name for spec in specs] == ["superjob"]
    client = specs[0].build_client()
    client.close()
    assert isinstance(client, SuperJobClient)
    assert "keywords[0][keys]" in specs[0].search_params


def test_build_source_specs_selects_single_habr_when_configured() -> None:
    settings = Settings(vacancy_source="habr", habr=HabrCareerSettings(search_query="LLM"))

    specs = _build_source_specs(settings)

    assert [spec.name for spec in specs] == ["habr"]
    client = specs[0].build_client()
    client.close()
    assert isinstance(client, HabrCareerSource)
    assert specs[0].search_params == {"q": "LLM"}


def test_build_source_specs_selects_single_hh_when_configured() -> None:
    settings = Settings(vacancy_source="hh")

    specs = _build_source_specs(settings)

    assert [spec.name for spec in specs] == ["hh"]
    client = specs[0].build_client()
    client.close()
    assert isinstance(client, HttpxHHClient)


def test_build_source_specs_combined_returns_superjob_then_habr_in_fixed_order() -> None:
    """TASK-015: «В combined-режиме порядок фиксирован: SuperJob, затем Habr»."""

    settings = Settings(
        vacancy_source="superjob_habr", habr=HabrCareerSettings(search_query="AI")
    )

    specs = _build_source_specs(settings)

    assert [spec.name for spec in specs] == ["superjob", "habr"]
    superjob_client = specs[0].build_client()
    habr_client = specs[1].build_client()
    try:
        assert isinstance(superjob_client, SuperJobClient)
        assert isinstance(habr_client, HabrCareerSource)
        assert "keywords[0][keys]" in specs[0].search_params
        assert specs[1].search_params == {"q": "AI"}
    finally:
        superjob_client.close()
        habr_client.close()


# --- _open_source_runs: partial construction failure closes earlier clients ---


class _RecordingSourceClient:
    """Минимальный `VacancySource`-совместимый fake — используется здесь
    только чтобы доказать, что `_open_source_runs` регистрирует его в
    `opened` СРАЗУ после конструктора, до попытки построить следующий."""

    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls

    def search_vacancies(self, params: Mapping[str, str]) -> Iterator[Vacancy]:
        raise NotImplementedError

    def close(self) -> None:
        self._calls.append(self._name)


def test_open_source_runs_builds_every_client_in_order() -> None:
    calls: list[str] = []
    specs = [
        _SourceSpec("a", lambda: _RecordingSourceClient("a", calls), {}),
        _SourceSpec("b", lambda: _RecordingSourceClient("b", calls), {}),
    ]
    opened: list[object] = []

    source_runs = _open_source_runs(specs, opened)

    assert [run.name for run in source_runs] == ["a", "b"]
    assert opened == [run.client for run in source_runs]


def test_open_source_runs_registers_earlier_client_before_later_constructor_fails() -> None:
    """TASK-015: «все source-клиенты закрываются... при partial construction
    failure» — если конструктор ВТОРОГО источника падает, уже построенный
    ПЕРВЫЙ обязан быть в `opened`, чтобы вызывающий код (`RuntimeComponents.
    __init__`) его закрыл, не оставив висеть открытое HTTP-соединение."""

    calls: list[str] = []

    def _boom() -> _RecordingSourceClient:
        raise RuntimeError("second source constructor boom")

    specs = [
        _SourceSpec("a", lambda: _RecordingSourceClient("a", calls), {}),
        _SourceSpec("b", _boom, {}),
    ]
    opened: list[object] = []

    with pytest.raises(RuntimeError, match="second source constructor boom"):
        _open_source_runs(specs, opened)

    assert len(opened) == 1
    assert isinstance(opened[0], _RecordingSourceClient)
    assert opened[0]._name == "a"


# --- _run_source_scans: TASK-015 combined orchestration ---


def _vacancy(**overrides: object) -> Vacancy:
    fields: dict[str, object] = {
        "hh_id": "1",
        "title": "AI-интегратор",
        "url": "https://example.test/1",
        "published_at": datetime(2026, 8, 30, tzinfo=UTC),
        "fetched_at": datetime(2026, 8, 30, tzinfo=UTC),
    }
    fields.update(overrides)
    return Vacancy(**fields)


def _criteria() -> FilterCriteria:
    return FilterCriteria(target_titles=["AI-интегратор"])


class _FakeSourceClient:
    """`VacancySource`-совместимый fake для `_run_source_scans` — не
    делает сетевых вызовов, либо возвращает заданный список вакансий, либо
    поднимает заданную ошибку прямо из `search_vacancies` (тот же путь,
    которым реальный `HabrCareerSource`/`SuperJobClient` поднимает
    `VacancySourceError`-наследников)."""

    def __init__(
        self, vacancies: Iterable[Vacancy] = (), *, fail_with: Exception | None = None
    ) -> None:
        self._vacancies = list(vacancies)
        self._fail_with = fail_with
        self.received_params: Mapping[str, str] | None = None
        self.call_count = 0

    def search_vacancies(self, params: Mapping[str, str]) -> Iterator[Vacancy]:
        self.call_count += 1
        self.received_params = params
        if self._fail_with is not None:
            raise self._fail_with
        return iter(self._vacancies)

    def close(self) -> None:
        pass


class _FakeRepository:
    def __init__(self) -> None:
        self.existing: set[str] = set()
        self.saved: list[str] = []

    def exists(self, hh_id: str) -> bool:
        return hh_id in self.existing

    def save(
        self, vacancy: Vacancy, filter_result: object, status: object, **kwargs: object
    ) -> None:
        self.saved.append(vacancy.hh_id)
        self.existing.add(vacancy.hh_id)

    def update_status(self, hh_id: str, status: object) -> None:
        raise NotImplementedError


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_vacancy_card(self, vacancy: Vacancy) -> None:
        self.sent.append(vacancy.hh_id)

    async def handle_callback(self, chat_id: int, payload: str) -> None:
        raise NotImplementedError


class _FakeQwenClient:
    def __init__(self) -> None:
        self.model = "fake-model"

    def assess(self, vacancy: Vacancy) -> VacancyAssessment:
        return VacancyAssessment(
            schema_version="1", score=100, relevant=True, recommendation="strong_match"
        )

    def close(self) -> None:
        pass


async def _scan(source_runs: list[_SourceRun], *, qwen_min_score: int = 70) -> object:
    return await _run_source_scans(
        source_runs,
        _FakeRepository(),
        _FakeNotifier(),
        _FakeQwenClient(),
        _criteria(),
        qwen_min_score=qwen_min_score,
    )


async def test_run_source_scans_calls_each_source_once_with_its_own_params() -> None:
    superjob = _FakeSourceClient([_vacancy(hh_id="superjob:1")])
    habr = _FakeSourceClient([_vacancy(hh_id="habr:1")])
    source_runs = [
        _SourceRun("superjob", superjob, {"keywords[0][keys]": "AI"}),
        _SourceRun("habr", habr, {"q": "AI"}),
    ]

    await _run_source_scans(
        source_runs,
        _FakeRepository(),
        _FakeNotifier(),
        _FakeQwenClient(),
        _criteria(),
        qwen_min_score=70,
    )

    assert superjob.call_count == 1
    assert habr.call_count == 1
    assert superjob.received_params == {"keywords[0][keys]": "AI"}
    assert habr.received_params == {"q": "AI"}


async def test_run_source_scans_saves_results_of_both_sources_in_one_repository() -> None:
    repository = _FakeRepository()
    source_runs = [
        _SourceRun("superjob", _FakeSourceClient([_vacancy(hh_id="superjob:1")]), {}),
        _SourceRun("habr", _FakeSourceClient([_vacancy(hh_id="habr:1")]), {}),
    ]

    await _run_source_scans(
        source_runs, repository, _FakeNotifier(), _FakeQwenClient(), _criteria(), qwen_min_score=70
    )

    assert sorted(repository.saved) == ["habr:1", "superjob:1"]


async def test_run_source_scans_aggregates_total_and_per_source_counts() -> None:
    source_runs = [
        _SourceRun(
            "superjob",
            _FakeSourceClient([_vacancy(hh_id="superjob:1"), _vacancy(hh_id="superjob:2")]),
            {},
        ),
        _SourceRun("habr", _FakeSourceClient([_vacancy(hh_id="habr:1")]), {}),
    ]

    summary = await _scan(source_runs)

    assert summary.fetched == 3
    assert summary.notified == 3
    assert summary.per_source["superjob"].fetched == 2
    assert summary.per_source["superjob"].notified == 2
    assert summary.per_source["habr"].fetched == 1
    assert summary.per_source["habr"].notified == 1
    assert summary.failed_sources == ()


async def test_run_source_scans_first_source_failure_does_not_block_second() -> None:
    habr = _FakeSourceClient([_vacancy(hh_id="habr:1")])
    source_runs = [
        _SourceRun(
            "superjob", _FakeSourceClient(fail_with=SuperJobNetworkError("boom")), {}
        ),
        _SourceRun("habr", habr, {}),
    ]

    summary = await _scan(source_runs)

    assert habr.call_count == 1
    assert summary.failed_sources == ("superjob",)
    assert summary.notified == 1
    assert "superjob" not in summary.per_source
    assert summary.per_source["habr"].notified == 1


async def test_run_source_scans_second_source_failure_keeps_first_results() -> None:
    source_runs = [
        _SourceRun("superjob", _FakeSourceClient([_vacancy(hh_id="superjob:1")]), {}),
        _SourceRun("habr", _FakeSourceClient(fail_with=HabrCareerNetworkError("boom")), {}),
    ]

    summary = await _scan(source_runs)

    assert summary.failed_sources == ("habr",)
    assert summary.per_source["superjob"].notified == 1
    assert "habr" not in summary.per_source
    assert summary.notified == 1


async def test_run_source_scans_vacancy_source_error_is_not_leaked_in_summary_text() -> None:
    """Спецификация: «текст исключения не помещается в структурированный
    summary» — только имя источника попадает в `failed_sources`."""

    secret_marker = "PRIVATE_TRACEBACK_TEXT_MUST_NOT_LEAK"
    source_runs = [
        _SourceRun(
            "habr", _FakeSourceClient(fail_with=HabrCareerNetworkError(secret_marker)), {}
        )
    ]

    summary = await _scan(source_runs)

    assert summary.failed_sources == ("habr",)
    assert secret_marker not in summary.model_dump_json()


async def test_run_source_scans_does_not_swallow_unexpected_exception() -> None:
    """Спецификация: «неожиданные ошибки... нельзя скрывать как сбой
    источника» — `PersistenceError` (SQLite) не наследует
    `VacancySourceError` и обязана остановить прогон целиком."""

    assert not issubclass(PersistenceError, VacancySourceError)

    source_runs = [
        _SourceRun("superjob", _FakeSourceClient(fail_with=PersistenceError("db boom")), {})
    ]

    with pytest.raises(PersistenceError, match="db boom"):
        await _run_source_scans(
            source_runs,
            _FakeRepository(),
            _FakeNotifier(),
            _FakeQwenClient(),
            _criteria(),
            qwen_min_score=70,
        )


async def test_run_source_scans_single_source_mode_uses_the_same_contract() -> None:
    """TASK-015: «Обычный режим с одним источником также использует этот
    контракт» — один элемент в `source_runs` даёт `per_source` с одним
    ключом и пустой `failed_sources`, не отдельную форму результата."""

    source_runs = [_SourceRun("superjob", _FakeSourceClient([_vacancy()]), {})]

    summary = await _scan(source_runs)

    assert set(summary.per_source) == {"superjob"}
    assert summary.failed_sources == ()
    assert summary.fetched == summary.per_source["superjob"].fetched


async def test_run_source_scans_repeated_run_does_not_duplicate_or_renotify() -> None:
    """TASK-015: «повторный combined-прогон не создаёт дублей и
    уведомлений» — тот же `repository`/`notifier` между двумя вызовами,
    как и было бы у одного `RuntimeComponents` на два последовательных
    `vacancy-agent scan`."""

    repository = _FakeRepository()
    notifier = _FakeNotifier()
    qwen_client = _FakeQwenClient()
    criteria = _criteria()

    first_run = [
        _SourceRun("superjob", _FakeSourceClient([_vacancy(hh_id="superjob:1")]), {}),
        _SourceRun("habr", _FakeSourceClient([_vacancy(hh_id="habr:1")]), {}),
    ]
    first = await _run_source_scans(
        first_run, repository, notifier, qwen_client, criteria, qwen_min_score=70
    )

    second_run = [
        _SourceRun("superjob", _FakeSourceClient([_vacancy(hh_id="superjob:1")]), {}),
        _SourceRun("habr", _FakeSourceClient([_vacancy(hh_id="habr:1")]), {}),
    ]
    second = await _run_source_scans(
        second_run, repository, notifier, qwen_client, criteria, qwen_min_score=70
    )

    assert first.notified == 2
    assert second.notified == 0
    assert second.already_known == 2
    assert sorted(repository.saved) == ["habr:1", "superjob:1"]
    assert sorted(notifier.sent) == ["habr:1", "superjob:1"]
