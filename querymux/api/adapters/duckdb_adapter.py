"""DuckDB adapter. Same shape as SQLiteAdapter: a file path in, SQL passed
through, `translate()` close to a no-op (see base.py's docstring for why
that's still part of the contract).

The target is a DuckDB database file. ":memory:" (or an empty target) gives a
throwaway in-memory database, which is still useful: DuckDB can query CSV,
Parquet, and JSON files directly (`SELECT * FROM 'people.csv'`).

Locking: DuckDB lets only one *process* hold a database file for writing, and
while it does, no other process can open the file at all. So, like the SQLite
adapter, this opens the file per query and closes it right after, keeping the
lock window as short as possible. A conflict comes back as a normal query
error ("Could not set lock on file ..."), not a crash.
"""
from __future__ import annotations

import duckdb

from .base import Adapter, QueryResult, MAX_ROWS, to_jsonable


class DuckDBAdapter(Adapter):
    EDITOR_LANGUAGE = "sql"

    def __init__(self, db_path: str):
        self.db_path = db_path or ":memory:"
        self.target = self.db_path

    def translate(self, query_text: str) -> str:
        return query_text.strip()

    def execute(self, native_query: str) -> QueryResult:
        if not native_query:
            return QueryResult(error="Empty query.")

        try:
            conn = duckdb.connect(self.db_path)
        except duckdb.Error as exc:
            return QueryResult(error=str(exc))
        try:
            try:
                cur = conn.execute(native_query)
            except duckdb.Error as exc:
                return QueryResult(error=str(exc))

            if cur.description is None:
                # DDL with no result set. (DuckDB's INSERT/UPDATE/DELETE
                # return a one-row "Count" result, which shows up below.)
                return QueryResult(columns=[], rows=[], row_count=0)

            columns = [d[0] for d in cur.description]
            fetched = cur.fetchmany(MAX_ROWS)
            # DuckDB returns Decimal, date, timestamp, interval, list, struct,
            # ... values; render them the way the DuckDB shell would.
            rows = [[to_jsonable(v) for v in r] for r in fetched]
            return QueryResult(columns=columns, rows=rows, row_count=len(rows))
        finally:
            conn.close()
