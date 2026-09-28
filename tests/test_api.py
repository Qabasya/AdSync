"""Тесты `api.py` на `TestClient` поверх тестовых зависимостей, без реального `main.py`."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from ad import DirectoryUnavailableError
from api import AppState, create_local_api, create_public_api
from auth import SignatureVerifier, canonical
from handlers import HandlerResult
from models import Job, ProvisionJob
from reconcile import ReconcileResult
from repository import JobLogEntry, JobRepository

_RECEIVED_AT = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)
_ACKED_AT = datetime(2026, 7, 20, 10, 0, 3, tzinfo=UTC)
_SECRET = "topsecret"
_TS = 1_700_000_000

_PROVISION = {
    "id": 7,
    "event": "provision",
    "idempotency_key": "app:5",
    "username": "i.petrov",
    "password": "СекретУченика",
    "first": "Иван",
    "last": "Петров",
    "subject_key": "inf",
}


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


def _signed(method: str, path: str, body: bytes = b"", *, secret: str = _SECRET) -> dict[str, str]:
    signature = hmac.new(
        secret.encode(), canonical(method, path, str(_TS), body), hashlib.sha256
    ).hexdigest()
    return {"X-Fs-Timestamp": str(_TS), "X-Fs-Signature": signature}


class PublicApi:
    """Публичное API с управляемыми зависимостями и учётом вызовов."""

    def __init__(self) -> None:
        self.jobs: list[Job] = []
        self.job_result = HandlerResult("done", outcome="created")
        self.reconcile_calls: list[tuple[list[str], bool]] = []
        self.reconcile_result = ReconcileResult(("x.stale",), aborted=False, applied=False)
        self.dc_down = False
        verifier = SignatureVerifier(_SECRET, max_skew_seconds=300, now=lambda: float(_TS))
        self.client = TestClient(
            create_public_api(
                verifier=verifier,
                process_job=self._process,
                run_reconcile=self._reconcile,
                check_directory=self._check,
            )
        )

    def _process(self, job: Job) -> HandlerResult:
        if self.dc_down:
            raise DirectoryUnavailableError("down")
        self.jobs.append(job)
        return self.job_result

    def _reconcile(self, usernames: list[str], apply: bool) -> ReconcileResult:
        self.reconcile_calls.append((usernames, apply))
        return self.reconcile_result

    def _check(self) -> None:
        if self.dc_down:
            raise DirectoryUnavailableError("down")

    def post(self, path: str, payload: object) -> tuple[int, dict[str, object]]:
        body = json.dumps(payload).encode()
        response = self.client.post(path, content=body, headers=_signed("POST", path, body))
        return response.status_code, response.json()


# ── Публичный API (сайт → сервис) ─────────────────────────────────────────


def test_unsigned_or_forged_request_is_rejected() -> None:
    api = PublicApi()
    body = json.dumps(_PROVISION).encode()

    unsigned = api.client.post("/v1/jobs", content=body)
    forged = api.client.post(
        "/v1/jobs", content=body, headers=_signed("POST", "/v1/jobs", body, secret="wrong")
    )

    assert unsigned.status_code == 401
    assert forged.status_code == 401
    assert api.jobs == []


def test_job_done_is_returned_synchronously() -> None:
    api = PublicApi()

    code, body = api.post("/v1/jobs", _PROVISION)

    assert code == 200
    assert body == {"status": "done", "error": None, "outcome": "created"}
    assert isinstance(api.jobs[0], ProvisionJob)


def test_job_failed_carries_error() -> None:
    api = PublicApi()
    api.job_result = HandlerResult("failed", error="учётная запись вне управляемой зоны")

    code, body = api.post("/v1/jobs", _PROVISION)

    assert code == 200
    assert body["status"] == "failed"
    assert body["error"] == "учётная запись вне управляемой зоны"


def test_invalid_job_is_422_without_processing() -> None:
    api = PublicApi()

    code, body = api.post("/v1/jobs", {"id": 1, "event": "unknown", "username": "u"})

    assert code == 422
    assert body["status"] == "failed"
    assert api.jobs == []


def test_unavailable_directory_is_503() -> None:
    """Контракт с сайтом: 503 — не ошибка задания, сайт повторит, не тратя попытку."""
    api = PublicApi()
    api.dc_down = True

    code, body = api.post("/v1/jobs", _PROVISION)

    assert code == 503
    assert "недоступен" in str(body["error"])


def test_reconcile_passes_list_and_mode() -> None:
    api = PublicApi()

    code, body = api.post("/v1/reconcile", {"usernames": ["a", "b"], "apply": False})

    assert code == 200
    assert api.reconcile_calls == [(["a", "b"], False)]
    assert body == {"status": "ok", "applied": False, "disabled": ["x.stale"], "abort_reason": None}


def test_reconcile_reports_abort() -> None:
    api = PublicApi()
    api.reconcile_result = ReconcileResult(
        (), aborted=True, applied=True, abort_reason="к отключению 12 учёток, порог 10"
    )

    code, body = api.post("/v1/reconcile", {"usernames": ["a"], "apply": True})

    assert code == 200
    assert body["status"] == "aborted"
    assert body["abort_reason"] == "к отключению 12 учёток, порог 10"


def test_invalid_reconcile_request_is_422() -> None:
    api = PublicApi()

    code, _ = api.post("/v1/reconcile", {"apply": True})

    assert code == 422
    assert api.reconcile_calls == []


def test_public_health_checks_directory() -> None:
    api = PublicApi()

    ok = api.client.get("/v1/health", headers=_signed("GET", "/v1/health"))
    api.dc_down = True
    down = api.client.get("/v1/health", headers=_signed("GET", "/v1/health"))

    assert (ok.status_code, ok.json()) == (200, {"status": "ok", "ldap": "ok"})
    assert down.status_code == 503


def test_public_api_hides_docs() -> None:
    api = PublicApi()

    assert api.client.get("/docs").status_code == 404
    assert api.client.get("/openapi.json").status_code == 404


# ── Локальный API (healthcheck/статус) ────────────────────────────────────


def test_local_health_returns_state_snapshot(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "state.db")
    state = AppState()
    client = TestClient(create_local_api(state=state, repository=repository))

    body = client.get("/health").json()
    assert body == {"status": "ok", "last_job_at": None, "last_reconcile_at": None}

    state.last_job_at = _RECEIVED_AT
    assert client.get("/health").json()["last_job_at"] is not None

    repository.close()


def test_status_returns_counts_and_recent_entries(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "state.db")
    repository.record(_entry(1, status="done"))
    repository.record(_entry(2, status="failed"))
    client = TestClient(create_local_api(state=AppState(), repository=repository))

    response = client.get("/status")
    repository.close()

    assert response.status_code == 200
    body = response.json()
    assert (body["done"], body["failed"], body["dead"]) == (1, 1, 0)
    assert len(body["recent"]) == 2
    assert body["recent"][0]["job_id"] == 2  # самая свежая запись первой


def test_status_limits_recent_to_twenty(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "state.db")
    for job_id in range(1, 25):
        repository.record(_entry(job_id))
    client = TestClient(create_local_api(state=AppState(), repository=repository))

    response = client.get("/status")
    repository.close()

    assert len(response.json()["recent"]) == 20


def test_local_api_has_no_job_endpoints(tmp_path: Path) -> None:
    """Задания принимает только публичный API с подписью — локальный их не знает."""
    repository = JobRepository(tmp_path / "state.db")
    client = TestClient(create_local_api(state=AppState(), repository=repository))

    assert client.post("/v1/jobs", json=_PROVISION).status_code == 404
    assert client.post("/reconcile").status_code == 404
    repository.close()
