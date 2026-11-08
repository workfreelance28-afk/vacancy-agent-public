-- Начальная схема хранилища вакансий. `hh_id` — первичный ключ: уникальный
-- индекс, который защищает от дубликатов (см. docs/ARCHITECTURE.md,
-- «Надёжность и безопасность»). Списки (`key_skills`, `work_format`,
-- `filter_reasons`, `filter_rejected_by`) хранятся как JSON-текст —
-- нативного JSON-типа в SQLite нет.
CREATE TABLE IF NOT EXISTS vacancies (
    hh_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    company TEXT,
    salary_from REAL,
    salary_to REAL,
    salary_currency TEXT,
    salary_gross INTEGER,
    description TEXT,
    requirements TEXT,
    key_skills TEXT NOT NULL,
    experience TEXT,
    employment TEXT,
    work_format TEXT NOT NULL,
    area TEXT,
    url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    filter_accepted INTEGER NOT NULL,
    filter_reasons TEXT NOT NULL,
    filter_rejected_by TEXT NOT NULL,
    status TEXT NOT NULL
);
