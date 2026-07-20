"""Тесты `api.py` на `TestClient` поверх тестовых зависимостей, без реального `main.py`."""

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from api import AppState, create_api
from reconcile import ReconcileResult
from repository import JobLogEntry, JobRepository

_RECEIVED_AT = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)
_ACKED_AT = datetime(2026, 7, 20, 10, 0, 3, tzinfo=UTC)


def _entry(job_id: int, status: str = "done") -> JobLogEntry:
    return JobLogEntry(
        job_id=job_id,
        idempotency_key=f"key-{job_id}",
        event="provision",
        username="i.petrov",
        subject_key="inf-ege",
        status=status,  # type: ignore[arg-type]
        error=None if status == "done" else "boom",
        received_at=_RECEIVED_AT,
        acked_at=_ACKED_AT,
    )


def _never_called() -> ReconcileResult:
    raise AssertionError("run_reconcile не должен вызываться в этом тесте")


def test_health_returns_state_snapshot(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "state.db")
    state = AppState()
    app = create_api(state=state, repository=repository, run_reconcile=_never_called)
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["last_jobs_poll_at"] is None
    assert body["last_reconcile_at"] is None

    state.last_jobs_poll_at = _RECEIVED_AT
    response = client.get("/health")
    assert response.json()["last_jobs_poll_at"] is not None

    repository.close()


def test_status_returns_counts_and_recent_entries(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "state.db")
    repository.record(_entry(1, status="done"))
    repository.record(_entry(2, status="failed"))

    app = create_api(state=AppState(), repository=repository, run_reconcile=_never_called)
    client = TestClient(app)

    response = client.get("/status")
    repository.close()

    assert response.status_code == 200
    body = response.json()
    assert body["done"] == 1
    assert body["failed"] == 1
    assert body["dead"] == 0
    assert len(body["recent"]) == 2
    assert body["recent"][0]["job_id"] == 2  # самая свежая запись первой


def test_status_limits_recent_to_twenty(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "state.db")
    for job_id in range(1, 25):
        repository.record(_entry(job_id))

    app = create_api(state=AppState(), repository=repository, run_reconcile=_never_called)
    client = TestClient(app)

    response = client.get("/status")
    repository.close()

    assert len(response.json()["recent"]) == 20


def test_reconcile_endpoint_calls_injected_callable_once(tmp_path: Path) -> None:
    calls = 0

    def run_reconcile() -> ReconcileResult:
        nonlocal calls
        calls += 1
        return ReconcileResult(("stale-user",), aborted=False)

    repository = JobRepository(tmp_path / "state.db")
    app = create_api(state=AppState(), repository=repository, run_reconcile=run_reconcile)
    client = TestClient(app)

    response = client.post("/reconcile")
    repository.close()

    assert response.status_code == 200
    assert calls == 1
    assert response.json() == {
        "aborted": False,
        "abort_reason": None,
        "disabled_usernames": ["stale-user"],
    }


def test_reconcile_endpoint_reports_abort(tmp_path: Path) -> None:
    def run_reconcile() -> ReconcileResult:
        return ReconcileResult((), aborted=True, abort_reason="пустой список активных логинов")

    repository = JobRepository(tmp_path / "state.db")
    app = create_api(state=AppState(), repository=repository, run_reconcile=run_reconcile)
    client = TestClient(app)

    response = client.post("/reconcile")
    repository.close()

    body = response.json()
    assert body["aborted"] is True
    assert body["abort_reason"] == "пустой список активных логинов"
    assert body["disabled_usernames"] == []
