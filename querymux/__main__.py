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


def main() -> None:
    parser = argparse.ArgumentParser(prog="querymux")
    parser.add_argument("--backend", default="sqlite", choices=["sqlite"])
    parser.add_argument(
        "--db", help="Path to the SQLite database file (required for --backend sqlite)"
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

    if args.backend == "sqlite" and not args.db:
        parser.error("--db is required for --backend sqlite")

    target = args.db
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

    try:
        from .app import QueryMuxApp

        QueryMuxApp(
            api_url=api_url,
            backend=args.backend,
            target=target,
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
