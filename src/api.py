"""Тонкий локальный HTTP API: `GET /health`, `GET /status`, `POST /reconcile`.

Без бизнес-логики — только чтение уже готового состояния и делегирование действий вызываемым
объектам, инжектированным из `main.py` (composition root). Входящих запросов от WP не существует
по модели — этот API только для админа/healthcheck.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi import FastAPI
from pydantic import BaseModel

from reconcile import ReconcileResult
from repository import JobRepository


@dataclass
class AppState:
    """Разделяемое между потоками состояние — время последнего тика заданий/сверки."""

    last_jobs_poll_at: datetime | None = None
    last_reconcile_at: datetime | None = None


class HealthResponse(BaseModel):
    """Тело ответа `GET /health`."""

    status: str
    last_jobs_poll_at: datetime | None
    last_reconcile_at: datetime | None


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


class ReconcileResponse(BaseModel):
    """Тело ответа `POST /reconcile`."""

    aborted: bool
    abort_reason: str | None
    disabled_usernames: list[str]


def create_api(
    *,
    state: AppState,
    repository: JobRepository,
    run_reconcile: Callable[[], ReconcileResult],
) -> FastAPI:
    """Собирает FastAPI-приложение поверх уже готовых зависимостей.

    Args:
        state: разделяемое состояние, обновляемое фоновыми потоками из `main.py`.
        repository: журнал обработок (для `/status`).
        run_reconcile: запускает внеочередную сверку и сам обновляет `state.last_reconcile_at`
            под тем же `threading.Lock()`, что и плановый поток — `api.py` про блокировку не знает.
    """
    app = FastAPI(title="fs-adsync")

    @app.get("/health")
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            last_jobs_poll_at=state.last_jobs_poll_at,
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

    @app.post("/reconcile")
    def trigger_reconcile() -> ReconcileResponse:
        result = run_reconcile()
        return ReconcileResponse(
            aborted=result.aborted,
            abort_reason=result.abort_reason,
            disabled_usernames=list(result.disabled_usernames),
        )

    return app
