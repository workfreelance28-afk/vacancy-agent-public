from datetime import UTC, datetime

import pytest

from vacancy_agent.application.application_drafts import (
    ApplicationDraftPreflight,
    get_application_draft,
    preflight_application_draft,
    prepare_application_draft,
)
from vacancy_agent.domain.application_draft import (
    ApplicationDraft,
    ApplicationDraftContent,
    EvidenceFact,
    EvidenceParagraph,
    MasterProfile,
    RequirementMatch,
)
from vacancy_agent.domain.application_draft_errors import (
    ApplicationDraftConcurrencyError,
    ApplicationDraftResponseError,
    EvidenceValidationError,
    RequirementQuoteError,
    VacancyNotEligibleError,
)
from vacancy_agent.domain.application_draft_plan import (
    ApplicationDraftPlan,
    RequirementEvidenceSelection,
)
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.repository_errors import VacancyNotFoundError
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.stored_application_draft import StoredApplicationDraft
from vacancy_agent.domain.stored_vacancy import StoredVacancy
from vacancy_agent.domain.vacancy import Vacancy

_NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
_DEFAULT_REQUIREMENTS_TEXT = "Требуется: опыт Python, опыт SQL, знание Docker."


def _vacancy(**overrides: object) -> Vacancy:
    fields: dict[str, object] = {
        "hh_id": "superjob:1",
        "title": "AI-интегратор",
        "requirements": _DEFAULT_REQUIREMENTS_TEXT,
        "url": "https://superjob.ru/vakansii/1.html",
        "published_at": _NOW,
        "fetched_at": _NOW,
    }
    fields.update(overrides)
    return Vacancy(**fields)


def _assessment() -> object:
    from vacancy_agent.domain.assessment import VacancyAssessment

    return VacancyAssessment(
        schema_version="1", score=90, relevant=True, recommendation="strong_match"
    )


def _stored_vacancy(**overrides: object) -> StoredVacancy:
    fields: dict[str, object] = {
        "vacancy": _vacancy(),
        "filter_result": FilterResult(accepted=True, reasons=["target_title_match"]),
        "status": VacancyStatus.NEW,
        "assessment": _assessment(),
        "qwen_model": "test-model",
        "qwen_min_score": 70,
        "qwen_passed": True,
    }
    fields.update(overrides)
    return StoredVacancy(**fields)  # type: ignore[arg-type]


def _profile() -> MasterProfile:
    return MasterProfile(
        version="test-v1",
        facts=[EvidenceFact(id="skill.python", category="skill", statement="Python.", source="t")],
    )


def _content(**overrides: object) -> ApplicationDraftContent:
    """Представляет уже сохранённую строку (существующий/`raced` draft) —
    не то, что возвращает generator (это `ApplicationDraftPlan`, см.
    `_plan` ниже, TASK-012)."""

    fields: dict[str, object] = {
        "match_summary": "Соответствие хорошее.",
        "requirements": [
            RequirementMatch(
                requirement="опыт Python",
                coverage="partial",
                explanation="Есть опыт",
                evidence_fact_ids=["skill.python"],
            )
        ],
        "resume_suggestions": [],
        "cover_letter_paragraphs": [
            EvidenceParagraph(text="Письмо.", evidence_fact_ids=["skill.python"])
        ],
        "gaps": [],
        "questions_for_user": [],
        "warnings": [],
    }
    fields.update(overrides)
    return ApplicationDraftContent(**fields)


def _plan(**overrides: object) -> ApplicationDraftPlan:
    """То, что возвращает `ApplicationDraftGenerator.generate` с TASK-012 —
    `prepare_application_draft` проверяет его (`validate_plan`) и рендерит
    в `ApplicationDraftContent` (`render_application_draft_content`)
    по-настоящему в этих тестах, не через фейк — application-service тесты
    здесь проверяют реальный сквозной путь plan → render → save."""

    fields: dict[str, object] = {
        "requirements": [
            RequirementEvidenceSelection(
                requirement_quote="опыт Python", evidence_fact_ids=["skill.python"]
            )
        ],
        "resume_fact_ids": [],
        "cover_letter_fact_ids": ["skill.python"],
    }
    fields.update(overrides)
    return ApplicationDraftPlan(**fields)


