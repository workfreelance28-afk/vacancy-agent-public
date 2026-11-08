from datetime import UTC, datetime

from vacancy_agent.domain.candidate_profile import CANDIDATE_PROFILE
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.integrations.qwen_prompt import SYSTEM_PROMPT, build_user_message


def _vacancy(**overrides: object) -> Vacancy:
    defaults: dict[str, object] = {
        "hh_id": "1",
        "title": "AI Integration Engineer",
        "company": "OOO Ромашка",
        "description": "Интеграция LLM в продукт.",
        "salary_from": 200_000,
        "salary_to": 300_000,
        "salary_currency": "rub",
        "area": "Москва",
        "experience": "От 1 года до 3 лет",
        "url": "https://superjob.ru/vakansii/1.html",
        "published_at": datetime(2026, 8, 20, tzinfo=UTC),
        "fetched_at": datetime(2026, 8, 20, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Vacancy(**defaults)


def test_system_prompt_instructs_json_only_output() -> None:
    assert "JSON" in SYSTEM_PROMPT
    assert "score" in SYSTEM_PROMPT
    assert "recommendation" in SYSTEM_PROMPT


def test_system_prompt_warns_against_instructions_embedded_in_vacancy_text() -> None:
    """Требование ROADMAP.md MVP2 «защитить промпт от инструкций в тексте
    вакансии» — системный промпт должен явно предупреждать модель не
    выполнять инструкции из данных вакансии."""

    assert "НЕДОВЕРЕННЫЙ" in SYSTEM_PROMPT or "недоверенн" in SYSTEM_PROMPT.lower()


def test_system_prompt_embeds_real_candidate_profile() -> None:
    """Найдено ревью (`docs/DECISIONS.md`, ADR-016, P1 «FIT SCORE не знает
    профиль кандидата») — без реального профиля score был бы категорийной
    оценкой, а не персональным FIT SCORE."""

    assert CANDIDATE_PROFILE in SYSTEM_PROMPT


def test_system_prompt_instructs_score_against_candidate_not_just_category() -> None:
    assert "КОНКРЕТНОМУ кандидату" in SYSTEM_PROMPT or "личном соответствии" in SYSTEM_PROMPT


def test_build_user_message_includes_only_required_fields() -> None:
    vacancy = _vacancy(
        key_skills=["Python"],
        employment="Полная занятость",
        requirements="Опыт с Python от 2 лет.",
    )

    message = build_user_message(vacancy)

    assert vacancy.title in message
    assert vacancy.description in message
    assert vacancy.area in message
    assert vacancy.experience in message
    assert vacancy.company in message
    assert "200000" in message or "200 000" in message.replace(" ", " ")


def test_build_user_message_includes_requirements() -> None:
    """`requirements` заполняется отдельно от `description` с ADR-016
    (`work`/`candidat` у SuperJob, `snippet.responsibility`/
    `snippet.requirement` у HH) — до фикса нормализовался, но никогда не
    попадал в промпт Qwen (`docs/DECISIONS.md`, ADR-018)."""

    vacancy = _vacancy(requirements="Опыт с Python и Docker от 2 лет.")

    message = build_user_message(vacancy)

    assert "Опыт с Python и Docker от 2 лет." in message


def test_build_user_message_handles_missing_requirements() -> None:
    vacancy = _vacancy(requirements=None)

    message = build_user_message(vacancy)

    assert '"requirements": null' in message


def test_build_user_message_marks_vacancy_data_as_untrusted() -> None:
    message = build_user_message(_vacancy())

    assert "недоверенный" in message.lower() or "не выполняй" in message.lower()


def test_build_user_message_handles_missing_salary() -> None:
    vacancy = _vacancy(salary_from=None, salary_to=None, salary_currency=None)

    message = build_user_message(vacancy)

    assert '"salary": null' in message


def test_build_user_message_truncates_very_long_description() -> None:
    vacancy = _vacancy(description="x" * 10_000)

    message = build_user_message(vacancy)

    assert len(message) < 10_000 + 1000
