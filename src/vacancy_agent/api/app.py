"""FastAPI-приложение (TASK-009/ADR-028/029, TASK-011/ADR-034/035) — тонкий
HTTP-адаптер.

`create_app` не читает `.env`, не открывает БД и не создаёт внешние
соединения на import/create time (см. `docs/TASK-009.md`, «Конфигурация и
запуск») — она принимает уже готовые `RepositoryFactory`
(`application/vacancy_queries.py`) и, опционально,
`ApplicationDraftGeneratorFactory`/`ApplicationDraftModelResolver`
(`application/application_drafts.py`), и только регистрирует routes. Кто
вызывает `create_app` и как собираются сами фабрики (реальная SQLite/LM
Studio — `api/main.py`, fake — тесты) — решает вызывающая сторона.
`generator_factory` — именно фабрика, а не готовый клиент: реальный
`api/main.py` создаёт `LMStudioApplicationDraftGenerator` только внутри
POST, после проверки preconditions/idempotency (`docs/TASK-011.md`, «Порты
и границы») — не на старте процесса. `model_resolver` — отдельная, ещё
более дешёвая проверка (ADR-049): только имя сконфигурированной модели, без
самого generator'а — `prepare_application_draft` вызывает её ПЕРЕД
`generator_factory`, чтобы решить reuse-vs-regenerate, не создавая
generator и не открывая сеть, если модель (наравне с profile/prompt
version) не изменилась.

Routes читающие/пишущие БД — обычные `def`, не `async def`: `sqlite3`
блокирует и имеет thread affinity, FastAPI выполняет sync path operations
в worker thread (см. `docs/TASK-009.md`, ссылка на официальную
документацию FastAPI про async/sync). Тот же приём используется для POST
черновика — реальный вызов LM Studio блокирует поток до 300 секунд
(`docs/TASK-011.md`, «Синхронная граница»), но не event loop. `health` не
обращается к репозиторию вовсе — liveness, а не readiness/проверка БД;
`health`/`list`/`detail` не читают Qwen settings и не создают generator.
"""

import logging
import time
import traceback
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import PlainTextResponse

from vacancy_agent.api.schemas import (
    ApplicationDraftRequest,
    ApplicationDraftResponse,
    HealthResponse,
    MetricsSnapshotResponse,
    ReadinessResponse,
    ScanJobEnqueueRequest,
    ScanJobEnqueueResponse,
    ScanJobStatusResponse,
    VacancyDetailResponse,
    VacancyListResponse,
    application_draft_response,
    scan_job_status_response,
    vacancy_detail_from_stored,
    vacancy_summary_from_stored,
)
from vacancy_agent.application.application_drafts import (
    ApplicationDraftGeneratorFactory,
    ApplicationDraftModelResolver,
    ApplicationDraftRepositoryFactory,
    get_application_draft,
    prepare_application_draft,
)
from vacancy_agent.application.vacancy_queries import RepositoryFactory, get_vacancy, list_vacancies
from vacancy_agent.domain.application_draft_errors import (
    ApplicationDraftApiError,
    ApplicationDraftConcurrencyError,
    ApplicationDraftNetworkError,
    ApplicationDraftResponseError,
    EvidenceValidationError,
    RequirementQuoteError,
    VacancyNotEligibleError,
)
from vacancy_agent.domain.master_profile import MASTER_PROFILE
from vacancy_agent.domain.ports import RepositoryHealthCheck, ScanJobQueue
from vacancy_agent.domain.repository_errors import VacancyNotFoundError
from vacancy_agent.integrations.application_draft_prompt import APPLICATION_DRAFT_PROMPT_VERSION
from vacancy_agent.observability.json_logging import bind_request_id, is_valid_request_id
from vacancy_agent.observability.metrics import MetricsRegistry

# `docs/TASK-011.md`, «HTTP-контракт»: длина 1–255, начинается с алфавитно-
# цифрового символа, дальше алфавитно-цифровые и `. _ : -`. Общий для обоих
# новых routes — path segment, отсутствующий вовсе, не совпадает с route и
# даёт `404` через обычный routing, не через эту валидацию.
_VACANCY_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"

