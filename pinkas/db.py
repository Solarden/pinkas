"""Query helpers shared by the pages."""


def fetch(cur, sql: str, args=()) -> list[dict]:
    cur.execute(sql, args)
    names = [d.name for d in cur.description]

    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


# Each character's newest snapshot as `s`, walking the (character_id, logout_at) unique index.
NEWEST = """CROSS JOIN LATERAL (SELECT logout_at, data FROM pinkas_snapshots
                    WHERE character_id = c.id ORDER BY logout_at DESC LIMIT 1) s"""
