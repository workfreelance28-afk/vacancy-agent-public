"""Преобразование сырого ответа HH API во внутреннюю модель `Vacancy`.

Источник полей — короткое представление вакансии из поиска (см.
https://github.com/hhru/api/blob/master/docs/vacancies.md, раздел
«Короткое представление вакансии»). Полный текст описания и `key_skills`
там не приходят — они доступны только через отдельный запрос детальной
вакансии (`GET /vacancies/{id}`), который TASK-002 не делает: это
architectural-решение зафиксировано в `docs/DECISIONS.md` (ADR-007).
`description`/`requirements` заполняются из усечённого `snippet`, а
`key_skills` в TASK-002 обычно остаётся пустым списком.

Поля `snippet`, `schedule`, `work_format`, `employment` в ответе HH не гарантированы —
их отсутствие не считается ошибкой, соответствующее поле `Vacancy`
просто остаётся пустым/`None`. Но если такое поле присутствует в
неожиданной форме (например, элемент списка — не словарь), это
считается ошибкой нормализации (`HHNormalizationError`), а не
пропускается молча и не роняет вызывающий код сырым исключением.
"""

import re
from datetime import UTC, datetime
from typing import Any

from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.integrations.hh_errors import HHNormalizationError

_HIGHLIGHT_TAG_RE = re.compile(r"</?highlighttext>")


def normalize_vacancy(item: dict[str, Any]) -> Vacancy:
    """Преобразует один элемент `items` из ответа поиска HH в `Vacancy`.

    `hh_id` берётся из `item["id"]` без какого-либо преобразования смысла.
    """

    try:
        hh_id = str(item["id"])
        title = str(item["name"])
        published_at = datetime.fromisoformat(str(item["published_at"]))

        salary = item.get("salary") or {}
        employer = item.get("employer") or {}
        area = item.get("area") or {}
        experience = item.get("experience") or {}
        employment = item.get("employment") or {}
        snippet = item.get("snippet") or {}
        key_skills = [
            skill["name"] for skill in item.get("key_skills") or [] if skill.get("name")
        ]
        work_format = _work_format(item)

        company = employer.get("name")
        salary_from = salary.get("from")
        salary_to = salary.get("to")
        salary_currency = salary.get("currency")
        salary_gross = salary.get("gross")
        description = _clean_snippet(snippet.get("responsibility"))
        requirements = _clean_snippet(snippet.get("requirement"))
        experience_name = experience.get("name")
        employment_name = employment.get("name")
        area_name = area.get("name")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        # Ловим не только KeyError/ValueError обязательных полей, но и
        # TypeError/AttributeError из необязательных секций (`key_skills`,
        # `work_format`, `salary`, ...) — их форма в реальном ответе HH не
        # подтверждена эмпирически (см. `docs/DECISIONS.md`, ADR-007), и
        # неожиданная форма не должна ронять вызывающий код сырым исключением.
        raise HHNormalizationError(f"Не удалось разобрать вакансию HH: {exc}") from exc

    url = item.get("alternate_url") or item.get("url")
    if not url:
        raise HHNormalizationError(f"У вакансии {hh_id} нет ни alternate_url, ни url")

    return Vacancy(
        hh_id=hh_id,
        title=title,
        company=company,
        salary_from=salary_from,
        salary_to=salary_to,
        salary_currency=salary_currency,
        salary_gross=salary_gross,
        description=description,
        requirements=requirements,
        key_skills=key_skills,
        experience=experience_name,
        employment=employment_name,
        work_format=work_format,
        area=area_name,
        url=str(url),
        published_at=published_at,
        fetched_at=datetime.now(UTC),
    )


def _clean_snippet(text: str | None) -> str | None:
    if text is None:
        return None
    return _HIGHLIGHT_TAG_RE.sub("", text)


def _work_format(item: dict[str, Any]) -> list[str]:
    work_format = item.get("work_format")
    if work_format:
        return [wf["name"] for wf in work_format if wf.get("name")]
    schedule = item.get("schedule")
    if schedule and schedule.get("name"):
        return [schedule["name"]]
    return []
