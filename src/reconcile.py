"""Сверка активных учёток управляемой зоны со списком от LMS — с предохранителями.

Односторонняя: только отключает лишних (по пути `DeprovisionHandler`), никого не включает,
не создаёт и не переносит обратно. Про журнал заданий не знает — сверка не привязана к
`job_id`/`idempotency_key` WP, это не обработка задания.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ad import DirectoryGateway
from lms import LmsApi

logger = logging.getLogger("adsync.reconcile")


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Итог одного прогона сверки."""

    disabled_usernames: tuple[str, ...]
    aborted: bool
    abort_reason: str | None = None


class Reconciler:
    """Сверяет активные учётки управляемой зоны со списком от LMS."""

    def __init__(
        self,
        lms: LmsApi,
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
            lms: клиент LMS (`get_active_usernames`).
            directory: шлюз AD (`list_zone_accounts`/`ensure_disabled`/`move_to_ou`).
            ou_disabled: DN OU «Отчисленные» — куда переносятся отключённые учётки.
            max_disable: порог `RECONCILE_MAX_DISABLE`.
            max_disable_pct: порог `RECONCILE_MAX_DISABLE_PCT` (0–100).
            grace_minutes: `RECONCILE_GRACE_MINUTES` — свежие учётки не трогаются.
            now: источник текущего времени (UTC) — внедряется для тестируемости.
        """
        self._lms = lms
        self._directory = directory
        self._ou_disabled = ou_disabled
        self._max_disable = max_disable
        self._max_disable_pct = max_disable_pct
        self._grace_minutes = grace_minutes
        self._now = now

    def run_once(self) -> ReconcileResult:
        """Один прогон сверки. Ошибка предохранителя — abort, никто не тронут."""
        zone_accounts = self._directory.list_zone_accounts()

        try:
            active_usernames = set(self._lms.get_active_usernames())
        except Exception:
            logger.exception(
                "не удалось получить список активных логинов из LMS",
                extra={"event": "lms_active_logins_fetch_error"},
            )
            return self._abort("сбой получения списка от LMS")

        if not active_usernames and zone_accounts:
            return self._abort(
                "пустой список активных логинов от LMS при непустой управляемой зоне"
            )

        grace_cutoff = self._now() - timedelta(minutes=self._grace_minutes)
        stale = [
            account
            for account in zone_accounts
            if account.username not in active_usernames and account.created_at < grace_cutoff
        ]

        if len(stale) > self._max_disable:
            return self._abort(f"к отключению {len(stale)} учёток, порог {self._max_disable}")

        if len(stale) * 100 > self._max_disable_pct * len(zone_accounts):
            return self._abort(
                f"к отключению {len(stale)} из {len(zone_accounts)} "
                f"(порог {self._max_disable_pct}% зоны)"
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
        return ReconcileResult(tuple(disabled), aborted=False)

    def _abort(self, reason: str) -> ReconcileResult:
        logger.error("Сверка отменена: %s", reason, extra={"event": "reconcile_aborted"})
        return ReconcileResult((), aborted=True, abort_reason=reason)
