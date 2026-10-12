"""PostgreSQL (and PostGIS) adapter. Like SQLite, `translate()` is close to a
no-op — see base.py's docstring for why that's still part of the contract.

The target is a libpq connection string, either URI or key/value form:

    postgresql://user:pass@localhost:5432/mydb
    "dbname=mydb user=postgres host=localhost"

An empty target falls back to libpq defaults (PGHOST, PGDATABASE, ... and
the local socket), same as running bare `psql`.

PostGIS: geometry/geography values come off the wire as hex EWKB, which is
unreadable in a results grid. When the extension is installed in the target
database, those columns are converted to EWKT (`SRID=4326;POINT(1 2)`) with
one extra round trip per spatial column. No PostGIS, no extra work.
"""
from __future__ import annotations

import datetime
import decimal
import json
import uuid
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from .base import Adapter, QueryResult, MAX_ROWS

_SPATIAL_TYPES = ("geometry", "geography", "box2d", "box3d")


def _display_target(conninfo: str) -> str:
    """Connection string minus the password — this ends up in the TUI title
    bar and in ~/.querymux/history.db."""
    if not conninfo:
        return "(libpq defaults)"
    try:
        params = conninfo_to_dict(conninfo)
    except psycopg.ProgrammingError:
        return conninfo
    if "password" in params:
        params["password"] = "***"
    return make_conninfo(**params)


def _to_jsonable(value: Any) -> Any:
    """Rows go back to the TUI as JSON. Keep primitives as-is; render the
    rest (Decimal, timestamps, intervals, bytea, arrays, jsonb, ...) the way
    psql would rather than letting the response encoder choke or reformat."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    if isinstance(value, (decimal.Decimal, uuid.UUID, datetime.date,
                          datetime.time, datetime.timedelta)):
        return str(value)
    return str(value)


class PostgresAdapter(Adapter):
    EDITOR_LANGUAGE = "sql"

    def __init__(self, conninfo: str):
        self.conninfo = conninfo
        self.target = _display_target(conninfo)
        # type OID -> PostGIS type name; filled lazily on first query since
        # the server may not be reachable yet at construction time.
        self._spatial_oids: dict[int, str] | None = None

    def translate(self, query_text: str) -> str:
        return query_text.strip()

    def _load_spatial_oids(self, conn: psycopg.Connection) -> dict[int, str]:
        if self._spatial_oids is None:
            rows = conn.execute(
                "SELECT oid::int, typname FROM pg_type WHERE typname = ANY(%s)",
                [list(_SPATIAL_TYPES)],
            ).fetchall()
            self._spatial_oids = {oid: name for oid, name in rows}
        return self._spatial_oids

    def _spatial_to_ewkt(self, conn: psycopg.Connection, typname: str,
                         values: list[Any]) -> list[Any]:
        if typname in ("box2d", "box3d"):
            return values  # already text like BOX(0 0,1 1)
        cast = "geography::geometry" if typname == "geography" else "geometry"
        rows = conn.execute(
            f"SELECT ST_AsEWKT(v::{cast}) FROM unnest(%s::text[]) WITH ORDINALITY AS t(v, i) ORDER BY i",
            [values],
        ).fetchall()
        return [r[0] for r in rows]

    def execute(self, native_query: str) -> QueryResult:
        if not native_query:
            return QueryResult(error="Empty query.")

        try:
            # autocommit: DDL/DML stick without an explicit COMMIT, and a
            # failed statement doesn't leave the session in an aborted
            # transaction. Users can still write BEGIN; ... COMMIT; themselves.
            conn = psycopg.connect(self.conninfo, autocommit=True, connect_timeout=5)
        except psycopg.Error as exc:
            return QueryResult(error=f"Connection failed: {exc}")

        try:
            cur = conn.cursor()
            try:
                # No params -> psycopg uses the simple query protocol, so a
                # box holding several `;`-separated statements works.
                cur.execute(native_query, prepare=False)
            except psycopg.Error as exc:
                return QueryResult(error=self._format_error(exc))

            # Multiple statements produce multiple result sets; show the last
            # one that returned rows (or the last one, if none did) — same
            # as what you'd care about at the bottom of a psql session.
            chosen = None
            while True:
                if cur.description is not None:
                    chosen = (
                        list(cur.description),
                        cur.fetchmany(MAX_ROWS),
                    )
                last_rowcount = cur.rowcount
                if not cur.nextset():
                    break

            if chosen is None:
                return QueryResult(
                    row_count=last_rowcount if last_rowcount and last_rowcount >= 0 else 0,
                )

            description, fetched = chosen
            columns = [d.name for d in description]
            rows = [list(r) for r in fetched]

            spatial = self._load_spatial_oids(conn)
            for col, d in enumerate(description):
                typname = spatial.get(d.type_code)
                if typname is None:
                    continue
                present = [i for i, r in enumerate(rows) if r[col] is not None]
                if not present:
                    continue
                converted = self._spatial_to_ewkt(
                    conn, typname, [rows[i][col] for i in present]
                )
                for i, v in zip(present, converted):
                    rows[i][col] = v

            rows = [[_to_jsonable(v) for v in r] for r in rows]
            return QueryResult(columns=columns, rows=rows, row_count=len(rows))
        except psycopg.Error as exc:
            return QueryResult(error=self._format_error(exc))
        finally:
            conn.close()

    @staticmethod
    def _format_error(exc: psycopg.Error) -> str:
        diag = exc.diag
        msg = str(exc).strip()
        extras = []
        if diag.sqlstate:
            extras.append(f"SQLSTATE {diag.sqlstate}")
        if diag.message_hint and diag.message_hint not in msg:
            extras.append(f"HINT: {diag.message_hint}")
        return msg + ("\n" + "\n".join(extras) if extras else "")
