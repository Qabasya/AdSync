"""Обработчики заданий: бизнес-ветвление поверх примитивов `DirectoryGateway`.

Реализует таблицу «События → действия в AD» и «Правила поверх таблицы» из `.docs/CLAUDE.md`.
Про HTTP/журнал не знает — это `jobs.py`, который также ловит любые исключения из
`handle()` (LDAP-ошибки и т.п.) и превращает их в `failed` (кроме недоступности DC — она
уходит сайту кодом `503`).
"""

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from ad import DirectoryGateway
from config import SubjectConfig
from models import DeprovisionJob, Job, Outcome, PasswordJob, ProvisionJob

logger = logging.getLogger("adsync.handlers")


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """Итог обработки одного задания."""

    status: Literal["done", "failed"]
    error: str | None = None
    # Что именно сделано с учёткой (для журнала сайта); у `failed` — None.
    outcome: Outcome | None = None


class JobHandler(Protocol):
    """Контракт обработчика одного типа задания."""

    def handle(self, job: Job) -> HandlerResult:
        """Обрабатывает задание и возвращает итог (без ответа сайту и без записи в журнал)."""
        ...


class ProvisionHandler:
    """Обработчик `provision`: создание учётки или её реактивация/обновление."""

    def __init__(
        self,
        directory: DirectoryGateway,
        *,
        subjects: dict[str, SubjectConfig],
        ou_fallback: str,
    ) -> None:
        self._directory = directory
        self._subjects = subjects
        self._ou_fallback = ou_fallback

    def handle(self, job: Job) -> HandlerResult:
        assert isinstance(job, ProvisionJob)

        subject = self._subjects.get(job.subject_key)
        if subject is not None:
            target_ou, target_group = subject.ou_dn, subject.group_dn
        else:
            target_ou, target_group = self._ou_fallback, None
            logger.warning(
                "Неизвестный subject_key %r у задания на %s, учётка создаётся в fallback-OU",
                job.subject_key,
                job.username,
                extra={"event": "subject_unmapped"},
            )

        existing = self._directory.find_user(job.username)

        if existing is None:
            dn = self._directory.create_user(
                ou_dn=target_ou, username=job.username, first=job.first, last=job.last
            )
            # create_user создаёт отключённой (у AD без пароля включить нельзя — WILL_NOT_PERFORM);
            # включаем только после того, как пароль реально задан.
            self._directory.ensure_password(dn, job.password)
            self._directory.ensure_enabled(dn)
            if target_group is not None:
                self._directory.ensure_group_membership(dn, target_group)
            self._directory.ensure_account_settings(dn, job.username)
            logger.info(
                "создана учётка %s в %s",
                job.username,
                target_ou,
                extra={"event": "account_created"},
            )
            return HandlerResult("done", outcome="created")

        if self._directory.is_in_disabled_ou(existing.dn):
            self._directory.ensure_enabled(existing.dn)
            new_dn = self._directory.move_to_ou(existing.dn, target_ou)
            self._directory.ensure_password(new_dn, job.password)
            if target_group is not None:
                self._directory.ensure_group_membership(new_dn, target_group)
            self._directory.ensure_account_settings(new_dn, job.username)
            logger.info(
                "реактивирована учётка %s: %s → %s",
                job.username,
                existing.dn,
                target_ou,
                extra={"event": "account_reactivated"},
            )
            return HandlerResult("done", outcome="reactivated")

        if self._directory.is_in_managed_zone(existing.dn):
            self._directory.ensure_password(existing.dn, job.password)
            # На случай, если предыдущая попытка упала между паролем и включением (см. ветку
            # выше) — без этого учётка так и осталась бы отключённой на все последующие ретраи,
            # т.к. эта ветка «уже существует в зоне» иначе состояние enabled не трогает.
            self._directory.ensure_enabled(existing.dn)
            if target_group is not None:
                self._directory.ensure_group_membership(existing.dn, target_group)
            self._directory.ensure_account_settings(existing.dn, job.username)
            logger.info(
                "обновлена учётка %s в зоне", job.username, extra={"event": "account_updated"}
            )
            return HandlerResult("done", outcome="updated")

        logger.error(
            "provision %s: учётная запись вне управляемой зоны, объект не тронут",
            job.username,
            extra={"event": "zone_violation"},
        )
        return HandlerResult("failed", error="учётная запись вне управляемой зоны")


class DeprovisionHandler:
    """Обработчик `deprovision`: отключение и перенос в OU «Отчисленные»."""

    def __init__(self, directory: DirectoryGateway, *, ou_disabled: str) -> None:
        self._directory = directory
        self._ou_disabled = ou_disabled

    def handle(self, job: Job) -> HandlerResult:
        assert isinstance(job, DeprovisionJob)

        user = self._directory.find_user(job.username)
        if user is None:
            logger.info(
                "deprovision %s: учётки уже нет, цель достигнута",
                job.username,
                extra={"event": "account_deprovisioned"},
            )
            return HandlerResult("done", outcome="absent")

        if not self._directory.is_in_managed_zone(user.dn):
            logger.error(
                "deprovision %s: учётная запись вне управляемой зоны, объект не тронут",
                job.username,
                extra={"event": "zone_violation"},
            )
            return HandlerResult("failed", error="учётная запись вне управляемой зоны")

        # Цель — оба условия сразу: отключена И лежит в «Отчисленных». Приводим каждое
        # отдельно — учётку могли вручную включить обратно или перенести, не отключив.
        if user.enabled:
            self._directory.ensure_disabled(user.dn)
        if not self._directory.is_in_disabled_ou(user.dn):
            self._directory.move_to_ou(user.dn, self._ou_disabled)
        logger.info(
            "deprovision %s: отключена, в %s",
            job.username,
            self._ou_disabled,
            extra={"event": "account_deprovisioned"},
        )
        return HandlerResult("done", outcome="deprovisioned")


class PasswordHandler:
    """Обработчик `password`: тот же пароль, что администратор задал на сайте.

    Только пароль — учётку не включает и не переносит: отключённая остаётся отключённой
    (включает её `provision` при повторном зачислении). Нет учётки — `failed`: цель не
    достигнута, и после 6 попыток администратор увидит задание «мёртвым».
    """

    def __init__(self, directory: DirectoryGateway) -> None:
        self._directory = directory

    def handle(self, job: Job) -> HandlerResult:
        assert isinstance(job, PasswordJob)

        user = self._directory.find_user(job.username)
        if user is None:
            logger.error(
                "смена пароля %s: учётки нет в домене",
                job.username,
                extra={"event": "password_account_missing"},
            )
            return HandlerResult("failed", error="учётки нет в домене")

        if not self._directory.is_in_managed_zone(user.dn):
            logger.error(
                "смена пароля %s: учётная запись вне управляемой зоны, объект не тронут",
                job.username,
                extra={"event": "zone_violation"},
            )
            return HandlerResult("failed", error="учётная запись вне управляемой зоны")

        self._directory.ensure_password(user.dn, job.password)
        # Заодно — путь к профилю и «пароль без срока»: так приводятся учётки, созданные
        # до появления этих настроек (достаточно сменить пароль на сайте).
        self._directory.ensure_account_settings(user.dn, job.username)
        logger.info(
            "смена пароля %s: пароль обновлён", job.username, extra={"event": "password_changed"}
        )
        return HandlerResult("done", outcome="password_changed")
