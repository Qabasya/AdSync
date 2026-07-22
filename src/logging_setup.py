"""Сборка технического логирования: file/Loki-хендлеры + redaction-фильтр пароля.

Единственный канал уведомлений — этот же поток логов: Loki собирает записи с обоих сервисов
инфраструктуры, а оповещение в мессенджер (если понадобится) настраивается поверх Loki через
Grafana Alerting, а не пушится из кода сервиса напрямую.
"""

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx

_MAX_LOG_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5
_FILE_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Без времени: Loki сам хранит и показывает время записи (мы передаём record.created в
# timestamp_ns при push) — свой asctime в тексте строки был бы дублем той же метки в Grafana.
_LOKI_LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"

_SENSITIVE_PATTERN = re.compile(r"(?i)(password|unicodePwd)=(?:'[^']*'|\"[^\"]*\"|\S+)")


class RedactionFilter(logging.Filter):
    """Вырезает значения `password`/`unicodePwd` из отформатированного текста записи."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _SENSITIVE_PATTERN.sub(lambda m: f"{m.group(1)}=***", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class LokiHandler(logging.Handler):
    """Пушит записи в Loki (`POST /loki/api/v1/push`) с низкокардинальными лейблами."""

    def __init__(
        self,
        loki_url: str,
        *,
        service: str = "fs-adsync",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__()
        self._loki_url = loki_url
        self._service = service
        self._transport = transport
        self._client = self._build_client()

    def _build_client(self) -> httpx.Client:
        return httpx.Client(base_url=self._loki_url, timeout=5.0, transport=self._transport)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._client.is_closed:
                # Наблюдалось на fs-video-uploader (том же Loki): клиент оказывался
                # закрытым не через явный close() этого хендлера — источник не
                # локализован, но тихая потеря ВСЕЙ доставки до конца жизни процесса
                # хуже пересборки клиента "на лету".
                self._client = self._build_client()
            line = self.format(record)
            timestamp_ns = str(int(record.created * 1_000_000_000))
            stream_labels: dict[str, str] = {
                "service": self._service,
                "level": record.levelname.lower(),
            }
            event = getattr(record, "event", None)
            if event is not None:
                stream_labels["event"] = str(event)
            payload = {
                "streams": [
                    {
                        "stream": stream_labels,
                        "values": [[timestamp_ns, line]],
                    }
                ]
            }
            response = self._client.post("/loki/api/v1/push", json=payload)
            response.raise_for_status()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self._client.close()
        super().close()


def configure_logging(
    *,
    data_dir: Path,
    loki_url: str | None,
    level: int = logging.INFO,
) -> None:
    """Собирает логгер `adsync`: file-хендлер всегда, Loki — если задан `LOKI_URL`.

    Args:
        data_dir: `DATA_DIR` — логи пишутся в `data_dir/logs/adsync.log`.
        loki_url: `LOKI_URL`; `None` — Loki-хендлер не добавляется.
        level: уровень логгера `adsync`.
    """
    logger = logging.getLogger("adsync")
    logger.setLevel(level)
    logger.propagate = False

    redaction_filter = RedactionFilter()

    log_path = data_dir / "logs" / "adsync.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=_MAX_LOG_BYTES, backupCount=_LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FILE_LOG_FORMAT))
    file_handler.addFilter(redaction_filter)
    logger.addHandler(file_handler)

    if loki_url:
        loki_handler = LokiHandler(loki_url)
        loki_handler.setFormatter(logging.Formatter(_LOKI_LOG_FORMAT))
        loki_handler.addFilter(redaction_filter)
        logger.addHandler(loki_handler)
