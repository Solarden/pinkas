"""The database tests run against the throwaway Postgres from docker-compose.test.yml.

Only the tests that use `database`, `conn` or `owner` need it; the parser and rested tests run without one.
"""

import psycopg2
import pytest

from tests.db import DSN, migrate

TABLES = (
    "pinkas_session_totals, pinkas_events, pinkas_snapshots, pinkas_targets, pinkas_pull_state,"
    " pinkas_characters, pinkas_files"
)


@pytest.fixture(scope="session")
def database():
    """A fresh schema, migrated by the test user, which plays the table owner."""
    try:
        admin = psycopg2.connect(DSN, connect_timeout=3)
    # The class name only: a driver message can quote the DSN.
    except psycopg2.Error as exc:
        pytest.exit(
            f"test database unreachable ({type(exc).__name__}) - run"
            " `docker compose -f docker-compose.test.yml up -d --wait`, or check PINKAS_TEST_DATABASE_URL"
        )
    admin.autocommit = True
    # The schema is dropped next: never on a database that isn't named as a test one.
    if not admin.info.dbname.endswith("_test"):
        pytest.exit(f"refusing to wipe database {admin.info.dbname!r}: a test database's name ends in _test")
    with admin.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        # The app role exists before the migration, as on the server.
        cur.execute("DO $$ BEGIN CREATE ROLE pinkas NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
        cur.execute("GRANT USAGE ON SCHEMA public TO pinkas")
    migrate("upgrade", "head")
    yield admin
    admin.close()


@pytest.fixture
def conn(database):
    """A connection acting as the app role, so a missing grant fails here, not on the server."""
    with database.cursor() as cur:
        cur.execute(f"TRUNCATE {TABLES} RESTART IDENTITY")
    c = psycopg2.connect(DSN)
    with c.cursor() as cur:
        cur.execute("SET ROLE pinkas")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def owner(database):
    """The table owner's connection, for reading back what the app role may not."""
    return database
