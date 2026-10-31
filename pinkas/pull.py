"""Fetches Pinkas.lua from the gaming PC, ingests it and purges old raw files. Run every few minutes.

Output is counts and the hash only, never file content: it goes to a log. A `task_log` row is
written for every new file and on every change of failure state, through the host's optional
`tasklog` module; without it the pull runs the same and only prints.
"""

import os
import select
import shlex
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg2
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from pinkas.db import execute, files, pull_state
from pinkas.ingest import ingest
from pinkas.parse import MAX_BYTES

PROBE_SECONDS = 3
SOURCE_SECONDS = 60
KEEP_FILES_DAYS = 30
CHUNK = 65536


class Failure(Exception):
    """A pull that failed past the probe; its status is one of pinkas_pull_state's."""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


def asleep(probe: str) -> bool:
    """No answer on host:port within the timeout: the PC is asleep, which is not a failure."""
    host, _, port = probe.rpartition(":")
    try:
        socket.create_connection((host, int(port)), timeout=PROBE_SECONDS).close()
    except OSError:
        return True

    return False


def fetch(source: str) -> bytes:
    """The source command's stdout, read up to the size cap so an oversized file is never buffered,
    within one deadline for the whole read, so a hung connection can't stall the job.

    stderr is discarded: it can carry host names, and the status says enough.
    """
    try:
        proc = subprocess.Popen(shlex.split(source), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        raise Failure("source-failed") from None

    deadline = time.monotonic() + SOURCE_SECONDS
    body = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([proc.stdout], [], [], remaining)[0]:
                raise Failure("source-failed")

            chunk = os.read(proc.stdout.fileno(), CHUNK)
            if not chunk:
                break

            body += chunk
            if len(body) > MAX_BYTES:
                raise Failure("too-large")

        if proc.wait(timeout=max(0.0, deadline - time.monotonic())) != 0:
            raise Failure("source-failed")
    except subprocess.TimeoutExpired:
        raise Failure("source-failed") from None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    return bytes(body)


def purge(conn) -> int:
    with conn, conn.cursor() as cur:
        execute(cur, sa.delete(files).where(files.c.fetched_at < sa.func.now() - timedelta(days=KEEP_FILES_DAYS)))

        return cur.rowcount


def record_state(conn, status: str, log: Callable) -> None:
    """Writes the status only when it changes, with one task_log row: a failure that persists for a
    day leaves one row, not one every few minutes."""
    with conn, conn.cursor() as cur:
        execute(cur, sa.select(pull_state.c.status))
        row = cur.fetchone()
        previous = row[0] if row else None
        if status == previous:
            return

        upsert = insert(pull_state).values(status=status, since=sa.func.now())
        execute(cur, upsert.on_conflict_do_update(
            index_elements=[pull_state.c.id], set_={"status": upsert.excluded.status, "since": upsert.excluded.since},
        ))

    if status != "ok":
        log("error", status)
    elif previous is not None:
        log("ok", f"recovered from {previous}")


def run(conn, source: str, probe: str | None, log: Callable) -> int:
    """One pull; returns the exit code."""
    purged = purge(conn)
    if purged:
        print(f"purged {purged} raw files")
    if probe and asleep(probe):
        print("asleep")

        return 0

    status, _ = store(conn, lambda: fetch(source), log)

    return 0 if status == "ok" else 1


def store(conn, read: Callable[[], bytes], log: Callable) -> tuple[str, dict | None]:
    """Reads one file, ingests it and records the outcome; returns its pull_state status and ingest's counts.
    The Windows app's watcher calls this too, so its files show in the nerd panel the same way."""
    try:
        body = read()
        counts = ingest(conn, body, datetime.now(UTC))
    except Failure as failure:
        record_state(conn, failure.status, log)
        print(failure.status)

        return failure.status, None
    # ParseError is a ValueError; a value the driver or Postgres refuses is the file's fault too.
    except (ValueError, psycopg2.DataError):
        record_state(conn, "parse-failed", log)
        print("parse-failed")

        return "parse-failed", None

    record_state(conn, "ok", log)
    digest = counts["sha256"][:12]
    if counts["unchanged"]:
        print(f"unchanged {digest}")

        return "ok", counts

    summary = " ".join(f"{k}={v}" for k, v in counts.items() if k not in ("sha256", "unchanged"))
    print(f"ingested {digest} {summary}")
    log("ok", f"ingested {summary}")

    return "ok", counts


def main() -> int:
    try:
        import tasklog
    except ImportError:
        tasklog = None

    def log(status: str, summary: str) -> None:
        if tasklog:
            tasklog.log_run("pinkas", "pinkas-pull", status, summary)

    # A libpq message can quote the DSN, and a constraint error the failing row: print the class only.
    try:
        conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=5)
    except psycopg2.Error as exc:
        print(f"database unreachable ({type(exc).__name__})")

        return 1

    try:
        return run(conn, os.environ["PINKAS_SOURCE"], os.environ.get("PINKAS_PROBE") or None, log)
    except psycopg2.Error as exc:
        print(f"database error ({type(exc).__name__})")

        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
