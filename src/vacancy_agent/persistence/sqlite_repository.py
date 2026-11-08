"""SQLite-репозиторий вакансий. Реализует `vacancy_agent.domain.ports.VacancyRepository`.

Схема — версионированные `.sql`-файлы в `persistence/migrations/`,
применяются `persistence.migrator.apply_migrations` при создании
репозитория (см. `docs/DECISIONS.md`, ADR-006).

Идемпотентность по `hh_id` обеспечена на уровне схемы: `hh_id` — первичный
ключ (см. `docs/ARCHITECTURE.md`, «Надёжность и безопасность» — «Уникальный
индекс по `hh_id` защищает от дубликатов»), а `save()` использует
`INSERT ... ON CONFLICT DO NOTHING` — повторный вызов с тем же `hh_id` не
создаёт вторую строку и не бросает ошибку.

MVP 1 использует только `NEW`/`VIEWED`/`SKIPPED` (см. `domain/status.py`,
`MVP1_STATUSES`) — `save`/`update_status` отклоняют прочие статусы явной
`ValueError`, а не тихо принимают их до того, как MVP 4 утвердит полную
таблицу переходов. Внутри MVP 1 `update_status` также проверяет сам
переход (`_ALLOWED_TRANSITIONS`), а не только то, что целевой статус
входит в `MVP1_STATUSES` — иначе можно было бы, например, откатить
`SKIPPED` обратно в `NEW`, что семантически не является «просмотром» или
«пропуском» и должно быть явной новой бизнес-операцией, а не побочным
эффектом `update_status`.

Любая ошибка внутри `update_status`/`save` (включая «не найдено» и
«переход недопустим») явно откатывает транзакцию (`rollback()`) перед
тем, как исключение уходит наружу — без этого соединение осталось бы с
незакоммиченной транзакцией, которая слилась бы со следующей операцией
на этом же соединении.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Self

from vacancy_agent.config.settings import DatabaseSettings
from vacancy_agent.domain.application_draft import ApplicationDraft
from vacancy_agent.domain.assessment import Gap, Strength, VacancyAssessment
from vacancy_agent.domain.filter_result import FilterResult
from vacancy_agent.domain.repository_errors import VacancyNotFoundError
from vacancy_agent.domain.status import VacancyStatus
from vacancy_agent.domain.stored_application_draft import StoredApplicationDraft
from vacancy_agent.domain.stored_vacancy import StoredVacancy
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.persistence.migrator import apply_migrations
from vacancy_agent.persistence.status_rules import require_allowed_transition, require_mvp1_status

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_SQLITE_URL_PREFIX = "sqlite:///"


class SqliteVacancyRepository:
    """Хранит вакансии, результаты фильтрации и статусы в SQLite."""

    def __init__(self, db_path: str) -> None:
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(db_path)
        self._connection.row_factory = sqlite3.Row
        # TASK-011 (ADR-034/ADR-035): `application_drafts.vacancy_id` — внешний
        # ключ на `vacancies.hh_id`. SQLite не проверяет внешние ключи по
        # умолчанию — без этого PRAGMA orphan-черновик (без соответствующей
        # вакансии) сохранялся бы молча.
        self._connection.execute("PRAGMA foreign_keys = ON")
        apply_migrations(self._connection, _MIGRATIONS_DIR)

    @classmethod
    def from_settings(cls, settings: DatabaseSettings) -> Self:
        """Создаёт репозиторий из `DatabaseSettings.url` (`sqlite:///путь`)."""

        return cls(_sqlite_path_from_url(settings.url))

    def ping(self) -> None:
        """Дешёвая проверка соединения для `GET /api/v1/ready`
        (`domain.ports.RepositoryHealthCheck`, TASK-S2.1). Бросает исключение
        при недоступной/повреждённой БД — вызывающий код (`api/app.py`)
        превращает это в generic `503`, не раскрывая SQL/путь наружу."""

        self._connection.execute("SELECT 1")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def exists(self, hh_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM vacancies WHERE hh_id = ?", (hh_id,)
        ).fetchone()
        return row is not None

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
        try:
            self._connection.execute(
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
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(hh_id) DO NOTHING
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
                    vacancy.published_at.isoformat(),
                    vacancy.fetched_at.isoformat(),
                    filter_result.accepted,
                    json.dumps(filter_result.reasons, ensure_ascii=False),
                    json.dumps(filter_result.rejected_by, ensure_ascii=False),
                    status.value,
                    assessment.schema_version if assessment else None,
                    assessment.score if assessment else None,
                    assessment.relevant if assessment else None,
                    json.dumps([s.model_dump() for s in assessment.strengths], ensure_ascii=False)
                    if assessment
                    else None,
                    json.dumps([g.model_dump() for g in assessment.gaps], ensure_ascii=False)
                    if assessment
                    else None,
                    json.dumps(assessment.risks, ensure_ascii=False) if assessment else None,
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
            row = self._connection.execute(
                "SELECT status FROM vacancies WHERE hh_id = ?", (hh_id,)
            ).fetchone()
            if row is None:
                raise VacancyNotFoundError(f"Вакансия {hh_id!r} не найдена в хранилище")

            current_status = VacancyStatus(row["status"])
            require_allowed_transition(current_status, status)

            self._connection.execute(
                "UPDATE vacancies SET status = ? WHERE hh_id = ?", (status.value, hh_id)
            )
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def get(self, hh_id: str) -> StoredVacancy | None:
        """Читает сохранённую вакансию целиком, включая оценку Qwen, если она есть.

        За пределами `VacancyRepository`-протокола (тот не требует чтения) —
        минимальный, но необходимый способ проверить сохранённое состояние
        в тестах и будущий источник данных для `application/` (TASK-005),
        включая MVP 3, которому нужна сама оценка, а не только факт, что
        вакансия прошла Qwen (ADR-022). `qwen_min_score`/`qwen_passed` —
        порог и итог порогового решения, действовавшие НА МОМЕНТ обработки
        (ADR-022, второе уточнение ревью) — `QwenSettings.min_score`
        конфигурируем и может измениться позже, поэтому исторический аудит
        нуждается в снимке порога на момент решения, не только в текущем
        значении настройки.
        """

        row = self._connection.execute(
            "SELECT * FROM vacancies WHERE hh_id = ?", (hh_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_domain(row)

    def list_vacancies(self, *, limit: int, offset: int) -> list[StoredVacancy]:
        """Страница сохранённых вакансий (TASK-009, ADR-028/ADR-029), read-путь
        `application/vacancy_queries.py` для `GET /api/v1/vacancies`.

        Порядок `published_at DESC, hh_id ASC` детерминирован даже при
        нескольких вакансиях с одинаковым `published_at` — `hh_id` уникален
        (первичный ключ), поэтому tie-break по нему не зависит от порядка
        вставки/физического расположения строк в файле SQLite."""

        rows = self._connection.execute(
            "SELECT * FROM vacancies ORDER BY published_at DESC, hh_id ASC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_row_to_domain(row) for row in rows]

    def count_vacancies(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS n FROM vacancies").fetchone()
        return int(row["n"])

    def get_vacancy(self, vacancy_id: str) -> StoredVacancy | None:
        """Алиас `get()` под именем, которое требует `ApplicationDraftRepository`
        (TASK-011, ADR-034/ADR-035, `docs/TASK-011.md`, «Порты и границы») —
        не переименование `get()`: тот уже используется `VacancyReadRepository`
        (ADR-028) и существующими тестами/вызывающим кодом под этим именем."""

        return self.get(vacancy_id)

    def get_application_draft(self, vacancy_id: str) -> StoredApplicationDraft | None:
        row = self._connection.execute(
            "SELECT draft_json, model, prompt_version, created_at "
            "FROM application_drafts WHERE vacancy_id = ?",
            (vacancy_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredApplicationDraft(
            draft=ApplicationDraft.model_validate_json(row["draft_json"]),
            model=row["model"],
            prompt_version=row["prompt_version"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def save_application_draft(self, stored: StoredApplicationDraft) -> bool:
        """Сохраняет текущую versioned-версию черновика на вакансию.

        Возвращает `True`, если строка создана или устаревшая строка атомарно
        заменена из-за другой `profile_version`/`prompt_version`/`model`.
        Возвращает `False`, если конкурент уже сохранил ту же текущую версию
        — вызывающий код читает победивший результат и возвращает
        `reused=True` (или, если победившая модель не совпадает с только что
        сгенерированной, повторяет попытку сохранения — см. `application/
        application_drafts.py: _resolve_after_lost_race`, ADR-049).

        Версионный UPSERT нужен после ADR-042: прежний `ON CONFLICT DO NOTHING`
        навсегда возвращал бы старый черновик даже после исправления Master
        Profile или grounding-промпта. `model` добавлен в условие замены после
        ADR-048/ADR-049: смена `QWEN_MODEL` при неизменных profile/prompt
        раньше не заменяла строку вовсе — A/B-сравнение моделей возвращало бы
        результат прежней модели, выданный за актуальный. Миграция таблицы не
        нужна — колонка `model` существовала с ADR-034/ADR-035, изменилось
        только условие WHERE.

        Orphan-черновик (несуществующий `vacancy_id` в `vacancies`) не
        подавляется `ON CONFLICT` — это отдельное ограничение (внешний
        ключ), и `sqlite3.IntegrityError` от него должен дойти до
        вызывающего кода, а не быть спутан с обычной идемпотентностью."""

        try:
            cursor = self._connection.execute(
                """
                INSERT INTO application_drafts (
                    vacancy_id, draft_json, schema_version, profile_version,
                    prompt_version, model, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vacancy_id) DO UPDATE SET
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
                    stored.created_at.isoformat(),
                ),
            )
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()
            return cursor.rowcount == 1


