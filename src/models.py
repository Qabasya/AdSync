"""DTO контракта с сайтом (push): задания `POST /v1/jobs` и сверка `POST /v1/reconcile`.

Сайт сам присылает задания; схемы и дискриминация по полю ``event`` описаны в
``.docs/AdSyncPythonService.md`` §4.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class ProvisionJob(BaseModel):
    """Задание на создание учётной записи ученика в AD."""

    id: int
    event: Literal["provision"]
    idempotency_key: str
    username: str
    password: str
    first: str
    last: str
    subject_key: str


class DeprovisionJob(BaseModel):
    """Задание на отключение учётной записи и перенос в OU «Отчисленные»."""

    id: int
    event: Literal["deprovision"]
    idempotency_key: str
    username: str


class PasswordJob(BaseModel):
    """Задание на смену пароля: администратор сменил пароль ученика на сайте."""

    id: int
    event: Literal["password"]
    idempotency_key: str
    username: str
    password: str


# Что сделано с учёткой — сайт пишет это в журнал «Зачисления».
# `absent` — deprovision, а учётки в домене нет (цель и так достигнута).
Outcome = Literal[
    "created", "reactivated", "updated", "deprovisioned", "absent", "password_changed"
]

Job = Annotated[ProvisionJob | DeprovisionJob | PasswordJob, Field(discriminator="event")]


class JobResultResponse(BaseModel):
    """Ответ на `POST /v1/jobs` — итог задания, синхронно.

    `failed` — ошибка самого задания (сайт потратит попытку и повторит с бэкоффом);
    недоступность DC отвечается не этим телом, а кодом `503`.
    """

    status: Literal["done", "failed"]
    error: str | None = None
    outcome: Outcome | None = None


class ReconcileRequest(BaseModel):
    """Тело `POST /v1/reconcile`: кто должен остаться активным, и отключать ли остальных."""

    usernames: list[str]
    apply: bool = False


class ReconcileResponse(BaseModel):
    """Ответ сверки. `disabled` — отключённые (при `apply: false` — кого отключили бы)."""

    status: Literal["ok", "aborted"]
    applied: bool
    disabled: list[str]
    abort_reason: str | None = None
