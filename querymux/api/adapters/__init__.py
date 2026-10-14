from __future__ import annotations

import importlib

from .base import Adapter, QueryResult

# backend name -> (adapter module, adapter class, driver package it needs).
# Adapters are imported only when used, so a missing driver (no duckdb, no
# psycopg, no pymongo) disables just *that* backend instead of breaking every import.
#
# "postgis" is an alias: PostgresAdapter renders geometry/geography columns
# as EWKT whenever the extension is installed in the target database.
_REGISTRY = {
    "sqlite":   ("sqlite_adapter",   "SQLiteAdapter",   "sqlite3"),
    "duckdb":   ("duckdb_adapter",   "DuckDBAdapter",   "duckdb"),
    "postgres": ("postgres_adapter", "PostgresAdapter", "psycopg"),
    "postgis":  ("postgres_adapter", "PostgresAdapter", "psycopg"),
    "mongodb":  ("mongo_adapter",    "MongoAdapter",    "pymongo"),
}

BACKENDS = list(_REGISTRY)


def driver_package(backend: str) -> str:
    """The Python package a backend's driver comes from, e.g. 'duckdb'."""
    return _REGISTRY[backend][2]


def build_adapter(backend: str, target: str) -> Adapter:
    try:
        module_name, class_name, driver = _REGISTRY[backend]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown backend '{backend}'. Known backends: {known}")
    try:
        module = importlib.import_module(f".{module_name}", __name__)
    except ImportError as exc:
        raise ValueError(
            f"Backend '{backend}' needs the '{driver}' package, which isn't installed "
            f"({exc}). Add it to QueryMUX's environment: pipx inject querymux {driver}"
        ) from exc
    return getattr(module, class_name)(target)


__all__ = ["Adapter", "QueryResult", "BACKENDS", "build_adapter", "driver_package"]
