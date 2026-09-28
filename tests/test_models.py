"""Тесты DTO-моделей контракта с сайтом (`models.py`)."""

import pytest
from pydantic import TypeAdapter, ValidationError

from models import (
    DeprovisionJob,
    Job,
    JobResultResponse,
    PasswordJob,
    ProvisionJob,
    ReconcileRequest,
)

_JOB = TypeAdapter(Job)


def test_provision_job_parses_from_raw_payload() -> None:
    job = ProvisionJob.model_validate(
        {
            "id": 7,
            "event": "provision",
            "idempotency_key": "app:5",
            "username": "i.petrov",
            "password": "СекретУченика",
            "first": "Иван",
            "last": "Петров",
            "subject_key": "inf",
        }
    )
    assert job.username == "i.petrov"
    assert job.subject_key == "inf"


def test_deprovision_job_parses_from_raw_payload() -> None:
    job = DeprovisionJob.model_validate(
        {
            "id": 8,
            "event": "deprovision",
            "idempotency_key": "deprovision:app:9",
            "username": "a.sidorov",
        }
    )
    assert job.username == "a.sidorov"


def test_job_adapter_resolves_event_by_discriminator() -> None:
    provision = _JOB.validate_python(
        {
            "id": 7,
            "event": "provision",
            "idempotency_key": "app:5",
            "username": "i.petrov",
            "password": "СекретУченика",
            "first": "Иван",
            "last": "Петров",
            "subject_key": "inf",
        }
    )
    deprovision = _JOB.validate_json(
        '{"id": 8, "event": "deprovision", "idempotency_key": "deprovision:app:9", '
        '"username": "a.sidorov"}'
    )
    assert isinstance(provision, ProvisionJob)
    assert isinstance(deprovision, DeprovisionJob)


def test_unknown_event_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        _JOB.validate_python({"id": 1, "event": "unknown", "idempotency_key": "x", "username": "u"})


def test_provision_job_missing_password_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        ProvisionJob.model_validate(
            {
                "id": 7,
                "event": "provision",
                "idempotency_key": "app:5",
                "username": "i.petrov",
                "first": "Иван",
                "last": "Петров",
                "subject_key": "inf",
            }
        )


def test_job_result_serializes_without_error_when_done() -> None:
    assert JobResultResponse(status="done").model_dump(exclude_none=True) == {"status": "done"}


def test_job_result_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        JobResultResponse.model_validate({"status": "ok"})


def test_reconcile_request_defaults_to_dry_run() -> None:
    request = ReconcileRequest.model_validate_json('{"usernames": ["i.petrov", "a.sidorov"]}')
    assert request.usernames == ["i.petrov", "a.sidorov"]
    assert request.apply is False


def test_password_job_resolved_by_discriminator() -> None:
    job = _JOB.validate_python(
        {
            "id": 9,
            "event": "password",
            "idempotency_key": "password:person:42:1",
            "username": "i.petrov",
            "password": "Новый123",
        }
    )
    assert isinstance(job, PasswordJob)
