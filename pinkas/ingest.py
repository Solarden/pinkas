"""Stores one pulled Pinkas.lua: the raw file, then each character's snapshot, events and totals."""

import hashlib
from datetime import UTC, datetime

from psycopg2.extras import Json, execute_values

from pinkas.parse import ParseError, parse

# These have tables of their own; copying them into every snapshot would repeat up to 500 events.
NOT_IN_SNAPSHOT = ("events", "totals")


def ts(epoch: int | None) -> datetime | None:
    if epoch is None:
        return None

    # Out of range raises OverflowError, ValueError or (macOS) OSError, depending on the platform.
    try:
        return datetime.fromtimestamp(epoch, UTC)
    except (OverflowError, ValueError, OSError):
        raise ParseError("a time is out of range") from None


def ingest(conn, body: bytes, fetched_at: datetime) -> dict:
    """One transaction: a broken file or any failure stores nothing, the raw file included.

    Returns the file's hash and counts only, never values: the caller logs them.
    """
    # Parsed first, so a file over the size cap or broken is never written, not even rolled back.
    export = parse(body)
    sha256 = hashlib.sha256(body).hexdigest()

    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pinkas_files (sha256, fetched_at, body) VALUES (%s, %s, %s)"
            " ON CONFLICT (sha256) DO NOTHING RETURNING id",
            (sha256, fetched_at, body),
        )
        row = cur.fetchone()
        # A file seen before, e.g. WoW restoring Pinkas.lua from its .bak.
        if row is None:
            return {"sha256": sha256, "unchanged": True}

        file_id = row[0]
        exported_at = ts(export["exported_at"])
        counts = {
            "sha256": sha256, "unchanged": False,
            "characters": 0, "snapshots": 0, "events": 0, "totals": 0, "skipped": 0,
        }

        for char in export["chars"].values():
            # Loaded but never logged out with the addon: nothing to snapshot yet.
            if char["logout_at"] is None:
                counts["skipped"] += 1
                continue

            counts["characters"] += 1
            cur.execute(
                "INSERT INTO pinkas_characters (realm, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (char["realm"], char["name"]),
            )
            cur.execute(
                "SELECT id FROM pinkas_characters WHERE realm = %s AND name = %s", (char["realm"], char["name"])
            )
            character_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO pinkas_snapshots (character_id, logout_at, session_started_at, data, file_id)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    character_id, ts(char["logout_at"]), ts(char["session_started_at"]),
                    Json({k: v for k, v in char.items() if k not in NOT_IN_SNAPSHOT}), file_id,
                ),
            )
            counts["snapshots"] += cur.rowcount

            if char["events"]:
                added = execute_values(
                    cur,
                    "INSERT INTO pinkas_events (character_id, event_id, at, kind, value) VALUES %s"
                    " ON CONFLICT DO NOTHING RETURNING 1",
                    [(character_id, e["id"], ts(e["at"]), e["kind"], Json(e["value"])) for e in char["events"]],
                    fetch=True,
                )
                counts["events"] += len(added)

            if char["totals"]:
                # An older file (a .bak coming back) can't roll a total back.
                written = execute_values(
                    cur,
                    "INSERT INTO pinkas_session_totals"
                    " (character_id, session_started_at, kind, key, value, exported_at) VALUES %s"
                    " ON CONFLICT (character_id, session_started_at, kind, key) DO UPDATE"
                    " SET value = excluded.value, exported_at = excluded.exported_at"
                    " WHERE excluded.exported_at > pinkas_session_totals.exported_at RETURNING 1",
                    [
                        (character_id, ts(t["session"]), t["kind"], t["key"], Json(t["value"]), exported_at)
                        for t in char["totals"]
                    ],
                    fetch=True,
                )
                counts["totals"] += len(written)

    return counts
