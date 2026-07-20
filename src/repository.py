"""Append-only журнал обработок заданий в SQLite (`DATA_DIR/state.db`).

Единственная точка доступа к БД в проекте — без ORM, только `sqlite3` (stdlib).
"""

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    event TEXT NOT NULL,
    username TEXT NOT NULL,
    subject_key TEXT,
    status TEXT NOT NULL CHECK (status IN ('done', 'failed')),
    error TEXT,
    received_at TEXT NOT NULL,
    acked_at TEXT NOT NULL
)
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_jobs_idempotency_key ON jobs (idempotency_key)
"""

_INSERT_SQL = """
INSERT INTO jobs (
    job_id, idempotency_key, event, username, subject_key, status, error, received_at, acked_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_DEAD_COUNT_SQL = """
SELECT COUNT(*) FROM jobs WHERE idempotency_key = ? AND status = 'failed'
"""

_DAILY_COUNTS_SQL = """
SELECT
    SUM(CASE WHEN event = 'provision' AND status = 'done' THEN 1 ELSE 0 END),
    SUM(CASE WHEN event = 'deprovision' AND status = 'done' THEN 1 ELSE 0 END),
    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
FROM jobs WHERE acked_at >= ?
"""

_DEAD_THRESHOLD = 6

_STATUS_DONE_FAILED_SQL = """
SELECT
    SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END),
    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END)
FROM jobs
"""

_STATUS_DEAD_SQL = """
SELECT COUNT(*) FROM (
    SELECT idempotency_key FROM jobs
    WHERE status = 'failed'
    GROUP BY idempotency_key
    HAVING COUNT(*) >= ?
)
"""

_RECENT_ENTRIES_SQL = """
SELECT job_id, idempotency_key, event, username, subject_key, status, error, received_at, acked_at
FROM jobs ORDER BY id DESC LIMIT ?
"""


@dataclass(frozen=True, slots=True)
class JobLogEntry:
    """Одна запись журнала — факт обработки задания и отправленного по нему ack.

    Пароль и сырой payload задания сюда никогда не попадают.
    """

    job_id: int
    idempotency_key: str
    event: str
    username: str
    subject_key: str | None
    status: Literal["done", "failed"]
    error: str | None
    received_at: datetime
    acked_at: datetime


@dataclass(frozen=True, slots=True)
class DailyCounts:
    """Агрегаты журнала за период — данные для дневной сводки в лог."""

    created: int
    disabled: int
    errors: int


@dataclass(frozen=True, slots=True)
class StatusCounts:
    """Агрегаты журнала за всё время — данные для `GET /status`."""

    done: int
    failed: int
    dead: int


class JobRepository:
    """Единственная точка доступа к SQLite-журналу обработок."""

    def __init__(self, db_path: Path) -> None:
        """Открывает (создавая при необходимости) базу в режиме WAL.

        Args:
            db_path: путь к файлу `state.db`; родительская директория создаётся, если её нет.
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(_CREATE_TABLE_SQL)
            self._connection.execute(_CREATE_INDEX_SQL)
            self._connection.commit()

    def record(self, entry: JobLogEntry) -> None:
        """Добавляет строку журнала. Append-only — существующие строки никогда не меняются."""
        with self._lock:
            self._connection.execute(
                _INSERT_SQL,
                (
                    entry.job_id,
                    entry.idempotency_key,
                    entry.event,
                    entry.username,
                    entry.subject_key,
                    entry.status,
                    entry.error,
                    entry.received_at.isoformat(),
                    entry.acked_at.isoformat(),
                ),
            )
            self._connection.commit()

    def dead_count(self, idempotency_key: str) -> int:
        """Возвращает число неудачных обработок (`status='failed'`) по данному ключу."""
        with self._lock:
            cursor = self._connection.execute(_DEAD_COUNT_SQL, (idempotency_key,))
            (count,) = cursor.fetchone()
        return int(count)

    def daily_counts(self, since: datetime) -> DailyCounts:
        """Агрегаты по журналу с `acked_at >= since`: успешные provision/deprovision, ошибки."""
        with self._lock:
            cursor = self._connection.execute(_DAILY_COUNTS_SQL, (since.isoformat(),))
            created, disabled, errors = cursor.fetchone()
        return DailyCounts(created=created or 0, disabled=disabled or 0, errors=errors or 0)

    def status_counts(self) -> StatusCounts:
        """Агрегаты по всему журналу для `GET /status`: успехи, ошибки, «мёртвые» задания."""
        with self._lock:
            done, failed = self._connection.execute(_STATUS_DONE_FAILED_SQL).fetchone()
            (dead,) = self._connection.execute(_STATUS_DEAD_SQL, (_DEAD_THRESHOLD,)).fetchone()
        return StatusCounts(done=done or 0, failed=failed or 0, dead=dead or 0)

    def recent_entries(self, limit: int) -> list[JobLogEntry]:
        """Последние `limit` записей журнала, самые свежие первыми."""
        with self._lock:
            rows = self._connection.execute(_RECENT_ENTRIES_SQL, (limit,)).fetchall()
        return [
            JobLogEntry(
                job_id=row[0],
                idempotency_key=row[1],
                event=row[2],
                username=row[3],
                subject_key=row[4],
                status=row[5],
                error=row[6],
                received_at=datetime.fromisoformat(row[7]),
                acked_at=datetime.fromisoformat(row[8]),
            )
            for row in rows
        ]

    def close(self) -> None:
        """Закрывает соединение с БД (используется при graceful shutdown)."""
        with self._lock:
            self._connection.close()
