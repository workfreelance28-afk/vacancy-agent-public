"""Локальный read-only HTTP API (TASK-009, ADR-028/ADR-029).

`create_app` (`api/app.py`) — тонкий HTTP-адаптер поверх
`application/vacancy_queries.py`; `api/main.py` — точка входа консольного
скрипта `vacancy-agent-api`, которая реально собирает зависимости из
настроек и поднимает Uvicorn.
"""
