"""Entry point: parses CLI args, starts the FastAPI backend as a background
subprocess (unless --no-server), waits for it to come up, then runs the TUI.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

DEBUG_LOG_PATH = Path.home() / ".querymux" / "debug.log"


def _configure_debug_logging() -> None:
    # Called first thing in main(), before anything else — including
    # argument-driven branching like _start_server/_fetch_health — so that
    # logging.debug() calls anywhere in the startup path are captured. This
    # is a real gap print() can't close: the debug *pane* only exists once
    # Textual has mounted QueryMuxApp, so anything printed before that
    # (e.g. in main() itself, or under `if __name__ == "__main__":`) has
    # nowhere to land. A file does, regardless of when it's written or
    # whether the TUI ever starts at all (e.g. a crash during startup).
    DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=DEBUG_LOG_PATH,
        filemode="a",
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # DEBUG on the root logger means httpx/httpcore (used for every backend
    # request) and asyncio dump per-byte wire tracing and event-loop
    # chatter too — enough to bury querymux's own breadcrumbs. --debug is
    # for tracing querymux's own call flow, not third-party internals.
    for _noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
    logging.debug("=== querymux starting (pid %s) ===", os.getpid())


def _find_free_port(preferred: int = 8756) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def _start_server(
    backend: str, target: str, port: int, debug: bool = False
) -> tuple[subprocess.Popen, dict]:
    env = os.environ.copy()
    env["QUERYMUX_BACKEND"] = backend
    env["QUERYMUX_TARGET"] = target
    if debug:
        # Tells server.py (a separate process — can't just share our logger)
        # to also write its own logging.debug() calls to DEBUG_LOG_PATH.
        env["QUERYMUX_DEBUG"] = "1"
    # Redirecting stdout to a pipe (below) makes CPython block-buffer it
    # instead of line-buffering as it would for a TTY, so the backend's own
    # print()/log output would sit unflushed — often until process exit —
    # instead of showing up promptly in the debug pane. Force it unbuffered.
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "querymux.api.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "debug" if debug else "warning",
        ],
        env=env,
        # Piped (rather than inherited) so the TUI's debug pane can show
        # backend print()/log output instead of it corrupting the terminal
        # Textual is drawing to — see QueryMuxApp._pump_backend_output.
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    health_url = f"http://127.0.0.1:{port}/health"
    for _ in range(50):  # ~5s
        try:
            resp = httpx.get(health_url, timeout=0.2)
            if resp.status_code == 200:
                return proc, resp.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("QueryMUX backend didn't come up in time.")


def _fetch_health(api_url: str) -> dict:
    try:
        resp = httpx.get(f"{api_url}/health", timeout=2)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Couldn't reach QueryMUX backend at {api_url}: {exc}"
        ) from exc


def _detect_backend(db: str, parser: argparse.ArgumentParser) -> str:
    """--db without --backend: read the file's header, or recognize a
    Postgres or MongoDB connection URL."""
    from .discover import detect_backend

    if db.startswith(("postgresql://", "postgres://")):
        return "postgres"
    if db.startswith(("mongodb://", "mongodb+srv://")):
        return "mongodb"
    path = Path(db).expanduser()
    if not path.exists():
        parser.error(f"--db {db}: no such file (add --backend for a new database)")
    backend = detect_backend(path)
    if backend is None:
        parser.error(f"--db {db}: not a SQLite or DuckDB file; say which with --backend")
    return backend


def _require_driver(backend: str, parser: argparse.ArgumentParser) -> None:
    """Fail here, with a fix, instead of as 'backend didn't come up' later."""
    import importlib.util

    from .api.adapters import driver_package

    pkg = driver_package(backend)
    if pkg != "sqlite3" and importlib.util.find_spec(pkg) is None:
        parser.error(
            f"--backend {backend} needs the '{pkg}' package. "
            f"Install it with:  pipx inject querymux {pkg}"
        )


def main() -> None:
    from .api.adapters import BACKENDS

    parser = argparse.ArgumentParser(
        prog="querymux",
        description="Run with no arguments to pick from the databases found on this machine.",
    )
    parser.add_argument(
        "--backend",
        default=None,
        choices=BACKENDS,
        help="Which engine. Optional when --db is a SQLite or DuckDB file: "
             "it's detected from the file itself.",
    )
    parser.add_argument(
        "--db",
        help=(
            "SQLite/DuckDB: path to the database file. "
            "Postgres/PostGIS: libpq connection string, e.g. "
            "postgresql://user:pass@localhost:5432/mydb or 'dbname=mydb' "
            "(omit to use PG* env vars / local defaults, like bare psql). "
            "MongoDB: a connection string whose path is the database, e.g. "
            "mongodb://localhost:27017/mydb?directConnection=true "
            "(omit to use $MONGO_URL, else localhost and database 'test'). "
            "Omit --db (and --backend) to choose from a list."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the backends available and the databases found, then exit.",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Create practice databases (randData) in every available engine: "
             "SQLite and DuckDB files, and a Postgres role and database. Then exit.",
    )
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="Don't spawn the API server — connect to one already running (requires --api-url)",
    )
    parser.add_argument(
        "--api-url", default=None, help="Existing backend URL, used with --no-server"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Verbose logging to ~/.querymux/debug.log (backend included), "
            "and open the debug pane on startup instead of leaving it hidden"
        ),
    )
    args = parser.parse_args()

    if args.debug:
        _configure_debug_logging()

    if args.list:
        from .discover import report
        print(report())
        return

    if args.setup:
        from . import setup_test
        use_docker = False
        if setup_test.postgres_plan() == "docker":
            print("No Postgres server is running, but Docker is installed.")
            print("Start one with:  " + " ".join(setup_test.docker_command()))
            use_docker = input("Run it now? [y/N] ").strip().lower() in ("y", "yes")
        print(setup_test.format_results(setup_test.run_setup(use_docker)))
        return

    if args.db and not args.backend:
        args.backend = _detect_backend(args.db, parser)
    if not args.db and args.backend not in ("postgres", "postgis", "mongodb"):
        # No database named: let the user pick one (or quit).
        from .picker import pick
        choice = pick()
        if choice is None:
            return
        args.backend, args.db = choice
    _require_driver(args.backend, parser)

    target = args.db or ""
    proc = None

    if args.no_server:
        if not args.api_url:
            parser.error("--no-server requires --api-url")
        api_url = args.api_url.rstrip("/")
        health = _fetch_health(api_url)
    else:
        port = args.port or _find_free_port()
        api_url = f"http://127.0.0.1:{port}"
        proc, health = _start_server(args.backend, target, port, debug=args.debug)

    editor_language = health.get("editor_language", "sql")
    # The adapter's own display form of the target (e.g. a Postgres
    # connection string with the password masked) for the title bar.
    display_target = health.get("target", target)

    try:
        from .app import QueryMuxApp

        QueryMuxApp(
            api_url=api_url,
            backend=args.backend,
            target=display_target,
            editor_language=editor_language,
            backend_proc=proc,
            debug=args.debug,
        ).run()
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
