"""Validate the public synthetic evidence fixture and privacy boundary."""

import re
from pathlib import Path

from vacancy_agent.domain.master_profile import MASTER_PROFILE, MASTER_PROFILE_VERSION

_DOC_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "synthetic_master_profile.md"
)
_VERSION_LINE_RE = re.compile(r"^Версия:\s*`([^`]+)`", re.MULTILINE)
_TABLE_ROW_RE = re.compile(
    r"^\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$",
    re.MULTILINE,
)


def _doc_text() -> str:
    return _DOC_PATH.read_text(encoding="utf-8")


def _doc_facts() -> dict[str, tuple[str, str, str]]:
    """`id -> (category, statement, source)` для каждой строки таблицы
    фактов `tests/fixtures/synthetic_master_profile.md`. Строки заголовка/разделителя таблицы
    (`| ID | ... |`, `|---|---|...|`) не совпадают с `_TABLE_ROW_RE` —
    первая колонка там не обёрнута в обратные кавычки."""

    return {
        match.group(1): (match.group(2), match.group(3), match.group(4))
        for match in _TABLE_ROW_RE.finditer(_doc_text())
    }


def test_doc_and_runtime_have_the_same_version() -> None:
    match = _VERSION_LINE_RE.search(_doc_text())
    assert match is not None, (
        "tests/fixtures/synthetic_master_profile.md должен содержать строку 'Версия: `...`'"
    )
    assert match.group(1) == MASTER_PROFILE_VERSION == MASTER_PROFILE.version


def test_doc_and_runtime_have_the_same_fact_ids() -> None:
    doc_ids = set(_doc_facts().keys())
    runtime_ids = {fact.id for fact in MASTER_PROFILE.facts}

    assert doc_ids, (
        "В синтетическом fixture не найдены строки таблицы фактов"
    )
    assert doc_ids == runtime_ids


def test_doc_and_runtime_facts_match_category_statement_and_source() -> None:
    """Codex review-fix (ADR-045): для каждого факта runtime сверяем не
    только `id` (уже проверено выше), но и `category`, `statement` и
    `source` дословно с соответствующей строкой таблицы документа —
    иначе документ и код могут разойтись по содержанию при формально
    совпадающем наборе ID."""

    doc_facts = _doc_facts()
    for fact in MASTER_PROFILE.facts:
        assert fact.id in doc_facts, (
            f"{fact.id}: есть в runtime, но не в tests/fixtures/synthetic_master_profile.md"
        )
        doc_category, doc_statement, doc_source = doc_facts[fact.id]
        assert fact.category == doc_category, f"{fact.id}: category разошлась с документом"
        assert fact.statement == doc_statement, f"{fact.id}: statement разошёлся с документом"
        assert fact.source == doc_source, f"{fact.id}: source разошёлся с документом"


def test_fact_ids_are_unique_and_nonempty() -> None:
    ids = [fact.id for fact in MASTER_PROFILE.facts]
    assert len(ids) == len(set(ids))
    assert all(fact_id.strip() for fact_id in ids)


def test_fact_statements_are_nonempty() -> None:
    assert all(fact.statement.strip() for fact in MASTER_PROFILE.facts)


def test_master_profile_has_at_least_one_fact() -> None:
    assert len(MASTER_PROFILE.facts) >= 1


def test_synthetic_profile_is_resume_sourced_and_excludes_contact_data() -> None:
    assert MASTER_PROFILE_VERSION == "synthetic-v1"
    serialized = MASTER_PROFILE.model_dump_json()

    assert all("резюме" in fact.source.lower() for fact in MASTER_PROFILE.facts)
    assert re.search(r"\+\d[\d ()-]{8,}\d", serialized) is None
    assert re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", serialized) is None
    assert "дата рождения" not in serialized.lower()


def test_synthetic_profile_preserves_narrow_experience_boundary() -> None:
    fact = next(fact for fact in MASTER_PROFILE.facts if fact.id == "experience.learning_boundary")

    assert "Три месяца учебной практики" in fact.statement
    assert "не означают коммерческий опыт или senior-уровень" in fact.statement


def test_synthetic_profile_contains_supported_skill_and_project_evidence() -> None:
    ids = {fact.id for fact in MASTER_PROFILE.facts}

    assert {"skill.python", "skill.sql", "project.csv_example"}.issubset(ids)


def test_real_owner_profile_is_not_included() -> None:
    ids = {fact.id for fact in MASTER_PROFILE.facts}

    assert "employment.nhl_role_and_social_content" not in ids
    assert "education.mami_economics_2017" not in ids
    assert "skill.typescript_javascript" not in ids
