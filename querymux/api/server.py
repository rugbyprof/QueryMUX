"""Local FastAPI backend. Started by the TUI as a background subprocess
(see __main__.py) — one process per QueryMUX session, one adapter active
for the whole session's lifetime (backend/target are chosen at launch,
not switched live; see the requirements doc)."""
from __future__ import annotations

import os

from fastapi import FastAPI
from pydantic import BaseModel

from .. import history
from .adapters import build_adapter

BACKEND = os.environ["QUERYMUX_BACKEND"]
TARGET = os.environ["QUERYMUX_TARGET"]

adapter = build_adapter(BACKEND, TARGET)

app = FastAPI(title="QueryMUX backend")


class QueryRequest(BaseModel):
    text: str


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list]
    row_count: int
    duration_ms: float
    error: str | None
    truncated: bool = False


@app.get("/health")
def health():
    return {"status": "ok", "backend": BACKEND, "target": TARGET}


@app.post("/query", response_model=QueryResponse)
def run_query(req: QueryRequest):
    result = adapter.run(req.text)

    history.log_entry(
        backend=BACKEND,
        target=TARGET,
        query_text=req.text,
        status="error" if result.error else "ok",
        row_count=result.row_count,
        duration_ms=result.duration_ms,
        error=result.error,
    )

    from .adapters.sqlite_adapter import MAX_ROWS  # only meaningful truncation signal today

    return QueryResponse(
        columns=result.columns,
        rows=result.rows,
        row_count=result.row_count,
        duration_ms=result.duration_ms,
        error=result.error,
        truncated=(result.row_count == MAX_ROWS and not result.error),
    )


@app.get("/history")
def get_history(limit: int = 50, q: str | None = None):
    entries = history.recent(limit=limit, search=q)
    return [e.as_dict() for e in entries]
