from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from vacancy_agent.config import load_settings
from vacancy_agent.config.settings import FilterSettings, HHSettings, QwenSettings

_ENV_EXAMPLE_PATH = Path(__file__).resolve().parents[2] / ".env.example"


def test_load_settings_without_env_does_not_require_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("TELEGRAM_BOT_TOKEN", "QWEN_MODEL", "SUPERJOB_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # Изолируем от реального `.env` в рабочей директории разработчика (если
    # он есть — например, для живой проверки TASK-005) — эта проверка про
    # поведение без конфигурации вообще, не про то, что в чьём-то `.env`.
    monkeypatch.chdir(tmp_path)

    settings = load_settings()

    assert settings.telegram.bot_token.get_secret_value() == ""
    assert settings.superjob.api_key.get_secret_value() == ""
    assert settings.database.url.startswith("sqlite:///")


def test_settings_reject_invalid_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN_MIN_SCORE", "not-a-number")

    with pytest.raises(ValidationError):
        QwenSettings()


def test_settings_reject_out_of_range_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN_MIN_SCORE", "150")

    with pytest.raises(ValidationError):
        QwenSettings()


def test_filter_list_settings_parse_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FILTER_INCLUDE_PHRASES", '["python", "backend"]')

    settings = load_settings()

    assert settings.filters.include_phrases == ["python", "backend"]


def test_empty_salary_min_env_var_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FILTER_SALARY_MIN", "")

    settings = FilterSettings()

    assert settings.salary_min is None


def test_empty_max_age_days_env_var_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FILTER_MAX_AGE_DAYS", "")

    settings = FilterSettings()

    assert settings.max_age_days is None


def test_dot_env_file_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HH_BASE_URL", raising=False)
    (tmp_path / ".env").write_text("HH_BASE_URL=https://from-dotenv.example\n")
    monkeypatch.chdir(tmp_path)

    settings = HHSettings()

    assert settings.base_url == "https://from-dotenv.example"


def test_real_env_var_overrides_dot_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("HH_BASE_URL=https://from-dotenv.example\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HH_BASE_URL", "https://from-real-env.example")

    settings = HHSettings()

    assert settings.base_url == "https://from-real-env.example"


def test_dot_env_value_with_embedded_double_quotes_needs_outer_single_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """python-dotenv не может разобрать значение вида `KEY="a" OR "b"` без
    внешних кавычек — оно должно быть обёрнуто в одинарные кавычки целиком
    (см. `docs/DECISIONS.md`, уточнение по итогам живой проверки TASK-005:
    `HH_SEARCH_TEXT` в `.env.example` изначально был в такой сломанной форме
    и молча подставлял дефолт из кода вместо значения из `.env`)."""

    monkeypatch.delenv("HH_SEARCH_TEXT", raising=False)
    value = '"AI-интегратор" OR "Prompt Engineer"'
    (tmp_path / ".env").write_text(f"HH_SEARCH_TEXT='{value}'\n")
    monkeypatch.chdir(tmp_path)

    settings = HHSettings()

    assert settings.search_text == value


def test_env_example_has_no_unparseable_lines() -> None:
    """Каждая `KEY=...` строка в `.env.example` должна реально разбираться
    `python-dotenv` — иначе pydantic-settings молча подставит дефолт из
    кода вместо значения из файла, и правка `.env.example`/`.env` не
    будет иметь эффекта, как это было с `HH_SEARCH_TEXT`."""

    raw_keys = {
        line.split("=", 1)[0]
        for line in _ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    }

    parsed = dotenv_values(_ENV_EXAMPLE_PATH)

    assert raw_keys == set(parsed.keys())
