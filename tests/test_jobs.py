"""Тесты обработки одного задания (`jobs.py`) на фейках, без сети/AD."""

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeDirectoryGateway

from ad import DirectoryUnavailableError
from handlers import DeprovisionHandler, HandlerResult, JobHandler, ProvisionHandler
from jobs import JobProcessor
from models import DeprovisionJob, Job, ProvisionJob
from repository import JobRepository

OU_SUBJECT = "OU=KEGE,OU=Ученики,DC=fs,DC=loc"
OU_DISABLED = "OU=Отчисленные,DC=fs,DC=loc"
OU_FALLBACK = "OU=Без направления,DC=fs,DC=loc"
_NOW = datetime(2026, 9, 28, 10, 0, 0, tzinfo=UTC)


def _provision(job_id: int = 7, key: str = "app:5") -> ProvisionJob:
    return ProvisionJob(
        id=job_id,
        event="provision",
        idempotency_key=key,
        username="i.petrov",
        password="СекретУченика",
        first="Иван",
        last="Петров",
        subject_key="inf",
    )


class RaisingHandler:
    """Обработчик, падающий заданным исключением."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def handle(self, job: Job) -> HandlerResult:
        raise self._exc


def _processor(
    tmp_path: Path, handlers: dict[str, JobHandler] | None = None
) -> tuple[JobProcessor, JobRepository, FakeDirectoryGateway]:
    directory = FakeDirectoryGateway(
        zone_ou_dns={OU_SUBJECT}, ou_disabled=OU_DISABLED, ou_fallback=OU_FALLBACK
    )
    repository = JobRepository(tmp_path / "state.db")
    handlers = handlers or {
        "provision": ProvisionHandler(directory, subjects={}, ou_fallback=OU_FALLBACK),
        "deprovision": DeprovisionHandler(directory, ou_disabled=OU_DISABLED),
    }
    return (
        JobProcessor(handlers=handlers, repository=repository, now=lambda: _NOW),
        repository,
        directory,
    )


def test_done_is_recorded_in_journal(tmp_path: Path) -> None:
    processor, repository, directory = _processor(tmp_path)

    result = processor.process(_provision())

    assert result.status == "done"
    assert directory.find_user("i.petrov") is not None
    entry = repository.recent_entries(1)[0]
    assert (entry.job_id, entry.status, entry.subject_key) == (7, "done", "inf")


def test_handler_exception_becomes_failed_and_is_recorded(tmp_path: Path) -> None:
    processor, repository, _ = _processor(
        tmp_path, {"provision": RaisingHandler(RuntimeError("ldap boom"))}
    )

    result = processor.process(_provision())

    assert result == HandlerResult("failed", error="ldap boom")
    assert repository.recent_entries(1)[0].status == "failed"


def test_missing_handler_is_failed(tmp_path: Path) -> None:
    processor, _, _ = _processor(
        tmp_path, {"provision": RaisingHandler(AssertionError("не тот обработчик"))}
    )

    result = processor.process(
        DeprovisionJob(id=8, event="deprovision", idempotency_key="deprovision:app:9", username="u")
    )

    assert result.status == "failed"
    assert "нет обработчика" in (result.error or "")


def test_unavailable_directory_propagates_and_is_not_journaled(tmp_path: Path) -> None:
    """DC недоступен — не ошибка задания: исключение уходит в API (503), журнал пуст."""
    processor, repository, directory = _processor(tmp_path)
    directory.unavailable = True

    with pytest.raises(DirectoryUnavailableError):
        processor.process(_provision())

    assert repository.recent_entries(10) == []


def test_sixth_failure_logs_dead(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    processor, _, _ = _processor(tmp_path, {"provision": RaisingHandler(RuntimeError("boom"))})

    with caplog.at_level(logging.ERROR, logger="adsync.jobs"):
        for _ in range(5):
            processor.process(_provision())
        assert not [r for r in caplog.records if getattr(r, "event", None) == "job_dead"]
        processor.process(_provision())

    assert [r for r in caplog.records if getattr(r, "event", None) == "job_dead"]


def test_password_never_reaches_journal(tmp_path: Path) -> None:
    processor, _, _ = _processor(tmp_path)

    processor.process(_provision())

    # WAL: свежие записи могут лежать в state.db-wal — проверяем все файлы базы.
    raw = b"".join(path.read_bytes() for path in tmp_path.glob("state.db*"))
    assert "СекретУченика".encode() not in raw
