"""Set up databases to practice on: `querymux --setup`, or `s` in the picker.

Creates the same small, randomly generated dataset ("randData") in each
engine that's available:

  * SQLite:   ~/.querymux/randData.db
  * DuckDB:   ~/.querymux/randData.duckdb
  * Postgres: role `student` (password `letmein`) owning database `randData`

Postgres needs a server. If one is listening, setup connects as an admin
($DATABASE_URL or libpq's defaults, like bare `psql`) to create the role and
database. If none is listening and Docker is installed, it can start one in a
container with the role and database already in place (it asks first).

An existing role keeps its password: setup never changes it. If the password
doesn't match, the report says so and gives the ALTER ROLE command to run.

The names can be overridden (mainly for testing) with QUERYMUX_TEST_USER,
QUERYMUX_TEST_PASSWORD, and QUERYMUX_TEST_DB.
"""
from __future__ import annotations

import os
import random
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import discover

USER = os.environ.get("QUERYMUX_TEST_USER", "student")
PASSWORD = os.environ.get("QUERYMUX_TEST_PASSWORD", "letmein")
DBNAME = os.environ.get("QUERYMUX_TEST_DB", "randData")
HOME_DIR = Path.home() / ".querymux"
DOCKER_CONTAINER = "querymux-postgres"
DOCKER_IMAGE = "postgres:17"
SEED = 5303

# --------------------------------------------------------------------------- #
# The sample data: two small related tables, the same rows in every engine.
# --------------------------------------------------------------------------- #

CITIES = ["Wichita Falls", "Austin", "Denver", "Chicago", "Portland", "Atlanta",
          "Boston", "Phoenix", "Seattle", "Omaha"]
FIRST = ["Ada", "Grace", "Alan", "Edsger", "Barbara", "Donald", "Margaret",
         "Linus", "Radia", "Katherine", "Guido", "Frances"]
LAST = ["Lovelace", "Hopper", "Turing", "Dijkstra", "Liskov", "Knuth",
        "Hamilton", "Torvalds", "Perlman", "Johnson", "Rossum", "Allen"]
CATEGORIES = ["books", "games", "garden", "kitchen", "music", "tools"]


def sample_rows(people: int = 100, orders: int = 1000):
    rng = random.Random(SEED)
    start = date(2025, 1, 1)
    person_rows = [
        (i, f"{rng.choice(FIRST)} {rng.choice(LAST)}", rng.choice(CITIES),
         (start + timedelta(days=rng.randrange(365))).isoformat())
        for i in range(1, people + 1)
    ]
    order_rows = [
        (i, rng.randint(1, people), rng.choice(CATEGORIES),
         round(rng.uniform(2, 250), 2),
         (start + timedelta(days=rng.randrange(600))).isoformat())
        for i in range(1, orders + 1)
    ]
    return person_rows, order_rows


DDL = [
    """CREATE TABLE IF NOT EXISTS people (
        person_id INTEGER PRIMARY KEY,
        name      TEXT NOT NULL,
        city      TEXT NOT NULL,
        joined    DATE NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS orders (
        order_id   INTEGER PRIMARY KEY,
        person_id  INTEGER NOT NULL REFERENCES people(person_id),
        category   TEXT NOT NULL,
        amount     DECIMAL(10, 2) NOT NULL,
        ordered_on DATE NOT NULL)""",
]


@dataclass
class SetupResult:
    engine: str
    ok: bool
    lines: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# SQLite and DuckDB: files
# --------------------------------------------------------------------------- #

def setup_sqlite() -> SetupResult:
    path = HOME_DIR / f"{DBNAME}.db"
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    people, orders = sample_rows()
    with sqlite3.connect(path) as con:
        for ddl in DDL:
            con.execute(ddl)
        if con.execute("SELECT count(*) FROM people").fetchone()[0] == 0:
            con.executemany("INSERT INTO people VALUES (?, ?, ?, ?)", people)
            con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", orders)
    return SetupResult("sqlite", True, [f"file: {path}",
                                        f"open:  querymux --db {path}"])


