"""Stores one pulled Pinkas.lua: the raw file, then each character's snapshot, events and totals."""

import hashlib
from datetime import UTC, datetime

import sqlalchemy as sa
from psycopg2.extras import Json
from sqlalchemy.dialects.postgresql import insert

from pinkas.db import characters, events, execute, files, session_totals, snapshots
from pinkas.parse import ParseError, parse

# These have tables of their own; copying them into every snapshot would repeat up to 500 events.
NOT_IN_SNAPSHOT = ("events", "totals")
# A rebuilt or reinstalled addon entry exports these as null until the next visit; the last capture,
# with its own "as of", is still the best answer, so each is carried from the previous snapshot.
CARRIED = ("bank", "mailbox")
# The export's account-wide fields, kept with each snapshot as "export": the page reads the routes,
# version and build from the newest one, and raw files are purged after 30 days.
EXPORT_IN_SNAPSHOT = ("v", "build", "routes")
# A time() outside these years is a damaged file; one near year 9999 overflows the pages' local zone.
EARLIEST = int(datetime(2000, 1, 1, tzinfo=UTC).timestamp())
LATEST = int(datetime(2100, 1, 1, tzinfo=UTC).timestamp())


def ts(epoch: int | None) -> datetime | None:
    if epoch is None:
        return None

    if not EARLIEST <= epoch < LATEST:
        raise ParseError("a time is out of range")

    return datetime.fromtimestamp(epoch, UTC)


def ingest(conn, body: bytes, fetched_at: datetime) -> dict:
    """One transaction: a broken file or any failure stores nothing, the raw file included.

    Returns the file's hash and counts only, never values: the caller logs them.
    """
    # Parsed first, so a file over the size cap or broken is never written, not even rolled back.
    export = parse(body)
    sha256 = hashlib.sha256(body).hexdigest()

    with conn, conn.cursor() as cur:
        execute(cur, insert(files).values(sha256=sha256, fetched_at=fetched_at, body=body)
                .on_conflict_do_nothing(index_elements=[files.c.sha256]).returning(files.c.id))
        row = cur.fetchone()
        # A file seen before, e.g. WoW restoring Pinkas.lua from its .bak.
        if row is None:
            return {"sha256": sha256, "unchanged": True}

        file_id = row[0]
        exported_at = ts(export["exported_at"])
        account = {k: export[k] for k in EXPORT_IN_SNAPSHOT}
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
            execute(cur, insert(characters).values(realm=char["realm"], name=char["name"]).on_conflict_do_nothing())
            execute(cur, sa.select(characters.c.id).where(characters.c.realm == char["realm"], characters.c.name == char["name"]))
            character_id = cur.fetchone()[0]
            snapshot = {k: v for k, v in char.items() if k not in NOT_IN_SNAPSHOT}
            logout_at = ts(char["logout_at"])

            if any(snapshot[k] is None for k in CARRIED):
                execute(cur, sa.select(*(snapshots.c.data[k] for k in CARRIED))
                        .where(snapshots.c.character_id == character_id, snapshots.c.logout_at < logout_at)
                        .order_by(snapshots.c.logout_at.desc()).limit(1))

                for k, previous in zip(CARRIED, cur.fetchone() or (None,) * len(CARRIED), strict=True):
                    if snapshot[k] is None:
                        snapshot[k] = previous

            execute(cur, insert(snapshots).values(
                character_id=character_id, logout_at=logout_at, session_started_at=ts(char["session_started_at"]),
                data=Json({**snapshot, "export": account}), file_id=file_id,
            ).on_conflict_do_nothing())
            counts["snapshots"] += cur.rowcount

            if char["events"]:
                execute(cur, insert(events).values([
                    {"character_id": character_id, "event_id": e["id"], "at": ts(e["at"]), "kind": e["kind"], "value": Json(e["value"])}
                    for e in char["events"]
                ]).on_conflict_do_nothing().returning(events.c.id))
                counts["events"] += len(cur.fetchall())

            if char["totals"]:
                # An older file (a .bak coming back) can't roll a total back.
                upsert = insert(session_totals).values([
                    {
                        "character_id": character_id, "session_started_at": ts(t["session"]), "kind": t["kind"],
                        "key": t["key"], "value": Json(t["value"]), "exported_at": exported_at,
                    }
                    for t in char["totals"]
                ])
                execute(cur, upsert.on_conflict_do_update(
                    index_elements=[session_totals.c.character_id, session_totals.c.session_started_at,
                                    session_totals.c.kind, session_totals.c.key],
                    set_={"value": upsert.excluded.value, "exported_at": upsert.excluded.exported_at},
                    where=upsert.excluded.exported_at > session_totals.c.exported_at,
                ).returning(session_totals.c.id))
                counts["totals"] += len(cur.fetchall())

    return counts
