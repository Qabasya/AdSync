"""Тесты `logging_setup.py`: redaction-фильтр, Loki-хендлер, монтаж `configure_logging`.

Сеть запрещена — `httpx.MockTransport` вместо реальных запросов.
"""

import json
import logging
from pathlib import Path

import httpx
import pytest

from logging_setup import LokiHandler, RedactionFilter, configure_logging


def _make_record(message: str, *args: object, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="adsync.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )


class TestRedactionFilter:
    def test_strips_password_value(self) -> None:
        record = _make_record("создан пользователь password='s3cret' готово")
        assert RedactionFilter().filter(record) is True
        assert "s3cret" not in record.getMessage()
        assert "password=***" in record.getMessage()

    def test_leaves_unrelated_message_unchanged(self) -> None:
        record = _make_record("задание %s обработано", "app:1")
        original = record.getMessage()
        assert RedactionFilter().filter(record) is True
        assert record.getMessage() == original


class TestLokiHandler:
    def test_emit_sends_expected_stream_labels_and_line(self) -> None:
        captured: dict[str, httpx.Request] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(204)

        loki_handler = LokiHandler("http://loki.local", transport=httpx.MockTransport(handler))
        loki_handler.setFormatter(logging.Formatter("%(message)s"))
        record = _make_record("тестовая запись", level=logging.WARNING)

        loki_handler.emit(record)
        loki_handler.close()

        payload = json.loads(captured["request"].content.decode("utf-8"))
        stream = payload["streams"][0]
        assert stream["stream"] == {"service": "fs-adsync", "level": "warning"}
        assert stream["values"][0][1] == "тестовая запись"

    def test_emit_after_close_self_heals_and_delivers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Клиент, закрытый (не обязательно явным close() этого хендлера — см. разбор в
        fs-video-uploader, тот же Loki), не должен ронять доставку до конца жизни процесса —
        хендлер обязан пересобрать клиент и всё же отправить запись."""
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(204)

        loki_handler = LokiHandler("http://loki.local", transport=httpx.MockTransport(handler))
        loki_handler.setFormatter(logging.Formatter("%(message)s"))
        loki_handler.close()
        assert loki_handler._client.is_closed

        calls: list[logging.LogRecord] = []
        monkeypatch.setattr(loki_handler, "handleError", calls.append)

        record = _make_record("после закрытия")
        loki_handler.emit(record)

        assert calls == []
        assert len(captured) == 1
        assert not loki_handler._client.is_closed

    def test_transport_failure_does_not_raise(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        loki_handler = LokiHandler("http://loki.local", transport=httpx.MockTransport(handler))
        loki_handler.setFormatter(logging.Formatter("%(message)s"))
        record = _make_record("тестовая запись")

        loki_handler.emit(record)  # must not raise
        loki_handler.close()


class TestConfigureLogging:
    def test_file_handler_always_added_and_creates_log_file(self, tmp_path: Path) -> None:
        logger = logging.getLogger("adsync")
        for existing in list(logger.handlers):
            logger.removeHandler(existing)

        configure_logging(data_dir=tmp_path, loki_url=None)

        assert len(logger.handlers) == 1
        assert (tmp_path / "logs" / "adsync.log").exists()

        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)

    def test_loki_handler_added_when_configured(self, tmp_path: Path) -> None:
        logger = logging.getLogger("adsync")
        for existing in list(logger.handlers):
            logger.removeHandler(existing)

        configure_logging(data_dir=tmp_path, loki_url="http://loki.local")

        handler_types = {type(h) for h in logger.handlers}
        assert LokiHandler in handler_types
        assert len(logger.handlers) == 2

        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
