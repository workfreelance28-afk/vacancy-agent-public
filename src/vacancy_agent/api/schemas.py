"""Публичные HTTP response schemas (TASK-009, ADR-028/ADR-029).

Отдельные модели, а не переиспользование `Vacancy`/`StoredVacancy` как
response_model напрямую: внешний контракт использует source-neutral `id`
(отображается на внутреннее `hh_id`, см. `docs/TASK-009.md`), а список
возвращает только сводку (`qwen_score`/`qwen_passed`), не полную оценку —
смешивать доменную модель с HTTP-контрактом означало бы, что смена
внутреннего поля молча меняет ответ API. `FilterResult`/`VacancyAssessment`
из `domain/` переиспользуются как есть внутри detail-ответа: это уже
стабильные, безопасные для показа pydantic-модели без секретов.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from vacancy_agent.domain.application_draft import ApplicationDraft
from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.scan_job import ScanJob, ScanJobStatus
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.stored_application_draft import StoredApplicationDraft
from vacancy_agent.domain.stored_vacancy import StoredVacancy


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    """`GET /api/v1/ready` (TASK-S2.1) — отдельная от `HealthResponse` модель,
    не переиспользование с другим значением `status`: `/health` — чистый
    liveness без обращения к БД, `/ready` подтверждает именно то, что
    последняя дешёвая проверка БД (`RepositoryHealthCheck.ping`) прошла;
    смешивать их в одной модели стирало бы это различие для читателя
    OpenAPI-схемы."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ready"] = "ready"


class MetricsSnapshotResponse(BaseModel):
    """`GET /api/v1/metrics` (TASK-S2.5) — process-local snapshot from
    `observability.metrics.MetricsRegistry`. `counters`/`latencies` map a
    fixed metric name to a small, bounded set of label combinations —
    never a vacancy ID, job UUID, idempotency key, DSN, or raw exception
    text (TASK-S2.5, «Не входит»)."""

    model_config = ConfigDict(frozen=True)

    counters: dict[str, dict[str, int]]
    latencies: dict[str, dict[str, float | int]]


class VacancySummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    company: str | None
    salary_from: float | None
    salary_to: float | None
    salary_currency: str | None
    work_format: list[str]
    area: str | None
    url: str
    published_at: datetime
    status: VacancyStatus
    filter_accepted: bool
    qwen_score: int | None
    qwen_passed: bool | None


class VacancyListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[VacancySummary]
    total: int
    limit: int
    offset: int


class VacancyDetailResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    company: str | None
    salary_from: float | None
    salary_to: float | None
    salary_currency: str | None
    salary_gross: bool | None
    description: str | None
    requirements: str | None
    key_skills: list[str]
    experience: str | None
    employment: str | None
    work_format: list[str]
    area: str | None
    url: str
    published_at: datetime
    fetched_at: datetime
    status: VacancyStatus
    filter_result: FilterResult
    assessment: VacancyAssessment | None
    qwen_model: str | None
    qwen_min_score: int | None
    qwen_passed: bool | None


