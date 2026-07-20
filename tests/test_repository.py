"""Тесты SQLite-журнала (`repository.py`)."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from repository import JobLogEntry, JobRepository

_RECEIVED_AT = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)
_ACKED_AT = datetime(2026, 7, 20, 10, 0, 3, tzinfo=UTC)


def _entry(
    *,
    job_id: int = 1,
    idempotency_key: str = "app:1",
    status: str = "done",
    error: str | None = None,
    subject_key: str | None = "inf-ege",
) -> JobLogEntry:
    return JobLogEntry(
        job_id=job_id,
        idempotency_key=idempotency_key,
        event="provision",
        username="i.petrov",
        subject_key=subject_key,
        status=status,  # type: ignore[arg-type]
        error=error,
        received_at=_RECEIVED_AT,
        acked_at=_ACKED_AT,
    )


def test_record_appends_row_with_expected_fields(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.record(_entry())
    repo.close()

    connection = sqlite3.connect(tmp_path / "state.db")
    row = connection.execute(
        "SELECT job_id, idempotency_key, event, username, subject_key, status, error,"
        " received_at, acked_at FROM jobs"
    ).fetchone()
    connection.close()

    assert row == (
        1,
        "app:1",
        "provision",
        "i.petrov",
        "inf-ege",
        "done",
        None,
        _RECEIVED_AT.isoformat(),
        _ACKED_AT.isoformat(),
    )


def test_schema_has_no_password_or_payload_column(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.close()

    connection = sqlite3.connect(tmp_path / "state.db")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    connection.close()

    assert "password" not in columns
    assert "payload" not in columns


def test_record_is_append_only_on_repeated_idempotency_key(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.record(_entry(idempotency_key="app:1", status="failed", error="ldap timeout"))
    repo.record(_entry(idempotency_key="app:1", status="done"))
    repo.close()

    connection = sqlite3.connect(tmp_path / "state.db")
    (count,) = connection.execute(
        "SELECT COUNT(*) FROM jobs WHERE idempotency_key = 'app:1'"
    ).fetchone()
    connection.close()

    assert count == 2


def test_dead_count_counts_only_failed_for_given_key(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.record(_entry(idempotency_key="app:1", status="failed", error="e1"))
    repo.record(_entry(idempotency_key="app:1", status="failed", error="e2"))
    repo.record(_entry(idempotency_key="app:1", status="done"))
    repo.record(_entry(idempotency_key="app:2", status="failed", error="e3"))

    assert repo.dead_count("app:1") == 2
    assert repo.dead_count("app:2") == 1
    assert repo.dead_count("app:unknown") == 0

    repo.close()


def test_wal_mode_enabled(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")

    connection = sqlite3.connect(tmp_path / "state.db")
    (mode,) = connection.execute("PRAGMA journal_mode").fetchone()
    connection.close()
    repo.close()

    assert mode.lower() == "wal"


def test_creates_missing_parent_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "state.db"

    repo = JobRepository(db_path)
    repo.record(_entry())
    repo.close()

    assert db_path.exists()


def test_close_does_not_raise(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.close()
