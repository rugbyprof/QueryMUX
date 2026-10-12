"""Local FastAPI backend. Started by the TUI as a background subprocess
(see __main__.py) — one process per QueryMUX session, one adapter active
for the whole session's lifetime (backend/target are chosen at launch,
not switched live; see the requirements doc)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

from .. import history
from .adapters import build_adapter
from .adapters.base import MAX_ROWS

if os.environ.get("QUERYMUX_DEBUG") == "1":
    # A separate process from the TUI (started via `uvicorn ...` by
    # __main__._start_server), so it can't share that process's logger —
    # write to the same file instead. Adds a handler rather than calling
    # logging.basicConfig(): uvicorn configures its own root-logger
    # handlers before importing this module (for --log-level), and
    # basicConfig() is a no-op once handlers already exist, so it would
    # silently do nothing here depending on import order.
    _debug_log_path = Path.home() / ".querymux" / "debug.log"
    _debug_log_path.parent.mkdir(parents=True, exist_ok=True)
    _file_handler = logging.FileHandler(_debug_log_path, mode="a")
    _file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(_file_handler)
    logging.getLogger().setLevel(logging.DEBUG)
    # asyncio's own event-loop chatter (uvicorn runs on one) drowns out
    # anything meaningful; uvicorn's own "uvicorn"/"uvicorn.access" loggers
    # are left alone — --log-level debug on the uvicorn command (see
    # __main__._start_server) is what's meant to surface those.
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.debug("=== querymux backend starting (pid %s) ===", os.getpid())

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
    return {
        "status": "ok",
        "backend": BACKEND,
        "target": adapter.target,
        "editor_language": adapter.EDITOR_LANGUAGE,
    }


@app.post("/query", response_model=QueryResponse)
def run_query(req: QueryRequest):
    print("whoop")
    result = adapter.run(req.text)

    history.log_entry(
        backend=BACKEND,
        target=adapter.target,
        query_text=req.text,
        status="error" if result.error else "ok",
        row_count=result.row_count,
        duration_ms=result.duration_ms,
        error=result.error,
    )

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


@app.delete("/history/{entry_id}")
def delete_history_entry(entry_id: int):
    history.delete_entry(entry_id)
    return {"status": "ok"}
