"""Тесты обработчиков заданий на `FakeDirectoryGateway` (без сети, без реального AD)."""

import logging

import pytest
from fakes import FakeDirectoryGateway

from config import SubjectConfig
from handlers import DeprovisionHandler, PromoteHandler, ProvisionHandler
from models import DeprovisionJob, PromoteJob, ProvisionJob

OU_SUBJECT = "OU=KEGE,OU=Ученики,DC=fs,DC=loc"
GROUP_SUBJECT = "CN=KEGE,OU=Группы,DC=fs,DC=loc"
OU_DISABLED = "OU=Отчисленные,DC=fs,DC=loc"
OU_FALLBACK = "OU=Без направления,DC=fs,DC=loc"
OU_OUTSIDE = "OU=Other,DC=fs,DC=loc"

SUBJECTS = {"inf-ege": SubjectConfig(ou_dn=OU_SUBJECT, group_dn=GROUP_SUBJECT)}


def make_directory() -> FakeDirectoryGateway:
    return FakeDirectoryGateway(
        zone_ou_dns={OU_SUBJECT}, ou_disabled=OU_DISABLED, ou_fallback=OU_FALLBACK
    )


def provision_job(**overrides: object) -> ProvisionJob:
    payload: dict[str, object] = {
        "id": 1,
        "event": "provision",
        "idempotency_key": "key-1",
        "username": "ivanov",
        "password": "s3cret",
        "first": "Иван",
        "last": "Иванов",
        "subject_key": "inf-ege",
    }
    payload.update(overrides)
    return ProvisionJob.model_validate(payload)


