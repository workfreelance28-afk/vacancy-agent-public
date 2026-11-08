"""PostgreSQL-репозиторий вакансий (TASK-S2.1). Реализует те же протоколы,
что `persistence.sqlite_repository.SqliteVacancyRepository`
(`domain.ports.VacancyRepository`/`VacancyReadRepository`/
`ApplicationDraftRepository`/`RepositoryHealthCheck`) — второй production-like
backend наравне с SQLite, без изменения бизнес-логики (см.
`docs/TASK-S2.1.md`). Домен и `application/` по-прежнему не импортируют ни
`sqlite3`, ни `psycopg` — оба адаптера видны им только через протоколы
`domain/ports.py`.

Разрешённая новая runtime-зависимость — `psycopg` версии 3 (`pyproject.toml`).
SQLAlchemy/Alembic сознательно не используются (см. `persistence/
postgres_migrator.py`) — для двух небольших адаптеров это расширило бы
scope и скрыло изучаемые SQL/transaction boundaries (`docs/TASK-S2.1.md`).

Как и `SqliteVacancyRepository`, каждый экземпляр — одно соединение на одну
операцию/HTTP-запрос (см. `api/main.py`/`persistence/factory.py`): фабрика
создаёт репозиторий, вызывающий код использует и закрывает его в одном
вызове. Отличия от SQLite-адаптера — только в деталях драйвера:

- параметры — `%s`, не `?` (синтаксис psycopg);
- `published_at`/`fetched_at`/`created_at` — нативные `TIMESTAMPTZ`,
  читаются/пишутся как `datetime` напрямую, без `isoformat()`/
  `fromisoformat()`;
- `salary_gross`/`filter_accepted`/`qwen_relevant`/`qwen_passed` — нативный
  `BOOLEAN`, без `_int_to_bool()` SQLite-адаптера;
- внешние ключи проверяются PostgreSQL по умолчанию — `PRAGMA foreign_keys`
  не нужен;
- правила MVP1-статусов и переходов — общий `persistence/status_rules.py`,
  не отдельная копия (см. его docstring: расхождение между двумя backend
  реализациями было бы трудноуловимым багом).

JSON-списки (`key_skills`, `work_format`, `filter_reasons`,
`filter_rejected_by`, `qwen_strengths`, `qwen_gaps`, `qwen_risks`) хранятся
как TEXT через `json.dumps`/`json.loads`, а не как `JSONB` — сознательно,
чтобы сериализация оставалась той же, что у SQLite, и не заводить второй
способ (де)сериализации ради одного backend'а.
"""

import json
from pathlib import Path
from typing import Any, Self

import psycopg

from vacancy_agent.config.settings import DatabaseSettings
from vacancy_agent.domain.application_draft import ApplicationDraft
from vacancy_agent.domain.assessment import Gap, Strength, VacancyAssessment
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.repository_errors import VacancyNotFoundError
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.stored_application_draft import StoredApplicationDraft
from vacancy_agent.domain.stored_vacancy import StoredVacancy
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.persistence.postgres_migrator import apply_migrations
from vacancy_agent.persistence.status_rules import require_allowed_transition, require_mvp1_status

_MIGRATIONS_DIR = Path(__file__).parent / "postgres_migrations"
_POSTGRES_URL_PREFIX = "postgresql://"


