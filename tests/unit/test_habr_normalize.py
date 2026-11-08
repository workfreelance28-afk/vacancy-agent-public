import pytest

from vacancy_agent.integrations.habr_errors import HabrCareerParsingError
from vacancy_agent.integrations.habr_normalize import (
    HabrListing,
    page_looks_blocked,
    parse_search_results_page,
    parse_vacancy_detail_page,
)


def _format_chip_html(text: str) -> str:
    """Chip формата работы — с иконкой `.svg-icon--icon-format` (TASK-013
    live smoke-test, 2026-08-30, единственный подтверждённый сигнал).

    Реалистичная DOM-структура (Codex повторное ревью [P1]): иконка живёт
    в СОБСТВЕННОЙ пустой обёртке, текст — в соседнем элементе, оба внутри
    `.basic-chip`. Прежняя упрощённая fixture (иконка и текст в одном
    узле) не поймала бы находку: там `icon.parent` случайно совпадал бы с
    узлом, содержащим текст — код с этой же ошибкой ложно проходил бы
    тест, который должен был её ловить."""

    return (
        '<div class="basic-chip">'
        '<span class="icon-wrapper"><svg class="svg-icon svg-icon--icon-format"></svg></span>'
        f'<span class="chip-text">{text}</span>'
        "</div>"
    )


def _plain_chip_html(text: str) -> str:
    """Chip без иконки формата — грейд/специализация/стаж и т.п. Не
    должен становиться ни `area`, ни `work_format` (live smoke-test нашёл:
    грейд «Lead» ошибочно принимался за город прежней эвристикой). Та же
    `.basic-chip`-структура, что и у `_format_chip_html`, без иконки."""

    return f'<div class="basic-chip"><span class="chip-text">{text}</span></div>'


def _card_html(
    *,
    external_id: str = "1000123",
    title: str = "AI Integration Engineer",
    company: str | None = "Acme Corp",
    published_at: str | None = "2026-08-20T10:00:00+03:00",
    salary: str | None = "150 000 – 250 000 ₽",
    format_chips: tuple[str, ...] = ("Можно удалённо",),
    other_chips: tuple[str, ...] = ("Lead",),
    href: str | None = None,
) -> str:
    link_href = href if href is not None else f"/vacancies/{external_id}"
    # `.vacancy-card__company a.link-comp--appearance-dark` — подтверждено
    # live smoke-test'ом (TASK-013, 2026-08-30); прежний
    # `.vacancy-card__company-title` компанию не давал вовсе.
    company_html = (
        f'<div class="vacancy-card__company">'
        f'<a class="link-comp link-comp--appearance-dark" href="/companies/acme">{company}</a>'
        f"</div>"
        if company is not None
        else ""
    )
    time_html = f'<time datetime="{published_at}"></time>' if published_at is not None else ""
    salary_html = f'<div class="vacancy-card__salary">{salary}</div>' if salary else ""
    chips_html = "".join(_plain_chip_html(chip) for chip in other_chips) + "".join(
        _format_chip_html(chip) for chip in format_chips
    )
    return f"""
    <div class="vacancy-card">
      <div class="vacancy-card__title">
        <a class="vacancy-card__title-link" href="{link_href}">{title}</a>
      </div>
      {company_html}
      {time_html}
      {salary_html}
      <div class="vacancy-card__meta">{chips_html}</div>
    </div>
    """


def _list_page(cards: list[str], *, empty_marker: str | None = None) -> str:
    body = "".join(cards)
    marker_html = f"<div>{empty_marker}</div>" if empty_marker else ""
    return f"<html><body>{marker_html}{body}</body></html>"


def _listing(**overrides: object) -> HabrListing:
    from datetime import UTC, datetime

    defaults: dict[str, object] = {
        "external_id": "1000123",
        "url": "https://career.habr.com/vacancies/1000123",
        "title": "AI Integration Engineer",
        "company": "Acme Corp",
        "published_at": datetime(2026, 8, 20, 10, 0, 0, tzinfo=UTC),
        "salary_from": None,
        "salary_to": None,
        "salary_currency": None,
        "area": None,
        "work_format": [],
    }
    defaults.update(overrides)
    return HabrListing(**defaults)  # type: ignore[arg-type]


# --- parse_search_results_page: required fields ---


def test_parses_required_fields_from_single_card() -> None:
    html = _list_page([_card_html()])

    listings = parse_search_results_page(html)

    assert len(listings) == 1
    listing = listings[0]
    assert listing.external_id == "1000123"
    assert listing.url == "https://career.habr.com/vacancies/1000123"
    assert listing.title == "AI Integration Engineer"