# TASK-S2.5, критерий №2: заголовок для request/response correlation ID.
_REQUEST_ID_HEADER = "X-Request-ID"

logger = logging.getLogger("vacancy_agent.api")


def _get_repository_factory(request: Request) -> RepositoryFactory:
    factory: RepositoryFactory = request.app.state.repository_factory
    return factory


def _get_health_check_factory(request: Request) -> Callable[[], RepositoryHealthCheck]:
    # Тот же объект, что `_get_repository_factory`/`_get_application_draft_repository_factory`
    # (ADR-028/ADR-034) — отдельный accessor только затем, чтобы `/ready`
    # объявлял именно тот протокол (`RepositoryHealthCheck`, TASK-S2.1),
    # который ему реально нужен, не весь `VacancyReadRepository`.
    factory: Callable[[], RepositoryHealthCheck] = request.app.state.repository_factory
    return factory


def _get_application_draft_repository_factory(
    request: Request,
) -> ApplicationDraftRepositoryFactory:
    # Тот же объект, что `_get_repository_factory` — `SqliteVacancyRepository`
    # реализует оба протокола структурно (ADR-028/ADR-034); два разных
    # accessor'а нужны только затем, чтобы каждый вызывающий код мог
    # объявить именно тот протокол, который ему реально нужен.
    factory: ApplicationDraftRepositoryFactory = request.app.state.repository_factory
    return factory


def _get_generator_factory(request: Request) -> ApplicationDraftGeneratorFactory | None:
    factory: ApplicationDraftGeneratorFactory | None = request.app.state.generator_factory
    return factory


def _get_model_resolver(request: Request) -> ApplicationDraftModelResolver | None:
    resolver: ApplicationDraftModelResolver | None = request.app.state.model_resolver
    return resolver


def _get_scan_job_queue_factory(request: Request) -> Callable[[], ScanJobQueue] | None:
    # `None` (default) — очередь вообще не подключена к этому `create_app`
    # (GET-only встраивание/тест, тот же приём, что `generator_factory`).
    # Реальный `api/main.py` всегда передаёт фабрику независимо от backend'а
    # (`persistence.factory.create_scan_job_queue`) — сам вызов фабрики
    # бросает явный `ValueError` для не-PostgreSQL `DATABASE_URL`, routes
    # ниже ловят это как безопасный `503` (TASK-S2.2, «Queue API доступен
    # только с PostgreSQL... для SQLite отвечает безопасным 503»).
    factory: Callable[[], ScanJobQueue] | None = request.app.state.scan_job_queue_factory
    return factory


def _close_quietly(resource: ScanJobQueue, *, what: str) -> None:
    # Тот же приём, что `ready()` для `checker.close()` (review-fix
    # TASK-S2.1 [P2]): ошибка закрытия не должна подменить собой уже
    # подготовленный ответ или превратить его в необработанный `500`.
    try:
        resource.close()
    except Exception as exc:
        logger.warning("Не удалось закрыть %s: %s", what, type(exc).__name__)


def _open_scan_job_queue_or_503(
    factory: Callable[[], ScanJobQueue] | None,
) -> ScanJobQueue:
    if factory is None:
        raise HTTPException(status_code=503, detail="Очередь сканирования не настроена")
    try:
        return factory()
    except Exception as exc:
        logger.warning("Не удалось открыть очередь сканирования: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Очередь сканирования недоступна") from exc


