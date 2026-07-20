"""Тесты `LmsClient` (`lms.py`): подпись запросов, парсинг, отсутствие ретраев.

Сеть запрещена — `httpx.MockTransport` вместо реальных запросов.
"""

import hashlib
import hmac
import json

import httpx
import pytest

from lms import LmsClient
from models import AckRequest, DeprovisionJob, ProvisionJob

SECRET = "test-secret"
BASE_URL = "https://example.com/wp-json/fs-lms/v1"


def _expected_signature(secret: str, timestamp: str, body: str) -> str:
    """Независимый пересчёт подписи по формуле `FS_LMS_API.md` §2 — эталонный «вектор»."""
    return hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()


def test_get_jobs_signs_empty_body_and_parses_mixed_jobs() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        assert request.url.path == "/wp-json/fs-lms/v1/ad/jobs"
        assert request.url.params["limit"] == "50"
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 7,
                        "event": "provision",
                        "idempotency_key": "app:5",
                        "username": "i.petrov",
                        "password": "secret",
                        "first": "Иван",
                        "last": "Петров",
                        "subject_key": "inf",
                    },
                    {
                        "id": 8,
                        "event": "deprovision",
                        "idempotency_key": "deprovision:app:9",
                        "username": "a.sidorov",
                    },
                ]
            },
        )

    client = LmsClient(BASE_URL, SECRET, transport=httpx.MockTransport(handler))
    jobs = client.get_jobs(limit=50)
    client.close()

    request = captured["request"]
    timestamp = request.headers["X-Fs-Timestamp"]
    assert request.headers["X-Fs-Signature"] == _expected_signature(SECRET, timestamp, "")

    assert isinstance(jobs[0], ProvisionJob)
    assert isinstance(jobs[1], DeprovisionJob)


def test_ack_signs_actual_body_bytes_and_omits_none_error() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"ok": True})

    client = LmsClient(BASE_URL, SECRET, transport=httpx.MockTransport(handler))
    client.ack(AckRequest(id=7, status="done"))
    client.close()

    request = captured["request"]
    body = request.content.decode("utf-8")
    payload = json.loads(body)
    assert payload == {"id": 7, "status": "done"}
    assert "sam_account_name" not in payload

    timestamp = request.headers["X-Fs-Timestamp"]
    assert request.headers["X-Fs-Signature"] == _expected_signature(SECRET, timestamp, body)


def test_ack_includes_error_field_when_failed() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, json={"ok": True})

    client = LmsClient(BASE_URL, SECRET, transport=httpx.MockTransport(handler))
    client.ack(AckRequest(id=7, status="failed", error="ldap timeout"))
    client.close()

    payload = json.loads(captured["request"].content.decode("utf-8"))
    assert payload == {"id": 7, "status": "failed", "error": "ldap timeout"}


def test_get_active_usernames_signs_empty_body_and_parses() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        assert request.url.path == "/wp-json/fs-lms/v1/ad/active-usernames"
        return httpx.Response(200, json={"usernames": ["i.petrov", "a.sidorov"]})

    client = LmsClient(BASE_URL, SECRET, transport=httpx.MockTransport(handler))
    usernames = client.get_active_usernames()
    client.close()

    request = captured["request"]
    timestamp = request.headers["X-Fs-Timestamp"]
    assert request.headers["X-Fs-Signature"] == _expected_signature(SECRET, timestamp, "")
    assert usernames == ["i.petrov", "a.sidorov"]


def test_get_jobs_raises_on_non_2xx_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"error": "boom"})

    client = LmsClient(BASE_URL, SECRET, transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        client.get_jobs(limit=50)
    client.close()

    assert calls == 1
