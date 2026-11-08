# Первоначальные контракты данных

Это концептуальные контракты. Точные Python-модели и схема БД утверждаются при реализации и должны сохранить смысл полей.

## Vacancy

```json
{
  "hh_id": "string",
  "title": "string",
  "company": "string|null",
  "salary_from": "number|null",
  "salary_to": "number|null",
  "salary_currency": "string|null",
  "salary_gross": "boolean|null",
  "description": "string|null",
  "requirements": "string|null",
  "key_skills": ["string"],
  "experience": "string|null",
  "employment": "string|null",
  "work_format": ["string"],
  "area": "string|null",
  "url": "string",
  "published_at": "datetime",
  "fetched_at": "datetime"
}
```

Денежные значения нельзя сравнивать без учёта валюты и признака gross/net. Отсутствующая зарплата должна обрабатываться отдельным настраиваемым правилом.

## FilterResult

```json
{
  "accepted": true,
  "reasons": ["target_title_match"],
  "rejected_by": []
}
```

Причины должны быть машинно-читаемыми кодами, а не только текстом для пользователя.

## VacancyAssessment

```json
{
  "schema_version": "1",
  "score": 84,
  "relevant": true,
  "strengths": [
    {"skill": "API", "evidence": "Факт из профиля"}
  ],
  "gaps": [
    {"skill": "Docker", "importance": "medium"}
  ],
  "risks": ["string"],
  "recommendation": "review"
}
```

Ограничения:

- `score`: целое число от 0 до 100;
- `recommendation`: фиксированное перечисление, например `skip`, `review`, `strong_match`;
- каждый strength требует основания;
- невалидный JSON не сохраняется как успешная оценка;
- исходный ответ модели при необходимости хранится отдельно от доверенной модели данных.

## Статусы вакансии

```text
new → viewed → preparing → applied → interview → offer
  └────→ skipped          └────────→ rejected
```

Не все переходы обязательны, но каждый переход должен быть явным и аудируемым. Точная таблица допустимых переходов утверждается в MVP 4; до этого MVP использует только необходимые статусы.

## ApplicationDraft

Минимальные разделы будущего результата backend подготовки отклика:

- идентификатор вакансии;
- резюме соответствия;
- полностью закрытые требования с доказательствами;
- частично закрытые требования;
- реальные пробелы;
- предлагаемые перестановки и формулировки резюме;
- сопроводительное письмо;
- предупреждения о фактах, требующих подтверждения;
- версия Master Profile, использованная при генерации.

Актуальный local-first контракт утверждён для реализации в
`TASK-011.md`: model-owned `ApplicationDraftContent` помещается
в Python-owned `ApplicationDraft` с `schema_version`, `vacancy_id` и
`profile_version`. Отклонённый Claude-вариант в `TASK-010.md`
сохранён только как исторический draft. Каждое положительное
утверждение/предложение
содержит `evidence_fact_ids`, которые Python проверяет по версионированному
профилю; результат хранится и показывается только как черновик.
