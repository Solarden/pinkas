import shlex
import socket
from datetime import UTC, datetime, timedelta

import psycopg2
import pytest

from pinkas import pull
from pinkas.ingest import ingest
from tests.db import DSN
from tests.files import FIXTURES, KEY, account, fixture, with_export

GOOD = f"cat {shlex.quote(str(FIXTURES / 'fake_account.lua'))}"


class Log(list):
    def __call__(self, status: str, summary: str) -> None:
        self.append((status, summary))


@pytest.fixture
def log():
    return Log()


@pytest.fixture
def closed_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    return f"127.0.0.1:{port}"


def state(owner) -> list[tuple]:
    with owner.cursor() as cur:
        cur.execute("SELECT status FROM pinkas_pull_state")

        return cur.fetchall()


def test_asleep_is_quiet_and_not_a_failure(conn, owner, log, closed_port, capsys):
    assert pull.run(conn, "false", closed_port, log) == 0

    assert capsys.readouterr().out == "asleep\n"
    assert log == [] and state(owner) == []


def test_a_new_file_is_ingested_and_logged_once(conn, owner, log, capsys):
    assert pull.run(conn, GOOD, None, log) == 0
    assert pull.run(conn, GOOD, None, log) == 0

    out = capsys.readouterr().out
    assert out.startswith("ingested ") and "\nunchanged " in out
    assert log == [("ok", "ingested characters=1 snapshots=1 events=0 totals=0 skipped=0")]
    assert state(owner) == [("ok",)]


@pytest.mark.parametrize(
    ("source", "status"),
    [("false", "source-failed"), ("no-such-command-pinkas", "source-failed"),
     (f"head -c {pull.MAX_BYTES + 1} /dev/zero", "too-large"), ("printf garbage", "parse-failed")],
    ids=["exit code", "missing command", "too large", "not a Pinkas.lua"],
)
def test_a_failure_is_logged_once_and_its_recovery_too(conn, owner, log, source, status):
    assert pull.run(conn, source, None, log) == 1
    assert pull.run(conn, source, None, log) == 1
    assert state(owner) == [(status,)]

    pull.run(conn, GOOD, None, log)

    assert log[:2] == [("error", status), ("ok", f"recovered from {status}")]
    assert state(owner) == [("ok",)]


def test_a_hung_source_fails_by_the_deadline(conn, owner, log, monkeypatch):
    monkeypatch.setattr(pull, "SOURCE_SECONDS", 0.3)

    assert pull.run(conn, "sleep 5", None, log) == 1
    assert state(owner) == [("source-failed",)]


def test_old_raw_files_are_purged_and_snapshots_kept(conn, owner, log, closed_port, capsys):
    ingest(conn, fixture("fake_account.lua"), datetime.now(UTC) - timedelta(days=31))

    pull.run(conn, "false", closed_port, log)

    assert capsys.readouterr().out == "purged 1 raw files\nasleep\n"
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM pinkas_files")
        assert cur.fetchall() == [(0,)]
        cur.execute("SELECT file_id FROM pinkas_snapshots")
        assert cur.fetchall() == [(None,)]


def test_the_output_carries_no_file_content(conn, log, capsys):
    pull.run(conn, GOOD, None, log)

    out = capsys.readouterr().out + repr(log)
    assert "Fakechar" not in out and "Hearthstone" not in out


@pytest.mark.parametrize(
    "edit",
    [
        lambda c: c.update(events=[{"id": 2**40, "at": 1792570950, "kind": "quest", "value": {}}]),
        lambda c: c.update(logout_at=10**18),
        lambda c: c["bags"][0].update(name="Hearth\u0000stone"),
    ],
    ids=["number past the column", "time past datetime", "NUL in a string"],
)
def test_a_value_the_database_refuses_is_a_bad_file(conn, owner, log, tmp_path, edit):
    export = account()
    edit(export["chars"][KEY])
    path = tmp_path / "Pinkas.lua"
    path.write_bytes(with_export(export))

    assert pull.run(conn, f"cat {shlex.quote(str(path))}", None, log) == 1
    assert state(owner) == [("parse-failed",)] and log == [("error", "parse-failed")]


@pytest.mark.parametrize(
    "dsn",
    ["postgresql://pinkas:FRAGMENT@127.0.0.1:1/pinkas", "postgresql://pinkas:FRAGMENT@[broken"],
    ids=["refused", "malformed"],
)
def test_a_database_error_never_prints_the_dsn(monkeypatch, capsys, dsn):
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("PINKAS_SOURCE", "false")

    assert pull.main() == 1
    out = capsys.readouterr().out

    assert out.startswith("database unreachable (") and "FRAGMENT" not in out


@pytest.mark.usefixtures("database")
def test_a_database_error_mid_run_never_prints_its_detail(monkeypatch, capsys):
    def failing_run(*_):
        raise psycopg2.errors.UniqueViolation("Failing row contains (FRAGMENT)")

    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.setenv("PINKAS_SOURCE", "false")
    monkeypatch.setattr(pull, "run", failing_run)

    assert pull.main() == 1
    out = capsys.readouterr().out

    assert out == "database error (UniqueViolation)\n"
