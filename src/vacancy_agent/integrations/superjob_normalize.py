"""Преобразование сырого ответа SuperJob API во внутреннюю модель `Vacancy`.

Источник полей — объект вакансии из `GET /2.0/vacancies/` (`objects[]`):
`id`, `profession`, `work`, `candidat`, `payment_from`/`payment_to`/
`currency`, `town.title`, `firm_name`/`client.title`, `link`,
`experience.title`, `type_of_work.title`, `date_published`. Официальная
документация SuperJob (`api.superjob.ru`) прямо называет `work` —
«Должностные обязанности», `candidat` — «Требования к кандидату»: `work`
→ `Vacancy.description`, `candidat` → `Vacancy.requirements` (по
аналогии с `hh_normalize.py`: `snippet.responsibility`→`description`,
`snippet.requirement`→`requirements`). До ревью-фикса оба текстовых
поля ошибочно сводились в один `candidat`→`description`, а `work`
(должностные обязанности — часто наиболее информативная для оценки
часть вакансии) не читался вовсе. SuperJob также отдаёт
`vacancyRichText` (HTML-версия текста) — не используется, т.к. текущий
пайплайн (Telegram-карточка, Qwen/Claude в будущих MVP) HTML не ожидает
— та же причина, по которой `hh_normalize.py` не протаскивает разметку
HH дальше нормализации.

`payment_from`/`payment_to`, равные `0`, по соглашению SuperJob означают
«не указано» для этой границы, а не нулевую зарплату — приводятся к
`None` независимо друг от друга (только эта конкретная граница, не обе
сразу), см. `Vacancy.salary_from`/`salary_to`.

`key_skills` в объекте SuperJob не приходит в поддерживаемом на этом
этапе наборе полей — остаётся пустым списком, как и в `hh_normalize.py`
для полей, которых нет в коротком представлении HH.

`work_format` берётся из `place_of_work.title` (не входило в
изначально согласованный набор полей задачи — добавлено по итогам
живого smoke-теста: без него `domain/filters.py` отклонял АБСОЛЮТНО
ВСЕ вакансии SuperJob с причиной `work_format_unknown`, независимо от
остальных критериев, и результат пайплайна был бы пустым). Как и для
HH, точная бакетизация (удалённо/гибрид/офис) остаётся на стороне
`domain/filters.py` — здесь только человекочитаемая строка, ничего не
канонизируется. Полный словарь значений `place_of_work` у SuperJob не
подтверждён вживую (эндпоинт `/dictionaries/place_of_work/` недоступен
напрямую) — тот же класс переносимого риска, что и HH-словарь формата
работы (`docs/DECISIONS.md`, ADR-007). `"Не имеет значения"` (частое
значение на реальных вакансиях) намеренно НЕ канонизируется здесь —
остаётся unknown/неблокирующим в `domain/filters.py`, не подгоняется
под «офис» (см. `docs/DECISIONS.md`, ADR-019).

`experience` — единственное поле, которое канонизируется на границе
источника (см. `_EXPERIENCE_MAP` ниже): `domain/filters.py`/
`domain/default_filter_profile.py` — source-agnostic и уже используют
канонические HH-формулировки («Нет опыта», «От 1 года до 3 лет», «От 3
до 6 лет», «Более 6 лет», см. `_check_experience`); живая диагностика
(`docs/DECISIONS.md`, ADR-019) показала, что SuperJob отдаёт другие
формулировки для того же смысла («Без опыта», «От 1 года», «От 3 лет»,
«От 6 лет») — без канонизации ВСЕ вакансии SuperJob отклонялись жёстким
фильтром с причиной `experience_not_allowed`, независимо от остальных
критериев. Канонизация — сознательно только для 4 подтверждённых вживую
значений; неизвестное будущее значение НЕ подгоняется под ближайшую
категорию (см. `_EXPERIENCE_MAP` про то, что происходит с
неотображённым значением).
"""