class _FakeRepository:
    """Единственный shared fake-репозиторий на весь `prepare_application_draft`:
    фабрика возвращает один и тот же объект дважды (read-фаза, save-фаза) —
    как и `SqliteVacancyRepository`, за которой стоит один и тот же файл."""

    def __init__(
        self,
        *,
        vacancy: StoredVacancy | None = None,
        existing_draft: StoredApplicationDraft | None = None,
        save_conflict_draft: StoredApplicationDraft | None = None,
        save_conflicts_remaining: int = 1,
        events: list[str] | None = None,
    ) -> None:
        self._vacancy = vacancy
        self.drafts: dict[str, StoredApplicationDraft] = {}
        if existing_draft is not None:
            self.drafts[existing_draft.draft.vacancy_id] = existing_draft
        self._save_conflict_draft = save_conflict_draft
        # TASK-011 ADR-049: по умолчанию конфликт срабатывает ровно один раз
        # — как и раньше, для тестов, ожидающих единственную проигранную
        # гонку. `_resolve_after_lost_race` может повторить попытку
        # сохранения при несовпадении модели — тесты на этот путь передают
        # больше, чтобы первая попытка проиграла, а вторая прошла.
        self._save_conflicts_remaining = (
            save_conflicts_remaining if save_conflict_draft is not None else 0
        )
        self.closed_count = 0
        self.get_application_draft_calls = 0
        self.save_calls: list[StoredApplicationDraft] = []
        self._events = events

    def get_vacancy(self, vacancy_id: str) -> StoredVacancy | None:
        return self._vacancy

    def get_application_draft(self, vacancy_id: str) -> StoredApplicationDraft | None:
        self.get_application_draft_calls += 1
        return self.drafts.get(vacancy_id)

    def save_application_draft(self, stored: StoredApplicationDraft) -> bool:
        self.save_calls.append(stored)
        if self._save_conflict_draft is not None and self._save_conflicts_remaining > 0:
            self._save_conflicts_remaining -= 1
            self.drafts[stored.draft.vacancy_id] = self._save_conflict_draft
            return False
        self.drafts[stored.draft.vacancy_id] = stored
        return True

    def close(self) -> None:
        self.closed_count += 1
        if self._events is not None:
            self._events.append("repository.close")