class PostgresVacancyRepository:
    """Хранит вакансии, результаты фильтрации, статусы и черновики
    откликов в PostgreSQL — второй backend наравне с
    `SqliteVacancyRepository` (TASK-S2.1)."""

    def __init__(self, dsn: str) -> None:
        self._connection = psycopg.connect(dsn, row_factory=psycopg.rows.dict_row)
        apply_migrations(self._connection, _MIGRATIONS_DIR)

    @classmethod
    def from_settings(cls, settings: DatabaseSettings) -> Self:
        """Создаёт репозиторий из `DatabaseSettings.url` (`postgresql://...`)."""

        return cls(_postgres_dsn_from_url(settings.url))

    def ping(self) -> None:
        """Дешёвая проверка соединения для `GET /api/v1/ready`
        (`domain.ports.RepositoryHealthCheck`). Бросает `psycopg.Error` при
        недоступной/повреждённой БД — вызывающий код (`api/app.py`)
        превращает это в generic `503`, не раскрывая DSN/SQL наружу."""

        with self._connection.cursor() as cursor:
            cursor.execute("SELECT 1")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def exists(self, hh_id: str) -> bool:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM vacancies WHERE hh_id = %s", (hh_id,))
            return cursor.fetchone() is not None

    def save(
        self,
        vacancy: Vacancy,
        filter_result: FilterResult,
        status: VacancyStatus,
        *,
        assessment: VacancyAssessment | None = None,
        qwen_model: str | None = None,
        qwen_min_score: int | None = None,
        qwen_passed: bool | None = None,
    ) -> None:
        require_mvp1_status(status)
        strengths_json = (
            json.dumps([s.model_dump() for s in assessment.strengths], ensure_ascii=False)
            if assessment
            else None
        )
        gaps_json = (
            json.dumps([g.model_dump() for g in assessment.gaps], ensure_ascii=False)
            if assessment
            else None
        )
        risks_json = json.dumps(assessment.risks, ensure_ascii=False) if assessment else None
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO vacancies (
                        hh_id, title, company, salary_from, salary_to, salary_currency,
                        salary_gross, description, requirements, key_skills, experience,
                        employment, work_format, area, url, published_at, fetched_at,
                        filter_accepted, filter_reasons, filter_rejected_by, status,
                        qwen_schema_version, qwen_score, qwen_relevant, qwen_strengths,
                        qwen_gaps, qwen_risks, qwen_recommendation, qwen_model,
                        qwen_min_score, qwen_passed
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (hh_id) DO NOTHING
                    """,
                    (
                        vacancy.hh_id,
                        vacancy.title,
                        vacancy.company,
                        vacancy.salary_from,
                        vacancy.salary_to,
                        vacancy.salary_currency,
                        vacancy.salary_gross,
                        vacancy.description,
                        vacancy.requirements,
                        json.dumps(vacancy.key_skills, ensure_ascii=False),
                        vacancy.experience,
                        vacancy.employment,
                        json.dumps(vacancy.work_format, ensure_ascii=False),
                        vacancy.area,
                        vacancy.url,
                        vacancy.published_at,
                        vacancy.fetched_at,
                        filter_result.accepted,
                        json.dumps(filter_result.reasons, ensure_ascii=False),
                        json.dumps(filter_result.rejected_by, ensure_ascii=False),
                        status.value,
                        assessment.schema_version if assessment else None,
                        assessment.score if assessment else None,
                        assessment.relevant if assessment else None,
                        strengths_json,
                        gaps_json,
                        risks_json,
                        assessment.recommendation if assessment else None,
                        qwen_model,
                        qwen_min_score,
                        qwen_passed,
                    ),
                )
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def update_status(self, hh_id: str, status: VacancyStatus) -> None:
        require_mvp1_status(status)
        try:
            with self._connection.cursor() as cursor:
                cursor.execute("SELECT status FROM vacancies WHERE hh_id = %s", (hh_id,))
                row = cursor.fetchone()
                if row is None:
                    raise VacancyNotFoundError(f"Вакансия {hh_id!r} не найдена в хранилище")

                current_status = VacancyStatus(row["status"])
                require_allowed_transition(current_status, status)

                cursor.execute(
                    "UPDATE vacancies SET status = %s WHERE hh_id = %s", (status.value, hh_id)
                )
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def get(self, hh_id: str) -> StoredVacancy | None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vacancies WHERE hh_id = %s", (hh_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_domain(row)

    def list_vacancies(self, *, limit: int, offset: int) -> list[StoredVacancy]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM vacancies ORDER BY published_at DESC, hh_id ASC LIMIT %s OFFSET %s",
                (limit, offset),
            )
            rows = cursor.fetchall()
        return [_row_to_domain(row) for row in rows]

    def count_vacancies(self) -> int:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS n FROM vacancies")
            row = cursor.fetchone()
        assert row is not None
        return int(row["n"])

    def get_vacancy(self, vacancy_id: str) -> StoredVacancy | None:
        return self.get(vacancy_id)

    def get_application_draft(self, vacancy_id: str) -> StoredApplicationDraft | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT draft_json, model, prompt_version, created_at "
                "FROM application_drafts WHERE vacancy_id = %s",
                (vacancy_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return StoredApplicationDraft(
            draft=ApplicationDraft.model_validate_json(row["draft_json"]),
            model=row["model"],
            prompt_version=row["prompt_version"],
            created_at=row["created_at"],
        )

    def save_application_draft(self, stored: StoredApplicationDraft) -> bool:
        """Та же версионная UPSERT-семантика, что `SqliteVacancyRepository.
        save_application_draft` (см. его docstring для полного обоснования):
        `True` — insert или атомарная замена устаревшей версии, `False` —
        конкурент уже сохранил ту же текущую версию. Orphan `vacancy_id` не
        подавляется `ON CONFLICT` — внешний ключ отклоняет его отдельным
        `psycopg.errors.ForeignKeyViolation` (подкласс `psycopg.Error`)."""

        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO application_drafts (
                        vacancy_id, draft_json, schema_version, profile_version,
                        prompt_version, model, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (vacancy_id) DO UPDATE SET
                        draft_json = excluded.draft_json,
                        schema_version = excluded.schema_version,
                        profile_version = excluded.profile_version,
                        prompt_version = excluded.prompt_version,
                        model = excluded.model,
                        created_at = excluded.created_at
                    WHERE application_drafts.profile_version != excluded.profile_version
                       OR application_drafts.prompt_version != excluded.prompt_version
                       OR application_drafts.model != excluded.model
                    """,
                    (
                        stored.draft.vacancy_id,
                        stored.draft.model_dump_json(),
                        stored.draft.schema_version,
                        stored.draft.profile_version,
                        stored.prompt_version,
                        stored.model,
                        stored.created_at,
                    ),
                )
                affected: int = cursor.rowcount
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()
            return affected == 1