from datetime import UTC, datetime
from typing import Any

from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.integrations.superjob_errors import SuperJobNormalizationError

SOURCE_PREFIX = "superjob:"
"""`Vacancy.hh_id` — общее поле для всех источников (см. `docs/DECISIONS.md`,
ADR-014, «`hh_id` не переименован») и не гарантирует уникальность
числового `id` между источниками: SuperJob и HH используют независимые
пространства идентификаторов, и одно и то же число могло бы совпасть у
двух разных вакансий из разных источников, из-за чего `exists(hh_id)`
(`domain/ports.VacancyRepository`, дедупликация по `hh_id`) молча
принял бы вакансию одного источника за уже известную вакансию другого
(найдено ревью, см. ADR-016). Префикс источника делает `hh_id`
составным ключом без изменения его типа/схемы хранения (строка)."""

_EXPERIENCE_MAP = {
    "без опыта": "Нет опыта",
    "от 1 года": "От 1 года до 3 лет",
    "от 3 лет": "От 3 до 6 лет",
    "от 6 лет": "Более 6 лет",
}
"""SuperJob → каноническое `Vacancy.experience` (`docs/DECISIONS.md`,
ADR-019). Только 4 значения, подтверждённые живой диагностикой — не
полный официальный словарь SuperJob (недоступен напрямую, тот же
перенесённый риск, что и `place_of_work`, см. модульный docstring).
Значение, отсутствующее здесь, сознательно НЕ канонизируется и
НЕ отбрасывается — passthrough человекочитаемой строки как есть,
чтобы `domain/filters.py` дал явный `experience_not_allowed`
(наблюдаемый, а не молчаливый отказ), а не тихо попало в одну из
четырёх категорий по догадке."""


def _canonicalize_experience(raw_title: str | None) -> str | None:
    if raw_title is None:
        return None
    return _EXPERIENCE_MAP.get(raw_title.strip().lower(), raw_title)


def normalize_vacancy(item: dict[str, Any]) -> Vacancy:
    """Преобразует один элемент `objects` из ответа поиска SuperJob в `Vacancy`."""

    try:
        hh_id = str(item["id"])
        title = str(item["profession"])
        published_at = datetime.fromtimestamp(int(item["date_published"]), tz=UTC)

        town = item.get("town") or {}
        client = item.get("client") or {}
        experience = item.get("experience") or {}
        type_of_work = item.get("type_of_work") or {}
        place_of_work = item.get("place_of_work") or {}

        company = item.get("firm_name") or client.get("title")
        salary_from = _salary_bound(item.get("payment_from"))
        salary_to = _salary_bound(item.get("payment_to"))
        salary_currency = item.get("currency") or None
        description = item.get("work") or None
        requirements = item.get("candidat") or None
        experience_name = _canonicalize_experience(experience.get("title"))
        employment_name = type_of_work.get("title")
        area_name = town.get("title")
        work_format = [place_of_work["title"]] if place_of_work.get("title") else []
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise SuperJobNormalizationError(f"Не удалось разобрать вакансию SuperJob: {exc}") from exc

    url = item.get("link")
    if not url:
        raise SuperJobNormalizationError(f"У вакансии {hh_id} нет ссылки (link)")

    return Vacancy(
        hh_id=f"{SOURCE_PREFIX}{hh_id}",
        title=title,
        company=company,
        salary_from=salary_from,
        salary_to=salary_to,
        salary_currency=salary_currency,
        salary_gross=None,
        description=description,
        requirements=requirements,
        key_skills=[],
        experience=experience_name,
        employment=employment_name,
        work_format=work_format,
        area=area_name,
        url=str(url),
        published_at=published_at,
        fetched_at=datetime.now(UTC),
    )


def _salary_bound(value: float | int | None) -> float | None:
    """`0`/`None`/отсутствие поля — SuperJob-соглашение «граница не указана»."""

    if value is None:
        return None
    number = float(value)
    return number if number != 0 else None
