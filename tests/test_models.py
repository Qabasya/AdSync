"""Тесты DTO-моделей контракта LMS (`models.py`)."""

import pytest
from pydantic import ValidationError

from models import (
    AckRequest,
    ActiveUsernamesResponse,
    DeprovisionJob,
    JobsResponse,
    ProvisionJob,
)


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


def test_jobs_response_resolves_mixed_list_by_discriminator() -> None:
    response = JobsResponse.model_validate(
        {
            "jobs": [
                {
                    "id": 7,
                    "event": "provision",
                    "idempotency_key": "app:5",
                    "username": "i.petrov",
                    "password": "СекретУченика",
                    "first": "Иван",
                    "last": "Петров",
                    "subject_key": "inf",
                },
                {
                    "id": 8,
                    "event": "deprovision",
                    "idempotency_key": "deprovision:app:9",
                    "username": "a.sidorov",
                },
            ]
        }
    )
    assert isinstance(response.jobs[0], ProvisionJob)
    assert isinstance(response.jobs[1], DeprovisionJob)


def test_unknown_event_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        JobsResponse.model_validate(
            {"jobs": [{"id": 1, "event": "unknown", "idempotency_key": "x", "username": "u"}]}
        )


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


def test_ack_request_accepts_done_without_error() -> None:
    ack = AckRequest.model_validate({"id": 7, "status": "done"})
    assert ack.error is None


def test_ack_request_accepts_failed_with_error() -> None:
    ack = AckRequest.model_validate({"id": 7, "status": "failed", "error": "ldap timeout"})
    assert ack.error == "ldap timeout"


def test_ack_request_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        AckRequest.model_validate({"id": 7, "status": "ok"})


def test_active_usernames_response_parses() -> None:
    response = ActiveUsernamesResponse.model_validate(
        {"usernames": ["i.petrov", "a.sidorov", "p.orlov"]}
    )
    assert response.usernames == ["i.petrov", "a.sidorov", "p.orlov"]
