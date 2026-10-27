from datetime import UTC, datetime

import psycopg2
import pytest

from pinkas.ingest import ingest
from pinkas.parse import ParseError
from tests.db import migrate
from tests.files import KEY, account, fixture, with_export

FETCHED = datetime(2026, 10, 21, 10, 30, tzinfo=UTC)


def rows(cur, sql: str, *args) -> list[tuple]:
    cur.execute(sql, args)

    return cur.fetchall()


def export_with(edit) -> bytes:
    export = account()
    edit(export, export["chars"][KEY])

    return with_export(export)


def event(event_id: int, at: int, kind: str = "quest", **value) -> dict:
    return {"id": event_id, "at": at, "kind": kind, "value": value or {"quest": 1, "xp": 0, "money": 0}}


def test_the_beta_file_lands(conn, owner):
    counts = ingest(conn, fixture("fake_account.lua"), FETCHED)

    assert len(counts.pop("sha256")) == 64
    assert counts == {"unchanged": False, "characters": 1, "snapshots": 1, "events": 0, "totals": 0, "skipped": 0}
    with owner.cursor() as cur:
        (data,) = rows(cur, "SELECT data FROM pinkas_snapshots")[0]
        assert data["bags"][0]["name"] == "Hearthstone" and "events" not in data
        assert data["export"] == {"v": 2, "build": "1.60.1.70450", "routes": {}}


def test_the_same_file_twice_is_a_no_op(conn, owner):
    ingest(conn, fixture("fake_account.lua"), FETCHED)

    assert ingest(conn, fixture("fake_account.lua"), FETCHED)["unchanged"] is True
    with owner.cursor() as cur:
        assert rows(cur, "SELECT count(*) FROM pinkas_files") == [(1,)]


def test_a_new_logout_adds_one_snapshot(conn):
    ingest(conn, fixture("fake_account.lua"), FETCHED)

    def relog(e, c):
        e["exported_at"] += 600
        c["logout_at"] += 600

    assert ingest(conn, export_with(relog), FETCHED)["snapshots"] == 1


def test_a_bad_file_writes_nothing(conn, owner):
    def broken(e, c):
        c["level"] = "ten"

    with pytest.raises(ParseError):
        ingest(conn, export_with(broken), FETCHED)
    with owner.cursor() as cur:
        assert rows(cur, "SELECT count(*) FROM pinkas_files") == [(0,)]


def test_events_are_stored_once_and_same_second_quests_are_two(conn):
    def two_quests(e, c):
        c["events"] = [event(1, 1792570950), event(2, 1792570950)]

    assert ingest(conn, export_with(two_quests), FETCHED)["events"] == 2

    def one_more(e, c):
        two_quests(e, c)
        e["exported_at"] += 1
        c["events"].append(event(3, 1792571000))

    assert ingest(conn, export_with(one_more), FETCHED)["events"] == 1


def test_an_event_id_reused_after_a_bak_restore_is_kept(conn):
    def first(e, c):
        c["events"] = [event(5, 1792570950)]

    def after_restore(e, c):
        e["exported_at"] += 1
        c["events"] = [event(5, 1792579999)]

    ingest(conn, export_with(first), FETCHED)

    assert ingest(conn, export_with(after_restore), FETCHED)["events"] == 1


def totals_file(exported_at: int, gold_in: int) -> bytes:
    def edit(e, c):
        e["exported_at"] = exported_at
        c["totals"] = [
            {"session": 1792570902, "kind": "gold", "key": "loot", "value": {"in": gold_in, "out": 0}},
            {"session": 1792570902, "kind": "rested", "key": "", "value": {"xp": 10, "rested_used": 5}},
        ]

    return export_with(edit)


def test_a_session_total_keeps_the_newest_export(conn, owner):
    for exported_at, gold_in in ((1792571000, 4), (1792571100, 9), (1792571200, 15)):
        ingest(conn, totals_file(exported_at, gold_in), FETCHED)
    # A .bak coming back: an older export of the same session.
    assert ingest(conn, totals_file(1792571100, 9), FETCHED)["unchanged"] is True
    ingest(conn, totals_file(1792571150, 11), FETCHED)

    with owner.cursor() as cur:
        totals = rows(cur, "SELECT kind, key, value FROM pinkas_session_totals ORDER BY kind")
    assert totals == [("gold", "loot", {"in": 15, "out": 0}), ("rested", "", {"xp": 10, "rested_used": 5})]


def test_a_level_with_no_seen_start_stays_null(conn, owner):
    def level(e, c):
        c["events"] = [event(1, 1792570950, "level", level=11, zone="Durotar", played_total=5000, played_level=None)]

    ingest(conn, export_with(level), FETCHED)

    with owner.cursor() as cur:
        assert rows(cur, "SELECT value -> 'played_level' FROM pinkas_events") == [(None,)]


def test_a_character_never_logged_out_is_skipped(conn):
    def never(e, c):
        c["logout_at"] = None

    assert ingest(conn, export_with(never), FETCHED)["skipped"] == 1


def test_a_non_ascii_name_is_stored_whole(conn, owner):
    ingest(conn, fixture("fake_nonascii.lua"), FETCHED)

    with owner.cursor() as cur:
        assert rows(cur, "SELECT name FROM pinkas_characters") == [("Zoë",)]


def test_the_app_role_purges_files_but_never_rewrites_history(conn, owner):
    ingest(conn, fixture("fake_account.lua"), FETCHED)
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM pinkas_files")
    with owner.cursor() as cur:
        assert rows(cur, "SELECT file_id FROM pinkas_snapshots") == [(None,)]

    with pytest.raises(psycopg2.errors.InsufficientPrivilege), conn, conn.cursor() as cur:
        cur.execute("UPDATE pinkas_snapshots SET data = '{}'")


@pytest.mark.usefixtures("database")
def test_the_migration_goes_down_and_up():
    migrate("downgrade", "base")
    migrate("upgrade", "head")
