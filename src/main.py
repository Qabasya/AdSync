"""Composition root: `Settings` → зависимости → два `uvicorn` (публичный TLS-API для сайта и
локальный healthcheck/статус) + daemon-поток heartbeat и опциональный поток дневной сводки.
Graceful shutdown по SIGTERM/SIGINT.

Push-модель: задания и сверку присылает сайт (`POST /v1/jobs`, `POST /v1/reconcile`), своих
циклов опроса у сервиса нет.
"""

import logging
import signal
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import FrameType
from zoneinfo import ZoneInfo

import uvicorn
from ldap3 import Connection

from ad import AdGateway, build_ldaps_connection
from api import AppState, create_local_api, create_public_api
from auth import SignatureVerifier
from config import Settings, SubjectConfig, load_subjects
from handlers import (
    DeprovisionHandler,
    HandlerResult,
    JobHandler,
    PasswordHandler,
    ProvisionHandler,
)
from jobs import JobProcessor
from logging_setup import configure_logging
from models import Job
from reconcile import Reconciler, ReconcileResult
from repository import JobRepository

logger = logging.getLogger("adsync.main")


def _now() -> datetime:
    return datetime.now(UTC)


def _build_ad_gateway(settings: Settings, subjects: dict[str, SubjectConfig]) -> AdGateway:
    """Собирает `AdGateway` с собственным LDAPS-соединением и `reconnect`-замыканием.

    Вызывается трижды (задания, сверка, проверка связи): запросы сайта обрабатываются в пуле
    потоков, а `ldap3.Connection` не потокобезопасен — отдельные соединения проще и надёжнее
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
        password_never_expires=settings.ad_password_never_expires,
        profile_path_template=settings.ad_profile_path_template,
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


def _require_tls_files(settings: Settings) -> None:
    """Fail fast: без сертификата публичный API не поднимется — лучше упасть на старте."""
    missing = [
        path for path in (settings.tls_cert_file, settings.tls_key_file) if not path.is_file()
    ]
    if missing:
        raise SystemExit(
            "не найдены файлы TLS публичного API: " + ", ".join(str(path) for path in missing)
        )


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(data_dir=settings.data_dir, loki_url=settings.loki_url)
    logger.info("fs-adsync запускается", extra={"event": "service_started"})
    started_at = _now()

    _require_tls_files(settings)
    subjects = load_subjects(settings.subjects_file)

    jobs_directory = _build_ad_gateway(settings, subjects)
    reconcile_directory = _build_ad_gateway(settings, subjects)
    health_directory = _build_ad_gateway(settings, subjects)
    jobs_directory.verify_zone_exists()

    repository = JobRepository(settings.data_dir / "state.db")

    handlers: dict[str, JobHandler] = {
        "provision": ProvisionHandler(
            jobs_directory, subjects=subjects, ou_fallback=settings.ad_ou_fallback
        ),
        "deprovision": DeprovisionHandler(jobs_directory, ou_disabled=settings.ad_ou_disabled),
        "password": PasswordHandler(jobs_directory),
    }

    processor = JobProcessor(handlers=handlers, repository=repository, now=_now)
    reconciler = Reconciler(
        reconcile_directory,
        ou_disabled=settings.ad_ou_disabled,
        max_disable=settings.reconcile_max_disable,
        max_disable_pct=settings.reconcile_max_disable_pct,
        grace_minutes=settings.reconcile_grace_minutes,
        now=_now,
    )
    verifier = SignatureVerifier(
        settings.fs_lms_ad_hmac_secret,
        max_skew_seconds=settings.hmac_max_skew_seconds,
        now=time.time,
    )

    state = AppState()
    health_lock = threading.Lock()
    stop = threading.Event()

    def process_job(job: Job) -> HandlerResult:
        result = processor.process(job)
        state.last_job_at = _now()
        return result

    def run_reconcile(usernames: list[str], apply: bool) -> ReconcileResult:
        result = reconciler.run(usernames, apply=apply)
        state.last_reconcile_at = _now()
        return result

    def check_directory() -> None:
        with health_lock:
            health_directory.ping()

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

    public_app = create_public_api(
        verifier=verifier,
        process_job=process_job,
        run_reconcile=run_reconcile,
        check_directory=check_directory,
    )
    local_app = create_local_api(state=state, repository=repository)
    servers = [
        uvicorn.Server(
            uvicorn.Config(
                public_app,
                host="0.0.0.0",
                port=settings.public_port,
                ssl_certfile=str(settings.tls_cert_file),
                ssl_keyfile=str(settings.tls_key_file),
                log_config=None,
            )
        ),
        uvicorn.Server(
            uvicorn.Config(local_app, host="0.0.0.0", port=settings.api_port, log_config=None)
        ),
    ]
    # uvicorn пропускает установку своих обработчиков сигналов, если запущен не из главного
    # потока — сигналами управляет только main(), без конфликта.
    server_threads = [
        threading.Thread(target=server.run, name=name, daemon=True)
        for server, name in zip(servers, ("api-public", "api-local"), strict=True)
    ]
    for thread in server_threads:
        thread.start()
    logger.info(
        "публичный API слушает :%d (TLS), локальный — :%d",
        settings.public_port,
        settings.api_port,
        extra={"event": "api_started"},
    )

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info(
            "получен сигнал %s, начинаю остановку",
            signum,
            extra={"event": "shutdown_signal_received"},
        )
        stop.set()
        for server in servers:
            server.should_exit = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    stop.wait()
    for thread in threads + server_threads:
        thread.join(timeout=10)

    repository.close()
    jobs_directory.close()
    reconcile_directory.close()
    health_directory.close()
    logger.info("fs-adsync остановлен", extra={"event": "service_stopped"})


if __name__ == "__main__":
    main()
