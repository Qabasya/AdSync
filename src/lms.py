"""Клиент LMS REST API (`FS_LMS_API.md` §3): задания, ack, список активных логинов.

Подпись запросов — HMAC по `FS_LMS_API.md` §2, только stdlib `time`/`hmac`/`hashlib`.
Собственных ретраев нет: сетевые и HTTP-ошибки пробрасываются вызывающему коду.
"""

import hashlib
import hmac
import time
from typing import Protocol

import httpx

from models import AckRequest, ActiveUsernamesResponse, Job, JobsResponse

_ACTIVE_USERNAMES_TIMEOUT = 30.0


class LmsApi(Protocol):
    """Контракт похода в LMS — реализуется боевым `LmsClient` и тестовым `FakeLmsApi`."""

    def get_jobs(self, limit: int) -> list[Job]:
        """Забирает до `limit` готовых заданий из `GET /ad/jobs`."""
        ...

    def ack(self, request: AckRequest) -> None:
        """Отчитывается о выполнении одного задания через `POST /ad/ack`."""
        ...

    def get_active_usernames(self) -> list[str]:
        """Возвращает авторитетный список логинов из `GET /ad/active-usernames`."""
        ...


class LmsClient:
    """Боевая реализация `LmsApi` поверх `httpx`."""

    def __init__(
        self,
        base_url: str,
        hmac_secret: str,
        *,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Создаёт HTTP-клиент к LMS.

        Args:
            base_url: `{LMS_BASE_URL}`, например `https://example.com/wp-json/fs-lms/v1`.
            hmac_secret: `FS_LMS_AD_HMAC_SECRET` — секрет подписи запросов.
            timeout: таймаут по умолчанию для запросов.
            transport: только для тестов — внедрение `httpx.MockTransport` вместо реальной сети.
        """
        self._secret = hmac_secret
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def _signed_headers(self, body: str) -> dict[str, str]:
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self._secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256
        ).hexdigest()
        return {"X-Fs-Timestamp": timestamp, "X-Fs-Signature": signature}

    def get_jobs(self, limit: int) -> list[Job]:
        """`GET /ad/jobs?limit=…` — тело для подписи пустое (GET)."""
        response = self._client.get(
            "/ad/jobs", params={"limit": limit}, headers=self._signed_headers("")
        )
        response.raise_for_status()
        return JobsResponse.model_validate(response.json()).jobs

    def ack(self, request: AckRequest) -> None:
        """`POST /ad/ack` — подпись считается по фактически отправляемым байтам тела."""
        body = request.model_dump_json(exclude_none=True)
        headers = self._signed_headers(body)
        headers["Content-Type"] = "application/json"
        response = self._client.post("/ad/ack", content=body, headers=headers)
        response.raise_for_status()

    def get_active_usernames(self) -> list[str]:
        """`GET /ad/active-usernames` — список может быть большим, таймаут увеличен."""
        response = self._client.get(
            "/ad/active-usernames",
            headers=self._signed_headers(""),
            timeout=_ACTIVE_USERNAMES_TIMEOUT,
        )
        response.raise_for_status()
        return ActiveUsernamesResponse.model_validate(response.json()).usernames

    def close(self) -> None:
        """Закрывает HTTP-клиент."""
        self._client.close()
