"""Обработка одного задания, присланного сайтом (`POST /v1/jobs`): dispatch → журнал → итог.

Push-модель: сайт сам шлёт задание и в том же ответе получает результат — отдельного ack нет.
Про HTTP не знает (это `api.py`), про LDAP — тоже: только реестр `JobHandler` и `JobRepository`.
"""

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from ad import DirectoryUnavailableError
from handlers import HandlerResult, JobHandler
from models import Job, ProvisionJob
from repository import JobLogEntry, JobRepository

logger = logging.getLogger("adsync.jobs")

# Столько же неудач подряд сайт терпит, прежде чем пометить задание «мёртвым» у себя.
_DEAD_THRESHOLD = 6


class JobProcessor:
    """Выполняет задание и пишет факт в журнал. Одно задание за раз — одно LDAP-соединение."""

    def __init__(
        self,
        *,
        handlers: dict[str, JobHandler],
        repository: JobRepository,
        now: Callable[[], datetime],
    ) -> None:
        """Собирает обработчик из готовых зависимостей.

        Args:
            handlers: реестр обработчиков по значению `event`.
            repository: журнал обработок.
            now: источник текущего времени (UTC) — внедряется для тестируемости.
        """
        self._handlers = handlers
        self._repository = repository
        self._now = now
        # FastAPI выполняет sync-эндпоинты в пуле потоков, а `ldap3.Connection` не
        # потокобезопасен: задания обрабатываются строго по одному.
        self._lock = threading.Lock()

    def process(self, job: Job) -> HandlerResult:
        """Обрабатывает задание. Ошибка задания → `failed`; недоступный DC → исключение.

        Raises:
            DirectoryUnavailableError: DC недоступен и после переподключения — не ошибка
                задания, в журнал не пишется (сайт повторит, не тратя попытку).
        """
        with self._lock:
            received_at = self._now()
            logger.info(
                "обрабатываю задание %s (%s) для %s",
                job.id,
                job.event,
                job.username,
                extra={"event": "job_received"},
            )
            handler = self._handlers.get(job.event)
            if handler is None:
                logger.error(
                    "нет обработчика для события %r у задания %s",
                    job.event,
                    job.id,
                    extra={"event": "job_handler_missing"},
                )
                result = HandlerResult("failed", error=f"нет обработчика для события {job.event!r}")
            else:
                result = self._run_handler(handler, job)

            self._record(job, result, received_at)
            return result

    def _run_handler(self, handler: JobHandler, job: Job) -> HandlerResult:
        try:
            return handler.handle(job)
        except DirectoryUnavailableError:
            raise
        except Exception as exc:
            logger.exception(
                "ошибка обработки задания %s (%s) для %s",
                job.id,
                job.event,
                job.username,
                extra={"event": "job_handler_error"},
            )
            return HandlerResult("failed", error=str(exc))

    def _record(self, job: Job, result: HandlerResult, received_at: datetime) -> None:
        subject_key = job.subject_key if isinstance(job, ProvisionJob) else None
        self._repository.record(
            JobLogEntry(
                job_id=job.id,
                idempotency_key=job.idempotency_key,
                event=job.event,
                username=job.username,
                subject_key=subject_key,
                status=result.status,
                error=result.error,
                received_at=received_at,
                acked_at=self._now(),
            )
        )
        logger.info(
            "задание %s (%s) для %s обработано: %s",
            job.id,
            job.event,
            job.username,
            result.status,
            extra={"event": "job_done" if result.status == "done" else "job_failed"},
        )

        if result.status == "failed":
            dead = self._repository.dead_count(job.idempotency_key)
            if dead >= _DEAD_THRESHOLD:
                logger.error(
                    "задание %s (%s) мертво: %d неудачных попыток подряд",
                    job.idempotency_key,
                    job.event,
                    dead,
                    extra={"event": "job_dead"},
                )
