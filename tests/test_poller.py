"""Тесты цикла заданий (`poller.py`) на фейках, без сети/AD."""

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeDirectoryGateway, FakeLmsApi

from config import SubjectConfig
from handlers import DeprovisionHandler, HandlerResult, PromoteHandler, ProvisionHandler
from models import DeprovisionJob, Job, PromoteJob, ProvisionJob
from poller import Poller
from repository import JobLogEntry, JobRepository

OU_SUBJECT = "OU=KEGE,OU=Ученики,DC=fs,DC=loc"
GROUP_SUBJECT = "CN=KEGE,OU=Группы,DC=fs,DC=loc"
OU_DISABLED = "OU=Отчисленные,DC=fs,DC=loc"
OU_FALLBACK = "OU=Без направления,DC=fs,DC=loc"

SUBJECTS = {"inf-ege": SubjectConfig(ou_dn=OU_SUBJECT, group_dn=GROUP_SUBJECT)}

_NOW = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)


class RaisingHandler:
    """Тестовый обработчик, всегда падающий с исключением."""

    def handle(self, job: Job) -> HandlerResult:
        raise RuntimeError("boom")


class RaisingForUsernameHandler:
    """Тестовый обработчик: падает только для одного username, иначе делегирует реальному."""

    def __init__(self, delegate: object, *, failing_username: str) -> None:
        self._delegate = delegate
        self._failing_username = failing_username

    def handle(self, job: Job) -> HandlerResult:
        if job.username == self._failing_username:
            raise RuntimeError("boom")
        return self._delegate.handle(job)  # type: ignore[attr-defined]


def make_directory() -> FakeDirectoryGateway:
    return FakeDirectoryGateway(
        zone_ou_dns={OU_SUBJECT}, ou_disabled=OU_DISABLED, ou_fallback=OU_FALLBACK
    )


def make_handlers(directory: FakeDirectoryGateway) -> dict[str, object]:
    return {
        "provision": ProvisionHandler(directory, subjects=SUBJECTS, ou_fallback=OU_FALLBACK),
        "promote": PromoteHandler(directory),
        "deprovision": DeprovisionHandler(directory, ou_disabled=OU_DISABLED),
    }


def make_poller(
    lms: FakeLmsApi, repository: JobRepository, handlers: dict[str, object], jobs_limit: int = 50
) -> Poller:
    return Poller(
        lms,
        handlers=handlers,  # type: ignore[arg-type]
        repository=repository,
        jobs_limit=jobs_limit,
        now=lambda: _NOW,
    )


def _rows(db_path: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT job_id, idempotency_key, event, username, subject_key, status, error,"
        " received_at, acked_at FROM jobs ORDER BY id"
    ).fetchall()
    connection.close()
    return rows


def test_mixed_batch_acks_and_journals_each_job(tmp_path: Path) -> None:
    directory = make_directory()
    directory.create_user(ou_dn=OU_SUBJECT, username="petrov", first="Пётр", last="Петров")
    directory.create_user(ou_dn=OU_SUBJECT, username="sidorov", first="Сидор", last="Сидоров")

    jobs: list[Job] = [
        ProvisionJob(
            id=1,
            event="provision",
            idempotency_key="k1",
            username="ivanov",
            password="s3cret",
            first="Иван",
            last="Иванов",
            subject_key="inf-ege",
        ),
        PromoteJob(id=2, event="promote", idempotency_key="k2", username="petrov"),
        DeprovisionJob(id=3, event="deprovision", idempotency_key="k3", username="sidorov"),
    ]
    lms = FakeLmsApi(jobs=jobs)
    repository = JobRepository(tmp_path / "state.db")
    handlers = make_handlers(directory)
    poller = make_poller(lms, repository, handlers)

    poller.run_once()
    repository.close()

    assert [ack.id for ack in lms.acks] == [1, 2, 3]
    assert all(ack.status == "done" for ack in lms.acks)

    rows = _rows(tmp_path / "state.db")
    assert len(rows) == 3
    by_job_id = {row["job_id"]: row for row in rows}
    assert by_job_id[1]["event"] == "provision"
    assert by_job_id[1]["subject_key"] == "inf-ege"
    assert by_job_id[2]["subject_key"] is None
    assert by_job_id[3]["subject_key"] is None
    assert by_job_id[1]["received_at"] == _NOW.isoformat()
    assert by_job_id[1]["acked_at"] == _NOW.isoformat()


