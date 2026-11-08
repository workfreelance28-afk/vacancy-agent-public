from pydantic import BaseModel, ConfigDict, Field


class FilterResult(BaseModel):
    """Результат применения жёстких фильтров. См. `docs/DATA_CONTRACTS.md`.

    `reasons` и `rejected_by` — машинно-читаемые коды, не текст для
    пользователя.
    """

    model_config = ConfigDict(frozen=True)

    accepted: bool
    reasons: list[str] = Field(default_factory=list)
    rejected_by: list[str] = Field(default_factory=list)
