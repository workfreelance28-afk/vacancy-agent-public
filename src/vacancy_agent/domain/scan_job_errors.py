"""Ошибки, которыми `application.scan_worker.ScanJobRunner` сигнализирует
исход job'ы воркеру (TASK-S2.2).

В домене, а не в `application/`/`persistence/`, по той же причине, что
`qwen_errors.py`/`notifier_errors.py` (ADR-012): `application/scan_worker.py`
(общий orchestration-код, тестируемый на fake handler) должен отличать
временный сбой от программной/validation ошибки, не зная, что реальный
runner оборачивает `run_scan_once`/PostgreSQL/HTTP-клиенты — эта граница
проведена самим runner'ом (реальная wiring-обвязка — `worker.py`, не
`application/scan_worker.py`).

`error_code` — короткий безопасный код без raw exception text/секретов
(TASK-S2.2, «безопасный `error_code`»): сохраняется в `scan_jobs.error_code`
и возвращается клиенту `GET /api/v1/jobs/{job_id}`."""


class TransientScanJobError(Exception):
    """Временный сбой — сетевая/БД/API ошибка. Job уходит в `retry_wait`
    (пока не исчерпаны попытки) с exponential backoff, а не в `failed`."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class PermanentScanJobError(Exception):
    """Программная или validation ошибка — не повторяется (TASK-S2.2,
    «программные/validation ошибки не повторять»). Job сразу уходит в
    `failed`, независимо от оставшихся попыток."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)
