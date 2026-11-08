"""Детерминированный разбор публичных HTML-страниц career.habr.com
(TASK-013, ADR-056) — чистые функции без сети и без Qwen.

career.habr.com не предоставляет официального API публичного поиска
(ADR-056) — единственный источник данных здесь — HTML, который отдаёт
браузеру сам сайт. Это НЕСТАБИЛЬНЫЙ контракт: в отличие от HH/SuperJob
(`hh_normalize.py`/`superjob_normalize.py`), где поля документированы
официальным API, разметка ниже частично подтверждена контролируемым live
smoke-test'ом Codex (`docs/TASK-013.md`) — селекторы, помеченные ниже как
подтверждённые, взяты из него; остальные (например, `_SELECTOR_DETAIL_
DESCRIPTION`) остаются лучшим известным предположением, ещё не
подтверждённым отдельно. Claude Code сам реальных запросов к Habr
по-прежнему не выполняет.

Live smoke-test также нашёл JSON-LD блок типа `JobPosting` на detail-
странице — потенциально более надёжный источник структурированных данных,
чем CSS-селекторы. Сознательно не используется здесь: задача явно просит
не расширять scope этого раунда без необходимости — переход на JSON-LD
остаётся отдельным будущим решением, не тихой заменой уже работающего
CSS-разбора.

Все CSS-селекторы вынесены в константы `_SELECTOR_*` в начале модуля —
единственное место, которое должно поменяться, если реальная разметка
отличается; сама логика разбора (обязательные/опциональные поля, форматы
дат/зарплаты, детект пустого результата/CAPTCHA) не должна.

Обязательные поля (`external_id`/`title`/`url`/`published_at` — все
нужны `domain.vacancy.Vacancy`, которая не допускает наивный/отсутствующий
`published_at`) при отсутствии поднимают `HabrCareerParsingError`, а не
угадываются и не пропускаются молча (TASK-013: «Обязательные поля...
не угадываются: при их отсутствии элемент отклоняется типизированной
ошибкой»). Зарплата и город/формат работы — опциональные: распознаются
только по подтверждённым форматам ниже, неизвестный формат остаётся
`None`/пустым списком, не домысливается."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.integrations.habr_errors import HabrCareerParsingError

_DEFAULT_ORIGIN = "https://career.habr.com"

SOURCE_PREFIX = "habr:"
"""`Vacancy.hh_id` — общее поле для всех источников (см. `superjob_normalize.
SOURCE_PREFIX` — тот же приём: составной ключ `habr:<external_id>`
защищает `exists(hh_id)`/дедупликацию от совпадения числовых ID между
независимыми пространствами SuperJob/HH/Habr, TASK-013: "ID всегда
`habr:<id>`"."""

# --- Селекторы (см. module docstring — единственное место для правки
# после живой проверки реальной разметки Codex) ---
#
# Помеченные «(подтверждено)» ниже взяты дословно из контролируемого live
# smoke-test'а Codex (`docs/TASK-013.md`, 2026-08-30) — не догадка.

_SELECTOR_VACANCY_CARD = ".vacancy-card"
_SELECTOR_CARD_TITLE_LINK = "a.vacancy-card__title-link"
_SELECTOR_CARD_COMPANY = ".vacancy-card__company a.link-comp--appearance-dark"  # подтверждено
_SELECTOR_CARD_TIME = "time[datetime]"
_SELECTOR_CARD_SALARY = ".vacancy-card__salary, .basic-salary"
_SELECTOR_CARD_META = ".vacancy-card__meta"
# Единственный подтверждённый сигнал того, что конкретный chip внутри
# `.vacancy-card__meta` означает формат работы (удалённо/офис/гибрид), не
# что-то другое вроде грейда или специализации — живой smoke-test нашёл,
# что грейд `Lead` наивно принимался за город (см. `_parse_meta` ниже).
_SELECTOR_CARD_FORMAT_CHIP_ICON = ".svg-icon--icon-format"  # подтверждено

_SELECTOR_DETAIL_DESCRIPTION = ".vacancy-description__text, .task-section__body"
_SELECTOR_DETAIL_COMPANY = ".vacancy-company__title"  # подтверждено
_SELECTOR_DETAIL_META = ".vacancy-header .vacancy-meta"  # подтверждено
_SELECTOR_DETAIL_SKILLS = ".vacancy-header .vacancy-meta .chip-without-icon__text"  # подтверждено
# Codex review-fix [P1] (TASK-013, предыдущий раунд): без обязательного
# структурного маркера `parse_vacancy_detail_page` принимала ЛЮБОЙ HTML
# (пустую страницу, 404-заглушку, постороннюю разметку) и молча собирала
# `Vacancy` со всеми detail-полями пустыми — нарушение fail-closed
# требования TASK-013.
#
# [P1] повторное ревью нашло: сам маркер до этой правки включал
# `h1.page-title__title, .vacancy-title` — НЕподтверждённые, обобщённо
# звучащие классы (тот же класс риска, что и голый `.link-comp` в более
# раннем раунде): career.habr.com — общая платформа, и «заголовок
# страницы» правдоподобно переиспользуется на страницах компаний,
# статей, поиска — не только на detail-странице вакансии. Такая страница
# проходила бы проверку и превращалась в `Vacancy` с пустыми detail-
# полями. Маркер теперь построен ИСКЛЮЧИТЕЛЬНО из селекторов, дословно
# подтверждённых live smoke-test'ом Codex и специфичных для вакансии по
# имени класса (`vacancy-company__title`, `vacancy-header`/`vacancy-meta`)
# — `_SELECTOR_DETAIL_DESCRIPTION` тоже остаётся неподтверждённым и
# сознательно исключён отсюда (по-прежнему используется только для
# извлечения `description`, не как доказательство «это вакансия»).
_SELECTOR_DETAIL_PAGE_MARKER = _SELECTOR_DETAIL_COMPANY + ", " + _SELECTOR_DETAIL_META

_NO_RESULTS_MARKERS = (
    "ничего не найдено",
    "по вашему запросу ничего",
    "вакансии не найдены",
)
_BLOCKED_MARKERS = (
    "captcha",
    "подтвердите, что вы не робот",
    "проверка браузера",
    "доступ ограничен",
    "unusual traffic",
)

# Живой smoke-test нашёл: блок зарплаты для вакансий без указанной оплаты
# содержит не только «Зарплата не указана», но и прогноз «Похожие
# специалисты получают N — M ₽» — прежний regex-разбор находил диапазон
# внутри ЭТОГО прогноза и ошибочно принимал его за зарплату вакансии.
# Любой из этих маркеров в тексте — сигнал «здесь нет собственной зарплаты
# вакансии», разбор диапазона/границ дальше не имеет смысла.
_SALARY_NOT_SPECIFIED_MARKERS = ("зарплата не указана", "похожие специалисты")

_CURRENCY_SYMBOLS = {"₽": "RUR", "$": "USD", "€": "EUR"}
_SALARY_RANGE_RE = re.compile(
    r"(?P<from>\d[\d\s\xa0]*)\s*[-–—]\s*(?P<to>\d[\d\s\xa0]*)\s*(?P<currency>[₽$€])"
)
_SALARY_FROM_RE = re.compile(r"от\s*(?P<from>\d[\d\s\xa0]*)\s*(?P<currency>[₽$€])", re.IGNORECASE)
_SALARY_TO_RE = re.compile(r"до\s*(?P<to>\d[\d\s\xa0]*)\s*(?P<currency>[₽$€])", re.IGNORECASE)


@dataclass(frozen=True)
class HabrListing:
    """Поля, извлекаемые со страницы списка (TASK-013: «ID, ссылка,
    название, компания, дата, зарплата, город/удалённость»). Внутренняя
    структура — не публичный домен: `parse_vacancy_detail_page` дополняет
    её описанием/требованиями/навыками и собирает итоговый `Vacancy`."""

    external_id: str
    url: str
    title: str
    company: str | None
    published_at: datetime
    salary_from: float | None
    salary_to: float | None
    salary_currency: str | None
    area: str | None
    work_format: list[str]


def page_looks_blocked(html: str) -> bool:
    """`True`, если страница — известная антибот/CAPTCHA-заглушка, не
    реальный контент (TASK-013: «CAPTCHA... дают явную типизированную
    ошибку», «без обхода антибот-защиты» — эта функция только
    ОБНАРУЖИВАЕТ блокировку, ничего не делает, чтобы её обойти)."""

    normalized = html.lower()
    return any(marker in normalized for marker in _BLOCKED_MARKERS)


def parse_search_results_page(
    html: str, expected_origin: str = _DEFAULT_ORIGIN
) -> list[HabrListing]:
    """Разбирает одну страницу `/vacancies?q=...&type=all&page=N`.

    Пустой результат допустим ТОЛЬКО при явном маркере «ничего не
    найдено» (TASK-013) — иначе (ни одной карточки, ни маркера) это
    считается изменением разметки, не нулём вакансий, и поднимает
    `HabrCareerParsingError`.

    `expected_origin` — схема+хост, которым обязана принадлежать любая
    АБСОЛЮТНАЯ ссылка на вакансию (Codex review-fix [P2]: относительный
    href, ведущий на посторонний домен под видом `/vacancies/<id>`, иначе
    заставил бы клиент выполнить запрос за пределы career.habr.com —
    вне заявленного TASK-013 scope). `HabrCareerSource` передаёт сюда
    настроенный `base_url`; тесты полагаются на дефолт."""

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(_SELECTOR_VACANCY_CARD)

    if not cards:
        normalized = soup.get_text(" ", strip=True).lower()
        if any(marker in normalized for marker in _NO_RESULTS_MARKERS):
            return []
        raise HabrCareerParsingError(
            "Страница списка вакансий Habr не содержит ни одной карточки "
            "и ни маркера «ничего не найдено» — возможно, разметка изменилась"
        )

    return [_parse_card(card, expected_origin) for card in cards]


def parse_vacancy_detail_page(html: str, listing: HabrListing) -> Vacancy:
    """Разбирает detail-страницу одной вакансии и собирает итоговый
    `Vacancy`, дополняя `listing` описанием/требованиями/навыками.

    `description`/`requirements`/`key_skills`, отсутствующие или не
    распознанные в разметке, остаются `None`/пустым списком — TASK-013
    прямо запрещает домысливать отсутствующие поля. Но страница ЦЕЛИКОМ
    обязана содержать хотя бы один ожидаемый структурный маркер
    (`_SELECTOR_DETAIL_PAGE_MARKER`) — иначе это не detail-страница
    вакансии Habr вовсе (пустая/404/посторонняя разметка), а не вакансия
    без опциональных полей (Codex review-fix [P1])."""

    soup = BeautifulSoup(html, "html.parser")

    if soup.select_one(_SELECTOR_DETAIL_PAGE_MARKER) is None:
        raise HabrCareerParsingError(
            f"Detail-страница Habr {listing.url!r} не содержит ни одного "
            "ожидаемого элемента — вероятно, разметка изменилась или это не "
            "страница вакансии"
        )

    description = _first_text(soup, _SELECTOR_DETAIL_DESCRIPTION)
    # Live smoke-test (TASK-013, 2026-08-30): factual requirements на Habr
    # — не отдельная секция "Требования" (прежняя, ещё не подтверждённая
    # эвристика по заголовку), а сам блок `.vacancy-header .vacancy-meta`
    # (грейд, стаж, формат занятости/работы, навыки-чипы) — читаем его
    # целиком, с пробелом между элементами, чтобы соседние чипы не
    # склеивались в одно слово.
    requirements = _detail_meta_text(soup)
    key_skills = [
        text
        for tag in soup.select(_SELECTOR_DETAIL_SKILLS)
        if (text := tag.get_text(strip=True))
    ]
    # Live smoke-test: `.vacancy-card__company-title` (прежний, неверный
    # селектор списка) не давал компанию вовсе — `listing.company` часто
    # уже `None` к этому моменту. Detail-страница — безопасный fallback:
    # используется, только когда список ничего не дал, не переопределяет
    # уже известное значение.
    company = listing.company or _first_text(soup, _SELECTOR_DETAIL_COMPANY)

    return Vacancy(
        hh_id=f"{SOURCE_PREFIX}{listing.external_id}",
        title=listing.title,
        company=company,
        salary_from=listing.salary_from,
        salary_to=listing.salary_to,
        salary_currency=listing.salary_currency,
        salary_gross=None,
        description=description,
        requirements=requirements,
        key_skills=key_skills,
        experience=None,
        employment=None,
        work_format=listing.work_format,
        area=listing.area,
        url=listing.url,
        published_at=listing.published_at,
        fetched_at=datetime.now(UTC),
    )


def _parse_card(card: Tag, expected_origin: str) -> HabrListing:
    link = card.select_one(_SELECTOR_CARD_TITLE_LINK)
    href = link.get("href") if link is not None else None
    title = link.get_text(strip=True) if link is not None else ""

    if link is None or not href or not title:
        raise HabrCareerParsingError(
            "Карточка вакансии Habr без ссылки/названия — обязательное поле отсутствует"
        )

    external_id = _external_id_from_href(str(href))
    if external_id is None:
        raise HabrCareerParsingError(
            f"Не удалось извлечь ID вакансии Habr из ссылки {href!r}"
        )

    time_tag = card.select_one(_SELECTOR_CARD_TIME)
    published_at = _parse_datetime_attr(time_tag)
    if published_at is None:
        raise HabrCareerParsingError(
            f"У вакансии Habr {external_id!r} нет распознаваемой даты публикации"
        )

    company = _first_text(card, _SELECTOR_CARD_COMPANY)
    salary_text = _first_text(card, _SELECTOR_CARD_SALARY)
    salary_from, salary_to, salary_currency = _parse_salary(salary_text)
    area, work_format = _parse_meta(card)

    return HabrListing(
        external_id=external_id,
        url=_absolute_url(str(href), expected_origin),
        title=title,
        company=company,
        published_at=published_at,
        salary_from=salary_from,
        salary_to=salary_to,
        salary_currency=salary_currency,
        area=area,
        work_format=work_format,
    )


_VACANCY_ID_RE = re.compile(r"/vacancies/(\d+)")


def _external_id_from_href(href: str) -> str | None:
    match = _VACANCY_ID_RE.search(href)
    return match.group(1) if match else None


def _absolute_url(href: str, expected_origin: str) -> str:
    """Относительные ссылки становятся абсолютными относительно
    `expected_origin`; абсолютные — принимаются только если их
    схема+хост совпадают с `expected_origin`, иначе `HabrCareerParsingError`
    (Codex review-fix [P2]: без этой проверки посторонний абсолютный href
    вида `https://evil.example/vacancies/1` заставил бы `HabrCareerSource`
    выполнить запрос за пределы career.habr.com — задача ограничивает
    источник только его публичными страницами)."""

    if href.startswith("http://") or href.startswith("https://"):
        parsed = urlsplit(href)
        expected = urlsplit(expected_origin)
        if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
            raise HabrCareerParsingError(
                f"Ссылка на вакансию Habr ведёт на посторонний домен: {href!r}"
            )
        return href
    return f"{expected_origin.rstrip('/')}{href}"


def _parse_datetime_attr(tag: Tag | None) -> datetime | None:
    if tag is None:
        return None
    raw = tag.get("datetime")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _first_text(node: Tag, selector: str) -> str | None:
    found = node.select_one(selector)
    if found is None:
        return None
    text = found.get_text(strip=True)
    return text or None


def _parse_meta(card: Tag) -> tuple[str | None, list[str]]:
    """Live smoke-test (TASK-013, 2026-08-30): предыдущая версия брала
    «первый chip, не похожий на маркер удалёнки» за город — грейд `Lead`
    ошибочно становился `area`. Подтверждённого селектора города в этом
    списке нет — `area` сознательно не заполняется отсюда, пока такой
    селектор не появится (не угадывать лучше, чем угадать неверно).

    `work_format` (удалённо/офис/гибрид) заполняется ТОЛЬКО по
    подтверждённому сигналу — chip, содержащий иконку
    `_SELECTOR_CARD_FORMAT_CHIP_ICON`; текст этого chip и есть значение
    формата работы.

    Повторное ревью Codex [P1]: сигнал выбран верно, но `icon.parent` —
    непосредственный родитель `<svg>` в реальной разметке — это пустой
    контейнер-обёртка самой иконки, не chip с текстом; текст лежит
    уровнем выше, во внешнем `.basic-chip`. Упрощённая тестовая fixture
    (иконка и текст — прямые соседи в одном узле) этого не поймала.
    `find_parent(class_="basic-chip")` поднимается до подтверждённого
    контейнера chip'а вместо непосредственного `.parent`."""

    meta = card.select_one(_SELECTOR_CARD_META)
    if meta is None:
        return None, []

    work_format: list[str] = []
    for icon in meta.select(_SELECTOR_CARD_FORMAT_CHIP_ICON):
        chip = icon.find_parent(class_="basic-chip")
        if chip is None:
            continue
        text = chip.get_text(strip=True)
        if text:
            work_format.append(text)

    return None, work_format


def _detail_meta_text(soup: BeautifulSoup) -> str | None:
    meta = soup.select_one(_SELECTOR_DETAIL_META)
    if meta is None:
        return None
    text = meta.get_text(" ", strip=True)
    return text or None


def _parse_salary(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Распознаёт только подтверждённые форматы («150 000 – 250 000 ₽»,
    «от 150 000 ₽», «до 250 000 ₽») — любой другой текст (например, «По
    договорённости») даёт `(None, None, None)`, не ошибку и не догадку.

    Live smoke-test (TASK-013, 2026-08-30): блок без указанной зарплаты
    содержит не только «Зарплата не указана», но и прогноз «Похожие
    специалисты получают N — M ₽» — без явной проверки на эти маркеры
    regex ниже находил диапазон ВНУТРИ прогноза и ошибочно принимал его за
    зарплату самой вакансии."""

    if not text:
        return None, None, None

    normalized_lower = text.lower()
    if any(marker in normalized_lower for marker in _SALARY_NOT_SPECIFIED_MARKERS):
        return None, None, None

    normalized = text.replace("\xa0", " ")

    range_match = _SALARY_RANGE_RE.search(normalized)
    if range_match:
        currency = _CURRENCY_SYMBOLS.get(range_match.group("currency"))
        return (
            _to_amount(range_match.group("from")),
            _to_amount(range_match.group("to")),
            currency,
        )

    from_match = _SALARY_FROM_RE.search(normalized)
    if from_match:
        currency = _CURRENCY_SYMBOLS.get(from_match.group("currency"))
        return _to_amount(from_match.group("from")), None, currency

    to_match = _SALARY_TO_RE.search(normalized)
    if to_match:
        currency = _CURRENCY_SYMBOLS.get(to_match.group("currency"))
        return None, _to_amount(to_match.group("to")), currency

    return None, None, None


def _to_amount(digits: str) -> float:
    return float(digits.replace(" ", "").replace("\xa0", ""))