def test_parses_multiple_cards_in_document_order() -> None:
    html = _list_page(
        [_card_html(external_id="1", title="First"), _card_html(external_id="2", title="Second")]
    )

    listings = parse_search_results_page(html)

    assert [listing.title for listing in listings] == ["First", "Second"]


def test_absolute_href_is_kept_as_is() -> None:
    html = _list_page([_card_html(href="https://career.habr.com/vacancies/999")])

    listings = parse_search_results_page(html)

    assert listings[0].url == "https://career.habr.com/vacancies/999"


def test_missing_title_link_raises_parsing_error() -> None:
    card = """
    <div class="vacancy-card">
      <time datetime="2026-08-20T10:00:00+03:00"></time>
    </div>
    """
    html = _list_page([card])

    with pytest.raises(HabrCareerParsingError):
        parse_search_results_page(html)


def test_href_without_recognizable_vacancy_id_raises_parsing_error() -> None:
    html = _list_page([_card_html(href="/companies/acme")])

    with pytest.raises(HabrCareerParsingError):
        parse_search_results_page(html)


def test_missing_published_at_raises_parsing_error() -> None:
    html = _list_page([_card_html(published_at=None)])

    with pytest.raises(HabrCareerParsingError):
        parse_search_results_page(html)


def test_unparseable_datetime_attribute_raises_parsing_error() -> None:
    html = _list_page([_card_html(published_at="not-a-date")])

    with pytest.raises(HabrCareerParsingError):
        parse_search_results_page(html)


# --- parse_search_results_page: optional fields ---


def test_extracts_company_from_confirmed_card_selector() -> None:
    """TASK-013 live smoke-test (2026-08-30): `.vacancy-card__company
    a.link-comp--appearance-dark` подтверждён; прежний
    `.vacancy-card__company-title` компанию не давал вовсе."""

    html = _list_page([_card_html(company="Acme Corp")])

    listings = parse_search_results_page(html)

    assert listings[0].company == "Acme Corp"


def test_missing_company_stays_none() -> None:
    html = _list_page([_card_html(company=None)])

    listings = parse_search_results_page(html)

    assert listings[0].company is None


def test_missing_salary_stays_none() -> None:
    html = _list_page([_card_html(salary=None)])

    listings = parse_search_results_page(html)

    assert listings[0].salary_from is None
    assert listings[0].salary_to is None
    assert listings[0].salary_currency is None


def test_salary_range_is_parsed() -> None:
    html = _list_page([_card_html(salary="150 000 – 250 000 ₽")])

    listing = parse_search_results_page(html)[0]

    assert listing.salary_from == 150_000
    assert listing.salary_to == 250_000
    assert listing.salary_currency == "RUR"


def test_salary_from_only_is_parsed() -> None:
    html = _list_page([_card_html(salary="от 150 000 ₽")])

    listing = parse_search_results_page(html)[0]

    assert listing.salary_from == 150_000
    assert listing.salary_to is None
    assert listing.salary_currency == "RUR"


def test_salary_to_only_is_parsed() -> None:
    html = _list_page([_card_html(salary="до 250 000 ₽")])

    listing = parse_search_results_page(html)[0]

    assert listing.salary_from is None
    assert listing.salary_to == 250_000
    assert listing.salary_currency == "RUR"


def test_unrecognized_salary_text_stays_none_not_an_error() -> None:
    """TASK-013: «неизвестное значение остаётся `None`, без домыслов» —
    «По договорённости» — валидное, часто встречающееся значение, не
    ошибка разметки."""

    html = _list_page([_card_html(salary="По договорённости")])

    listing = parse_search_results_page(html)[0]

    assert listing.salary_from is None
    assert listing.salary_to is None
    assert listing.salary_currency is None


def test_salary_currency_symbol_not_in_allow_list_stays_none() -> None:
    html = _list_page([_card_html(salary="150 000 – 250 000 £")])

    listing = parse_search_results_page(html)[0]

    assert listing.salary_currency is None


