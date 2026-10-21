"""The throwaway test database (docker-compose.test.yml) and its migrations."""

import os
from pathlib import Path

from alembic.config import main as alembic

DSN = os.environ.get("PINKAS_TEST_DATABASE_URL", "postgresql://pinkas_test:pinkas_test@127.0.0.1:55434/pinkas_test")
ROOT = Path(__file__).parent.parent


def migrate(*args: str) -> None:
    os.environ["MIGRATE_DATABASE_URL"] = DSN
    alembic(argv=["-c", str(ROOT / "alembic.ini"), *args])
