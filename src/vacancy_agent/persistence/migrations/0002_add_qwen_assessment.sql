-- Добавляет оценку Qwen к сохранённой вакансии (`docs/DECISIONS.md`, ADR-022,
-- найдено ревью TASK-007): без этого решение Qwen нельзя было проверить —
-- вакансия, отклонённая Qwen, ничем не отличалась в хранилище от прошедшей
-- его. Все столбцы NULL-совместимы: вакансия, отклонённая жёсткими фильтрами,
-- никогда не доходит до Qwen, и для строк, сохранённых до этой миграции,
-- оценки не будет. `qwen_strengths`/`qwen_gaps`/`qwen_risks` — JSON-текст,
-- как и остальные списки в этой таблице (см. 0001_initial.sql).
--
-- `qwen_min_score`/`qwen_passed` — порог и итог порогового решения на
-- момент обработки (тоже найдено ревью TASK-007): `QwenSettings.min_score`
-- конфигурируем и может измениться в будущем — без снимка порога на момент
-- решения по одному только `qwen_score` нельзя было бы восстановить, был
-- ли конкретный балл в тот момент выше или ниже действовавшего порога.
ALTER TABLE vacancies ADD COLUMN qwen_schema_version TEXT;
ALTER TABLE vacancies ADD COLUMN qwen_score INTEGER;
ALTER TABLE vacancies ADD COLUMN qwen_relevant INTEGER;
ALTER TABLE vacancies ADD COLUMN qwen_strengths TEXT;
ALTER TABLE vacancies ADD COLUMN qwen_gaps TEXT;
ALTER TABLE vacancies ADD COLUMN qwen_risks TEXT;
ALTER TABLE vacancies ADD COLUMN qwen_recommendation TEXT;
ALTER TABLE vacancies ADD COLUMN qwen_model TEXT;
ALTER TABLE vacancies ADD COLUMN qwen_min_score INTEGER;
ALTER TABLE vacancies ADD COLUMN qwen_passed INTEGER;