def test_similar_specialists_forecast_is_not_mistaken_for_the_vacancy_salary() -> None:
    """TASK-013 live smoke-test (2026-08-30): точное воспроизведение
    находки — блок без указанной зарплаты содержит и «Зарплата не
    указана», и прогноз «Похожие специалисты получают N — M ₽». Прежний
    regex-разбор находил диапазон ВНУТРИ прогноза и ошибочно принимал его
    за зарплату самой вакансии."""

    salary_text = "Зарплата не указана Похожие специалисты получают 395 000 - 611 000 ₽"
    html = _list_page([_card_html(salary=salary_text)])

    listing = parse_search_results_page(html)[0]

    assert listing.salary_from is None
    assert listing.salary_to is None
    assert listing.salary_currency is None


def test_similar_specialists_forecast_alone_is_not_mistaken_for_the_vacancy_salary() -> None:
    """То же, но без явного префикса «Зарплата не указана» — сам факт
    «Похожие специалисты получают...» уже означает, что это не зарплата
    вакансии."""

    html = _list_page([_card_html(salary="Похожие специалисты получают 395 000 - 611 000 ₽")])

    listing = parse_search_results_page(html)[0]

    assert listing.salary_from is None
    assert listing.salary_to is None
    assert listing.salary_currency is None


def test_work_format_comes_only_from_confirmed_format_chip_icon() -> None:
    """TASK-013 live smoke-test (2026-08-30): «Можно удалённо» подтверждён
    как chip с иконкой `.svg-icon--icon-format` — единственный сигнал
    формата работы."""

    html = _list_page([_card_html(format_chips=("Можно удалённо",), other_chips=())])

    listing = parse_search_results_page(html)[0]

    assert listing.work_format == ["Можно удалённо"]


def test_work_format_text_read_from_basic_chip_ancestor_not_icon_immediate_parent() -> None:
    """Codex повторное ревью [P1]: сигнал (иконка `.svg-icon--icon-format`)
    выбран верно, но `icon.parent` в реальной разметке — пустая обёртка
    самой иконки, не chip с текстом; текст лежит уровнем выше, во внешнем
    `.basic-chip`. Явное прямое воспроизведение реальной DOM-структуры
    (иконка и текст — РАЗНЫЕ узлы внутри общего `.basic-chip`), не
    упрощённая fixture, которая не поймала бы эту находку."""

    card = """
    <div class="vacancy-card">
      <a class="vacancy-card__title-link" href="/vacancies/1000123">T</a>
      <time datetime="2026-08-20T10:00:00+03:00"></time>
      <div class="vacancy-card__meta">
        <div class="basic-chip">
          <span class="icon-wrapper">
            <svg class="svg-icon svg-icon--icon-format"></svg>
          </span>
          <span class="chip-text">Можно удалённо</span>
        </div>
      </div>
    </div>
    """
    html = _list_page([card])

    listing = parse_search_results_page(html)[0]

    assert listing.work_format == ["Можно удалённо"]


def test_chip_without_format_icon_is_not_treated_as_work_format() -> None:
    html = _list_page([_card_html(format_chips=(), other_chips=("Полная занятость",))])

    listing = parse_search_results_page(html)[0]

    assert listing.work_format == []


def test_grade_chip_is_not_mistaken_for_a_city() -> None:
    """TASK-013 live smoke-test: грейд `Lead` — не город. Прежняя эвристика
    («первый chip, не похожий на маркер удалёнки») ошибочно принимала его
    за `area`. Подтверждённого селектора города нет — `area` остаётся
    `None`, а не берётся наугад из соседнего chip."""

    html = _list_page([_card_html(format_chips=("Можно удалённо",), other_chips=("Lead",))])

    listing = parse_search_results_page(html)[0]

    assert listing.area is None
    assert listing.work_format == ["Можно удалённо"]


def test_area_is_never_populated_from_meta_chips() -> None:
    """Нет подтверждённого селектора города в chip-списке (TASK-013 live
    smoke-test) — `area` сознательно всегда `None` со страницы списка,
    даже когда chips присутствуют, пока такой селектор не появится."""

    html = _list_page(
        [_card_html(format_chips=("Можно удалённо",), other_chips=("Senior", "Python"))]
    )

    listing = parse_search_results_page(html)[0]

    assert listing.area is None


def test_missing_meta_stays_empty() -> None:
    card = """
    <div class="vacancy-card">
      <a class="vacancy-card__title-link" href="/vacancies/1">T</a>
      <time datetime="2026-08-20T10:00:00+03:00"></time>
    </div>
    """
    html = _list_page([card])

    listing = parse_search_results_page(html)[0]

    assert listing.area is None
    assert listing.work_format == []


# --- parse_search_results_page: empty result vs markup change ---


