"""Публичная read/write-модель сохранённого черновика отклика (TASK-011,
ADR-034/ADR-035) — тот же принцип, что `domain/stored_vacancy.py` (TASK-009,
ADR-028): отдельный небольшой модуль для "вакансия/черновик + метаданные
хранения", а не поле внутри `ApplicationDraft` самого — `model`/`prompt_version`/
`created_at` описывают, КАК и КОГДА был создан черновик, а не то, ЧТО
сгенерировала модель.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vacancy_agent.domain.application_draft import ApplicationDraft


class StoredApplicationDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    draft: ApplicationDraft
    model: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=200)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _require_timezone_aware(cls, value: datetime) -> datetime:
        """Тот же инвариант, что `Vacancy.published_at`/`fetched_at`
        (`domain/vacancy.py`) — наивный `datetime` здесь недопустим."""

        if value.tzinfo is None:
            raise ValueError("datetime должен быть timezone-aware (содержать tzinfo)")
        return value
