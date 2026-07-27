"""Цикл заданий: `fetch → dispatch → ack → журнал`, последовательно, один тик за вызов.

Про потоки и `SIGTERM` не знает — движущий цикл (`while not stop.wait(...)`) собирается в
`main.py`. Про AD/LDAP не знает — только `LmsApi`, реестр `JobHandler` и `JobRepository`.
"""

import logging
from collections.abc import Callable
from datetime import datetime

from handlers import HandlerResult, JobHandler
from lms import LmsApi, LmsModuleUnavailableError
from models import AckRequest, Job, ProvisionJob
from repository import JobLogEntry, JobRepository

logger = logging.getLogger("adsync.poller")

_DEAD_THRESHOLD = 6


class Poller:
    """Прогоняет один тик цикла заданий: забирает задания у LMS, обрабатывает, отчитывается."""

    def __init__(
        self,
        lms: LmsApi,
        *,
        handlers: dict[str, JobHandler],
        repository: JobRepository,
        jobs_limit: int,
        now: Callable[[], datetime],
    ) -> None:
        """Собирает поллер из готовых зависимостей.

        Args:
            lms: клиент LMS (`get_jobs`/`ack`).
            handlers: реестр обработчиков по значению `event`.
            repository: журнал обработок.
            jobs_limit: `limit` для `GET /ad/jobs`.
            now: источник текущего времени (UTC) — внедряется для тестируемости.
        """
        self._lms = lms
        self._handlers = handlers
        self._repository = repository
        self._jobs_limit = jobs_limit
        self._now = now
        self._module_unavailable = False

    def run_once(self) -> None:
        """Один тик: fetch → dispatch → ack → журнал. Ошибка тика не пробрасывается наружу."""
        received_at = self._now()
        try:
            jobs = self._lms.get_jobs(self._jobs_limit)
        except LmsModuleUnavailableError as exc:
            self._note_module_unavailable(exc)
            return
        except Exception:
            logger.exception(
                "не удалось получить задания с LMS", extra={"event": "lms_jobs_fetch_error"}
            )
            return

        self._note_module_available()
        # logger.info("получено %d заданий от LMS", len(jobs))
        for job in jobs:
            self._process(job, received_at)

    def _note_module_unavailable(self, exc: LmsModuleUnavailableError) -> None:
        """Логирует выключенный на сайте модуль ровно один раз — на переходе в это состояние.

        Модуль выключают в админке WP надолго (часы, дни), а тик заданий идёт каждые несколько
        секунд: traceback на каждый тик забил бы и файл, и Loki, скрыв под собой настоящие ошибки.
        """
        if self._module_unavailable:
            return
        self._module_unavailable = True
        logger.warning(
            "синхронизация с доменом на стороне LMS выключена: %s. Продолжаю опрос молча — "
            "сообщу, когда модуль снова ответит",
            exc,
            extra={"event": "lms_module_unavailable"},
        )

    def _note_module_available(self) -> None:
        """Парный лог к `_note_module_unavailable`: модуль снова отвечает — молчание закончилось."""
        if not self._module_unavailable:
            return
        self._module_unavailable = False
        logger.info(
            "синхронизация с доменом на стороне LMS снова доступна",
            extra={"event": "lms_module_available"},
        )

    def _process(self, job: Job, received_at: datetime) -> None:
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

        acked_at = self._now()
        try:
            self._lms.ack(AckRequest(id=job.id, status=result.status, error=result.error))
        except LmsModuleUnavailableError as exc:
            # Модуль выключили между fetch и ack: без этой ветки каждое задание пачки (до 200)
            # дало бы собственный traceback. WP переотдаст задание после включения — ack не потерян.
            self._note_module_unavailable(exc)
            return
        except Exception:
            logger.exception(
                "не удалось отправить ack для задания %s (%s)",
                job.id,
                job.event,
                extra={"event": "lms_ack_error"},
            )
            return

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
                acked_at=acked_at,
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

    def _run_handler(self, handler: JobHandler, job: Job) -> HandlerResult:
        try:
            return handler.handle(job)
        except Exception as exc:
            logger.exception(
                "ошибка обработки задания %s (%s) для %s",
                job.id,
                job.event,
                job.username,
                extra={"event": "job_handler_error"},
            )
            return HandlerResult("failed", error=str(exc))
