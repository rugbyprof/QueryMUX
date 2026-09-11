"""The contract every backend adapter implements — see Section 03 of the
requirements doc. Two stages on purpose: `translate` turns raw box text into
whatever the engine's driver actually wants (a no-op for SQL engines today;
real parsing for Mongo/Redis later), and `execute` runs it. Keeping both
stages in the contract from day one means Phase 3/4 never has to reshape
this pipeline — only fill in real logic where SQLite currently just passes
text through.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    duration_ms: float = 0.0
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


class Adapter(Protocol):
    """One adapter instance == one configured connection target."""

    target: str  # human-readable, shown in the TUI title bar

    def translate(self, query_text: str) -> Any:
        """Turn raw query-pane text into whatever `execute` needs."""
        ...

    def execute(self, native_query: Any) -> QueryResult:
        """Run the translated query. Never raises — engine errors come back
        as QueryResult(error=...), since a bad query is an expected outcome
        here, not an exceptional one."""
        ...

    def run(self, query_text: str) -> QueryResult:
        """translate() + execute(), timed. Adapters get this for free."""
        start = time.perf_counter()
        try:
            native_query = self.translate(query_text)
            result = self.execute(native_query)
        except Exception as exc:  # belt-and-suspenders — a translate() bug
            # shouldn't crash the server, it should read like a query error.
            result = QueryResult(error=f"{type(exc).__name__}: {exc}")
        result.duration_ms = (time.perf_counter() - start) * 1000
        return result
