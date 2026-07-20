"""DTO модели контракта LMS: задания из ``GET /ad/jobs`` и тело ``POST /ad/ack``.

Схемы и дискриминация по полю ``event`` описаны в ``.docs/FS_LMS_API.md`` §3.
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


class PromoteJob(BaseModel):
    """Задание на идемпотентную проверку «всё на месте» для зачисленного ученика."""

    id: int
    event: Literal["promote"]
    idempotency_key: str
    username: str


class DeprovisionJob(BaseModel):
    """Задание на отключение учётной записи и перенос в OU «Отчисленные»."""

    id: int
    event: Literal["deprovision"]
    idempotency_key: str
    username: str


Job = Annotated[ProvisionJob | PromoteJob | DeprovisionJob, Field(discriminator="event")]


class JobsResponse(BaseModel):
    """Тело ответа ``GET /ad/jobs``."""

    jobs: list[Job]


class AckRequest(BaseModel):
    """Тело запроса ``POST /ad/ack``. ``sam_account_name`` не отправляется — не используется WP."""

    id: int
    status: Literal["done", "failed"]
    error: str | None = None


class ActiveUsernamesResponse(BaseModel):
    """Тело ответа ``GET /ad/active-usernames``."""

    usernames: list[str]
