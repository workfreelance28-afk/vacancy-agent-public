"""Типизированная конфигурация. Точные имена переменных окружения фиксируются
здесь и отражаются в `.env.example` без секретных значений (см.
`docs/ARCHITECTURE.md`, раздел «Конфигурация»).

Секции, которые не используются в MVP 1 (Telegram, Qwen, Claude), имеют
безопасные пустые значения по умолчанию, чтобы приложение запускалось
локально без реальных секретов. Их обязательность добавится в задаче,
которая начнёт их использовать.
"""

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _SectionSettings(BaseSettings):
    """Общая база: читает `.env` (если он есть) вместе с переменными окружения.

    Реальные переменные окружения имеют приоритет над значениями из `.env` —
    это поведение pydantic-settings по умолчанию.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class HHSettings(_SectionSettings):
    model_config = SettingsConfigDict(env_prefix="HH_")

    base_url: str = "https://api.hh.ru"
    user_agent: str = "vacancy-agent/0.1 (contact: not-set)"
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    per_page: int = Field(default=50, ge=1, le=100)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=0.5, gt=0)
    # Поисковый запрос к HH: сформирован из основных целевых названий
    # docs/FILTER_PROFILE.md (ADR-008), чтобы не тянуть по стране все
    # вакансии подряд — точная релевантность остаётся за жёсткими
    # фильтрами (domain/filters.py), не за этим запросом. area/period
    # намеренно не заданы по умолчанию: FILTER_PROFILE.md допускает
    # удалённую работу по всей России, а не только по региону area,
    # поэтому сужение по area на уровне запроса HH могло бы отсеять
    # валидные удалённые вакансии ещё до жёсткой фильтрации.
    search_text: str = (
        '"AI-интегратор" OR "AI Integration Engineer" OR "AI Automation Engineer" '
        'OR "AI Solutions Engineer" OR "LLM Engineer" OR "Prompt Engineer"'
    )
    area: str | None = None
    period_days: int | None = Field(default=7, ge=1, le=30)


class SuperJobSettings(_SectionSettings):
    """См. `docs/DECISIONS.md`, ADR-014: источник вакансий по умолчанию с
    момента закрытия анонимного доступа к HH API (ADR-013)."""

    model_config = SettingsConfigDict(env_prefix="SUPERJOB_")

    api_key: SecretStr = Field(default=SecretStr(""))
    base_url: str = "https://api.superjob.ru/2.0"
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    count: int = Field(default=50, ge=1, le=100)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=0.5, gt=0)
    # Несколько направлений поиска (см. задачу перехода на SuperJob) — не
    # захардкожены в клиенте, собираются в один официальный `keywords[0]`-блок
    # (`srws=10`, `skwc=or`) в `build_superjob_search_params`
    # (application/wiring.py) — синтаксис подтверждён вживую, см.
    # `docs/DECISIONS.md`, ADR-016.
    keywords: list[str] = Field(
        default_factory=lambda: [
            "AI интегратор",
            "AI automation",
            "AI agent",
            "LLM интеграция",
            "n8n",
            "чат-бот API",
            "автоматизация бизнес-процессов AI",
        ]
    )
    town: str | None = None


class HabrCareerSettings(_SectionSettings):
    """career.habr.com — публичный HTML-каталог, не официальный API
    (TASK-013, ADR-056: Habr не предоставляет API публичного поиска
    вакансий для соискателя). `search_query` пустой по умолчанию —
    осознанно: пустой запрос на career.habr.com отдаёт весь каталог, не
    ноль вакансий, поэтому `HabrCareerSource.search_vacancies` отклоняет
    его как fail-fast ошибку ДО первого сетевого запроса, а не здесь на
    старте процесса (см. `_require_configured_qwen_settings` в
    `api/main.py` — тот же приём: секция всегда конструируется, даже
    когда её источник не выбран `VACANCY_SOURCE`, поэтому валидация не
    может жить в самом `HabrCareerSettings`)."""

    model_config = SettingsConfigDict(env_prefix="HABR_")

    base_url: str = "https://career.habr.com"
    # Тот же приём и тот же дефолт, что `HHSettings.user_agent` — честно
    # идентифицирует клиента, не пытается выглядеть браузером (задача
    # прямо запрещает обход антибот-защиты); перед боевым использованием
    # укажите реальные название приложения и контакт.
    user_agent: str = "vacancy-agent/0.1 (contact: not-set)"
    search_query: str = ""
    # TASK-013: «максимум 2 страницы и 40 вакансий по умолчанию» — узкие
    # верхние границы намеренно, не производительность: публичный HTML-
    # каталог без официального API не должен выглядеть как массовый обход.
    max_pages: int = Field(default=2, ge=1, le=10)
    max_vacancies: int = Field(default=40, ge=1, le=200)
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    # TASK-013: «последовательные запросы с настраиваемой задержкой» —
    # «не меньше секунды» проверяется здесь же (`ge=1.0`), не оставлено
    # вызывающему коду.
    request_delay_seconds: float = Field(default=1.0, ge=1.0)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=0.5, gt=0)


class FilterSettings(_SectionSettings):
    """Переопределения поверх `domain.default_filter_profile.DEFAULT_FILTER_CRITERIA`.

    Поля здесь по умолчанию пустые/`None` — это означает «взять значение
    из утверждённого профиля» (`docs/FILTER_PROFILE.md`, ADR-008), а не
    «пустой список фраз». Сборка `FilterCriteria` из этих настроек — в
    `application.wiring.build_filter_criteria` (см. `docs/DECISIONS.md`,
    ADR-012): не дублировать весь профиль в `.env`, чтобы переопределить
    один параметр (например, только `FILTER_SALARY_MIN`).
    """

    model_config = SettingsConfigDict(env_prefix="FILTER_")

    target_titles: list[str] = Field(default_factory=list)
    secondary_titles: list[str] = Field(default_factory=list)
    include_phrases: list[str] = Field(default_factory=list)
    exclude_title_phrases: list[str] = Field(default_factory=list)
    internship_phrases: list[str] = Field(default_factory=list)
    exclude_anywhere_phrases: list[str] = Field(default_factory=list)
    salary_min: int | None = None
    allow_missing_salary: bool = True
    allowed_areas: list[str] = Field(default_factory=list)
    allowed_work_formats: list[str] = Field(default_factory=list)
    allowed_experience: list[str] = Field(default_factory=list)
    max_age_days: int | None = None

    @field_validator("salary_min", "max_age_days", mode="before")
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        """Пустая строка из `.env`/окружения означает «не задано», а не 0 или ошибку."""

        if isinstance(value, str) and value.strip() == "":
            return None
        return value


class TelegramSettings(_SectionSettings):
    model_config = SettingsConfigDict(env_prefix="TELEGRAM_")

    bot_token: SecretStr = Field(default=SecretStr(""))
    allowed_chat_ids: list[int] = Field(default_factory=list)
    request_timeout_seconds: float = Field(default=15.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=0.5, gt=0)


class QwenSettings(_SectionSettings):
    """LM Studio — локальный OpenAI-compatible сервер (см. `docs/DECISIONS.md`,
    ADR-015). `api_key` в LM Studio не проверяется, но библиотека/протокол
    OpenAI-совместимого клиента требует значение — `lm-studio` по
    умолчанию, как рекомендует сам LM Studio, не реальный секрет."""

    model_config = SettingsConfigDict(env_prefix="QWEN_")

    base_url: str = "http://localhost:1234/v1"
    model: str = ""
    api_key: SecretStr = Field(default=SecretStr("lm-studio"))
    min_score: int = Field(default=70, ge=0, le=100)
    # 30с (типичный дефолт для сетевых API) недостаточно для локальной
    # модели: живые smoke-test-прогоны (ADR-015) на одном и том же железе
    # заняли от ~70с до ~172с на полную оценку одной вакансии (полный
    # системный промпт + структурированный JSON-вывод) — заметный разброс,
    # похожий на разницу между «тёплым» и «холодным» состоянием модели в
    # LM Studio. 180с — более безопасный дефолт с запасом, но на медленном
    # железе может потребоваться больше — подстройте под своё окружение.
    request_timeout_seconds: float = Field(default=180.0, gt=0)
    max_retries: int = Field(default=1, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=1.0, gt=0)
    # TASK-011 (ADR-034/ADR-035): подготовка черновика отклика — отдельный,
    # редкий и намного более тяжёлый вызов, чем массовая оценка `assess`
    # (весь Master Profile в контексте, куда более длинный structured
    # output) — отдельные timeout/max_tokens, тот же `base_url`/`model`/
    # `api_key`/retry-политика, что и у массовой оценки.
    draft_request_timeout_seconds: float = Field(default=300.0, gt=0)
    draft_max_output_tokens: int = Field(default=6000, ge=1)


class ScheduleSettings(_SectionSettings):
    model_config = SettingsConfigDict(env_prefix="SCHEDULE_")

    daily_run_hour: int = Field(default=9, ge=0, le=23)
    timezone: str = "Europe/Moscow"


class DatabaseSettings(_SectionSettings):
    model_config = SettingsConfigDict(env_prefix="DATABASE_")

    url: str = "sqlite:///./data/vacancy_agent.db"


class ApiSettings(_SectionSettings):
    """`vacancy-agent-api` (TASK-009, ADR-028/ADR-029). По умолчанию —
    только loopback: аутентификации пока нет, слушать `0.0.0.0` или
    внешний интерфейс по умолчанию нельзя (см. `docs/TASK-009.md`)."""

    model_config = SettingsConfigDict(env_prefix="API_")

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)


class Settings(_SectionSettings):
    """Корневая конфигурация. Валидируется целиком при создании — см. `load_settings`."""

    # `hh` оставлен переключаемым (не удалён) на случай, если HH снова
    # откроет анонимный доступ — см. `docs/DECISIONS.md`, ADR-013/ADR-014.
    # `habr` — TASK-013/ADR-056, тоже переключаемый. `superjob_habr`
    # (TASK-015) — последовательный combined-прогон SuperJob, затем Habr,
    # через общие hard filters/Qwen/SQLite/Telegram; HH в combined-режим не
    # входит, остаётся отдельным резервным вариантом.
    vacancy_source: Literal["hh", "superjob", "habr", "superjob_habr"] = "superjob"
    hh: HHSettings = Field(default_factory=HHSettings)
    superjob: SuperJobSettings = Field(default_factory=SuperJobSettings)
    habr: HabrCareerSettings = Field(default_factory=HabrCareerSettings)
    filters: FilterSettings = Field(default_factory=FilterSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    qwen: QwenSettings = Field(default_factory=QwenSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)


def load_settings() -> Settings:
    """Загружает и валидирует конфигурацию из переменных окружения / `.env`.

    Каждая вложенная секция сама является `BaseSettings` и читает свои
    переменные независимо. Некорректный тип или значение поля приводит к
    `pydantic.ValidationError` сразу при вызове — конфигурация не может
    быть частично невалидной.
    """

    return Settings()
