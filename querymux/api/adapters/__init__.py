from __future__ import annotations

from .base import Adapter, QueryResult
from .sqlite_adapter import SQLiteAdapter

_REGISTRY = {
    "sqlite": SQLiteAdapter,
}


def build_adapter(backend: str, target: str) -> Adapter:
    try:
        cls = _REGISTRY[backend]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown backend '{backend}'. Known backends: {known}")
    return cls(target)


__all__ = ["Adapter", "QueryResult", "SQLiteAdapter", "build_adapter"]
