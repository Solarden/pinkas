from pinkas import db


def test_the_declared_tables_are_the_migrated_ones(owner):
    with owner.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name <> 'pinkas_alembic_version'"
        )
        migrated: dict[str, dict[str, str]] = {}

        for table, column, kind in cur.fetchall():
            migrated.setdefault(table, {})[column] = kind

    declared = {
        table.name: {column.name: column.type.compile(dialect=db.DIALECT).lower() for column in table.columns}
        for table in db.metadata.tables.values()
    }

    assert declared == migrated


def test_jsonb_is_read_with_the_arrow_on_every_sqlalchemy_2_0():
    assert "->" in str(db.snapshots.c.data["bank"].compile(dialect=db.DIALECT))