class _FakeGenerator:
    """`generate` возвращает `ApplicationDraftPlan` (TASK-012), не
    `ApplicationDraftContent` — `prepare_application_draft` проверяет план
    и рендерит его настоящим `validate_plan`/`render_application_draft_content`."""

    def __init__(
        self,
        plan: ApplicationDraftPlan | None = None,
        *,
        model: str = "fake-model",
        fail_with: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.model = model
        self._plan = plan
        self._fail_with = fail_with
        self.generate_calls = 0
        self.closed = False
        self._events = events

    def generate(self, vacancy: object, assessment: object, profile: object) -> object:
        self.generate_calls += 1
        if self._events is not None:
            self._events.append("generator.generate")
        if self._fail_with is not None:
            raise self._fail_with
        assert self._plan is not None
        return self._plan

    def close(self) -> None:
        self.closed = True
        if self._events is not None:
            self._events.append("generator.close")


def _forbidden_generator_factory() -> object:
    pytest.fail("generator_factory не должен вызываться при этой ошибке пользовательского ввода")


def _forbidden_model_resolver() -> str:
    pytest.fail("model_resolver не должен вызываться, когда нет existing-черновика для сравнения")


# --- MCP preflight: validation without generator/LLM ---


def test_preflight_returns_safe_summary_and_closes_repository() -> None:
    repository = _FakeRepository(
        vacancy=_stored_vacancy(
            vacancy=_vacancy(
                title="AI Integration Engineer",
                company="Example",
            )
        )
    )

    result = preflight_application_draft(
        lambda: repository,
        "superjob:1",
    )

    assert result == ApplicationDraftPreflight(
        vacancy_id="superjob:1",
        title="AI Integration Engineer",
        company="Example",
    )
    assert repository.closed_count == 1
    assert repository.get_application_draft_calls == 0
    assert repository.save_calls == []


def test_preflight_missing_vacancy_raises_and_closes_repository() -> None:
    repository = _FakeRepository(vacancy=None)

    with pytest.raises(VacancyNotFoundError):
        preflight_application_draft(
            lambda: repository,
            "unknown",
        )

    assert repository.closed_count == 1


def test_preflight_ineligible_vacancy_raises_and_closes_repository() -> None:
    repository = _FakeRepository(
        vacancy=_stored_vacancy(
            filter_result=FilterResult(
                accepted=False,
                rejected_by=["hard_filter"],
            )
        )
    )

    with pytest.raises(VacancyNotEligibleError):
        preflight_application_draft(
            lambda: repository,
            "superjob:1",
        )

    assert repository.closed_count == 1


def test_preflight_preserves_missing_company_as_none() -> None:
    repository = _FakeRepository(
        vacancy=_stored_vacancy(
            vacancy=_vacancy(company=None),
        )
    )

    result = preflight_application_draft(
        lambda: repository,
        "superjob:1",
    )

    assert result.company is None
    assert repository.closed_count == 1


# --- preconditions checked before generator factory ---


def test_missing_vacancy_raises_not_found_without_calling_generator_factory() -> None:
    repository = _FakeRepository(vacancy=None)

    with pytest.raises(VacancyNotFoundError):
        prepare_application_draft(
            lambda: repository,
            _forbidden_generator_factory,
            _forbidden_model_resolver,
            _profile(),
            "1",
            "unknown",
        )


def test_hard_filter_rejected_vacancy_raises_not_eligible_without_generator() -> None:
    repository = _FakeRepository(
        vacancy=_stored_vacancy(filter_result=FilterResult(accepted=False, rejected_by=["x"]))
    )

    with pytest.raises(VacancyNotEligibleError):
        prepare_application_draft(
            lambda: repository,
            _forbidden_generator_factory,
            _forbidden_model_resolver,
            _profile(),
            "1",
            "superjob:1",
        )


def test_missing_assessment_raises_not_eligible_without_generator() -> None:
    repository = _FakeRepository(
        vacancy=_stored_vacancy(assessment=None, qwen_passed=None, qwen_model=None)
    )

    with pytest.raises(VacancyNotEligibleError):
        prepare_application_draft(
            lambda: repository,
            _forbidden_generator_factory,
            _forbidden_model_resolver,
            _profile(),
            "1",
            "superjob:1",
        )


def test_qwen_not_passed_raises_not_eligible_without_generator() -> None:
    repository = _FakeRepository(vacancy=_stored_vacancy(qwen_passed=False))

    with pytest.raises(VacancyNotEligibleError):
        prepare_application_draft(
            lambda: repository,
            _forbidden_generator_factory,
            _forbidden_model_resolver,
            _profile(),
            "1",
            "superjob:1",
        )


def test_skipped_status_raises_not_eligible_without_generator() -> None:
    repository = _FakeRepository(vacancy=_stored_vacancy(status=VacancyStatus.SKIPPED))

    with pytest.raises(VacancyNotEligibleError):
        prepare_application_draft(
            lambda: repository,
            _forbidden_generator_factory,
            _forbidden_model_resolver,
            _profile(),
            "1",
            "superjob:1",
        )


def test_missing_requirements_raises_not_eligible_without_generator() -> None:
    """TASK-012, «Детерминированные проверки», пункт 1: без непустого
    `vacancy.requirements` плану нечего дословно цитировать — безопасный
    `409` ДО создания generator, не `502` после реального вызова."""

    repository = _FakeRepository(vacancy=_stored_vacancy(vacancy=_vacancy(requirements=None)))

    with pytest.raises(VacancyNotEligibleError):
        prepare_application_draft(
            lambda: repository,
            _forbidden_generator_factory,
            _forbidden_model_resolver,
            _profile(),
            "1",
            "superjob:1",
        )


def test_whitespace_only_requirements_raises_not_eligible_without_generator() -> None:
    repository = _FakeRepository(vacancy=_stored_vacancy(vacancy=_vacancy(requirements="   ")))

    with pytest.raises(VacancyNotEligibleError):
        prepare_application_draft(
            lambda: repository,
            _forbidden_generator_factory,
            _forbidden_model_resolver,
            _profile(),
            "1",
            "superjob:1",
        )


# --- idempotency: existing draft short-circuits before generator ---


def test_existing_draft_is_returned_without_calling_generator_factory() -> None:
    """Profile/prompt/model — все три совпадают (TASK-011, ADR-049) —
    `model_resolver` возвращает то же `"old-model"`, что и у existing-
    черновика, поэтому reuse применяется, а `_forbidden_generator_factory`
    подтверждает, что generator не создаётся вовсе."""

    draft = ApplicationDraft(
        schema_version="2",
        vacancy_id="superjob:1",
        profile_version="test-v1",
        content=_content(),
    )
    existing = StoredApplicationDraft(
        draft=draft, model="old-model", prompt_version="1", created_at=_NOW
    )
    repository = _FakeRepository(existing_draft=existing)

    result = prepare_application_draft(
        lambda: repository,
        _forbidden_generator_factory,
        lambda: "old-model",
        _profile(),
        "1",
        "superjob:1",
    )

    assert result.reused is True
    assert result.stored == existing


def test_same_profile_prompt_and_model_reuses_without_creating_generator() -> None:
    """TASK-011 ADR-049, отдельно от теста выше: тот же сценарий, но
    сформулирован явно под требование «same profile/prompt/model →
    reuse без generator» — `model_resolver` совпадает с `existing.model`
    точь-в-точь, `_forbidden_generator_factory` падает, если его вообще
    вызовут."""

    existing = StoredApplicationDraft(
        draft=ApplicationDraft(
            schema_version="2",
            vacancy_id="superjob:1",
            profile_version="test-v1",
            content=_content(),
        ),
        model="qwen/qwen3-8b",
        prompt_version="1",
        created_at=_NOW,
    )
    repository = _FakeRepository(existing_draft=existing)

    result = prepare_application_draft(
        lambda: repository,
        _forbidden_generator_factory,
        lambda: "qwen/qwen3-8b",
        _profile(),
        "1",
        "superjob:1",
    )

    assert result.reused is True
    assert result.stored == existing


def test_existing_draft_with_different_model_triggers_regeneration_and_replaces_it() -> None:
    """TASK-011 ADR-049: «другая model → generator вызывается, draft
    заменяется». Profile/prompt version у existing-черновика совпадают —
    единственное отличие от текущей конфигурации — `model`. Новый план
    цитирует другую часть requirements («опыт SQL» вместо «опыт Python»),
    чтобы отличить реально отрендеренный НОВЫЙ контент от старого, не
    только по `model`."""

    old = StoredApplicationDraft(
        draft=ApplicationDraft(
            schema_version="2",
            vacancy_id="superjob:1",
            profile_version="test-v1",
            content=_content(),
        ),
        model="deepseek-r1-0528-qwen3-8b",
        prompt_version="1",
        created_at=_NOW,
    )
    repository = _FakeRepository(vacancy=_stored_vacancy(), existing_draft=old)
    new_plan = _plan(
        requirements=[
            RequirementEvidenceSelection(requirement_quote="опыт SQL", evidence_fact_ids=[])
        ]
    )
    generator = _FakeGenerator(new_plan, model="qwen/qwen3-8b")

    result = prepare_application_draft(
        lambda: repository,
        lambda: generator,
        lambda: "qwen/qwen3-8b",
        _profile(),
        "1",
        "superjob:1",
    )

    assert result.reused is False
    assert generator.generate_calls == 1
    assert result.stored.model == "qwen/qwen3-8b"
    assert result.stored.draft.content.requirements[0].requirement == "опыт SQL"
    # Реально заменённая (не задублированная) строка — тот же `vacancy_id`.
    assert repository.drafts["superjob:1"].model == "qwen/qwen3-8b"


def test_generation_error_with_different_model_does_not_destroy_previous_draft() -> None:
    """TASK-011 ADR-049: «ошибка новой модели не уничтожает предыдущий
    draft». Модель сменилась → regeneration запускается, но падает — старый
    draft (прежней модели) должен остаться в хранилище нетронутым, ничего
    нового не сохраняется."""

    old = StoredApplicationDraft(
        draft=ApplicationDraft(
            schema_version="2",
            vacancy_id="superjob:1",
            profile_version="test-v1",
            content=_content(match_summary="Проверенный старый черновик"),
        ),
        model="old-model",
        prompt_version="1",
        created_at=_NOW,
    )
    repository = _FakeRepository(vacancy=_stored_vacancy(), existing_draft=old)
    generator = _FakeGenerator(fail_with=ApplicationDraftResponseError("boom"), model="new-model")

    with pytest.raises(ApplicationDraftResponseError):
        prepare_application_draft(
            lambda: repository,
            lambda: generator,
            lambda: "new-model",
            _profile(),
            "1",
            "superjob:1",
        )

    assert repository.save_calls == []
    assert repository.drafts["superjob:1"] == old


def test_stale_profile_version_is_regenerated_and_replaced() -> None:
    old = StoredApplicationDraft(
        draft=ApplicationDraft(
            schema_version="2",
            vacancy_id="superjob:1",
            profile_version="old-profile",
            content=_content(match_summary="Старый черновик"),
        ),
        model="old-model",
        prompt_version="1",
        created_at=_NOW,
    )
    repository = _FakeRepository(vacancy=_stored_vacancy(), existing_draft=old)
    new_plan = _plan(
        requirements=[
            RequirementEvidenceSelection(requirement_quote="опыт SQL", evidence_fact_ids=[])
        ]
    )
    generator = _FakeGenerator(new_plan)

    # `_forbidden_model_resolver`: profile_version уже расходится, поэтому
    # `and` замыкает накоротко до чтения model — не должен вызываться.
    result = prepare_application_draft(
        lambda: repository,
        lambda: generator,
        _forbidden_model_resolver,
        _profile(),
        "1",
        "superjob:1",
    )

    assert result.reused is False
    assert generator.generate_calls == 1
    assert result.stored.draft.profile_version == "test-v1"
    assert result.stored.draft.content.requirements[0].requirement == "опыт SQL"


def test_stale_prompt_version_is_regenerated_and_replaced() -> None:
    old = StoredApplicationDraft(
        draft=ApplicationDraft(
            schema_version="2",
            vacancy_id="superjob:1",
            profile_version="test-v1",
            content=_content(match_summary="Старый черновик"),
        ),
        model="old-model",
        prompt_version="1",
        created_at=_NOW,
    )
    repository = _FakeRepository(vacancy=_stored_vacancy(), existing_draft=old)
    new_plan = _plan(
        requirements=[
            RequirementEvidenceSelection(requirement_quote="опыт SQL", evidence_fact_ids=[])
        ]
    )
    generator = _FakeGenerator(new_plan)

    # `_forbidden_model_resolver`: prompt_version уже расходится, поэтому
    # `and` замыкает накоротко до чтения model — не должен вызываться.
    result = prepare_application_draft(
        lambda: repository,
        lambda: generator,
        _forbidden_model_resolver,
        _profile(),
        "2",
        "superjob:1",
    )

    assert result.reused is False
    assert generator.generate_calls == 1
    assert result.stored.prompt_version == "2"
    assert result.stored.draft.content.requirements[0].requirement == "опыт SQL"


# --- resource lifecycle ---


def test_repository_is_closed_before_generator_is_called() -> None:
    events: list[str] = []
    repository = _FakeRepository(vacancy=_stored_vacancy(), events=events)
    generator = _FakeGenerator(_plan(), events=events)

    prepare_application_draft(
        lambda: repository,
        lambda: generator,
        _forbidden_model_resolver,
        _profile(),
        "1",
        "superjob:1",
    )

    close_index = events.index("repository.close")
    generate_index = events.index("generator.generate")
    assert close_index < generate_index


def test_generator_is_closed_after_generate_succeeds() -> None:
    repository = _FakeRepository(vacancy=_stored_vacancy())
    generator = _FakeGenerator(_plan())

    prepare_application_draft(
        lambda: repository,
        lambda: generator,
        _forbidden_model_resolver,
        _profile(),
        "1",
        "superjob:1",
    )

    assert generator.closed is True


def test_generator_is_closed_even_when_generate_raises() -> None:
    repository = _FakeRepository(vacancy=_stored_vacancy())
    generator = _FakeGenerator(fail_with=ApplicationDraftResponseError("boom"))

    with pytest.raises(ApplicationDraftResponseError):
        prepare_application_draft(
            lambda: repository,
            lambda: generator,
            _forbidden_model_resolver,
            _profile(),
            "1",
            "superjob:1",
        )

    assert generator.closed is True


def test_generator_is_closed_even_when_reading_model_raises() -> None:
    """Найдено ревью TASK-011: `generator.model` раньше читался ДО
    `try/finally` — если бы само чтение атрибута подняло исключение,
    `close()` не вызывался бы вовсе, нарушая гарантию «ресурс закрывается
    при любой ошибке»."""

    class _ExplodingModelGenerator:
        def __init__(self) -> None:
            self.closed = False

        @property
        def model(self) -> str:
            raise RuntimeError("model read boom")

        def generate(self, vacancy: object, assessment: object, profile: object) -> object:
            raise AssertionError("generate не должен вызываться, если чтение model упало")

        def close(self) -> None:
            self.closed = True

    repository = _FakeRepository(vacancy=_stored_vacancy())
    generator = _ExplodingModelGenerator()

    with pytest.raises(RuntimeError, match="model read boom"):
        prepare_application_draft(
            lambda: repository,
            lambda: generator,
            _forbidden_model_resolver,
            _profile(),
            "1",
            "superjob:1",
        )

    assert generator.closed is True


def test_generator_close_failure_does_not_mask_generation_error() -> None:
    class _ExplodingCloseGenerator(_FakeGenerator):
        def close(self) -> None:
            raise RuntimeError("close boom")

    repository = _FakeRepository(vacancy=_stored_vacancy())
    generator = _ExplodingCloseGenerator(fail_with=ApplicationDraftResponseError("primary error"))

    with pytest.raises(ApplicationDraftResponseError, match="primary error"):
        prepare_application_draft(
            lambda: repository,
            lambda: generator,
            _forbidden_model_resolver,
            _profile(),
            "1",
            "superjob:1",
        )


# --- invalid model output is never saved ---


def test_generation_error_does_not_save_anything() -> None:
    repository = _FakeRepository(vacancy=_stored_vacancy())
    generator = _FakeGenerator(fail_with=ApplicationDraftResponseError("boom"))

    with pytest.raises(ApplicationDraftResponseError):
        prepare_application_draft(
            lambda: repository,
            lambda: generator,
            _forbidden_model_resolver,
            _profile(),
            "1",
            "superjob:1",
        )

    assert repository.save_calls == []


def test_evidence_validation_error_does_not_save_anything() -> None:
    """TASK-012: план ссылается на evidence_fact_id, которого нет в Master
    Profile — `validate_plan` отклоняет его ДО renderer/save."""

    repository = _FakeRepository(vacancy=_stored_vacancy())
    bad_plan = _plan(
        requirements=[
            RequirementEvidenceSelection(
                requirement_quote="опыт Python", evidence_fact_ids=["unknown.fact"]
            )
        ]
    )
    generator = _FakeGenerator(bad_plan)

    with pytest.raises(EvidenceValidationError):
        prepare_application_draft(
            lambda: repository,
            lambda: generator,
            _forbidden_model_resolver,
            _profile(),
            "1",
            "superjob:1",
        )

    assert repository.save_calls == []


def test_quote_validation_error_does_not_save_anything() -> None:
    """TASK-012: `requirement_quote` не является дословной подстрокой
    `vacancy.requirements` (пересказ) — `validate_plan` отклоняет его ДО
    renderer/save, старый draft (если был) остаётся нетронутым."""

    repository = _FakeRepository(vacancy=_stored_vacancy())
    bad_plan = _plan(
        requirements=[
            RequirementEvidenceSelection(
                requirement_quote="некое требование, которого нет в вакансии",
                evidence_fact_ids=[],
            )
        ]
    )
    generator = _FakeGenerator(bad_plan)

    with pytest.raises(RequirementQuoteError):
        prepare_application_draft(
            lambda: repository,
            lambda: generator,
            _forbidden_model_resolver,
            _profile(),
            "1",
            "superjob:1",
        )

    assert repository.save_calls == []


# --- success path ---


def test_success_saves_exact_rendered_content_and_metadata() -> None:
    repository = _FakeRepository(vacancy=_stored_vacancy())
    generator = _FakeGenerator(_plan(), model="deepseek-r1-0528-qwen3-8b")

    result = prepare_application_draft(
        lambda: repository,
        lambda: generator,
        _forbidden_model_resolver,
        _profile(),
        "prompt-v1",
        "superjob:1",
    )

    assert result.reused is False
    assert len(repository.save_calls) == 1
    saved = repository.save_calls[0]
    assert saved.draft.vacancy_id == "superjob:1"
    assert saved.draft.profile_version == _profile().version
    assert saved.draft.schema_version == "2"
    assert saved.draft.content.requirements[0].requirement == "опыт Python"
    assert saved.draft.content.requirements[0].coverage == "partial"
    assert "Python." in saved.draft.content.requirements[0].explanation
    assert saved.model == "deepseek-r1-0528-qwen3-8b"
    assert saved.prompt_version == "prompt-v1"
    assert saved.created_at.tzinfo is not None
    assert result.stored == saved


def test_second_call_returns_reused_and_generator_is_called_only_once() -> None:
    """Второй вызов находит existing-черновик с совпадающими profile/prompt
    — `model_resolver` возвращает `generator.model` (та же «текущая
    модель», что сохранил первый вызов), поэтому reuse применяется и
    `generate()` не вызывается повторно (TASK-011, ADR-049)."""

    repository = _FakeRepository(vacancy=_stored_vacancy())
    generator = _FakeGenerator(_plan())

    first = prepare_application_draft(
        lambda: repository,
        lambda: generator,
        lambda: generator.model,
        _profile(),
        "1",
        "superjob:1",
    )
    second = prepare_application_draft(
        lambda: repository,
        lambda: generator,
        lambda: generator.model,
        _profile(),
        "1",
        "superjob:1",
    )

    assert first.reused is False
    assert second.reused is True
    assert second.stored == first.stored
    assert generator.generate_calls == 1


def test_lost_local_race_returns_the_already_saved_draft() -> None:
    """`save_application_draft` возвращает `False` (кто-то другой уже
    вставил первым), и победивший черновик совпадает с этой генерацией по
    ВСЕЙ тройке `profile_version`/`prompt_version`/`model` (TASK-011,
    ADR-049/ADR-050 — сравнивается не только `model`, см.
    `test_lost_local_race_with_only_model_matching_still_retries`, где
    profile отличается, а модель совпадает): сервис должен прочитать и
    вернуть УЖЕ сохранённый результат, не считать это ошибкой и не
    пытаться сохранить снова."""

    raced_draft = ApplicationDraft(
        schema_version="2", vacancy_id="superjob:1", profile_version="test-v1", content=_content()
    )
    raced_stored = StoredApplicationDraft(
        draft=raced_draft, model="fake-model", prompt_version="1", created_at=_NOW
    )
    repository = _FakeRepository(vacancy=_stored_vacancy(), save_conflict_draft=raced_stored)
    generator = _FakeGenerator(_plan())

    result = prepare_application_draft(
        lambda: repository,
        lambda: generator,
        _forbidden_model_resolver,
        _profile(),
        "1",
        "superjob:1",
    )

    assert result.reused is True
    assert result.stored == raced_stored
    assert len(repository.save_calls) == 1


def test_lost_local_race_with_only_model_matching_still_retries() -> None:
    """Codex review-fix (ADR-050): race-ветка раньше сравнивала только
    `model` — победивший черновик с той же моделью, но ЧУЖИМ
    `profile_version`, принимался как `reused=True`. Здесь модель
    совпадает, а `profile_version` — нет; сервис обязан отличить это от
    полного совпадения и повторить сохранение своего результата, как и
    при несовпадающей модели."""

    raced_draft = ApplicationDraft(
        schema_version="2",
        vacancy_id="superjob:1",
        profile_version="other-profile-version",
        content=_content(),
    )
    raced_stored = StoredApplicationDraft(
        draft=raced_draft, model="fake-model", prompt_version="1", created_at=_NOW
    )
    repository = _FakeRepository(
        vacancy=_stored_vacancy(),
        save_conflict_draft=raced_stored,
        save_conflicts_remaining=1,
    )
    new_plan = _plan(
        requirements=[
            RequirementEvidenceSelection(requirement_quote="опыт SQL", evidence_fact_ids=[])
        ]
    )
    generator = _FakeGenerator(new_plan)

    result = prepare_application_draft(
        lambda: repository,
        lambda: generator,
        lambda: "fake-model",
        _profile(),
        "1",
        "superjob:1",
    )

    assert result.reused is False
    assert result.stored.draft.profile_version == "test-v1"
    assert result.stored.draft.content.requirements[0].requirement == "опыт SQL"
    assert len(repository.save_calls) == 2
    assert repository.drafts["superjob:1"].draft.profile_version == "test-v1"


def test_lost_race_twice_with_mismatched_version_raises_concurrency_error() -> None:
    """Codex review-fix (ADR-050): если и повторная попытка сохранения
    проигрывает гонку, а победивший результат ВНОВЬ не совпадает по
    полной тройке — раньше сервис безусловно возвращал его как
    `reused=True`, ничего не проверяя. Теперь это должно быть безопасной
    ошибкой (`ApplicationDraftConcurrencyError`, HTTP `409`), а не тихая
    выдача чужой версии за актуальную."""

    raced_draft = ApplicationDraft(
        schema_version="2",
        vacancy_id="superjob:1",
        profile_version="other-profile-version",
        content=_content(),
    )
    raced_stored = StoredApplicationDraft(
        draft=raced_draft, model="fake-model", prompt_version="1", created_at=_NOW
    )
    repository = _FakeRepository(
        vacancy=_stored_vacancy(),
        save_conflict_draft=raced_stored,
        # Обе попытки сохранения (первая и повторная) проигрывают гонку —
        # `_resolve_after_lost_race` не должен зациклиться, а должен
        # поднять ошибку после второго несовпадающего чтения.
        save_conflicts_remaining=2,
    )
    generator = _FakeGenerator(_plan())

    with pytest.raises(ApplicationDraftConcurrencyError):
        prepare_application_draft(
            lambda: repository,
            lambda: generator,
            lambda: "fake-model",
            _profile(),
            "1",
            "superjob:1",
        )

    assert len(repository.save_calls) == 2
    # Хранилище осталось с чужой (проигравшей нашей проверке) версией — мы
    # её не перезаписали и не выдали как свою.
    assert repository.drafts["superjob:1"] == raced_stored


def test_lost_race_with_different_model_retries_and_replaces_stale_model_draft() -> None:
    """TASK-011 ADR-049: «race path не возвращает draft неподходящей модели
    как актуальный». Побеждающая сторона сохранила черновик ДРУГОЙ модели
    (например, более раннего in-flight вызова до смены `QWEN_MODEL`) —
    сервис не должен выдать его за `reused=True`; вместо этого повторяет
    сохранение своего результата, и вторая попытка должна победить (UPSERT
    учитывает `model`, `_FakeRepository` с `save_conflicts_remaining=1`
    отдаёт конфликт только на первую попытку)."""

    raced_draft = ApplicationDraft(
        schema_version="2", vacancy_id="superjob:1", profile_version="test-v1", content=_content()
    )
    raced_stored = StoredApplicationDraft(
        draft=raced_draft, model="stale-model", prompt_version="1", created_at=_NOW
    )
    repository = _FakeRepository(
        vacancy=_stored_vacancy(),
        save_conflict_draft=raced_stored,
        save_conflicts_remaining=1,
    )
    new_plan = _plan(
        requirements=[
            RequirementEvidenceSelection(requirement_quote="опыт SQL", evidence_fact_ids=[])
        ]
    )
    generator = _FakeGenerator(new_plan, model="current-model")

    result = prepare_application_draft(
        lambda: repository,
        lambda: generator,
        lambda: "current-model",
        _profile(),
        "1",
        "superjob:1",
    )

    assert result.reused is False
    assert result.stored.model == "current-model"
    assert result.stored.draft.content.requirements[0].requirement == "опыт SQL"
    # Первая попытка проиграла гонку (записала `raced_stored`), вторая —
    # выиграла и заменила её; итог в хранилище — модель ЭТОГО вызова, не
    # чужая.
    assert len(repository.save_calls) == 2
    assert repository.drafts["superjob:1"].model == "current-model"


# --- get_application_draft (no generator involved) ---


def test_get_application_draft_returns_none_when_missing() -> None:
    repository = _FakeRepository()

    assert get_application_draft(lambda: repository, "unknown") is None


def test_get_application_draft_returns_stored_result() -> None:
    draft = ApplicationDraft(
        schema_version="2", vacancy_id="superjob:1", profile_version="v1", content=_content()
    )
    stored = StoredApplicationDraft(draft=draft, model="m", prompt_version="1", created_at=_NOW)
    repository = _FakeRepository(existing_draft=stored)

    assert get_application_draft(lambda: repository, "superjob:1") == stored
    assert repository.closed_count == 1
