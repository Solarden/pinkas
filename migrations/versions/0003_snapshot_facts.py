"""the facts /stats charts over time, as stored generated columns of each snapshot

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-22
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# History reads these instead of `data`: `data` is toasted, so extracting from it decompresses every bag.
# A value past the column's type fails the file at insert.
FACTS = (
    ("level", sa.Integer, "(data->>'level')::int"),
    ("xp", sa.Integer, "(data->>'xp')::int"),
    ("xp_max", sa.Integer, "(data->>'xp_max')::int"),
    ("money", sa.BigInteger, "(data->>'money')::bigint"),
    ("professions", JSONB, "data->'professions'"),
    # Trade goods (item class 7) in bags and bank: the stockpiles. A null bank gives none.
    (
        "mats", JSONB,
        "jsonb_path_query_array(data, '$.bags[*] ? (@.class == 7)')"
        " || jsonb_path_query_array(data, '$.bank.items[*] ? (@.class == 7)')",
    ),
)


def upgrade() -> None:
    for name, kind, expression in FACTS:
        op.add_column("pinkas_snapshots", sa.Column(name, kind, sa.Computed(expression, persisted=True)))


def downgrade() -> None:
    for name, _, _ in reversed(FACTS):
        op.drop_column("pinkas_snapshots", name)
