"""A local demo: weeks of made-up play in its own database, served on 127.0.0.1:8095.

    docker compose -f docker-compose.test.yml up -d --wait
    uv run python -m tests.demo [--days 42]        then log in with the password "demo"

The database is `pinkas_demo` in the test container, rebuilt on every run; the test database is
never touched. The page runs as the app role, so a missing grant shows here too.
"""

import argparse
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import psycopg2
from alembic.config import main as alembic

from pinkas import web
from pinkas.ingest import ingest
from tests.db import DSN, ROOT
from tests.files import with_export
from tests.played import play

NAME = "pinkas_demo"
DEMO_DSN = urlunsplit(urlsplit(DSN)._replace(path=f"/{NAME}"))
HOSTILE = "</script><img src=x onerror=alert(1)>"


def rebuild(days: int) -> None:
    admin = psycopg2.connect(DSN, connect_timeout=3)
    admin.autocommit = True

    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {NAME} WITH (FORCE)")
        cur.execute(f"CREATE DATABASE {NAME}")
        cur.execute("DO $$ BEGIN CREATE ROLE pinkas NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$")

    admin.close()
    demo = psycopg2.connect(DEMO_DSN)
    # Refuses anything but the demo database: the schema grants below and the ingest write into it.
    assert demo.info.dbname == NAME
    demo.autocommit = True

    with demo.cursor() as cur:
        cur.execute("GRANT USAGE ON SCHEMA public TO pinkas")

    migrate_demo()
    demo.autocommit = False
    started = time.monotonic()
    # The play ends yesterday evening, so every chart and the calendar reach today.
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    files = play(days=days, hostile=HOSTILE, first_day=today - timedelta(days=days))

    for fetched, export in files:
        ingest(demo, with_export(export), fetched)

    with demo, demo.cursor() as cur:
        cur.execute("UPDATE pinkas_characters SET role = 'main' WHERE name = 'Fakechar'")

    demo.close()
    print(f"demo: {len(files)} logouts over {days} days ingested in {time.monotonic() - started:.1f} s")


def migrate_demo() -> None:
    os.environ["MIGRATE_DATABASE_URL"] = DEMO_DSN
    alembic(argv=["-c", str(ROOT / "alembic.ini"), "upgrade", "head"])


def as_app_role(dsn: str):
    conn = psycopg2.connect(dsn, connect_timeout=5)

    with conn.cursor() as cur:
        cur.execute("SET ROLE pinkas")

    conn.commit()

    return conn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=42)
    args = parser.parse_args()
    rebuild(args.days)
    os.environ.update(
        DATABASE_URL=DEMO_DSN, SESSION_SECRET=secrets.token_hex(16),
        PINKAS_PASSWORD_HASH=web.hash_password("demo"), PINKAS_LISTEN="127.0.0.1:8095",
    )
    web.connect = as_app_role
    web.main([])


if __name__ == "__main__":
    main()
