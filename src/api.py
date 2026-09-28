"""HTTP API сервиса: публичный (задания от сайта) и локальный (healthcheck/статус для админа).

Без бизнес-логики — проверка подписи, разбор тела и делегирование вызываемым объектам из
`main.py` (composition root).

- Публичный (`create_public_api`, TLS, `PUBLIC_PORT`): `POST /v1/jobs`, `POST /v1/reconcile`,
  `GET /v1/health`. Каждый запрос подписан сайтом (`auth.py`); неверная подпись — `401`.
  На роутере порт открыт только для исходящего IP сайта.
- Локальный (`create_local_api`, HTTP, `API_PORT`): `GET /health` (Docker HEALTHCHECK),
  `GET /status` (журнал для админа). Наружу не пробрасывается.

Коды ответа публичного API — это контракт с сайтом (`.docs/AdSyncPythonService.md` §4.1):
`200` — итог задания (`done`/`failed`), `422` — невалидное задание (ошибка задания),
`503` — DC недоступен (не ошибка задания: сайт повторит, не тратя попытку).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, TypeAdapter, ValidationError

from ad import DirectoryUnavailableError
from auth import SignatureVerifier
from handlers import HandlerResult
from models import Job, JobResultResponse, ReconcileRequest, ReconcileResponse
from reconcile import ReconcileResult
from repository import JobRepository

logger = logging.getLogger("adsync.api")

_JOB_ADAPTER: TypeAdapter[Job] = TypeAdapter(Job)
_DC_UNAVAILABLE = "контроллер домена недоступен"


@dataclass
class AppState:
    """Разделяемое между потоками состояние — время последнего задания и последней сверки."""

    last_job_at: datetime | None = None
    last_reconcile_at: datetime | None = None


class SignatureRejectedError(Exception):
    """Запрос сайта не прошёл проверку подписи — ответ `401`."""


class HealthResponse(BaseModel):
    """Тело ответа локального `GET /health`."""

    status: str
    last_job_at: datetime | None
    last_reconcile_at: datetime | None


class PublicHealthResponse(BaseModel):
    """Тело ответа публичного `GET /v1/health` (кнопка «Проверить соединение» на сайте)."""

    status: str
    ldap: str


class JournalEntryResponse(BaseModel):
    """Одна запись журнала в теле `GET /status`."""

    job_id: int
    idempotency_key: str
    event: str
    username: str
    subject_key: str | None
    status: str
    error: str | None
    received_at: datetime
    acked_at: datetime


class StatusResponse(BaseModel):
    """Тело ответа `GET /status`."""

    done: int
    failed: int
    dead: int
    recent: list[JournalEntryResponse]


def create_public_api(
    *,
    verifier: SignatureVerifier,
    process_job: Callable[[Job], HandlerResult],
    run_reconcile: Callable[[list[str], bool], ReconcileResult],
    check_directory: Callable[[], None],
) -> FastAPI:
    """Собирает публичное API для сайта.

    Args:
        verifier: проверка подписи запросов сайта.
        process_job: выполняет задание (`JobProcessor.process` + обновление состояния).
        run_reconcile: прогон сверки по списку от сайта (`Reconciler.run` + состояние).
        check_directory: лёгкая проверка связи с DC; недоступен — `DirectoryUnavailableError`.
    """
    app = FastAPI(title="fs-adsync", docs_url=None, redoc_url=None, openapi_url=None)

    async def signed_body(request: Request) -> bytes:
        body = await request.body()
        if not verifier.verify(
            method=request.method,
            path=request.url.path,
            timestamp=request.headers.get("X-Fs-Timestamp", ""),
            signature=request.headers.get("X-Fs-Signature", ""),
            body=body,
        ):
            raise SignatureRejectedError
        return body

    @app.exception_handler(SignatureRejectedError)
    def on_bad_signature(request: Request, _exc: SignatureRejectedError) -> JSONResponse:
        logger.warning(
            "запрос %s %s отклонён: неверная или просроченная подпись",
            request.method,
            request.url.path,
            extra={"event": "signature_rejected"},
        )
        return JSONResponse({"error": "bad signature"}, status_code=401)

    @app.exception_handler(DirectoryUnavailableError)
    def on_dc_unavailable(_request: Request, _exc: DirectoryUnavailableError) -> JSONResponse:
        return JSONResponse({"error": _DC_UNAVAILABLE}, status_code=503)

    @app.get("/v1/health")
    def health(_body: bytes = Depends(signed_body)) -> PublicHealthResponse:
        check_directory()
        return PublicHealthResponse(status="ok", ldap="ok")

    @app.post("/v1/jobs", response_model=None)
    def jobs(body: bytes = Depends(signed_body)) -> JobResultResponse | JSONResponse:
        try:
            job = _JOB_ADAPTER.validate_json(body)
        except ValidationError as exc:
            logger.error(
                "невалидное задание от сайта: %s",
                exc.errors(include_input=False, include_url=False),
                extra={"event": "job_invalid"},
            )
            return JSONResponse(
                {"status": "failed", "error": "невалидное задание"}, status_code=422
            )

        result = process_job(job)
        return JobResultResponse(status=result.status, error=result.error)

    @app.post("/v1/reconcile", response_model=None)
    def reconcile(body: bytes = Depends(signed_body)) -> ReconcileResponse | JSONResponse:
        try:
            request = ReconcileRequest.model_validate_json(body)
        except ValidationError:
            logger.error("невалидный запрос сверки от сайта", extra={"event": "reconcile_invalid"})
            return JSONResponse({"error": "невалидный запрос сверки"}, status_code=422)

        result = run_reconcile(request.usernames, request.apply)
        return ReconcileResponse(
            status="aborted" if result.aborted else "ok",
            applied=result.applied,
            disabled=list(result.disabled_usernames),
            abort_reason=result.abort_reason,
        )

    return app


def create_local_api(*, state: AppState, repository: JobRepository) -> FastAPI:
    """Собирает локальное API: healthcheck контейнера и журнал для админа.

    Args:
        state: разделяемое состояние, обновляемое публичным API через `main.py`.
        repository: журнал обработок (для `/status`).
    """
    app = FastAPI(title="fs-adsync (local)")

    @app.get("/health")
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            last_job_at=state.last_job_at,
            last_reconcile_at=state.last_reconcile_at,
        )

    @app.get("/status")
    def status() -> StatusResponse:
        counts = repository.status_counts()
        recent = [
            JournalEntryResponse(
                job_id=entry.job_id,
                idempotency_key=entry.idempotency_key,
                event=entry.event,
                username=entry.username,
                subject_key=entry.subject_key,
                status=entry.status,
                error=entry.error,
                received_at=entry.received_at,
                acked_at=entry.acked_at,
            )
            for entry in repository.recent_entries(20)
        ]
        return StatusResponse(
            done=counts.done, failed=counts.failed, dead=counts.dead, recent=recent
        )

    return app