def setup_duckdb() -> SetupResult:
    import duckdb

    path = HOME_DIR / f"{DBNAME}.duckdb"
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    people, orders = sample_rows()
    try:
        con = duckdb.connect(str(path))
    except duckdb.Error as exc:   # e.g. another process holds the file
        return SetupResult("duckdb", False, [f"couldn't open {path}: {exc}"])
    try:
        for ddl in DDL:
            con.execute(ddl)
        if con.execute("SELECT count(*) FROM people").fetchone()[0] == 0:
            con.executemany("INSERT INTO people VALUES (?, ?, ?, ?)", people)
            con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", orders)
    finally:
        con.close()
    return SetupResult("duckdb", True, [f"file: {path}",
                                        f"open:  querymux --db {path}"])


# --------------------------------------------------------------------------- #
# Postgres: a role and a database on a server
# --------------------------------------------------------------------------- #

def _host_port() -> tuple[str, int]:
    return discover._postgres_host_port(discover.postgres_conninfo())


def _tcp_host(host: str) -> str:
    """psql/psycopg need a hostname for password login; a socket dir won't do."""
    return "localhost" if host.startswith("/") else host


def connection_lines(host: str, port: int) -> list[str]:
    url = f"postgresql://{USER}:{PASSWORD}@{host}:{port}/{DBNAME}"
    return [
        f"user:     {USER}",
        f"password: {PASSWORD}",
        f"database: {DBNAME}",
        f"host:     {host}:{port}",
        "",
        "psql, from any terminal:",
        f'  psql "{url}"',
        "or:",
        f'  PGPASSWORD={PASSWORD} psql -h {host} -p {port} -U {USER} -d "{DBNAME}"',
        "",
        "QueryMUX:",
        f'  querymux --db "{url}"',
    ]


def _load_postgres_tables(conn) -> None:
    from psycopg import sql

    people, orders = sample_rows()
    for ddl in DDL:
        conn.execute(ddl)
    if conn.execute("SELECT count(*) FROM people").fetchone()[0] == 0:
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO people VALUES (%s, %s, %s, %s)", people)
            cur.executemany("INSERT INTO orders VALUES (%s, %s, %s, %s, %s)", orders)
    for table in ("people", "orders"):
        conn.execute(sql.SQL("ALTER TABLE {} OWNER TO {}").format(
            sql.Identifier(table), sql.Identifier(USER)))


def _verify_login(host: str, port: int, password: str | None = None) -> str | None:
    """None if USER can log in to DBNAME over TCP with this password, else why not."""
    import psycopg

    try:
        with psycopg.connect(host=host, port=port, user=USER, password=password or PASSWORD,
                             dbname=DBNAME, connect_timeout=3) as c:
            c.execute("SELECT count(*) FROM orders").fetchone()
        return None
    except psycopg.Error as exc:
        return str(exc).strip().splitlines()[0]


def setup_postgres_on_server() -> SetupResult:
    """A server is listening: create the role and database through an admin login."""
    import psycopg
    from psycopg import sql

    res = SetupResult("postgres", False)
    host, port = _host_port()
    admin = discover.postgres_conninfo()
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as conn:
            me, can_role, can_db = conn.execute(
                "SELECT current_user, rolsuper OR rolcreaterole, rolsuper OR rolcreatedb "
                "FROM pg_roles WHERE rolname = current_user").fetchone()
            if not (can_role and can_db):
                res.lines += [f"connected as {me}, who can't create roles and databases.",
                              "Run this as a superuser (e.g. psql -U postgres):",
                              *_admin_sql()]
                return res

            role_exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", [USER]).fetchone()
            if role_exists:
                res.lines.append(f"role {USER} already exists (password left unchanged)")
            else:
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(USER), sql.Literal(PASSWORD)))
                res.lines.append(f"created role {USER}")

            if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", [DBNAME]).fetchone():
                res.lines.append(f"database {DBNAME} already exists")
            else:
                conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(DBNAME), sql.Identifier(USER)))
                res.lines.append(f"created database {DBNAME}, owned by {USER}")

        with psycopg.connect(discover.postgres_target(admin, DBNAME),
                             autocommit=True, connect_timeout=3) as conn:
            _load_postgres_tables(conn)
        res.lines.append("sample tables people and orders are in place")
    except psycopg.Error as exc:
        res.lines.append(f"Postgres error: {str(exc).strip().splitlines()[0]}")
        return res

    tcp = _tcp_host(host)
    problem = _verify_login(tcp, port)
    if problem:
        res.lines += ["", f"but {USER}/{PASSWORD} can't log in: {problem}"]
        if role_exists:
            res.lines += ["The role existed before with a different password. To use this one:",
                          f"  ALTER ROLE {USER} PASSWORD '{PASSWORD}';"]
        return res
    if _verify_login(tcp, port, password="querymux-wrong-password-probe") is None:
        # The server let a wrong password in: it doesn't check passwords for
        # this connection (pg_hba "trust"), so the login above proves nothing.
        res.lines += ["", f"note: this server doesn't check passwords for connections from here "
                          f"(pg_hba.conf 'trust'), so any password logs in as {USER}."]
        if role_exists:
            res.lines += [f"{USER} existed before setup, so its real password may not be "
                          f"'{PASSWORD}'. To make sure it is:",
                          f"  ALTER ROLE {USER} PASSWORD '{PASSWORD}';"]
    res.ok = True
    res.lines += ["", *connection_lines(tcp, port)]
    return res