class TestProvisionHandler:
    def test_new_user_known_subject(self, caplog: pytest.LogCaptureFixture) -> None:
        directory = make_directory()
        handler = ProvisionHandler(directory, subjects=SUBJECTS, ou_fallback=OU_FALLBACK)

        with caplog.at_level(logging.INFO, logger="adsync.handlers"):
            result = handler.handle(provision_job())

        assert result.status == "done"
        user = directory.users_by_username["ivanov"]
        assert user.dn.endswith(OU_SUBJECT)
        assert directory.passwords[user.dn] == "s3cret"
        assert user.dn in directory.group_members[GROUP_SUBJECT]
        assert "ivanov" in caplog.text

    def test_new_user_unknown_subject(self, caplog: pytest.LogCaptureFixture) -> None:
        directory = make_directory()
        handler = ProvisionHandler(directory, subjects=SUBJECTS, ou_fallback=OU_FALLBACK)

        with caplog.at_level(logging.WARNING, logger="adsync.handlers"):
            result = handler.handle(provision_job(subject_key="unknown-subject"))

        assert result.status == "done"
        user = directory.users_by_username["ivanov"]
        assert user.dn.endswith(OU_FALLBACK)
        assert directory.group_members == {}
        assert "unknown-subject" in caplog.text

    def test_existing_user_in_zone_updates_password_and_group(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        directory = make_directory()
        dn = directory.create_user(ou_dn=OU_SUBJECT, username="ivanov", first="Иван", last="Иванов")
        handler = ProvisionHandler(directory, subjects=SUBJECTS, ou_fallback=OU_FALLBACK)

        with caplog.at_level(logging.INFO, logger="adsync.handlers"):
            result = handler.handle(provision_job(password="new-pass"))

        assert result.status == "done"
        assert directory.users_by_username["ivanov"].dn == dn
        assert directory.passwords[dn] == "new-pass"
        assert dn in directory.group_members[GROUP_SUBJECT]
        assert "ivanov" in caplog.text

    def test_existing_user_in_disabled_ou_is_reactivated(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        directory = make_directory()
        dn = directory.create_user(
            ou_dn=OU_DISABLED, username="ivanov", first="Иван", last="Иванов"
        )
        directory.ensure_disabled(dn)
        handler = ProvisionHandler(directory, subjects=SUBJECTS, ou_fallback=OU_FALLBACK)

        with caplog.at_level(logging.INFO, logger="adsync.handlers"):
            result = handler.handle(provision_job())

        assert result.status == "done"
        user = directory.users_by_username["ivanov"]
        assert user.enabled is True
        assert user.dn.endswith(OU_SUBJECT)
        assert directory.passwords[user.dn] == "s3cret"
        assert user.dn in directory.group_members[GROUP_SUBJECT]
        assert "реактивирована" in caplog.text

    def test_existing_user_outside_zone_fails_without_touching(self) -> None:
        directory = make_directory()
        dn = directory.create_user(ou_dn=OU_OUTSIDE, username="ivanov", first="Иван", last="Иванов")
        handler = ProvisionHandler(directory, subjects=SUBJECTS, ou_fallback=OU_FALLBACK)

        result = handler.handle(provision_job())

        assert result.status == "failed"
        assert result.error
        assert directory.users_by_username["ivanov"].dn == dn
        assert dn not in directory.passwords
        assert directory.group_members == {}

    def test_repeated_job_is_idempotent(self) -> None:
        directory = make_directory()
        handler = ProvisionHandler(directory, subjects=SUBJECTS, ou_fallback=OU_FALLBACK)
        job = provision_job()

        first = handler.handle(job)
        second = handler.handle(job)

        assert first.status == "done"
        assert second.status == "done"
        dn = directory.users_by_username["ivanov"].dn
        assert directory.group_members[GROUP_SUBJECT] == {dn}


def promote_job(**overrides: object) -> PromoteJob:
    payload: dict[str, object] = {
        "id": 2,
        "event": "promote",
        "idempotency_key": "key-2",
        "username": "ivanov",
    }
    payload.update(overrides)
    return PromoteJob.model_validate(payload)


class TestPromoteHandler:
    def test_missing_user_fails(self) -> None:
        directory = make_directory()
        handler = PromoteHandler(directory)

        result = handler.handle(promote_job())

        assert result.status == "failed"
        assert result.error

    def test_enabled_user_in_zone_done(self, caplog: pytest.LogCaptureFixture) -> None:
        directory = make_directory()
        directory.create_user(ou_dn=OU_SUBJECT, username="ivanov", first="Иван", last="Иванов")
        handler = PromoteHandler(directory)

        with caplog.at_level(logging.INFO, logger="adsync.handlers"):
            result = handler.handle(promote_job())

        assert result.status == "done"
        assert "ivanov" in caplog.text

    def test_disabled_user_fails(self) -> None:
        directory = make_directory()
        dn = directory.create_user(ou_dn=OU_SUBJECT, username="ivanov", first="Иван", last="Иванов")
        directory.ensure_disabled(dn)
        handler = PromoteHandler(directory)

        result = handler.handle(promote_job())

        assert result.status == "failed"

    def test_user_outside_zone_fails(self) -> None:
        directory = make_directory()
        directory.create_user(ou_dn=OU_OUTSIDE, username="ivanov", first="Иван", last="Иванов")
        handler = PromoteHandler(directory)

        result = handler.handle(promote_job())

        assert result.status == "failed"


def deprovision_job(**overrides: object) -> DeprovisionJob:
    payload: dict[str, object] = {
        "id": 3,
        "event": "deprovision",
        "idempotency_key": "key-3",
        "username": "ivanov",
    }
    payload.update(overrides)
    return DeprovisionJob.model_validate(payload)


class TestDeprovisionHandler:
    def test_missing_user_is_done_without_side_effects(self) -> None:
        directory = make_directory()
        handler = DeprovisionHandler(directory, ou_disabled=OU_DISABLED)

        result = handler.handle(deprovision_job())

        assert result.status == "done"
        assert directory.users_by_username == {}

    def test_enabled_user_in_zone_is_disabled_and_moved(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        directory = make_directory()
        directory.create_user(ou_dn=OU_SUBJECT, username="ivanov", first="Иван", last="Иванов")
        handler = DeprovisionHandler(directory, ou_disabled=OU_DISABLED)

        with caplog.at_level(logging.INFO, logger="adsync.handlers"):
            result = handler.handle(deprovision_job())

        assert result.status == "done"
        user = directory.users_by_username["ivanov"]
        assert user.enabled is False
        assert user.dn.endswith(OU_DISABLED)
        assert "ivanov" in caplog.text

    def test_already_disabled_user_is_done_without_further_changes(self) -> None:
        directory = make_directory()
        dn = directory.create_user(ou_dn=OU_SUBJECT, username="ivanov", first="Иван", last="Иванов")
        directory.ensure_disabled(dn)
        handler = DeprovisionHandler(directory, ou_disabled=OU_DISABLED)

        result = handler.handle(deprovision_job())

        assert result.status == "done"
        user = directory.users_by_username["ivanov"]
        assert user.dn == dn
        assert user.enabled is False

    def test_user_outside_zone_fails_without_touching(self) -> None:
        directory = make_directory()
        dn = directory.create_user(ou_dn=OU_OUTSIDE, username="ivanov", first="Иван", last="Иванов")
        handler = DeprovisionHandler(directory, ou_disabled=OU_DISABLED)

        result = handler.handle(deprovision_job())

        assert result.status == "failed"
        user = directory.users_by_username["ivanov"]
        assert user.dn == dn
        assert user.enabled is True