def test_empty_page_with_no_results_marker_returns_empty_list() -> None:
    html = _list_page([], empty_marker="К сожалению, по вашему запросу ничего не найдено")

    assert parse_search_results_page(html) == []


def test_empty_page_without_marker_raises_parsing_error() -> None:
    """TASK-013: «исчезновение ожидаемых элементов при непустой странице
    считается изменением контракта, а не нулём вакансий» — здесь страница
    формально непуста (есть `<html><body>`), но без единой карточки и без
    явного маркера отсутствия результатов."""

    html = "<html><body><div>что-то не то</div></body></html>"

    with pytest.raises(HabrCareerParsingError):
        parse_search_results_page(html)


# --- page_looks_blocked (CAPTCHA/антибот) ---


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>Please complete the CAPTCHA to continue</body></html>",
        "<html><body>Подтвердите, что вы не робот</body></html>",
        "<html><body>Проверка браузера, подождите...</body></html>",
        "<html><body>We have detected unusual traffic from your network</body></html>",
    ],
)
def test_page_looks_blocked_detects_known_markers(html: str) -> None:
    assert page_looks_blocked(html) is True


def test_page_looks_blocked_is_false_for_normal_page() -> None:
    html = _list_page([_card_html()])

    assert page_looks_blocked(html) is False


# --- parse_vacancy_detail_page ---

# Codex review-fix [P1]: `parse_vacancy_detail_page` теперь требует хотя бы
# один структурный маркер настоящей detail-страницы
# (`_SELECTOR_DETAIL_PAGE_MARKER`) — без него ЛЮБОЙ HTML (пустая страница,
# 404-заглушка, посторонняя разметка) поднимает `HabrCareerParsingError`,
# а не превращается в `Vacancy` со всеми detail-полями пустыми.
#
# [P1] повторное ревью: маркер сузили до ДВУХ подтверждённых
# live smoke-test'ом, vacancy-specific селекторов
# (`.vacancy-company__title`, `.vacancy-header .vacancy-meta`) —
# неподтверждённые обобщённые `h1.page-title__title`/`.vacancy-title`
# убраны (тот же класс риска, что голый `.link-comp` в более раннем
# раунде: правдоподобно переиспользуются на других страницах платформы).
# `_detail_page()` больше не подмешивает маркер автоматически — фикстуры
# ниже, которые проверяют ОТСУТСТВИЕ конкретного опционального поля
# (description/requirements/skills/company), явно добавляют
# `_COMPANY_MARKER_HTML` ИЛИ пустой `_detail_meta_html()` как «страница
# точно настоящая», не полагаясь на побочный эффект общего фикстурного
# хелпера — иначе они путали бы «известная страница без этого конкретного
# поля» с «страница неизвестной структуры вовсе», которую покрывает
# отдельный `test_detail_page_raises_parsing_error_for_unknown_markup`
# ниже.

_COMPANY_MARKER_HTML = '<div class="vacancy-company__title">Marker Co</div>'


def _detail_page(body: str = "") -> str:
    return f"<html><body>{body}</body></html>"


def _detail_meta_html(
    *, plain_chips: tuple[str, ...] = (), skill_chips: tuple[str, ...] = ()
) -> str:
    """`.vacancy-header .vacancy-meta` (TASK-013 live smoke-test,
    2026-08-30, подтверждено) — общий контейнер грейда/стажа/формата и
    навыков. Навыки распознаются только по `.chip-without-icon__text`
    (тоже подтверждено); прочие chips (`plain_chips`) — не навыки, но всё
    равно часть factual `requirements` (весь блок читается целиком)."""

    plain_html = "".join(f'<span class="chip">{chip}</span>' for chip in plain_chips)
    skill_html = "".join(
        f'<span class="chip-without-icon">'
        f'<span class="chip-without-icon__text">{chip}</span></span>'
        for chip in skill_chips
    )
    return (
        '<div class="vacancy-header"><div class="vacancy-meta">'
        f"{plain_html}{skill_html}"
        "</div></div>"
    )


def test_detail_page_raises_parsing_error_for_unknown_markup() -> None:
    """TASK-013 fail-closed: ни один ожидаемый структурный элемент —
    изменение разметки/посторонняя страница, не вакансия с пустыми
    detail-полями."""

    html = "<html><body><div>что-то совсем не то</div></body></html>"

    with pytest.raises(HabrCareerParsingError):
        parse_vacancy_detail_page(html, _listing())


