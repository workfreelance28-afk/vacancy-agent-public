-- PostgreSQL-эквивалент src/vacancy_agent/persistence/migrations/0001_initial.sql
-- (TASK-S2.1). Та же схема и те же инварианты (hh_id — первичный ключ,
-- защищает от дубликатов; списки хранятся как JSON-текст, как в SQLite —
-- без JSONB/массивов, чтобы `save()`/`get()` PostgreSQL-адаптера оставались
-- структурно теми же, что у SQLite, без отдельной JSON(B)-сериализации).
-- `published_at`/`fetched_at` — TIMESTAMPTZ, а не TEXT: PostgreSQL-адаптер
-- передаёт/читает `datetime` напрямую через psycopg, без `isoformat()`.
CREATE TABLE IF NOT EXISTS vacancies (
    hh_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    company TEXT,
    salary_from DOUBLE PRECISION,
    salary_to DOUBLE PRECISION,
    salary_currency TEXT,
    salary_gross BOOLEAN,
    description TEXT,
    requirements TEXT,
    key_skills TEXT NOT NULL,
    experience TEXT,
    employment TEXT,
    work_format TEXT NOT NULL,
    area TEXT,
    url TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    filter_accepted BOOLEAN NOT NULL,
    filter_reasons TEXT NOT NULL,
    filter_rejected_by TEXT NOT NULL,
    status TEXT NOT NULL
);