def _postgres_dsn_from_url(url: str) -> str:
    if not url.startswith(_POSTGRES_URL_PREFIX):
        raise ValueError(f"Ожидался URL вида '{_POSTGRES_URL_PREFIX}...', получено: {url!r}")
    return url


def _row_to_domain(row: dict[str, Any]) -> StoredVacancy:
    vacancy = Vacancy(
        hh_id=row["hh_id"],
        title=row["title"],
        company=row["company"],
        salary_from=row["salary_from"],
        salary_to=row["salary_to"],
        salary_currency=row["salary_currency"],
        salary_gross=row["salary_gross"],
        description=row["description"],
        requirements=row["requirements"],
        key_skills=json.loads(row["key_skills"]),
        experience=row["experience"],
        employment=row["employment"],
        work_format=json.loads(row["work_format"]),
        area=row["area"],
        url=row["url"],
        published_at=row["published_at"],
        fetched_at=row["fetched_at"],
    )
    filter_result = FilterResult(
        accepted=bool(row["filter_accepted"]),
        reasons=json.loads(row["filter_reasons"]),
        rejected_by=json.loads(row["filter_rejected_by"]),
    )
    status = VacancyStatus(row["status"])
    assessment = _row_to_assessment(row)
    return StoredVacancy(
        vacancy=vacancy,
        filter_result=filter_result,
        status=status,
        assessment=assessment,
        qwen_model=row["qwen_model"],
        qwen_min_score=row["qwen_min_score"],
        qwen_passed=row["qwen_passed"],
    )


def _row_to_assessment(row: dict[str, Any]) -> VacancyAssessment | None:
    if row["qwen_schema_version"] is None:
        return None
    return VacancyAssessment(
        schema_version=row["qwen_schema_version"],
        score=row["qwen_score"],
        relevant=bool(row["qwen_relevant"]),
        strengths=[Strength(**s) for s in json.loads(row["qwen_strengths"])],
        gaps=[Gap(**g) for g in json.loads(row["qwen_gaps"])],
        risks=json.loads(row["qwen_risks"]),
        recommendation=row["qwen_recommendation"],
    )