class ApplicationDraftRequest(BaseModel):
    """Request body `POST /api/v1/vacancies/{vacancy_id}/application-drafts`
    (TASK-011, ADR-034/ADR-035).

    `extra="forbid"` + единственное обязательное поле `StrictBool` — строки
    `"true"`/`"false"`, числа `1`/`0` и `null` не приводятся к boolean
    (`StrictBool` вообще не совершает coercion), а лишнее поле или
    отсутствующее `confirm_local_generation` дают стандартную pydantic-
    ошибку раньше, чем этот класс успевает что-либо проверить сам. Значение
    `false` — валидный `StrictBool`, но не проходит `_must_be_true` ниже:
    отдельная проверка нужна, потому что `StrictBool` сам по себе допускает
    и `True`, и `False` — того, что тип строгий, недостаточно, чтобы
    отклонить осознанный, но отрицательный ответ. Это не cost gate (Qwen
    бесплатный и локальный), а защита от случайного долгого локального
    вычисления (`docs/TASK-011.md`, «HTTP-контракт»)."""

    model_config = ConfigDict(extra="forbid")

    confirm_local_generation: StrictBool

    @field_validator("confirm_local_generation")
    @classmethod
    def _must_be_exactly_true(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("confirm_local_generation должен быть ровно true")
        return value


class ApplicationDraftResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    draft: ApplicationDraft
    model: str
    prompt_version: str
    created_at: datetime
    reused: bool


def application_draft_response(
    stored: StoredApplicationDraft, *, reused: bool
) -> ApplicationDraftResponse:
    return ApplicationDraftResponse(
        draft=stored.draft,
        model=stored.model,
        prompt_version=stored.prompt_version,
        created_at=stored.created_at,
        reused=reused,
    )


def vacancy_summary_from_stored(stored: StoredVacancy) -> VacancySummary:
    vacancy = stored.vacancy
    return VacancySummary(
        id=vacancy.hh_id,
        title=vacancy.title,
        company=vacancy.company,
        salary_from=vacancy.salary_from,
        salary_to=vacancy.salary_to,
        salary_currency=vacancy.salary_currency,
        work_format=vacancy.work_format,
        area=vacancy.area,
        url=vacancy.url,
        published_at=vacancy.published_at,
        status=stored.status,
        filter_accepted=stored.filter_result.accepted,
        qwen_score=stored.assessment.score if stored.assessment else None,
        qwen_passed=stored.qwen_passed,
    )


class ScanJobEnqueueRequest(BaseModel):
    """Request body `POST /api/v1/jobs/scan` (TASK-S2.2).

    `extra="forbid"`, тот же приём, что `ApplicationDraftRequest` — лишнее
    поле даёт стандартную pydantic-ошибку раньше, чем route успевает
    что-либо проверить сам. `idempotency_key` выбирает клиент (не сервер):
    повторный POST с тем же ключом возвращает уже существующую job, а не
    создаёт вторую (TASK-S2.2, «Идемпотентность и границы гарантий»)."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=255)


class ScanJobEnqueueResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: UUID
    status: ScanJobStatus
    reused: bool


class ScanJobStatusResponse(BaseModel):
    """`GET /api/v1/jobs/{job_id}` (TASK-S2.2). `result`/`error_code` —
    ровно один из двух заполнен для терминальных статусов
    (`succeeded`/`failed`), оба `None` для `queued`/`running`/`retry_wait`.
    `error_code` — короткий безопасный код (TASK-S2.2 инвариант: без raw
    exception text/секретов), не текст исключения."""

    model_config = ConfigDict(frozen=True)

    job_id: UUID
    status: ScanJobStatus
    attempt: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    result: dict[str, object] | None
    error_code: str | None


def scan_job_status_response(job: ScanJob) -> ScanJobStatusResponse:
    return ScanJobStatusResponse(
        job_id=job.id,
        status=job.status,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        created_at=job.created_at,
        updated_at=job.updated_at,
        result=job.result,
        error_code=job.error_code,
    )


def vacancy_detail_from_stored(stored: StoredVacancy) -> VacancyDetailResponse:
    vacancy = stored.vacancy
    return VacancyDetailResponse(
        id=vacancy.hh_id,
        title=vacancy.title,
        company=vacancy.company,
        salary_from=vacancy.salary_from,
        salary_to=vacancy.salary_to,
        salary_currency=vacancy.salary_currency,
        salary_gross=vacancy.salary_gross,
        description=vacancy.description,
        requirements=vacancy.requirements,
        key_skills=vacancy.key_skills,
        experience=vacancy.experience,
        employment=vacancy.employment,
        work_format=vacancy.work_format,
        area=vacancy.area,
        url=vacancy.url,
        published_at=vacancy.published_at,
        fetched_at=vacancy.fetched_at,
        status=stored.status,
        filter_result=stored.filter_result,
        assessment=stored.assessment,
        qwen_model=stored.qwen_model,
        qwen_min_score=stored.qwen_min_score,
        qwen_passed=stored.qwen_passed,
    )
