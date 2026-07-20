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

    def close(self) -> None:
        """Закрывает соединение с БД (используется при graceful shutdown)."""
        with self._lock:
            self._connection.close()
