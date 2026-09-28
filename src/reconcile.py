"""Сверка активных учёток управляемой зоны со списком от сайта — с предохранителями.

Push-модель: сайт раз в сутки присылает `POST /v1/reconcile` со списком логинов, которые
должны остаться активными, и флагом `apply`. Без `apply` сверка только пишет в журнал, кого
отключила бы (режим первой недели). Односторонняя: только отключает лишних (по пути
`DeprovisionHandler`), никого не включает, не создаёт и не переносит обратно. Про журнал
заданий не знает — сверка не привязана к `job_id`/`idempotency_key`, это не обработка задания.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ad import DirectoryGateway

logger = logging.getLogger("adsync.reconcile")


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Итог одного прогона сверки.

    При `applied=False` (режим «только журнал») в `disabled_usernames` — кого отключила бы.
    """

    disabled_usernames: tuple[str, ...]
    aborted: bool
    applied: bool = False
    abort_reason: str | None = None


class Reconciler:
    """Сверяет активные учётки управляемой зоны со списком от сайта."""

    def __init__(
        self,
        directory: DirectoryGateway,
        *,
        ou_disabled: str,
        max_disable: int,
        max_disable_pct: int,
        grace_minutes: int,
        now: Callable[[], datetime],
    ) -> None:
        """Собирает сверку из готовых зависимостей.

        Args:
            directory: шлюз AD (`list_zone_accounts`/`ensure_disabled`/`move_to_ou`).
            ou_disabled: DN OU «Отчисленные» — куда переносятся отключённые учётки.
            max_disable: порог `RECONCILE_MAX_DISABLE`.
            max_disable_pct: порог `RECONCILE_MAX_DISABLE_PCT` (0–100).
            grace_minutes: `RECONCILE_GRACE_MINUTES` — свежие учётки не трогаются.
            now: источник текущего времени (UTC) — внедряется для тестируемости.
        """
        self._directory = directory
        self._ou_disabled = ou_disabled
        self._max_disable = max_disable
        self._max_disable_pct = max_disable_pct
        self._grace_minutes = grace_minutes
        self._now = now
        # Своё LDAP-соединение; два запроса сверки подряд не должны делить его одновременно.
        self._lock = threading.Lock()

    def run(self, active_usernames: list[str], *, apply: bool) -> ReconcileResult:
        """Один прогон сверки. Нарушен предохранитель — abort, никто не тронут.

        Args:
            active_usernames: логины, которые должны остаться активными (от сайта).
            apply: `True` — отключать лишних; `False` — только журнал.

        Raises:
            DirectoryUnavailableError: DC недоступен — сайт повторит сверку в следующий раз.
        """
        with self._lock:
            return self._run(set(active_usernames), apply=apply)

    def _run(self, active_usernames: set[str], *, apply: bool) -> ReconcileResult:
        zone_accounts = self._directory.list_zone_accounts()

        if not active_usernames and zone_accounts:
            return self._abort(
                "пустой список активных логинов от сайта при непустой управляемой зоне", apply
            )

        grace_cutoff = self._now() - timedelta(minutes=self._grace_minutes)
        stale = [
            account
            for account in zone_accounts
            if account.username not in active_usernames and account.created_at < grace_cutoff
        ]

        if len(stale) > self._max_disable:
            return self._abort(
                f"к отключению {len(stale)} учёток, порог {self._max_disable}", apply
            )

        if len(stale) * 100 > self._max_disable_pct * len(zone_accounts):
            return self._abort(
                f"к отключению {len(stale)} из {len(zone_accounts)} "
                f"(порог {self._max_disable_pct}% зоны)",
                apply,
            )

        if not apply:
            logger.info(
                "сверка (только журнал): отключила бы %d из %d учёток зоны: %s",
                len(stale),
                len(zone_accounts),
                ", ".join(account.username for account in stale) or "никого",
                extra={"event": "reconcile_dry_run"},
            )
            return ReconcileResult(
                tuple(account.username for account in stale), aborted=False, applied=False
            )

        disabled: list[str] = []
        for account in stale:
            self._directory.ensure_disabled(account.dn)
            self._directory.move_to_ou(account.dn, self._ou_disabled)
            disabled.append(account.username)

        logger.info(
            "сверка завершена: отключено %d из %d учёток зоны",
            len(disabled),
            len(zone_accounts),
            extra={"event": "reconcile_done"},
        )
        return ReconcileResult(tuple(disabled), aborted=False, applied=True)

    def _abort(self, reason: str, apply: bool) -> ReconcileResult:
        logger.error("Сверка отменена: %s", reason, extra={"event": "reconcile_aborted"})
        return ReconcileResult((), aborted=True, applied=apply, abort_reason=reason)
