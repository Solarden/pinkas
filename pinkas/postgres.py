"""The Windows app's own Postgres: the binaries from pixeltable-pgserver, driven through initdb and pg_ctl.

pgserver's own `get_server` is not used: its initdb trusts every local login, it keeps its log inside the
data folder (30 s of retries after a crash on Windows), and it trusts a stale pid.
"""

import json
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

import psycopg2
from pixeltable_pgserver.utils import POSTGRES_VERSIONS

BIN = POSTGRES_VERSIONS[18]
WINDOWS = sys.platform == "win32"
EXE = ".exe" if WINDOWS else ""
READY_SECONDS = 90
# Postgres' own log starts over past this, as the app's does (desktop.LOG_LIMIT).
LOG_LIMIT = 1_000_000
SETTINGS = "\nlisten_addresses = '127.0.0.1'\nunix_socket_directories = ''\n"


class LostSecrets(RuntimeError):
    """The data folder exists but its passwords don't: new ones would never log in to the old roles."""


class Server(NamedTuple):
    port: int
    app_dsn: str
    owner_dsn: str


def short(path: Path) -> str:
    """Postgres reads its arguments in the ANSI code page; the 8.3 name of an existing path is ASCII."""
    if not WINDOWS:
        return str(path)

    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)

    return buffer.value if ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024) else str(path)


def run(tool: str, *args: str, timeout: float = 120) -> int:
    # Never a pipe: postgres inherits it and holds it open, so reading it to the end never returns.
    return subprocess.run(
        [str(BIN / f"{tool}{EXE}"), *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=timeout,
    ).returncode


def load_secrets(path: Path) -> dict[str, str]:
    """Three random passwords, made once."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    made = {role: secrets.token_hex(24) for role in ("postgres", "pinkas_owner", "pinkas")}
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(made), encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(path)

    return made


def initdb(data: Path, password: str) -> None:
    # This Windows initdb can't create its own folder: it reports the existing parent as "File exists".
    data.mkdir()
    pwfile = data.parent / "initdb-password.tmp"
    pwfile.write_text(password, encoding="utf-8")

    try:
        code = run(
            "initdb", "-D", short(data), "-U", "postgres", f"--pwfile={short(pwfile)}", "--auth=scram-sha-256",
            "--encoding=UTF8", "--locale=C", "--locale-provider=builtin", "--builtin-locale=C.UTF-8",
        )
    finally:
        pwfile.unlink()

    if code:
        raise RuntimeError(f"initdb failed ({code})")

    with open(data / "postgresql.conf", "a", encoding="utf-8") as conf:
        conf.write(SETTINGS)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))

        return s.getsockname()[1]


def dsn(port: int, user: str, password: str, db: str) -> str:
    return f"postgresql://{user}:{password}@127.0.0.1:{port}/{db}"


def wait_ready(port: int, password: str) -> None:
    """On Windows `pg_ctl -w` can't check the pid, and takes a pid file under 2 s old for the new server."""
    deadline = time.monotonic() + READY_SECONDS

    while True:
        try:
            psycopg2.connect(dsn(port, "postgres", password, "postgres"), connect_timeout=5).close()

            return
        except psycopg2.OperationalError:
            if time.monotonic() > deadline:
                raise

            time.sleep(0.5)


def stop(data: Path) -> None:
    if (data / "postmaster.pid").exists():
        run("pg_ctl", "-D", short(data), "-m", "fast", "-w", "stop", timeout=90)


def start(base: Path) -> Server:
    """Postgres in `base/pg`, listening on 127.0.0.1 only, made on the first run; returns where to reach it."""
    base.mkdir(parents=True, exist_ok=True)
    data = base / "pg"

    if (data / "PG_VERSION").exists() and not (base / "secrets.json").exists():
        raise LostSecrets

    passwords = load_secrets(base / "secrets.json")

    if not (data / "PG_VERSION").exists():
        # A first run that died inside initdb leaves a folder initdb won't reuse; it holds no data yet.
        shutil.rmtree(data, ignore_errors=True)

        initdb(data, passwords["postgres"])

    # A killed app leaves its server running; pg_ctl checks the pid file is really Postgres.
    if run("pg_ctl", "-D", short(data), "status", timeout=30) == 0:
        stop(data)

    port = free_port()
    log = base / "postgres.log"

    # Between servers: nothing holds the file now.
    if log.exists() and log.stat().st_size > LOG_LIMIT:
        log.replace(log.with_suffix(".log.1"))

    log.touch()
    code = run("pg_ctl", "-D", short(data), "-l", short(log), "-o", f"-p {port}", "-w", "-t", str(READY_SECONDS), "start",
               timeout=READY_SECONDS + 30)

    if code:
        raise RuntimeError(f"pg_ctl start failed ({code})")

    wait_ready(port, passwords["postgres"])
    roles(port, passwords)

    return Server(
        port,
        dsn(port, "pinkas", passwords["pinkas"], "pinkas"),
        dsn(port, "pinkas_owner", passwords["pinkas_owner"], "pinkas"),
    )


def roles(port: int, passwords: dict[str, str]) -> None:
    """Both roles and the database, each step only if missing."""
    admin = psycopg2.connect(dsn(port, "postgres", passwords["postgres"], "postgres"))
    admin.autocommit = True

    try:
        with admin.cursor() as cur:
            # A failed statement goes to the log whole, and this one holds a password.
            cur.execute("SET log_min_error_statement = panic")

            for role in ("pinkas_owner", "pinkas"):
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))

                if not cur.fetchone():
                    cur.execute(f"CREATE ROLE {role} LOGIN PASSWORD %s", (passwords[role],))

            cur.execute("SELECT 1 FROM pg_database WHERE datname = 'pinkas'")

            if not cur.fetchone():
                cur.execute("CREATE DATABASE pinkas OWNER pinkas_owner")
    finally:
        admin.close()

    owner = psycopg2.connect(dsn(port, "pinkas_owner", passwords["pinkas_owner"], "pinkas"))
    owner.autocommit = True

    try:
        with owner.cursor() as cur:
            cur.execute("GRANT USAGE ON SCHEMA public TO pinkas")
    finally:
        owner.close()


def is_elevated() -> bool:
    """Postgres drops the Administrators group, and then can't read what an elevated run created."""
    if not WINDOWS:
        return False

    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())
