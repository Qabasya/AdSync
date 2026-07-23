"""Composition root: `Settings` → зависимости → три daemon-потока (jobs/reconcile/heartbeat) +
опциональный поток дневной сводки + `uvicorn` с локальным API. Graceful shutdown по SIGTERM/SIGINT.
"""

import logging
import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import FrameType
from zoneinfo import ZoneInfo

import uvicorn
from ldap3 import Connection

from ad import AdGateway, build_ldaps_connection
from api import AppState, create_api
from config import Settings, SubjectConfig, load_subjects
from handlers import DeprovisionHandler, JobHandler, ProvisionHandler
from lms import LmsClient
from logging_setup import configure_logging
from poller import Poller
from reconcile import Reconciler, ReconcileResult
from repository import JobRepository

logger = logging.getLogger("adsync.main")


def _now() -> datetime:
    return datetime.now(UTC)


def _build_ad_gateway(settings: Settings, subjects: dict[str, SubjectConfig]) -> AdGateway:
    """Собирает `AdGateway` с собственным LDAPS-соединением и `reconnect`-замыканием.

    Вызывается дважды (для потока заданий и для потока сверки) — `ldap3.Connection` не
    потокобезопасен для конкурентного использования, отдельные соединения проще и надёжнее
    блокировок внутри `ad.py`.
    """

    def reconnect() -> Connection:
        return build_ldaps_connection(
            host=settings.ldap_host,
            port=settings.ldap_port,
            ca_cert_path=settings.ldap_ca_cert,
            bind_dn=settings.ldap_bind_dn,
            bind_password=settings.ldap_bind_password,
        )

    return AdGateway(
        reconnect(),
        reconnect=reconnect,
        subjects=subjects,
        ou_disabled=settings.ad_ou_disabled,
        ou_fallback=settings.ad_ou_fallback,
        upn_suffix=settings.ad_upn_suffix,
    )


def _seconds_until_next_summary(target_time: str, tz: ZoneInfo, now: datetime) -> float:
    """Секунды до ближайшего срабатывания `target_time` (`"HH:MM"`) в таймзоне `tz`.

    Если время сегодня уже наступило (включая точное совпадение) — берётся завтрашний день, чтобы
    не отправить сводку дважды в одну и ту же минуту.
    """
    hour, minute = (int(part) for part in target_time.split(":"))
    local_now = now.astimezone(tz)
    target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local_now:
        target += timedelta(days=1)
    return (target - local_now).total_seconds()


def _loop(
    stop: threading.Event, interval_seconds: float, tick: Callable[[], object], label: str
) -> None:
    """Общий идиом фонового цикла: `while not stop.wait(interval): tick()`.

    Ошибка одного тика логируется и не убивает поток — следующий тик будет предпринят как обычно.
    """
    while not stop.wait(interval_seconds):
        try:
            tick()
        except Exception:
            logger.exception(
                "ошибка в фоновом цикле %s", label, extra={"event": "background_loop_error"}
            )


def _daily_summary_loop(
    stop: threading.Event, settings: Settings, repository: JobRepository
) -> None:
    """Раз в сутки, в `settings.daily_summary_time` по `settings.tz_name`, логирует агрегаты."""
    assert settings.daily_summary_time is not None
    tz = ZoneInfo(settings.tz_name)
    while not stop.wait(_seconds_until_next_summary(settings.daily_summary_time, tz, _now())):
        try:
            counts = repository.daily_counts(_now() - timedelta(hours=24))
            logger.info(
                "дневная сводка: создано %d, отключено %d, ошибок %d",
                counts.created,
                counts.disabled,
                counts.errors,
                extra={"event": "daily_summary"},
            )
        except Exception:
            logger.exception(
                "ошибка в потоке дневной сводки", extra={"event": "daily_summary_loop_error"}
            )


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(data_dir=settings.data_dir, loki_url=settings.loki_url)
    logger.info("fs-adsync запускается", extra={"event": "service_started"})
    started_at = _now()

    subjects = load_subjects(settings.subjects_file)

    jobs_directory = _build_ad_gateway(settings, subjects)
    reconcile_directory = _build_ad_gateway(settings, subjects)
    jobs_directory.verify_zone_exists()

    lms = LmsClient(settings.lms_base_url, settings.fs_lms_ad_hmac_secret)
    repository = JobRepository(settings.data_dir / "state.db")

    handlers: dict[str, JobHandler] = {
        "provision": ProvisionHandler(
            jobs_directory, subjects=subjects, ou_fallback=settings.ad_ou_fallback
        ),
        "deprovision": DeprovisionHandler(jobs_directory, ou_disabled=settings.ad_ou_disabled),
    }

    poller = Poller(
        lms,
        handlers=handlers,
        repository=repository,
        jobs_limit=settings.jobs_limit,
        now=_now,
    )
    reconciler = Reconciler(
        lms,
        reconcile_directory,
        ou_disabled=settings.ad_ou_disabled,
        max_disable=settings.reconcile_max_disable,
        max_disable_pct=settings.reconcile_max_disable_pct,
        grace_minutes=settings.reconcile_grace_minutes,
        now=_now,
    )

    state = AppState()
    reconcile_lock = threading.Lock()
    stop = threading.Event()

    def run_jobs_tick() -> None:
        poller.run_once()
        state.last_jobs_poll_at = _now()

    def run_reconcile_tick() -> ReconcileResult:
        with reconcile_lock:
            result = reconciler.run_once()
            state.last_reconcile_at = _now()
            return result

    def run_heartbeat_tick() -> None:
        counts = repository.status_counts()
        uptime_hours = (_now() - started_at).total_seconds() / 3600
        logger.info(
            "fs-adsync жив: uptime_hours=%.2f, done=%d, failed=%d, dead=%d",
            uptime_hours,
            counts.done,
            counts.failed,
            counts.dead,
            extra={"event": "heartbeat"},
        )

    threads = [
        threading.Thread(
            target=_loop,
            args=(stop, settings.jobs_poll_seconds, run_jobs_tick, "заданий"),
            name="jobs",
            daemon=True,
        ),
        threading.Thread(
            target=_loop,
            args=(stop, settings.reconcile_interval_hours * 3600, run_reconcile_tick, "сверки"),
            name="reconcile",
            daemon=True,
        ),
        threading.Thread(
            target=_loop,
            args=(stop, settings.heartbeat_interval_seconds, run_heartbeat_tick, "heartbeat"),
            name="heartbeat",
            daemon=True,
        ),
    ]
    if settings.daily_summary_time:
        threads.append(
            threading.Thread(
                target=_daily_summary_loop,
                args=(stop, settings, repository),
                name="daily-summary",
                daemon=True,
            )
        )
    for thread in threads:
        thread.start()

    app = create_api(state=state, repository=repository, run_reconcile=run_reconcile_tick)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=settings.api_port, log_config=None)
    )
    # uvicorn пропускает установку своих обработчиков сигналов, если запущен не из главного
    # потока — сигналами управляет только main(), без конфликта.
    api_thread = threading.Thread(target=server.run, name="api", daemon=True)
    api_thread.start()

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info(
            "получен сигнал %s, начинаю остановку",
            signum,
            extra={"event": "shutdown_signal_received"},
        )
        stop.set()
        server.should_exit = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    stop.wait()
    for thread in threads:
        thread.join(timeout=10)
    api_thread.join(timeout=10)

    lms.close()
    repository.close()
    jobs_directory.close()
    reconcile_directory.close()
    logger.info("fs-adsync остановлен", extra={"event": "service_stopped"})


if __name__ == "__main__":
    main()
