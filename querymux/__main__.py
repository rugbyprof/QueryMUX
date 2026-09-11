"""Entry point: parses CLI args, starts the FastAPI backend as a background
subprocess (unless --no-server), waits for it to come up, then runs the TUI.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time

import httpx


def _find_free_port(preferred: int = 8756) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def _start_server(backend: str, target: str, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["QUERYMUX_BACKEND"] = backend
    env["QUERYMUX_TARGET"] = target
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "querymux.api.server:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        env=env,
    )
    health_url = f"http://127.0.0.1:{port}/health"
    for _ in range(50):  # ~5s
        try:
            if httpx.get(health_url, timeout=0.2).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("QueryMUX backend didn't come up in time.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="querymux")
    parser.add_argument("--backend", default="sqlite", choices=["sqlite"])
    parser.add_argument("--db", help="Path to the SQLite database file (required for --backend sqlite)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--no-server", action="store_true",
        help="Don't spawn the API server — connect to one already running (requires --api-url)",
    )
    parser.add_argument("--api-url", default=None, help="Existing backend URL, used with --no-server")
    args = parser.parse_args()

    if args.backend == "sqlite" and not args.db:
        parser.error("--db is required for --backend sqlite")

    target = args.db
    proc = None

    if args.no_server:
        if not args.api_url:
            parser.error("--no-server requires --api-url")
        api_url = args.api_url.rstrip("/")
    else:
        port = args.port or _find_free_port()
        proc = _start_server(args.backend, target, port)
        api_url = f"http://127.0.0.1:{port}"

    try:
        from .app import QueryMuxApp
        QueryMuxApp(api_url=api_url, backend=args.backend, target=target).run()
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
