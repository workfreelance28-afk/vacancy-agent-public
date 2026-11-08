import json
from datetime import UTC, datetime
from typing import Any

from vacancy_agent.domain.application_draft import EvidenceFact, MasterProfile
from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.domain.master_profile import MASTER_PROFILE
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.integrations.application_draft_prompt import (
    _BASE_PLAN_SCHEMA,
    _MAX_DESCRIPTION_CHARS,
    APPLICATION_DRAFT_PROMPT_VERSION,
    SYSTEM_PROMPT,
    _truncate,
    _truncate_requirements,
    build_response_json_schema,
    build_user_message,
)


def _vacancy(**overrides: object) -> Vacancy:
    defaults: dict[str, object] = {
        "hh_id": "1",
        "title": "AI Integration Engineer",
        "url": "https://superjob.ru/vakansii/1.html",
        "published_at": datetime(2026, 8, 20, tzinfo=UTC),
        "fetched_at": datetime(2026, 8, 20, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Vacancy(**defaults)


def test_truncate_returns_text_unchanged_when_within_limit() -> None:
    text = "x" * _MAX_DESCRIPTION_CHARS

    assert _truncate(text) == text


def test_truncate_result_never_exceeds_the_declared_limit() -> None:
    """Найдено ревью TASK-011: `text[:N] + "…"` давал строку длиной N+1, не
    N — обрезка должна оставлять место под сам символ многоточия."""

    text = "x" * (_MAX_DESCRIPTION_CHARS + 1)

    result = _truncate(text)

    assert result is not None
    assert len(result) == _MAX_DESCRIPTION_CHARS
    assert result.endswith("…")


def test_truncate_result_exceeding_limit_by_a_lot_is_still_exact() -> None:
    text = "x" * 10_000

    result = _truncate(text)

    assert result is not None
    assert len(result) == _MAX_DESCRIPTION_CHARS


def test_truncate_returns_none_for_none() -> None:
    assert _truncate(None) is None


def test_build_user_message_truncated_description_respects_exact_limit() -> None:
    vacancy = _vacancy(description="x" * (_MAX_DESCRIPTION_CHARS + 500))

    assessment = VacancyAssessment(
        schema_version="1", score=90, relevant=True, recommendation="strong_match"
    )
    message = build_user_message(vacancy, assessment, MASTER_PROFILE)

    vacancy_block = message.split("ОЦЕНКА")[0]
    payload = json.loads(vacancy_block.split("\n", 1)[1])
    assert len(payload["description"]) == _MAX_DESCRIPTION_CHARS


# --- _truncate_requirements (Codex review-fix, [P2]: обрезка создавала
# несуществующую цитату) ---
#
# Живая находка: `_truncate` (для `description`) добавляет синтетический
# «…», которого нет в оригинале. Для `requirements` это ломает
# `validate_plan` (`domain/application_draft_plan.py`), если модель
# процитирует фрагмент рядом с границей обрезки, включающий этот символ —
# исходные `requirements` длиной 4053 символа, показанные 4000
# (`text[:3999] + "…"`), цитата `"...TAR…"` отсутствовала в оригинале, хотя
# была верна относительно того, что модель реально видела. `requirements`
# теперь обрезается отдельной функцией — точный `text[:N]` без добавленных
# символов, гарантирующий, что показанный текст всегда остаётся подстрокой
# оригинала.


def test_truncate_requirements_returns_text_unchanged_when_within_limit() -> None:
    text = "x" * _MAX_DESCRIPTION_CHARS

    assert _truncate_requirements(text) == text


def test_truncate_requirements_returns_none_for_none() -> None:
    assert _truncate_requirements(None) is None


def test_truncate_requirements_is_exact_prefix_without_added_characters() -> None:
    """В отличие от `_truncate`, здесь НЕ добавляется «…» — результат всегда
    ровно `text[:N]`, длиной `_MAX_DESCRIPTION_CHARS`, без единого символа,
    которого нет в оригинале."""

    text = "x" * (_MAX_DESCRIPTION_CHARS + 500)

    result = _truncate_requirements(text)

    assert result == text[:_MAX_DESCRIPTION_CHARS]
    assert result is not None
    assert len(result) == _MAX_DESCRIPTION_CHARS
    assert "…" not in result


def test_truncate_requirements_result_is_always_a_substring_of_the_original() -> None:
    """Обратный инвариант, который и был нарушен находкой: показанный текст
    должен оставаться подстрокой оригинала при любой длине входа."""

    for extra in (1, 53, 500, 6000):
        text = "A" * (_MAX_DESCRIPTION_CHARS + extra)
        result = _truncate_requirements(text)
        assert result is not None
        assert result in text


def test_build_user_message_requirements_truncation_has_no_added_characters() -> None:
    """Живая находка Codex-ревью, воспроизведённая точно: исходные
    `requirements` длиной 4053 символа — на 53 больше лимита, как в отчёте."""

    original = "AAAAA TAR" + "x" * 4044
    assert len(original) == 4053
    vacancy = _vacancy(requirements=original)

    assessment = VacancyAssessment(
        schema_version="1", score=90, relevant=True, recommendation="strong_match"
    )
    message = build_user_message(vacancy, assessment, MASTER_PROFILE)

    vacancy_block = message.split("ОЦЕНКА")[0]
    payload = json.loads(vacancy_block.split("\n", 1)[1])
    shown = payload["requirements"]

    assert len(shown) == _MAX_DESCRIPTION_CHARS
    assert shown == original[:_MAX_DESCRIPTION_CHARS]
    assert "…" not in shown
    assert shown in original


def test_prompt_version_is_bumped_for_deterministic_plan_layer() -> None:
    assert APPLICATION_DRAFT_PROMPT_VERSION == "3"


def test_prompt_requires_exact_quote_and_forbids_free_text() -> None:
    """TASK-012: модель больше не пишет ни одного слова о кандидате — только
    цитаты требований и ссылки на evidence facts."""

    required_rules = (
        "ТОЧНОЙ, дословной, посимвольно совпадающей подстрокой",
        "НЕ пишешь ни одного слова текста о кандидате",
        "не расширяй смысл факта",
    )
    assert all(rule in SYSTEM_PROMPT for rule in required_rules)


def test_prompt_does_not_ask_model_for_free_text_fields() -> None:
    """Ни одного из прежних текстовых полей `ApplicationDraftContent`
    (`explanation`/`suggested_text`/`text`/`match_summary`/`gaps`/
    `questions_for_user`/`warnings`) больше нет в форме ответа, которую
    описывает промпт."""

    forbidden_fields = (
        '"explanation"',
        '"suggested_text"',
        '"text":',
        '"match_summary"',
        '"gaps"',
        '"questions_for_user"',
        '"warnings"',
        '"resume_suggestions"',
        '"cover_letter_paragraphs"',
    )
    assert not any(field in SYSTEM_PROMPT for field in forbidden_fields)
    required_fields = (
        '"requirement_quote"',
        '"evidence_fact_ids"',
        '"resume_fact_ids"',
        '"cover_letter_fact_ids"',
    )
    for field in required_fields:
        assert field in SYSTEM_PROMPT


# --- build_response_json_schema (TASK-012, план вместо контента) ---
#
# Схема строится заново для каждого вызова через
# `build_response_json_schema(profile)`, привязанная к точному профилю
# (тот же приём, что прежняя схема `ApplicationDraftContent`, ADR-039/040/041)
# — тесты ниже проверяют структурные границы (унаследованные из
# `ApplicationDraftPlan.model_json_schema()`) и evidence `enum`/`uniqueItems`
# на всех трёх местах, где план ссылается на evidence facts.


def _profile(*fact_ids: str) -> MasterProfile:
    return MasterProfile(
        version="test-v1",
        facts=[
            EvidenceFact(id=fid, category="skill", statement="x", source="t")
            for fid in fact_ids
        ],
    )


_TEST_PROFILE = _profile("skill.python", "skill.sql")


def _schema(profile: MasterProfile = _TEST_PROFILE) -> dict[str, Any]:
    return build_response_json_schema(profile)["schema"]  # type: ignore[return-value]


def _requirement_evidence(profile: MasterProfile = _TEST_PROFILE) -> dict[str, Any]:
    schema = _schema(profile)
    return schema["properties"]["requirements"]["items"]["properties"]["evidence_fact_ids"]  # type: ignore[no-any-return]


def test_schema_envelope_uses_strict_structured_output_contract() -> None:
    schema = build_response_json_schema(_TEST_PROFILE)
    assert schema["name"] == "application_draft_plan"
    assert schema["strict"] is True


def test_schema_requirement_evidence_has_no_minimum_but_has_maximum_of_three() -> None:
    """`RequirementEvidenceSelection.evidence_fact_ids` разрешает 0..3
    элементов (`docs/TASK-012.md`, «Доменный контракт плана»)."""

    field = _requirement_evidence()
    assert "minItems" not in field
    assert field["maxItems"] == 3


def test_schema_collection_size_limits_match_plan_model() -> None:
    schema = _schema()
    assert schema["properties"]["requirements"]["minItems"] == 1
    assert schema["properties"]["requirements"]["maxItems"] == 50
    assert "minItems" not in schema["properties"]["resume_fact_ids"]
    assert schema["properties"]["resume_fact_ids"]["maxItems"] == 20
    assert schema["properties"]["cover_letter_fact_ids"]["minItems"] == 1
    assert schema["properties"]["cover_letter_fact_ids"]["maxItems"] == 10


def test_schema_string_minimum_length_limits_match_plan_model() -> None:
    """`minLength` остаётся в схеме, отправляемой Qwen — только `maxLength`
    убран (ADR-040, см. `test_schema_has_no_max_length`)."""

    requirement_quote = _schema()["properties"]["requirements"]["items"]["properties"][
        "requirement_quote"
    ]
    assert requirement_quote["minLength"] == 1


def test_schema_has_no_max_length() -> None:
    """Живая compatibility-диагностика (ADR-040): полная схема со всеми
    `maxLength` давала `HTTP 400` от LM Studio — компиляция grammar не
    выдерживала многочисленные крупные `maxLength`. `maxLength` остаётся в
    Pydantic-модели без изменений
    (`RequirementEvidenceSelection.requirement_quote`) — убран только из
    JSON Schema, которую видит модель."""

    serialized = json.dumps(build_response_json_schema(_TEST_PROFILE))
    assert "maxLength" not in serialized


def test_schema_forbids_additional_properties_at_every_object_level() -> None:
    schema = _schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["requirements"]["items"]["additionalProperties"] is False


def test_schema_requires_every_declared_property_at_every_level() -> None:
    """Pydantic по умолчанию не помечает поля с `default_factory`
    обязательными (`evidence_fact_ids`/`resume_fact_ids` допускают пустой
    список) — для strict structured outputs любое поле объекта должно быть
    в `required`, иначе модель могла бы просто не прислать ключ вовсе."""

    schema = _schema()
    assert set(schema["required"]) == set(schema["properties"].keys())
    item_schema = schema["properties"]["requirements"]["items"]
    assert set(item_schema["required"]) == set(item_schema["properties"].keys())


def test_schema_has_no_refs_or_defs() -> None:
    """Совместимость `$ref`/`$defs` с strict-режимом конкретного LM Studio
    не проверена вживую (задача запрещает реальный вызов) — схема строится
    из `ApplicationDraftPlan.model_json_schema()`, но разворачивается в
    плоскую форму без `$ref`/`$defs` (см. `_harden_for_strict_json_schema`)."""

    serialized = json.dumps(build_response_json_schema(_TEST_PROFILE))
    assert "$ref" not in serialized
    assert "$defs" not in serialized


def test_schema_evidence_enum_matches_exact_profile_fact_ids_in_all_three_places() -> None:
    """Схема ограничивает evidence ID во всех трёх местах, где план на них
    ссылается (`requirements[].evidence_fact_ids`, `resume_fact_ids`,
    `cover_letter_fact_ids`), точным списком `fact.id` переданного профиля,
    а не произвольной строкой (тот же приём, что ADR-041 для прежней схемы
    контента)."""

    profile = _profile("skill.python", "skill.sql", "skill.docker")
    expected = ["skill.python", "skill.sql", "skill.docker"]
    schema = _schema(profile)

    assert schema["properties"]["requirements"]["items"]["properties"]["evidence_fact_ids"][
        "items"
    ]["enum"] == expected
    assert schema["properties"]["resume_fact_ids"]["items"]["enum"] == expected
    assert schema["properties"]["cover_letter_fact_ids"]["items"]["enum"] == expected


def test_schema_evidence_arrays_require_unique_items_in_all_three_places() -> None:
    schema = _schema()

    assert schema["properties"]["requirements"]["items"]["properties"]["evidence_fact_ids"][
        "uniqueItems"
    ] is True
    assert schema["properties"]["resume_fact_ids"]["uniqueItems"] is True
    assert schema["properties"]["cover_letter_fact_ids"]["uniqueItems"] is True


def test_schema_evidence_enum_does_not_leak_between_profiles() -> None:
    """Разные вызовы `build_response_json_schema` для разных `MasterProfile`
    не должны делить состояние — `enum` вакансии A не должен просочиться в
    схему вакансии B и наоборот (ADR-041)."""

    profile_a = _profile("profile.a.fact1", "profile.a.fact2")
    profile_b = _profile("profile.b.fact1")

    enum_a = _requirement_evidence(profile_a)["items"]["enum"]
    enum_b = _requirement_evidence(profile_b)["items"]["enum"]

    assert enum_a == ["profile.a.fact1", "profile.a.fact2"]
    assert enum_b == ["profile.b.fact1"]
    assert "profile.b.fact1" not in enum_a
    assert "profile.a.fact1" not in enum_b


def test_build_response_json_schema_does_not_mutate_shared_base_schema() -> None:
    """`build_response_json_schema` глубоко копирует `_BASE_PLAN_SCHEMA`
    (`copy.deepcopy`) перед тем, как проставить `enum`/`uniqueItems` — после
    нескольких вызовов с разными профилями сам шаблон, построенный один раз
    при импорте, должен остаться нетронутым (ADR-041)."""

    build_response_json_schema(_profile("some.fact"))
    build_response_json_schema(_profile("other.fact.one", "other.fact.two"))

    requirement_evidence = _BASE_PLAN_SCHEMA["properties"]["requirements"]["items"]["properties"][
        "evidence_fact_ids"
    ]
    assert "enum" not in requirement_evidence["items"]
    assert "uniqueItems" not in requirement_evidence
    assert "enum" not in _BASE_PLAN_SCHEMA["properties"]["resume_fact_ids"]["items"]
    assert "enum" not in _BASE_PLAN_SCHEMA["properties"]["cover_letter_fact_ids"]["items"]
