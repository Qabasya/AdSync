"""Проверка подписи входящих запросов сайта (push-модель, `.docs/AdSyncPythonService.md` §3).

Формула — общая с сайтом (`Inc\\Modules\\AdSync\\Services\\AdRequestSigner`):
`hex(hmac_sha256(METHOD + "\\n" + PATH + "\\n" + TIMESTAMP + "\\n" + BODY, secret))`.
Только stdlib `hmac`/`hashlib`; время — зависимостью `now`, как везде в сервисе.
"""

import hashlib
import hmac
from collections.abc import Callable


def canonical(method: str, path: str, timestamp: str, body: bytes) -> bytes:
    """Подписываемая строка: метод, путь, время и сырое тело через перевод строки."""
    return f"{method.upper()}\n{path}\n{timestamp}\n".encode() + body


class SignatureVerifier:
    """Проверяет `X-Fs-Timestamp` + `X-Fs-Signature` запроса сайта."""

    def __init__(self, secret: str, *, max_skew_seconds: int, now: Callable[[], float]) -> None:
        """Создаёт проверяющего.

        Args:
            secret: `FS_LMS_AD_HMAC_SECRET` — тот же, что в `wp-config.php` сайта.
            max_skew_seconds: допустимое расхождение часов сайта и сервиса (окно анти-replay).
            now: источник текущего unix-времени — внедряется для тестируемости.
        """
        self._secret = secret.encode()
        self._max_skew = max_skew_seconds
        self._now = now

    def verify(
        self, *, method: str, path: str, timestamp: str, signature: str, body: bytes
    ) -> bool:
        """`True`, если подпись верна и запрос свежий. Пустой секрет — отказ всегда."""
        if not self._secret or not timestamp.isdigit() or not signature:
            return False
        if abs(self._now() - int(timestamp)) > self._max_skew:
            return False
        expected = hmac.new(
            self._secret, canonical(method, path, timestamp, body), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