def create_app(
    repository_factory: RepositoryFactory,
    generator_factory: ApplicationDraftGeneratorFactory | None = None,
    model_resolver: ApplicationDraftModelResolver | None = None,
    scan_job_queue_factory: Callable[[], ScanJobQueue] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Vacancy Agent API",
        version="1",
        description=(
            "Локальный read-only API поверх сохранённых вакансий (TASK-009) "
            "и подготовка проверяемого черновика отклика локальным Qwen (TASK-011)."
        ),
    )
    app.state.repository_factory = repository_factory
    app.state.generator_factory = generator_factory
    # `model_resolver` — дешёвая проверка сконфигурированного имени модели
    # БЕЗ создания generator'а (TASK-011, ADR-049) — нужна `prepare_
    # application_draft`, чтобы решить, применим ли reuse, ДО того, как
    # вообще стоит создавать `ApplicationDraftGenerator`/открывать сеть.
    app.state.model_resolver = model_resolver
    app.state.scan_job_queue_factory = scan_job_queue_factory
    # Process-local (TASK-S2.5, «Не входит»: без Prometheus/collector,
    # без хранения в PostgreSQL) — один `MetricsRegistry` на процесс,
    # созданный здесь, а не переданный параметром: это внутренняя
    # bookkeeping-деталь `create_app`, не подменяемая вызывающим кодом
    # так, как `repository_factory`/`generator_factory`.
    app.state.metrics = MetricsRegistry()

    def _log_http_request(request: Request, response: Response, duration_seconds: float) -> None:
        # Route TEMPLATE (`/api/v1/vacancies/{vacancy_id}`), не фактический
        # path (`/api/v1/vacancies/superjob:123`) — иначе каждый разный
        # `vacancy_id`/`job_id` завёл бы свою метку метрики без верхней
        # границы (TASK-S2.5, критерий №3, «отсутствуют unbounded labels»).
        # Ничего не находится (404 на незарегистрированный путь) — `route`
        # в scope не появляется вовсе, используем фиксированную метку.
        route = request.scope.get("route")
        path_label = route.path if route is not None else "unmatched"
        # Наблюдаемость сама по себе не должна влиять на реальный ответ
        # (TASK-S2.5, критерий №4): запись в registry уже not-raising
        # (`MetricsRegistry`, см. её docstring), но вызов оборачиваем и
        # здесь — единственное место, где сбор extra-полей ДО
        # `logger.info` мог бы утечь наружу и подменить собой уже готовый
        # `response`.
        try:
            app.state.metrics.increment(
                "http_requests_total",
                {
                    "method": request.method,
                    "path": path_label,
                    "status_code": str(response.status_code),
                },
            )
            app.state.metrics.observe_latency("http_request_duration_seconds", duration_seconds)
            logger.info(
                "http.request",
                extra={
                    "method": request.method,
                    "path": path_label,
                    "status_code": response.status_code,
                    "duration_seconds": round(duration_seconds, 3),
                },
            )
        except Exception:
            logger.warning("Не удалось записать метрику/лог HTTP-запроса", exc_info=True)

    @app.middleware("http")
    async def observability_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # `X-Request-ID`: принимаем валидный входящий, иначе генерируем
        # свой (TASK-S2.5, критерий №2) — `bind_request_id` кладёт его в
        # ContextVar, который попадает в КАЖДУЮ запись LogRecord, созданную
        # во время обработки этого запроса (включая sync-routes: FastAPI
        # выполняет их в worker thread через `anyio.to_thread.run_sync`,
        # которая копирует текущий `contextvars.Context` — см.
        # `observability/json_logging.py`).
        incoming = request.headers.get(_REQUEST_ID_HEADER)
        request_id = incoming if incoming is not None and is_valid_request_id(incoming) else None
        if request_id is None:
            request_id = str(uuid4())
        token = bind_request_id(request_id)
        started = time.monotonic()
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                # Необработанное исключение из route/ExceptionMiddleware
                # иначе улетело бы мимо этого middleware прямо во внешний
                # `ServerErrorMiddleware`, чей 500-ответ строится ВНЕ этой
                # функции — у него никогда не было бы шанса получить
                # `X-Request-ID` (критерий №2: заголовок должен быть в
                # КАЖДОМ ответе, включая 500). `PlainTextResponse` с тем
                # же телом/media-type, что и дефолтный ответ Starlette —
                # ручной `Response(...)` не проставлял `Content-Type`.
                response = PlainTextResponse("Internal Server Error", status_code=500)
                duration_seconds = time.monotonic() - started
                response.headers[_REQUEST_ID_HEADER] = request_id
                # НИКОГДА `exc_info=`/`logger.exception` здесь: это положило
                # бы само исключение (с его `str(exc)`, который у сторонних
                # драйверов/клиентов может содержать DSN, заголовок или
                # токен) на `LogRecord` — а он существует в памяти дольше,
                # чем один вызов `JsonFormatter.format()` (например, у
                # `caplog` в тестах, или у будущего второго handler'а без
                # нашей санитизации). Извлекаем только безопасные факты
                # (имя класса, расположение фреймов) ДО того, как что-либо
                # логируется, и передаём их через `extra=` — сам `exc`
                # никогда не попадает ни в один `LogRecord`.
                exc_type_name = type(exc).__name__
                exc_trace = [
                    f"{frame.filename}:{frame.lineno} in {frame.name}"
                    for frame in traceback.extract_tb(exc.__traceback__)
                ]
                try:
                    logger.error(
                        "http.request_failed",
                        extra={"exc_type": exc_type_name, "exc_trace": exc_trace},
                    )
                    _log_http_request(request, response, duration_seconds)
                except Exception:
                    logger.warning("Не удалось записать метрику/лог сбойного HTTP-запроса")
                return response
            duration_seconds = time.monotonic() - started
            response.headers[_REQUEST_ID_HEADER] = request_id
            _log_http_request(request, response, duration_seconds)
        finally:
            # Сбрасываем ПОСЛЕ логирования завершения запроса — иначе
            # `_log_http_request` увидела бы уже отвязанный `request_id`
            # (найдено этим же тестовым набором: `http.request` терял
            # `request_id`, потому что `reset()` раньше стоял в `finally`
            # ДО этого лога).
            token.reset()
        return response

    @app.get("/api/v1/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/api/v1/ready", response_model=ReadinessResponse)
    def ready(
        factory: Callable[[], RepositoryHealthCheck] = Depends(_get_health_check_factory),
    ) -> ReadinessResponse:
        # Инвариант TASK-S2.1: ни DSN/путь к БД, ни текст SQL-ошибки не
        # должны попасть в HTTP-ответ — исключение логируется только по
        # имени класса (`type(exc).__name__`), не по `str(exc)`, который у
        # некоторых драйверов может включать DSN. `503` без деталей и для
        # ошибки открытия соединения, и для ошибки самого `ping()` —
        # клиенту не нужно различать эти два случая.
        try:
            checker = factory()
        except Exception as exc:
            logger.warning("Проверка готовности БД не прошла: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Хранилище недоступно") from exc
        try:
            checker.ping()
        except Exception as exc:
            logger.warning("Проверка готовности БД не прошла: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Хранилище недоступно") from exc
        finally:
            # `close()` тоже может бросить (например, если `ping()` уже
            # обнаружил разорванное соединение) — эта ошибка не должна
            # подменить собой уже подготовленный `503` (или превратить
            # успешный `ping()` в необработанный `500`, review-fix
            # TASK-S2.1 P2): она только логируется, наружу не уходит.
            try:
                checker.close()
            except Exception as exc:
                logger.warning(
                    "Не удалось закрыть соединение после проверки готовности: %s",
                    type(exc).__name__,
                )
        return ReadinessResponse()

    @app.get("/api/v1/metrics", response_model=MetricsSnapshotResponse)
    def metrics() -> MetricsSnapshotResponse:
        # Read-only, process-local (TASK-S2.5): только этот API-процесс,
        # без worker'а (отдельный процесс, свой `MetricsRegistry`) и без
        # внешнего collector'а/хранилища.
        snapshot = app.state.metrics.snapshot()
        return MetricsSnapshotResponse(**snapshot)

    @app.get("/api/v1/vacancies", response_model=VacancyListResponse)
    def get_vacancies(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        factory: RepositoryFactory = Depends(_get_repository_factory),
    ) -> VacancyListResponse:
        page = list_vacancies(factory, limit=limit, offset=offset)
        return VacancyListResponse(
            items=[vacancy_summary_from_stored(item) for item in page.items],
            total=page.total,
            limit=page.limit,
            offset=page.offset,
        )

    @app.get("/api/v1/vacancies/{vacancy_id}", response_model=VacancyDetailResponse)
    def get_vacancy_detail(
        vacancy_id: str,
        factory: RepositoryFactory = Depends(_get_repository_factory),
    ) -> VacancyDetailResponse:
        stored = get_vacancy(factory, vacancy_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="Вакансия не найдена")
        return vacancy_detail_from_stored(stored)

    @app.post(
        "/api/v1/vacancies/{vacancy_id}/application-drafts",
        response_model=ApplicationDraftResponse,
    )
    def create_application_draft(
        request_body: ApplicationDraftRequest,
        vacancy_id: str = Path(min_length=1, max_length=255, pattern=_VACANCY_ID_PATTERN),
        repo_factory: ApplicationDraftRepositoryFactory = Depends(
            _get_application_draft_repository_factory
        ),
        gen_factory: ApplicationDraftGeneratorFactory | None = Depends(_get_generator_factory),
        model_resolver: ApplicationDraftModelResolver | None = Depends(_get_model_resolver),
    ) -> ApplicationDraftResponse:
        # `request_body` сам по себе не используется дальше: сам факт, что
        # исполнение дошло до этой строки, уже доказывает, что запрос прошёл
        # строгую Pydantic-валидацию (`ApplicationDraftRequest` — extra="forbid"
        # + `confirm_local_generation` строго `true`) — иначе FastAPI ответил
        # бы `422` раньше, не вызывая тело route вовсе.
        assert request_body.confirm_local_generation is True

        if gen_factory is None or model_resolver is None:
            # `docs/TASK-011.md`, «Порты и границы»: если фабрика (или
            # парный ей model_resolver, ADR-049) вообще не передана в
            # `create_app` (GET-only встраивание/тест), POST отвечает
            # `503`, не пытаясь ничего сгенерировать.
            raise HTTPException(status_code=503, detail="Генератор черновиков не настроен")

        try:
            result = prepare_application_draft(
                repo_factory,
                gen_factory,
                model_resolver,
                MASTER_PROFILE,
                APPLICATION_DRAFT_PROMPT_VERSION,
                vacancy_id,
            )
        except VacancyNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Вакансия не найдена") from exc
        except VacancyNotEligibleError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ApplicationDraftNetworkError, ApplicationDraftApiError) as exc:
            raise HTTPException(
                status_code=503, detail="LM Studio недоступен или не настроен"
            ) from exc
        except ApplicationDraftResponseError as exc:
            if exc.diagnostics:
                # `exc.diagnostics` (ADR-044) — только числа/позиции/booleans
                # о ФОРМЕ content (обрезан ли JSON, markdown fence, `<think>`
                # и т.д.), никогда сам content — безопасно логировать целиком,
                # той же дисциплиной, что и `category` (ADR-043). HTTP-ответ
                # ниже не меняется — остаётся тем же generic `502`.
                logger.warning(
                    "Черновик Qwen отклонён: category=%s diagnostics=%s",
                    exc.category,
                    exc.diagnostics,
                )
            else:
                logger.warning("Черновик Qwen отклонён: category=%s", exc.category)
            raise HTTPException(
                status_code=502, detail="Ответ модели не прошёл валидацию"
            ) from exc
        except EvidenceValidationError as exc:
            logger.warning("Черновик Qwen отклонён: category=plan_evidence_unknown_id")
            raise HTTPException(
                status_code=502, detail="Ответ модели не прошёл валидацию"
            ) from exc
        except RequirementQuoteError as exc:
            # TASK-012: `requirement_quote` не является дословной подстрокой
            # реального `vacancy.requirements` — тот же безопасный generic
            # `502`, что и остальные нарушения плана, без raw content модели.
            logger.warning("Черновик Qwen отклонён: category=plan_quote_invalid")
            raise HTTPException(
                status_code=502, detail="Ответ модели не прошёл валидацию"
            ) from exc
        except ApplicationDraftConcurrencyError as exc:
            # ADR-050: race-ветка проиграла дважды подряд с несовпадающей
            # версией (profile/prompt/model) — не выдаём чужую версию за
            # актуальную. `409`, как и `VacancyNotEligibleError` выше:
            # состояние конфликтное, но транзиентное — повтор запроса,
            # скорее всего, увидит уже устоявшийся результат.
            logger.warning("Черновик Qwen отклонён: category=concurrent_write_conflict")
            raise HTTPException(
                status_code=409, detail="Конкурентная запись — повторите запрос"
            ) from exc

        return application_draft_response(result.stored, reused=result.reused)

    @app.get(
        "/api/v1/vacancies/{vacancy_id}/application-draft",
        response_model=ApplicationDraftResponse,
    )
    def read_application_draft(
        vacancy_id: str = Path(min_length=1, max_length=255, pattern=_VACANCY_ID_PATTERN),
        repo_factory: ApplicationDraftRepositoryFactory = Depends(
            _get_application_draft_repository_factory
        ),
    ) -> ApplicationDraftResponse:
        stored = get_application_draft(repo_factory, vacancy_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="Черновик не найден")
        return application_draft_response(stored, reused=True)

    @app.post("/api/v1/jobs/scan", response_model=ScanJobEnqueueResponse, status_code=202)
    def enqueue_scan_job(
        request_body: ScanJobEnqueueRequest,
        factory: Callable[[], ScanJobQueue] | None = Depends(_get_scan_job_queue_factory),
    ) -> ScanJobEnqueueResponse:
        # API только ставит job в очередь — не выполняет scan внутри HTTP
        # request (TASK-S2.2, критерий №5): реальная работа делает отдельный
        # worker-процесс (`worker.py`), забирающий job асинхронно.
        queue = _open_scan_job_queue_or_503(factory)
        try:
            job, reused = queue.enqueue(request_body.idempotency_key)
        except Exception as exc:
            logger.warning("Не удалось поставить job в очередь: %s", type(exc).__name__)
            raise HTTPException(
                status_code=503, detail="Очередь сканирования недоступна"
            ) from exc
        finally:
            _close_quietly(queue, what="очередь сканирования")
        # TASK-S2.5, критерий №2: `job_id` — сквозной ID, который worker
        # использует во ВСЕХ своих событиях (claim/retry/success/final
        # failure, `application/scan_worker.py`) — здесь он появляется
        # впервые, в том же логе, что и `request_id` этого HTTP-запроса
        # (добавлен автоматически, см. `observability_middleware`).
        try:
            app.state.metrics.increment("scan_jobs_enqueued_total", {"reused": str(reused)})
            logger.info(
                "scan_job.enqueued", extra={"job_id": str(job.id), "reused": reused}
            )
        except Exception:
            logger.warning("Не удалось записать метрику/лог enqueue", exc_info=True)
        return ScanJobEnqueueResponse(job_id=job.id, status=job.status, reused=reused)

    @app.get("/api/v1/jobs/{job_id}", response_model=ScanJobStatusResponse)
    def get_scan_job(
        job_id: str,
        factory: Callable[[], ScanJobQueue] | None = Depends(_get_scan_job_queue_factory),
    ) -> ScanJobStatusResponse:
        try:
            job_uuid = UUID(job_id)
        except ValueError:
            # Синтаксически невалидный job_id не может существовать — тот
            # же безопасный `404`, что и для настоящего, но неизвестного
            # UUID ниже, без отдельного `422` для этого случая.
            raise HTTPException(status_code=404, detail="Job не найдена") from None

        queue = _open_scan_job_queue_or_503(factory)
        try:
            job = queue.get(job_uuid)
        except Exception as exc:
            logger.warning("Не удалось прочитать job из очереди: %s", type(exc).__name__)
            raise HTTPException(
                status_code=503, detail="Очередь сканирования недоступна"
            ) from exc
        finally:
            _close_quietly(queue, what="очередь сканирования")
        if job is None:
            raise HTTPException(status_code=404, detail="Job не найдена")
        return scan_job_status_response(job)

    return app
