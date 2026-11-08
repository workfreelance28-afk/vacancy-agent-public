-- PostgreSQL-эквивалент 0003_application_drafts.sql (SQLite, TASK-011,
-- ADR-034/ADR-035/ADR-042). Тот же внешний ключ vacancy_id -> vacancies.hh_id
-- — PostgreSQL проверяет внешние ключи по умолчанию, `PRAGMA`-эквивалент
-- (в отличие от SQLite) здесь не нужен.
CREATE TABLE IF NOT EXISTS application_drafts (
    vacancy_id TEXT PRIMARY KEY,
    draft_json TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (vacancy_id) REFERENCES vacancies(hh_id)
);
