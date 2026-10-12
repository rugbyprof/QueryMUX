from __future__ import annotations

from .base import Adapter, QueryResult
from .postgres_adapter import PostgresAdapter
from .sqlite_adapter import SQLiteAdapter

# "postgis" is an alias: PostgresAdapter renders geometry/geography columns
# as EWKT whenever the extension is installed in the target database.
_REGISTRY = {
    "sqlite": SQLiteAdapter,
    "postgres": PostgresAdapter,
    "postgis": PostgresAdapter,
}


def build_adapter(backend: str, target: str) -> Adapter:
    try:
        cls = _REGISTRY[backend]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown backend '{backend}'. Known backends: {known}")
    return cls(target)


__all__ = ["Adapter", "QueryResult", "PostgresAdapter", "SQLiteAdapter", "build_adapter"]
