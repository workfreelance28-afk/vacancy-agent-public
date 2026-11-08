"""Ошибки Qwen-клиента. В домене, а не в `integrations/` — по той же причине,
что `notifier_errors.py`/`repository_errors.py` (`docs/DECISIONS.md`, ADR-012):
`application/daily_scan.py` (TASK-007, ADR-021) должен различать «штатный
отказ» от «непредвиденного бага», не зная о конкретной реализации
(`LMStudioQwenClient`) — а это возможно только если типы ошибок объявлены
в домене, рядом с портами (`domain/ports.QwenClient`), чей контракт они
описывают."""


class QwenClientError(Exception):
    """Базовая ошибка Qwen-клиента и разбора его ответа."""


class QwenNetworkError(QwenClientError):
    """Сбой сети или тайм-аут при обращении к LM Studio (например, LM Studio выключен)."""


class QwenApiError(QwenClientError):
    """LM Studio вернул ответ с кодом ошибки (после исчерпания повторов, если применимо).

    Типичная причина — модель не загружена в LM Studio: сервер отвечает,
    но не может выполнить запрос."""

    def __init__(self, status_code: int, description: str) -> None:
        self.status_code = status_code
        super().__init__(f"LM Studio вернул {status_code}: {description}")


class QwenResponseError(QwenClientError):
    """Ответ модели не удалось разобрать как `VacancyAssessment`.

    Покрывает и невалидный JSON в content, и JSON, не проходящий валидацию
    `VacancyAssessment` (например, `score` вне диапазона, отсутствующее
    поле, неизвестное значение `recommendation`) — невалидный ответ LLM не
    считается успешным анализом (`docs/ARCHITECTURE.md`, «Надёжность и
    безопасность»)."""
