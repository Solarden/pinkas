import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# Without this, alembic.ini's [logger_alembic] is never applied and an upgrade prints nothing.
fileConfig(context.config.config_file_name)


def _dsn() -> str:
    """MIGRATE_DATABASE_URL from the environment, or from the env file that PINKAS_ENV names."""
    if dsn := os.environ.get("MIGRATE_DATABASE_URL"):
        return dsn
    env_file = os.environ.get("PINKAS_ENV")
    if not env_file:
        raise SystemExit("set MIGRATE_DATABASE_URL, or PINKAS_ENV to the env file holding it")
    for line in Path(env_file).read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "MIGRATE_DATABASE_URL":
            return value.strip().strip('"').strip("'")

    raise SystemExit(f"MIGRATE_DATABASE_URL missing from {env_file}")


# psycopg2, the driver the server has from apt: SQLAlchemy 2.1 maps bare postgresql:// to psycopg 3.
url = _dsn().replace("postgresql://", "postgresql+psycopg2://", 1)
engine = create_engine(url, poolclass=pool.NullPool)

with engine.connect() as connection:
    # The database may be shared: the default alembic_version would collide with another app's.
    context.configure(connection=connection, version_table="pinkas_alembic_version")
    with context.begin_transaction():
        context.run_migrations()
