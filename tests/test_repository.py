"""Тесты SQLite-журнала (`repository.py`)."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from repository import JobLogEntry, JobRepository

_RECEIVED_AT = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)
_ACKED_AT = datetime(2026, 7, 20, 10, 0, 3, tzinfo=UTC)


def _entry(
    *,
    job_id: int = 1,
    idempotency_key: str = "app:1",
    event: str = "provision",
    status: str = "done",
    error: str | None = None,
    subject_key: str | None = "inf-ege",
    received_at: datetime = _RECEIVED_AT,
    acked_at: datetime = _ACKED_AT,
) -> JobLogEntry:
    return JobLogEntry(
        job_id=job_id,
        idempotency_key=idempotency_key,
        event=event,
        username="i.petrov",
        subject_key=subject_key,
        status=status,  # type: ignore[arg-type]
        error=error,
        received_at=received_at,
        acked_at=acked_at,
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


def test_daily_counts_aggregates_since_cutoff(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    since = datetime(2026, 7, 20, 0, 0, 0, tzinfo=UTC)
    before_cutoff = since - timedelta(hours=1)
    after_cutoff = since + timedelta(hours=1)

    repo.record(
        _entry(event="provision", status="done", acked_at=after_cutoff, idempotency_key="p1")
    )
    repo.record(
        _entry(event="provision", status="done", acked_at=after_cutoff, idempotency_key="p2")
    )
    repo.record(
        _entry(event="deprovision", status="done", acked_at=after_cutoff, idempotency_key="d1")
    )
    repo.record(
        _entry(event="deprovision", status="failed", acked_at=after_cutoff, idempotency_key="f1")
    )
    # до cutoff — не должно попасть в агрегат
    repo.record(
        _entry(event="provision", status="done", acked_at=before_cutoff, idempotency_key="old")
    )

    counts = repo.daily_counts(since)
    repo.close()

    assert counts.created == 2
    assert counts.disabled == 1
    assert counts.errors == 1


def test_daily_counts_returns_zeroes_for_empty_journal(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")

    counts = repo.daily_counts(datetime(2026, 7, 20, 0, 0, 0, tzinfo=UTC))
    repo.close()

    assert counts.created == 0
    assert counts.disabled == 0
    assert counts.errors == 0


def test_status_counts_totals_done_and_failed(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    repo.record(_entry(idempotency_key="a", status="done"))
    repo.record(_entry(idempotency_key="a", status="done"))
    repo.record(_entry(idempotency_key="b", status="failed", error="e"))

    counts = repo.status_counts()
    repo.close()

    assert counts.done == 2
    assert counts.failed == 1


def test_status_counts_dead_counts_keys_with_six_or_more_failures(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    for _ in range(6):
        repo.record(_entry(idempotency_key="dead-key", status="failed", error="e"))
    for _ in range(3):
        repo.record(_entry(idempotency_key="alive-key", status="failed", error="e"))
    repo.record(_entry(idempotency_key="done-key", status="done"))

    counts = repo.status_counts()
    repo.close()

    assert counts.dead == 1


def test_status_counts_zero_on_empty_journal(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")

    counts = repo.status_counts()
    repo.close()

    assert (counts.done, counts.failed, counts.dead) == (0, 0, 0)


def test_recent_entries_returns_latest_first_limited(tmp_path: Path) -> None:
    repo = JobRepository(tmp_path / "state.db")
    for job_id in range(1, 6):
        repo.record(_entry(job_id=job_id, idempotency_key=f"k{job_id}"))

    entries = repo.recent_entries(3)
    repo.close()

    assert [e.job_id for e in entries] == [5, 4, 3]
    assert entries[0].received_at == _RECEIVED_AT
    assert entries[0].acked_at == _ACKED_AT
