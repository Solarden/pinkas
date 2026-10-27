"""The Windows app's Postgres launcher, against the bundled binaries. One server, so the tests run in order."""

import json
import shutil
import sys

import psutil
import psycopg2
import pytest

from pinkas import postgres
from tests.files import server_folder


@pytest.fixture(scope="module")
def base():
    base = server_folder()
    yield base
    postgres.stop(base / "pg")
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def fresh():
    folder = server_folder()
    yield folder
    postgres.stop(folder / "pg")
    shutil.rmtree(folder, ignore_errors=True)


def server_pids(base) -> list[int]:
    # As pg_ctl hands it over: the short path, with forward slashes on Windows.
    folder = postgres.short(base / "pg").replace("\\", "/").lower()

    return [p.pid for p in psutil.process_iter(["name", "cmdline"])
            if (p.info["name"] or "").startswith("postgres")
            and folder in " ".join(p.info["cmdline"] or []).replace("\\", "/").lower()]


def test_a_first_start_makes_both_roles_on_loopback_only(base):
    server = postgres.start(base)
    password = json.loads((base / "secrets.json").read_text())["postgres"]

    with psycopg2.connect(server.app_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_user")
        app = cur.fetchone()[0]

    with psycopg2.connect(postgres.dsn(server.port, "postgres", password, "pinkas")) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_setting('listen_addresses'), current_setting('unix_socket_directories')")

        assert (app, *cur.fetchone()) == ("pinkas", "127.0.0.1", "")


def test_every_login_needs_its_password(base):
    server = postgres.start(base)
    password = json.loads((base / "secrets.json").read_text())["postgres"]

    for user in ("postgres", "pinkas_owner", "pinkas"):
        with pytest.raises(psycopg2.OperationalError):
            psycopg2.connect(postgres.dsn(server.port, user, "wrong", "pinkas"), connect_timeout=5)

    psycopg2.connect(postgres.dsn(server.port, "postgres", password, "postgres")).close()


@pytest.mark.skipif(sys.platform == "win32", reason="the folder's ACL guards it on Windows")
def test_the_passwords_file_is_the_users_own(base):
    postgres.start(base)

    assert (base / "secrets.json").stat().st_mode & 0o077 == 0


def test_a_start_after_a_killed_app_replaces_its_server(base):
    first = postgres.start(base)
    old = server_pids(base)
    second = postgres.start(base)

    psycopg2.connect(second.app_dsn).close()

    with pytest.raises(psycopg2.OperationalError):
        psycopg2.connect(first.app_dsn, connect_timeout=5)

    assert not set(old) & set(server_pids(base))


def test_a_start_after_a_crash_recovers(base):
    postgres.start(base)

    processes = [psutil.Process(pid) for pid in server_pids(base)]

    for process in processes:
        process.kill()

    psutil.wait_procs(processes, timeout=10)
    server = postgres.start(base)

    psycopg2.connect(server.app_dsn).close()


def test_stop_leaves_no_server(base):
    postgres.start(base)
    postgres.stop(base / "pg")

    assert server_pids(base) == []


def test_a_lost_passwords_file_is_refused_not_replaced(fresh):
    postgres.start(fresh)
    postgres.stop(fresh / "pg")
    (fresh / "secrets.json").unlink()

    with pytest.raises(postgres.LostSecrets):
        postgres.start(fresh)

    assert not (fresh / "secrets.json").exists()


def test_postgres_log_starts_over_past_its_limit(base):
    log = base / "postgres.log"
    postgres.stop(base / "pg")
    log.write_bytes(b"x" * (postgres.LOG_LIMIT + 1))
    postgres.start(base)

    assert (base / "postgres.log.1").stat().st_size == postgres.LOG_LIMIT + 1
    assert log.stat().st_size < postgres.LOG_LIMIT


def test_the_role_statements_run_with_failed_statements_left_out_of_the_log(base, monkeypatch):
    server = postgres.start(base)
    passwords = json.loads((base / "secrets.json").read_text())
    statements = []
    connect = psycopg2.connect

    class Recording:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def __setattr__(self, name, value):
            if name == "conn":
                return object.__setattr__(self, name, value)

            setattr(self.conn, name, value)

        def cursor(self):
            cur = self.conn.cursor()
            execute = cur.execute

            class Cursor:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    cur.close()

                def execute(self, sql, args=None):
                    statements.append(sql)

                    return execute(sql, args)

                def fetchone(self):
                    return cur.fetchone()

            return Cursor()

    monkeypatch.setattr(postgres.psycopg2, "connect", lambda dsn: Recording(connect(dsn)))
    postgres.roles(server.port, passwords)
    admin = statements[:statements.index("SELECT 1 FROM pg_database WHERE datname = 'pinkas'")]

    assert admin[0] == "SET log_min_error_statement = panic"