def test_detail_page_raises_parsing_error_for_empty_page() -> None:
    html = "<html><body></body></html>"

    with pytest.raises(HabrCareerParsingError):
        parse_vacancy_detail_page(html, _listing())


def test_detail_page_raises_parsing_error_for_stray_navigational_link_comp() -> None:
    """Codex review-fix [P1] (предыдущий раунд): голый `.link-comp` (без
    привязки к контейнеру) раньше совпадал с ЛЮБОЙ ссылкой, оформленной
    этим общим классом, включая обычную навигацию. Текущий
    `_SELECTOR_DETAIL_SKILLS` (`.vacancy-header .vacancy-meta
    .chip-without-icon__text`) не совпадает с посторонней навигационной
    ссылкой вовсе — страница без реального маркера detail-страницы
    обязана быть отклонена."""

    html = '<html><body><a class="link-comp">Главная</a></body></html>'

    with pytest.raises(HabrCareerParsingError):
        parse_vacancy_detail_page(html, _listing())


def test_detail_page_generic_template_heading_alone_is_not_enough() -> None:
    """[P1] повторное ревью — новый обходной случай, не повтор: маркер
    раньше включал НЕподтверждённые `h1.page-title__title`/`.vacancy-title`
    — обобщённо звучащие классы, правдоподобно переиспользуемые как общий
    шаблон на других страницах career.habr.com (профиль компании, статья,
    поиск), не только на detail-странице вакансии. Такая страница
    проходила бы проверку и превращалась в `Vacancy` с пустыми detail-
    полями. Маркер теперь построен только из `.vacancy-company__title`/
    `.vacancy-header .vacancy-meta`, подтверждённых live smoke-test'ом и
    специфичных для вакансии по имени класса — просто заголовок страницы
    этому больше не удовлетворяет."""

    html = (
        '<html><body><h1 class="page-title__title">'
        "Какая-то другая страница Habr</h1></body></html>"
    )

    with pytest.raises(HabrCareerParsingError):
        parse_vacancy_detail_page(html, _listing())


def test_detail_page_link_comp_outside_meta_block_is_not_a_skill() -> None:
    """Даже когда страница ЯВЛЯЕТСЯ настоящей detail-страницей (есть
    подтверждённый маркер), посторонняя ссылка `.link-comp` вне
    `.vacancy-header .vacancy-meta` не должна становиться выдуманным
    навыком в `key_skills`."""

    html = _detail_page(_detail_meta_html() + '<nav><a class="link-comp">Главная</a></nav>')

    vacancy = parse_vacancy_detail_page(html, _listing())

    assert vacancy.key_skills == []


def test_detail_page_extracts_description() -> None:
    html = _detail_page(
        _COMPANY_MARKER_HTML
        + '<div class="vacancy-description__text">Описание работы над AI-продуктом.</div>'
    )

    vacancy = parse_vacancy_detail_page(html, _listing())

    assert vacancy.description == "Описание работы над AI-продуктом."


def test_detail_page_description_none_when_absent() -> None:
    html = _detail_page(_COMPANY_MARKER_HTML + "<div>ничего полезного</div>")

    vacancy = parse_vacancy_detail_page(html, _listing())

    assert vacancy.description is None


def test_detail_page_extracts_requirements_from_meta_block() -> None:
    """TASK-013 live smoke-test (2026-08-30): factual requirements — не
    отдельная секция «Требования» (прежняя, неподтверждённая эвристика по
    заголовку), а сам подтверждённый блок `.vacancy-header .vacancy-meta`
    целиком."""

    html = _detail_page(
        _detail_meta_html(plain_chips=("Lead", "Полная занятость"), skill_chips=("Python",))
    )

    vacancy = parse_vacancy_detail_page(html, _listing())

    assert vacancy.requirements == "Lead Полная занятость Python"


def test_detail_page_requirements_none_when_meta_block_absent() -> None:
    html = _detail_page(_COMPANY_MARKER_HTML + "<div>ничего полезного</div>")

    vacancy = parse_vacancy_detail_page(html, _listing())

    assert vacancy.requirements is None


def test_detail_page_extracts_key_skills_from_confirmed_selector() -> None:
    """TASK-013 live smoke-test: `.vacancy-header .vacancy-meta
    .chip-without-icon__text` подтверждён как единственный источник
    навыков."""

    html = _detail_page(
        _detail_meta_html(plain_chips=("Lead",), skill_chips=("Python", "LLM"))
    )

    vacancy = parse_vacancy_detail_page(html, _listing())

    assert vacancy.key_skills == ["Python", "LLM"]


