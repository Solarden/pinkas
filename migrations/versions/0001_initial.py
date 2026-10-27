"""characters, snapshots, events, session totals, raw files, targets, pull state

Revision ID: 0001
Revises:
Create Date: 2026-10-21
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def character_fk() -> sa.Column:
    return sa.Column("character_id", sa.Integer, sa.ForeignKey("pinkas_characters.id"), nullable=False)




def upgrade() -> None:
    op.create_table(
        "pinkas_files",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("sha256", sa.Text, nullable=False, unique=True),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("body", sa.LargeBinary, nullable=False),
    )
    op.create_table(
        "pinkas_characters",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("realm", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("role", sa.Text),
        sa.Column("sort", sa.Integer),
        sa.Column("well_rested_rank", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("archived_at", TS),
        sa.Column("playing_since", TS),
        sa.UniqueConstraint("realm", "name", name="pinkas_characters_realm_name_key"),
        sa.CheckConstraint("well_rested_rank BETWEEN 0 AND 5", name="pinkas_characters_rank_check"),
    )
    # At most one character is being played at a time.
    op.create_index(
        "pinkas_characters_one_playing", "pinkas_characters", [sa.text("(true)")], unique=True,
        postgresql_where=sa.text("playing_since IS NOT NULL"),
    )
    op.create_table(
        "pinkas_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True),
        character_fk(),
        sa.Column("logout_at", TS, nullable=False),
        sa.Column("session_started_at", TS),
        sa.Column("data", JSONB, nullable=False),
        # The pull deletes raw files after 30 days; the snapshot outlives its file.
        sa.Column("file_id", sa.BigInteger, sa.ForeignKey("pinkas_files.id", ondelete="SET NULL")),
        sa.UniqueConstraint("character_id", "logout_at", name="pinkas_snapshots_character_logout_key"),
    )
    op.create_table(
        "pinkas_targets",
        sa.Column("id", sa.Integer, primary_key=True),
        character_fk(),
        sa.Column("skill_line", sa.Integer, nullable=False),
        sa.Column("skill", sa.SmallInteger, nullable=False),
        sa.UniqueConstraint("character_id", "skill_line", "skill", name="pinkas_targets_key"),
        sa.CheckConstraint("skill IN (150, 225, 300)", name="pinkas_targets_skill_check"),
    )
    op.create_table(
        "pinkas_pull_state",
        # One row: the key can only be true.
        sa.Column("id", sa.Boolean, primary_key=True, server_default=sa.true()),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("since", TS, nullable=False),
        sa.CheckConstraint("id", name="pinkas_pull_state_one_row"),
        sa.CheckConstraint(
            "status IN ('ok', 'asleep-skipped', 'source-failed', 'parse-failed', 'too-large')",
            name="pinkas_pull_state_status_check",
        ),
    )
    op.create_table(
        "pinkas_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        character_fk(),
        sa.Column("event_id", sa.Integer, nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("value", JSONB, nullable=False),
        # `at` is in the key: after a .bak restore the addon reuses event ids with later times.
        sa.UniqueConstraint("character_id", "event_id", "at", name="pinkas_events_key"),
        sa.CheckConstraint("kind IN ('level', 'death', 'quest', 'mail', 'dungeon')", name="pinkas_events_kind_check"),
    )
    op.create_table(
        "pinkas_session_totals",
        sa.Column("id", sa.BigInteger, primary_key=True),
        character_fk(),
        sa.Column("session_started_at", TS, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        # '' where a kind has no key: NULLs would never conflict in the unique key.
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("exported_at", TS, nullable=False),
        sa.UniqueConstraint("character_id", "session_started_at", "kind", "key", name="pinkas_session_totals_key"),
        sa.CheckConstraint("kind IN ('gold', 'combat', 'gather', 'rested')", name="pinkas_session_totals_kind_check"),
    )

    # Alembic has no grant op. The app role `pinkas` (web app and pull) must exist first. It can't
    # rewrite snapshots or events, and deletes only raw files, which the pull purges.
    op.execute("GRANT SELECT, INSERT, DELETE ON pinkas_files TO pinkas")
    op.execute("GRANT SELECT, INSERT, UPDATE ON pinkas_characters, pinkas_session_totals, pinkas_pull_state TO pinkas")
    op.execute("GRANT SELECT, INSERT ON pinkas_snapshots, pinkas_events TO pinkas")
    op.execute("GRANT SELECT ON pinkas_targets TO pinkas")
    op.execute(
        "GRANT USAGE ON SEQUENCE pinkas_files_id_seq, pinkas_characters_id_seq, pinkas_snapshots_id_seq,"
        " pinkas_events_id_seq, pinkas_session_totals_id_seq TO pinkas"
    )


def downgrade() -> None:
    for table in (
        "pinkas_session_totals", "pinkas_events", "pinkas_pull_state", "pinkas_targets",
        "pinkas_snapshots", "pinkas_characters", "pinkas_files",
    ):
        op.drop_table(table)
