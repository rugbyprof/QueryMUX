"""Local query history log — always SQLite, regardless of which engine a
query actually ran against. Lives at ~/.querymux/history.db.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

HISTORY_DIR = Path.home() / ".querymux"
HISTORY_DB = HISTORY_DIR / "history.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    backend     TEXT NOT NULL,
    target      TEXT NOT NULL,
    query_text  TEXT NOT NULL,
    status      TEXT NOT NULL,      -- 'ok' | 'error'
    row_count   INTEGER,
    duration_ms REAL,
    error       TEXT
);
"""


@dataclass
class HistoryEntry:
    id: Optional[int]
    ts: float
    backend: str
    target: str
    query_text: str
    status: str
    row_count: Optional[int]
    duration_ms: Optional[float]
    error: Optional[str]

    def as_dict(self) -> dict:
        return asdict(self)


def _connect() -> sqlite3.Connection:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(HISTORY_DB)
    conn.execute(_SCHEMA)
    return conn


def log_entry(
    *,
    backend: str,
    target: str,
    query_text: str,
    status: str,
    row_count: Optional[int],
    duration_ms: Optional[float],
    error: Optional[str],
) -> int:
    """Record one run (success or failure). Returns the new row's id."""
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO query_history
               (ts, backend, target, query_text, status, row_count, duration_ms, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), backend, target, query_text, status, row_count, duration_ms, error),
        )
        return cur.lastrowid


def recent(limit: int = 50, search: Optional[str] = None) -> list[HistoryEntry]:
    """Most recent entries first, optionally filtered by a substring match
    against the query text."""
    with _connect() as conn:
        if search:
            rows = conn.execute(
                """SELECT id, ts, backend, target, query_text, status, row_count, duration_ms, error
                   FROM query_history WHERE query_text LIKE ?
                   ORDER BY id DESC LIMIT ?""",
                (f"%{search}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, ts, backend, target, query_text, status, row_count, duration_ms, error
                   FROM query_history ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
    return [HistoryEntry(*row) for row in rows]
