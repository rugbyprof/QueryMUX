"""What can QueryMUX open on this machine? Three questions, answered cheaply:

1. Backends: is each engine's Python driver installed?
2. Servers:  for server engines (Postgres), is one listening, and which
             databases can we see?
3. Files:    which SQLite / DuckDB database files are in the folders we scan?

Used by `querymux --list` (a printed report) and by the startup picker shown
when querymux is run without --db. Nothing here opens a database for
writing: files are identified by their first bytes, so browsing never takes
DuckDB's write lock.

Folders scanned for files, in order:
  * the current directory
  * $DATA_DIR and $DUCKDB_DIR, if set
  * `scan = [...]` in ~/.querymux/config.toml, if present, e.g.
        scan = ["~/5303_data", "~/projects/shop"]
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .api.adapters import BACKENDS, driver_package

CONFIG_PATH = Path.home() / ".querymux" / "config.toml"
FILE_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".duckdb", ".ddb"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox"}
MAX_DEPTH = 2          # folder levels below each scan root
MAX_FILES = 200


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #

def detect_backend(path: str | Path) -> str | None:
    """'sqlite' or 'duckdb' from a file's header bytes, or None."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if head.startswith(b"SQLite format 3\x00"):
        return "sqlite"
    if head[8:12] == b"DUCK":
        return "duckdb"
    return None


@dataclass
class FoundFile:
    backend: str
    path: Path
    size_bytes: int

    @property
    def size_label(self) -> str:
        n = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} GB"


def _config_scan_dirs() -> list[str]:
    if not CONFIG_PATH.exists():
        return []
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        return []
    try:
        with open(CONFIG_PATH, "rb") as f:
            return [str(d) for d in tomllib.load(f).get("scan", [])]
    except (OSError, ValueError):
        return []


def scan_dirs() -> list[Path]:
    """The folders to look in, existing ones only, without duplicates."""
    raw = [os.getcwd(), os.environ.get("DATA_DIR", ""), os.environ.get("DUCKDB_DIR", "")]
    raw += _config_scan_dirs()
    seen: set[Path] = set()
    out: list[Path] = []
    for d in raw:
        if not d:
            continue
        p = Path(d).expanduser().resolve()
        if p.is_dir() and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def find_databases(dirs: list[Path] | None = None) -> list[FoundFile]:
    found: dict[Path, FoundFile] = {}
    for root in dirs if dirs is not None else scan_dirs():
        root_depth = len(root.parts)
        for here, subdirs, files in os.walk(root):
            depth = len(Path(here).parts) - root_depth
            subdirs[:] = [] if depth >= MAX_DEPTH else sorted(
                d for d in subdirs if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(files):
                path = Path(here, name)
                if path.suffix.lower() not in FILE_EXTENSIONS or path in found:
                    continue
                backend = detect_backend(path)
                if backend:
                    found[path] = FoundFile(backend, path, path.stat().st_size)
                if len(found) >= MAX_FILES:
                    return list(found.values())
    return list(found.values())


# --------------------------------------------------------------------------- #
# Backends and servers
# --------------------------------------------------------------------------- #

@dataclass
class BackendStatus:
    backend: str
    available: bool
    version: str = ""
    note: str = ""
    databases: list[str] = field(default_factory=list)   # server engines only
    base_conninfo: str = ""                               # server engines only


def _driver_version(backend: str) -> tuple[bool, str]:
    pkg = driver_package(backend)
    if pkg == "sqlite3":
        import sqlite3
        return True, f"SQLite {sqlite3.sqlite_version}"
    if importlib.util.find_spec(pkg) is None:
        return False, ""
    try:
        return True, f"{pkg} {importlib.metadata.version(pkg)}"
    except importlib.metadata.PackageNotFoundError:
        return True, pkg


def postgres_conninfo() -> str:
    """Where to look for Postgres: $DATABASE_URL, else libpq's own defaults
    (PGHOST, PGPORT, PGUSER, ... or the local socket), like bare `psql`."""
    return os.environ.get("DATABASE_URL", "")


def _postgres_host_port(conninfo: str) -> tuple[str, int]:
    if conninfo.startswith(("postgresql://", "postgres://")):
        u = urlparse(conninfo)
        return u.hostname or "localhost", u.port or 5432
    return os.environ.get("PGHOST", "localhost"), int(os.environ.get("PGPORT", "5432"))


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    if host.startswith("/"):            # a Unix socket directory, not a host
        return Path(host, f".s.PGSQL.{port}").exists()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _postgres_status(status: BackendStatus) -> None:
    conninfo = postgres_conninfo()
    host, port = _postgres_host_port(conninfo)
    status.base_conninfo = conninfo
    if not _port_open(host, port):
        status.note = f"no server on {host}:{port}"
        return
    import psycopg
    try:
        with psycopg.connect(conninfo, connect_timeout=2) as conn:
            rows = conn.execute(
                "SELECT datname FROM pg_database "
                "WHERE NOT datistemplate AND datallowconn ORDER BY datname"
            ).fetchall()
        status.databases = [r[0] for r in rows]
        status.note = f"server on {host}:{port}"
    except psycopg.Error as exc:
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        status.note = (f"server on {host}:{port}, but can't list databases "
                       f"({first_line}). Set DATABASE_URL to connect.")


def backend_statuses(check_servers: bool = True) -> list[BackendStatus]:
    """One entry per backend, aliases (postgis) folded into their engine."""
    out = []
    for backend in BACKENDS:
        if backend == "postgis":
            continue
        ok, version = _driver_version(backend)
        st = BackendStatus(backend, ok, version)
        if not ok:
            st.note = f"driver not installed: pipx inject querymux {driver_package(backend)}"
        elif backend == "postgres" and check_servers:
            _postgres_status(st)
        out.append(st)
    return out


def postgres_target(base_conninfo: str, dbname: str) -> str:
    """The base connection info, pointed at one database."""
    from psycopg.conninfo import make_conninfo
    return make_conninfo(base_conninfo, dbname=dbname)


# --------------------------------------------------------------------------- #
# `querymux --list`
# --------------------------------------------------------------------------- #

def _short(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return "~" + s[len(home):] if s.startswith(home) else s


def report() -> str:
    lines = ["Backends"]
    statuses = backend_statuses()
    for st in statuses:
        mark = "✓" if st.available else "✗"
        detail = st.version if st.available else ""
        lines.append(f"  {st.backend:<9} {mark} {detail}".rstrip())
        if st.note:
            lines.append(f"  {'':<9}   {st.note}")
    lines.append("")
    lines.append("Databases found")
    found = find_databases()
    pg = next((s for s in statuses if s.backend == "postgres"), None)
    if not found and not (pg and pg.databases):
        lines.append("  (none)")
    for f in found:
        lines.append(f"  {f.backend:<9} {_short(f.path):<60} {f.size_label:>9}")
    if pg and pg.databases:
        for name in pg.databases:
            lines.append(f"  {'postgres':<9} {name}")
    lines.append("")
    lines.append("Scanned: " + ", ".join(_short(d) for d in scan_dirs()))
    return "\n".join(lines)