def _admin_sql() -> list[str]:
    return [f"  CREATE ROLE {USER} LOGIN PASSWORD '{PASSWORD}';",
            f'  CREATE DATABASE "{DBNAME}" OWNER {USER};']


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def docker_command(port: int = 5432) -> list[str]:
    return ["docker", "run", "-d", "--name", DOCKER_CONTAINER,
            "-e", f"POSTGRES_USER={USER}", "-e", f"POSTGRES_PASSWORD={PASSWORD}",
            "-e", f"POSTGRES_DB={DBNAME}", "-p", f"{port}:5432", DOCKER_IMAGE]


def setup_postgres_in_docker() -> SetupResult:
    """No server listening: start one in Docker with the role and database
    built in, wait for it, then load the sample tables."""
    import psycopg

    res = SetupResult("postgres", False)
    cmd = docker_command()
    res.lines.append("$ " + " ".join(cmd))
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        res.lines.append(run.stderr.strip() or "docker run failed")
        return res
    for _ in range(60):   # first run downloads the image; then ~3 s to start
        problem = _verify_login("localhost", 5432)
        if problem is None or "orders" in problem:     # up; tables not loaded yet
            break
        time.sleep(1)
    try:
        with psycopg.connect(host="localhost", port=5432, user=USER, password=PASSWORD,
                             dbname=DBNAME, autocommit=True, connect_timeout=3) as conn:
            _load_postgres_tables(conn)
    except psycopg.Error as exc:
        res.lines.append(f"started the container, but couldn't load data: {exc}")
        return res
    res.ok = True
    res.lines += [f"started container {DOCKER_CONTAINER} ({DOCKER_IMAGE})",
                  f"note: in Docker, {USER} is the server's superuser",
                  f"stop it: docker stop {DOCKER_CONTAINER}   remove it: docker rm -f {DOCKER_CONTAINER}",
                  "", *connection_lines("localhost", 5432)]
    return res


def postgres_plan() -> str:
    """'server', 'docker', or 'none': what setup would do for Postgres."""
    if not any(s.backend == "postgres" and s.available for s in discover.backend_statuses(False)):
        return "none"
    host, port = _host_port()
    if discover._port_open(host, port):
        return "server"
    return "docker" if docker_available() else "none"


# --------------------------------------------------------------------------- #
# Everything, for `querymux --setup`
# --------------------------------------------------------------------------- #

def run_setup(use_docker: bool) -> list[SetupResult]:
    available = {s.backend for s in discover.backend_statuses(False) if s.available}
    results = []
    if "sqlite" in available:
        results.append(setup_sqlite())
    if "duckdb" in available:
        results.append(setup_duckdb())
    plan = postgres_plan()
    if plan == "server":
        results.append(setup_postgres_on_server())
    elif plan == "docker" and use_docker:
        results.append(setup_postgres_in_docker())
    elif "postgres" in available:
        host, port = _host_port()
        results.append(SetupResult("postgres", False, [
            f"no server on {host}:{port}.",
            "Start one with Docker (the role and database are created for you):",
            "  " + " ".join(docker_command()),
            "then run querymux --setup again to load the sample tables.",
        ]))
    return results


def format_results(results: list[SetupResult]) -> str:
    out = []
    for r in results:
        out.append(f"{r.engine}  {'✓' if r.ok else '✗'}")
        out += [f"  {line}" if line else "" for line in r.lines]
        out.append("")
    return "\n".join(out).rstrip()