def test_handler_error_is_isolated_and_batch_continues(tmp_path: Path) -> None:
    directory = make_directory()
    directory.create_user(ou_dn=OU_SUBJECT, username="petrov", first="Пётр", last="Петров")

    jobs: list[Job] = [
        PromoteJob(id=1, event="promote", idempotency_key="k1", username="missing"),
        PromoteJob(id=2, event="promote", idempotency_key="k2", username="petrov"),
    ]
    lms = FakeLmsApi(jobs=jobs)
    repository = JobRepository(tmp_path / "state.db")
    real_promote_handler = PromoteHandler(directory)
    handlers: dict[str, object] = {
        "promote": RaisingForUsernameHandler(real_promote_handler, failing_username="missing"),
    }
    poller = make_poller(lms, repository, handlers)

    poller.run_once()
    repository.close()

    assert [ack.status for ack in lms.acks] == ["failed", "done"]
    rows = _rows(tmp_path / "state.db")
    assert len(rows) == 2
    assert rows[0]["status"] == "failed"
    assert rows[0]["error"] == "boom"
    assert rows[1]["status"] == "done"


def test_get_jobs_error_skips_tick_without_crashing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    lms = FakeLmsApi(get_jobs_error=RuntimeError("network down"))
    repository = JobRepository(tmp_path / "state.db")
    poller = make_poller(lms, repository, {})

    with caplog.at_level(logging.ERROR, logger="adsync.poller"):
        poller.run_once()
    repository.close()

    assert lms.acks == []
    assert _rows(tmp_path / "state.db") == []
    assert "не удалось получить задания" in caplog.text


def test_ack_error_skips_journal_entry(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    directory = make_directory()
    jobs: list[Job] = [
        DeprovisionJob(id=1, event="deprovision", idempotency_key="k1", username="ghost"),
    ]
    lms = FakeLmsApi(jobs=jobs, ack_error=RuntimeError("ack network error"))
    repository = JobRepository(tmp_path / "state.db")
    handlers = make_handlers(directory)
    poller = make_poller(lms, repository, handlers)

    with caplog.at_level(logging.ERROR, logger="adsync.poller"):
        poller.run_once()
    repository.close()

    assert lms.acks == []
    assert _rows(tmp_path / "state.db") == []
    assert "не удалось отправить ack" in caplog.text


def test_dead_threshold_logs_error_on_sixth_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repository = JobRepository(tmp_path / "state.db")

    for i in range(5):
        repository.record(
            JobLogEntry(
                job_id=i,
                idempotency_key="dead-key",
                event="promote",
                username="ghost",
                subject_key=None,
                status="failed",
                error="e",
                received_at=_NOW,
                acked_at=_NOW,
            )
        )

    jobs: list[Job] = [
        PromoteJob(id=6, event="promote", idempotency_key="dead-key", username="ghost"),
    ]
    lms = FakeLmsApi(jobs=jobs)
    handlers: dict[str, object] = {"promote": RaisingHandler()}
    poller = make_poller(lms, repository, handlers)

    with caplog.at_level(logging.ERROR, logger="adsync.poller"):
        poller.run_once()
    repository.close()

    assert "мертво" in caplog.text


def test_missing_handler_for_event_fails_gracefully(tmp_path: Path) -> None:
    jobs: list[Job] = [
        PromoteJob(id=1, event="promote", idempotency_key="k1", username="ghost"),
    ]
    lms = FakeLmsApi(jobs=jobs)
    repository = JobRepository(tmp_path / "state.db")
    poller = make_poller(lms, repository, {})

    poller.run_once()
    repository.close()

    assert lms.acks[0].status == "failed"
    assert lms.acks[0].error
    rows = _rows(tmp_path / "state.db")
    assert rows[0]["status"] == "failed"
