-- Хранит ровно один validated черновик отклика на вакансию (TASK-011,
-- ADR-034/ADR-035). `vacancy_id` — первичный ключ и внешний ключ на
-- `vacancies.hh_id`: хранится одна актуальная versioned-версия на вакансию,
-- а репозиторий атомарно заменяет её при смене profile/prompt version
-- (ADR-042). Повтор той же версии считается идемпотентным. Orphan-черновик
-- без соответствующей
-- вакансии отклоняется на уровне схемы (требует `PRAGMA foreign_keys = ON`
-- на каждом соединении, см. `persistence/sqlite_repository.py`).
-- `draft_json` — точный validated `ApplicationDraft.model_dump_json()`, а
-- не сырой ответ модели. `schema_version`/`profile_version` дублируют то
-- же, что уже внутри `draft_json`, отдельными колонками, чтобы можно было
-- фильтровать/аудировать черновики по версии без разбора JSON на каждой
-- строке. Разбор миграций (`migrator.py`) делит файл по литеральному
-- символу конца оператора, поэтому этот комментарий его не использует
-- внутри прозы, только в конце самого CREATE TABLE ниже.
CREATE TABLE application_drafts (
    vacancy_id TEXT PRIMARY KEY,
    draft_json TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (vacancy_id) REFERENCES vacancies(hh_id)
);
