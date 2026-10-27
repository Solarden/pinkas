"""the Windows app: the guide written in the app, and the routes set in the app

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# The guide editor's cap (pinkas/web.py GUIDE_LIMIT), in bytes of UTF-8.
GUIDE_BYTES = 262144
# pinkas/parse.py ROUTE_KINDS.
KINDS = ("herb", "cloth", "leather", "ore", "enchanting")


def upgrade() -> None:
    op.create_table(
        "pinkas_guide",
        # One row: the key can only be true.
        sa.Column("id", sa.Boolean, primary_key=True, server_default=sa.true()),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id", name="pinkas_guide_one_row"),
        sa.CheckConstraint(f"octet_length(text) <= {GUIDE_BYTES}", name="pinkas_guide_size"),
    )
    op.create_table(
        "pinkas_routes",
        sa.Column("kind", sa.Text, primary_key=True),
        sa.Column("character_id", sa.Integer, sa.ForeignKey("pinkas_characters.id"), nullable=False),
        sa.CheckConstraint("kind IN (" + ", ".join(f"'{k}'" for k in KINDS) + ")", name="pinkas_routes_kind_check"),
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON pinkas_guide TO pinkas")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON pinkas_routes TO pinkas")


def downgrade() -> None:
    op.drop_table("pinkas_routes")
    op.drop_table("pinkas_guide")
