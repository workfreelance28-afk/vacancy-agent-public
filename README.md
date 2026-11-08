# Vacancy Agent

AI-сервис поиска и отбора вакансий: ежедневный прогон источников → жёсткие фильтры → оценка локальной LLM → хранение → карточки в Telegram. Решение об отклике всегда остаётся за человеком: сервис готовит черновик только после явного подтверждения (human-in-the-loop).

## Что делает

```text
SuperJob / Habr Career / HH (резерв)
→ нормализация вакансий
→ детерминированные фильтры
→ оценка релевантности локальной LLM (Qwen через LM Studio)
→ SQLite / PostgreSQL
→ карточки в Telegram
→ выбор и отклик — вручную, человеком
```

Второй этап — развитие рабочего MVP в production-like сервис:

- **FastAPI + PostgreSQL** — стабильные API-контракты, миграции (SQLite поддерживается для локальной разработки)
- **Очередь фоновых задач** — retry с ограничениями, таймауты, идемпотентность: повторная доставка не создаёт дублей, финальный сбой диагностируем
- **Versioned evaluation** — golden dataset исходной версии (50 кейсов) и offline regression gate по accuracy / macro-F1 / latency / cost на каждый push
- **Наблюдаемость** — структурированные JSON-логи, request/job correlation ID; инцидент диагностируется по логам без доступа к БД
- **MCP tool подготовки черновика отклика** (официальный MCP Python SDK): schema validation → backend preflight → подтверждение человеком через MCP elicitation → повторная backend-валидация → генерация черновика. Модель не может подтвердить действие сама: fail-closed при отказе, отмене или отсутствии capability у клиента

## Ключевой принцип

Критичную логику выполняет детерминированный Python-код. LLM оценивает и предлагает — но не принимает решений за пользователя и не может авторизовать действие: подтверждение приходит через отдельный канал, которым управляет человек.

## Тесты и качество

- Исходная версия: 761 пройденный автотест. Публичная копия: 758 пройденных тестов; 32 PostgreSQL-теста требуют тестовой БД, ещё 3 — исключённого приватного golden dataset. Покрытие: unit (домен, фильтры, адаптеры, очередь), integration (API, очередь, MCP-протокол против живой БД), тесты безопасности MCP tool, тесты наблюдаемости
- CI на каждый push: Ruff, mypy (strict), полный набор тестов на PostgreSQL-сервисе, gitleaks по всей публичной истории, проверки контрактов offline eval/regression gate, container smoke-тест
- Воспроизводимый запуск: `docker compose up --build --wait` поднимает весь стек одной командой

## Стек

Python 3.12 · FastAPI · PostgreSQL · SQLite · Pydantic · APScheduler · aiogram / Telegram Bot API · SuperJob API · Qwen + LM Studio · MCP Python SDK · Docker Compose · GitHub Actions

## Публичная конфигурация

Профили в `src/vacancy_agent/domain/candidate_profile.py` и `master_profile.py` полностью вымышленные. Это учебный пример, не резюме автора. Перед персональным использованием замените оба согласованными проверенными фактами. Фильтры по умолчанию не задают личный порог зарплаты или регион.

Приватные данные прогонов, owner-labelled golden dataset и baseline LLM исключены. В публичном CI проверяются алгоритмы оценки и regression gate на синтетических тестовых сценариях; это не повторная оценка качества модели на исходных 50 кейсах.

## Быстрый старт

Требуется Python 3.12+.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env   # все параметры задокументированы внутри
```

```bash
.venv/bin/pytest                     # тесты
.venv/bin/vacancy-agent scan         # один прогон: источники → фильтры → LLM → БД → Telegram
.venv/bin/vacancy-agent-scheduler    # ежедневный сценарий + Telegram-бот
docker compose up --build --wait     # API + PostgreSQL + worker
python scripts/mcp_demo.py           # демо human-in-the-loop подтверждения
```

## Документация

- [PORTFOLIO.md](PORTFOLIO.md) — разбор проекта как кейса: роль, архитектура, что доказывает
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — архитектура и конфигурация
- [docs/DATA_CONTRACTS.md](docs/DATA_CONTRACTS.md) — контракты данных между компонентами
- [screenshots/](screenshots/) — как выглядит в работе

## Лицензия

MIT