def _sqlite_path_from_url(url: str) -> str:
    if not url.startswith(_SQLITE_URL_PREFIX):
        raise ValueError(f"Ожидался URL вида '{_SQLITE_URL_PREFIX}путь', получено: {url!r}")
    return url[len(_SQLITE_URL_PREFIX) :]


def _row_to_domain(row: sqlite3.Row) -> StoredVacancy:
    vacancy = Vacancy(
        hh_id=row["hh_id"],
        title=row["title"],
        company=row["company"],
        salary_from=row["salary_from"],
        salary_to=row["salary_to"],
        salary_currency=row["salary_currency"],
        salary_gross=_int_to_bool(row["salary_gross"]),
        description=row["description"],
        requirements=row["requirements"],
        key_skills=json.loads(row["key_skills"]),
        experience=row["experience"],
        employment=row["employment"],
        work_format=json.loads(row["work_format"]),
        area=row["area"],
        url=row["url"],
        published_at=datetime.fromisoformat(row["published_at"]),
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
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
        qwen_passed=_int_to_bool(row["qwen_passed"]),
    )


def _row_to_assessment(row: sqlite3.Row) -> VacancyAssessment | None:
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


def _int_to_bool(value: int | None) -> bool | None:
    if value is None:
        return None
    return bool(value)
