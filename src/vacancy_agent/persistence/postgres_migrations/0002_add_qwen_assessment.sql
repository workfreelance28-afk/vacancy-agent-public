-- PostgreSQL-эквивалент 0002_add_qwen_assessment.sql (SQLite). Те же поля,
-- тот же смысл (ADR-022) — `qwen_relevant`/`qwen_passed` здесь нативный
-- BOOLEAN, а не INTEGER 0/1, как в SQLite (PostgreSQL-адаптер не делает
-- `_int_to_bool`, читает значение напрямую).
ALTER TABLE vacancies ADD COLUMN qwen_schema_version TEXT;
ALTER TABLE vacancies ADD COLUMN qwen_score INTEGER;
ALTER TABLE vacancies ADD COLUMN qwen_relevant BOOLEAN;
ALTER TABLE vacancies ADD COLUMN qwen_strengths TEXT;
ALTER TABLE vacancies ADD COLUMN qwen_gaps TEXT;
ALTER TABLE vacancies ADD COLUMN qwen_risks TEXT;
ALTER TABLE vacancies ADD COLUMN qwen_recommendation TEXT;
ALTER TABLE vacancies ADD COLUMN qwen_model TEXT;
ALTER TABLE vacancies ADD COLUMN qwen_min_score INTEGER;
ALTER TABLE vacancies ADD COLUMN qwen_passed BOOLEAN;
