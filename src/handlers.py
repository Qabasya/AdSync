"""Обработчики заданий: бизнес-ветвление поверх примитивов `DirectoryGateway`.

Реализует таблицу «События → действия в AD» и «Правила поверх таблицы» из `.docs/CLAUDE.md`.
Про HTTP/ack/журнал не знает — это `poller.py`, который также ловит любые исключения из
`handle()` (LDAP-ошибки и т.п.) и превращает их в `ack(failed)`.
"""

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from ad import DirectoryGateway
from config import SubjectConfig
from models import DeprovisionJob, Job, PromoteJob, ProvisionJob

logger = logging.getLogger("adsync.handlers")


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """Итог обработки одного задания."""

    status: Literal["done", "failed"]
    error: str | None = None


class JobHandler(Protocol):
    """Контракт обработчика одного типа задания."""

    def handle(self, job: Job) -> HandlerResult:
        """Обрабатывает задание и возвращает итог (без ack и без записи в журнал)."""
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
            )

        existing = self._directory.find_user(job.username)

        if existing is None:
            dn = self._directory.create_user(
                ou_dn=target_ou, username=job.username, first=job.first, last=job.last
            )
            self._directory.ensure_password(dn, job.password)
            if target_group is not None:
                self._directory.ensure_group_membership(dn, target_group)
            return HandlerResult("done")

        if self._directory.is_in_disabled_ou(existing.dn):
            self._directory.ensure_enabled(existing.dn)
            new_dn = self._directory.move_to_ou(existing.dn, target_ou)
            self._directory.ensure_password(new_dn, job.password)
            if target_group is not None:
                self._directory.ensure_group_membership(new_dn, target_group)
            return HandlerResult("done")

        if self._directory.is_in_managed_zone(existing.dn):
            self._directory.ensure_password(existing.dn, job.password)
            if target_group is not None:
                self._directory.ensure_group_membership(existing.dn, target_group)
            return HandlerResult("done")

        logger.error("Учётная запись %s вне управляемой зоны, объект не тронут", job.username)
        return HandlerResult("failed", error="учётная запись вне управляемой зоны")


class PromoteHandler:
    """Обработчик `promote`: идемпотентная проверка «всё на месте»."""

    def __init__(self, directory: DirectoryGateway) -> None:
        self._directory = directory

    def handle(self, job: Job) -> HandlerResult:
        assert isinstance(job, PromoteJob)

        user = self._directory.find_user(job.username)
        if user is None:
            return HandlerResult("failed", error="учётная запись не найдена")
        if user.enabled and self._directory.is_in_managed_zone(user.dn):
            return HandlerResult("done")
        return HandlerResult("failed", error="учётная запись отключена или вне управляемой зоны")


class DeprovisionHandler:
    """Обработчик `deprovision`: отключение и перенос в OU «Отчисленные»."""

    def __init__(self, directory: DirectoryGateway, *, ou_disabled: str) -> None:
        self._directory = directory
        self._ou_disabled = ou_disabled

    def handle(self, job: Job) -> HandlerResult:
        assert isinstance(job, DeprovisionJob)

        user = self._directory.find_user(job.username)
        if user is None:
            return HandlerResult("done")

        if not self._directory.is_in_managed_zone(user.dn):
            logger.error("Учётная запись %s вне управляемой зоны, объект не тронут", job.username)
            return HandlerResult("failed", error="учётная запись вне управляемой зоны")

        if not user.enabled:
            return HandlerResult("done")

        self._directory.ensure_disabled(user.dn)
        self._directory.move_to_ou(user.dn, self._ou_disabled)
        return HandlerResult("done")