def test_detail_page_key_skills_ignores_plain_chips_without_confirmed_class() -> None:
    """Chips без `.chip-without-icon__text` (грейд, стаж, формат) не
    становятся навыками, даже находясь в том же `.vacancy-meta` блоке."""

    html = _detail_page(_detail_meta_html(plain_chips=("Lead", "Python"), skill_chips=()))

    vacancy = parse_vacancy_detail_page(html, _listing())

    assert vacancy.key_skills == []


def test_detail_page_key_skills_empty_when_meta_block_absent() -> None:
    html = _detail_page(_COMPANY_MARKER_HTML)

    vacancy = parse_vacancy_detail_page(html, _listing())

    assert vacancy.key_skills == []


def test_detail_page_builds_vacancy_with_habr_prefixed_id() -> None:
    html = _detail_page(_COMPANY_MARKER_HTML)

    vacancy = parse_vacancy_detail_page(html, _listing(external_id="1000123"))

    assert vacancy.hh_id == "habr:1000123"


def test_detail_page_carries_over_listing_fields() -> None:
    html = _detail_page(_COMPANY_MARKER_HTML)
    listing = _listing(
        title="Custom Title",
        company="Custom Co",
        url="https://career.habr.com/vacancies/42",
        salary_from=100.0,
        salary_to=200.0,
        salary_currency="RUR",
        area="Санкт-Петербург",
        work_format=["Можно удалённо"],
    )

    vacancy = parse_vacancy_detail_page(html, listing)

    assert vacancy.title == "Custom Title"
    assert vacancy.company == "Custom Co"
    assert vacancy.url == "https://career.habr.com/vacancies/42"
    assert vacancy.salary_from == 100.0
    assert vacancy.salary_to == 200.0
    assert vacancy.salary_currency == "RUR"
    assert vacancy.area == "Санкт-Петербург"
    assert vacancy.work_format == ["Можно удалённо"]
    assert vacancy.fetched_at.tzinfo is not None


# --- company: detail-page fallback (TASK-013 live smoke-test, 2026-08-30) ---


def test_detail_page_company_fallback_used_when_list_page_company_is_missing() -> None:
    """Живой smoke-test: прежний `.vacancy-card__company-title` (список)
    компанию не давал вовсе — `listing.company` часто уже `None`.
    `.vacancy-company__title` на detail-странице — безопасный fallback."""

    html = _detail_page('<div class="vacancy-company__title">Acme Corp</div>')

    vacancy = parse_vacancy_detail_page(html, _listing(company=None))

    assert vacancy.company == "Acme Corp"


def test_detail_page_company_fallback_does_not_override_list_page_company() -> None:
    html = _detail_page('<div class="vacancy-company__title">Detail Co</div>')

    vacancy = parse_vacancy_detail_page(html, _listing(company="List Co"))

    assert vacancy.company == "List Co"


def test_detail_page_company_stays_none_when_neither_source_has_it() -> None:
    """Маркер здесь — пустой `.vacancy-header .vacancy-meta` (не
    `.vacancy-company__title`): страница валидна, но не даёт компанию ни
    одним подтверждённым способом."""

    html = _detail_page(_detail_meta_html())

    vacancy = parse_vacancy_detail_page(html, _listing(company=None))

    assert vacancy.company is None


# --- absolute URLs must stay on the expected origin (Codex review-fix [P2]) ---


def test_absolute_href_on_a_foreign_domain_raises_parsing_error() -> None:
    """Без этой проверки посторонний абсолютный `href`, замаскированный под
    `/vacancies/<id>`, заставил бы `HabrCareerSource` выполнить запрос за
    пределы career.habr.com — вне заявленного TASK-013 scope."""

    html = _list_page([_card_html(href="https://evil.example.com/vacancies/1")])

    with pytest.raises(HabrCareerParsingError):
        parse_search_results_page(html)


def test_absolute_href_matching_custom_expected_origin_is_accepted() -> None:
    html = _list_page([_card_html(href="https://staging.habr.internal/vacancies/1")])

    listings = parse_search_results_page(html, expected_origin="https://staging.habr.internal")

    assert listings[0].url == "https://staging.habr.internal/vacancies/1"


def test_relative_href_becomes_absolute_using_expected_origin() -> None:
    html = _list_page([_card_html(href="/vacancies/1")])

    listings = parse_search_results_page(html, expected_origin="https://staging.habr.internal")

    assert listings[0].url == "https://staging.habr.internal/vacancies/1"
