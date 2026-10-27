"""the settings page: targets written by the app role, character updates limited to the owner's columns

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Realm and name are the game's key for a character; ingest only inserts them.
OWNER_COLUMNS = "role, sort, well_rested_rank, archived_at, playing_since"


def upgrade() -> None:
    op.execute("GRANT INSERT, DELETE ON pinkas_targets TO pinkas")
    op.execute("GRANT USAGE ON SEQUENCE pinkas_targets_id_seq TO pinkas")
    op.execute("REVOKE UPDATE ON pinkas_characters FROM pinkas")
    op.execute(f"GRANT UPDATE ({OWNER_COLUMNS}) ON pinkas_characters TO pinkas")


def downgrade() -> None:
    op.execute(f"REVOKE UPDATE ({OWNER_COLUMNS}) ON pinkas_characters FROM pinkas")
    op.execute("GRANT UPDATE ON pinkas_characters TO pinkas")
    op.execute("REVOKE USAGE ON SEQUENCE pinkas_targets_id_seq FROM pinkas")
    op.execute("REVOKE INSERT, DELETE ON pinkas_targets FROM pinkas")
