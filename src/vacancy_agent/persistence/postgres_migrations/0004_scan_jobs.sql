-- Durable scan job queue (TASK-S2.2). PostgreSQL-only: не имеет SQLite-
-- эквивалента, в отличие от 0001-0003 — persistence/factory.py отклоняет
-- очередь для sqlite:/// явной ошибкой до попытки что-либо открыть.
-- id генерируется в Python (uuid.uuid4(), psycopg передаёт uuid.UUID как
-- нативный UUID) — без pgcrypto/uuid-ossp extension ради одной таблицы.
-- idempotency_key уникален (повторный enqueue не создаёт вторую job);
-- result хранится как JSON-текст, тем же приёмом, что qwen_strengths и
-- остальные списки в vacancies (persistence/postgres_repository.py) — не
-- заводить JSONB ради одной колонки. error_code — короткий безопасный код
-- без raw exception text (TASK-S2.2, инвариант).
CREATE TABLE IF NOT EXISTS scan_jobs (
    id UUID PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    available_at TIMESTAMPTZ NOT NULL,
    lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    result_json TEXT,
    error_code TEXT
);

-- Ускоряет `claim_one`: выбор кандидата на claim по статусу/`available_at`/
-- `lease_expires_at` — та же тройка условий, что WHERE в claim-запросе.
CREATE INDEX IF NOT EXISTS scan_jobs_claim_idx
    ON scan_jobs (status, available_at, lease_expires_at);
