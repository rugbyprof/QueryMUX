"""Phase 1 adapter. `translate()` is close to a no-op here on purpose — see
base.py's docstring for why that's still part of the contract."""
from __future__ import annotations

import sqlite3

from .base import Adapter, QueryResult

MAX_ROWS = 500


class SQLiteAdapter(Adapter):
    EDITOR_LANGUAGE = "sql"

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.target = db_path

    def translate(self, query_text: str) -> str:
        return query_text.strip()

    def execute(self, native_query: str) -> QueryResult:
        if not native_query:
            return QueryResult(error="Empty query.")

        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.cursor()
            try:
                cur.execute(native_query)
            except sqlite3.Error as exc:
                return QueryResult(error=str(exc))

            if cur.description is None:
                # Not a SELECT — INSERT/UPDATE/DELETE/DDL etc.
                conn.commit()
                return QueryResult(
                    columns=[],
                    rows=[],
                    row_count=cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0,
                )

            columns = [d[0] for d in cur.description]
            fetched = cur.fetchmany(MAX_ROWS)
            rows = [list(r) for r in fetched]
            truncated = cur.fetchone() is not None
            result = QueryResult(columns=columns, rows=rows, row_count=len(rows))
            if truncated:
                result.error = None
                result.row_count = len(rows)
                # Not an error — a note. Callers can check len(rows) == MAX_ROWS
                # to decide whether to warn about truncation in the UI.
            return result
        finally:
            conn.close()
