from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Vacancy(BaseModel):
    """Нормализованная вакансия HH. См. `docs/DATA_CONTRACTS.md`.

    Денежные поля нельзя сравнивать без учёта `salary_currency` и
    `salary_gross` — эта проверка выполняется в слое фильтров, не здесь.
    """

    model_config = ConfigDict(frozen=True)

    hh_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    company: str | None = None
    salary_from: float | None = None
    salary_to: float | None = None
    salary_currency: str | None = None
    salary_gross: bool | None = None
    description: str | None = None
    requirements: str | None = None
    key_skills: list[str] = Field(default_factory=list)
    experience: str | None = None
    employment: str | None = None
    work_format: list[str] = Field(default_factory=list)
    area: str | None = None
    url: str = Field(min_length=1)
    published_at: datetime
    fetched_at: datetime

    @field_validator("published_at", "fetched_at")
    @classmethod
    def _require_timezone_aware(cls, value: datetime) -> datetime:
        """Наивный datetime здесь недопустим: `domain/filters.py` сравнивает
        `published_at` с `datetime.now(UTC)`, и разница часовых поясов молча
        сдвинула бы результат фильтра по давности на величину локального
        смещения — или, при сравнении с offset-aware временем, упала бы
        `TypeError`. Лучше отклонить на границе домена, чем ловить это в
        фильтре."""

        if value.tzinfo is None:
            raise ValueError("datetime должен быть timezone-aware (содержать tzinfo)")
        return value
