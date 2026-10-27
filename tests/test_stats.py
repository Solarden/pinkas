from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg2
import pytest

from pinkas import stats
from pinkas.ingest import ingest
from tests.files import KEY, account_v3, with_export
from tests.played import play

WARSAW = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 10, 22, 22, tzinfo=UTC)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=WARSAW)


def event(character_id: int, **value) -> dict:
    return {"character_id": character_id, "at": NOW, "value": value}


def total(character_id: int, key: str, **value) -> dict:
    return {"character_id": character_id, "key": key, "value": value}


@pytest.fixture
def played(conn):
    for fetched, export in play():
        ingest(conn, with_export(export), fetched)

    return conn


def test_generated_columns_match_the_snapshot(played, owner):
    with owner.cursor() as cur:
        cur.execute(
            "SELECT count(*) FILTER (WHERE level IS DISTINCT FROM (data->>'level')::int"
            " OR money IS DISTINCT FROM (data->>'money')::bigint OR xp_max IS DISTINCT FROM (data->>'xp_max')::int"
            " OR professions IS DISTINCT FROM data->'professions'"
            " OR jsonb_array_length(mats) <> (SELECT count(*) FROM jsonb_array_elements(data->'bags') i"
            "   WHERE i->>'class' = '7') + (SELECT count(*) FROM jsonb_array_elements(data->'bank'->'items') i"
            "   WHERE i->>'class' = '7')), count(*) FROM pinkas_snapshots"
        )
        mismatched, snapshots = cur.fetchone()

    assert snapshots > 40 and mismatched == 0


def test_an_unknown_level_stays_null_and_a_null_bank_gives_no_mats(conn, owner):
    export = account_v3()
    export["chars"][KEY].update(level=None, bank=None, bags=[])
    ingest(conn, with_export(export), NOW)

    with owner.cursor() as cur:
        cur.execute("SELECT level, mats FROM pinkas_snapshots")

        assert cur.fetchone() == (None, [])


def test_a_level_past_int_fails_the_file_and_stores_nothing(conn, owner):
    export = account_v3()
    export["chars"][KEY]["level"] = 2**40

    with pytest.raises(psycopg2.DataError):
        ingest(conn, with_export(export), NOW)

    conn.rollback()

    with owner.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM pinkas_files) + (SELECT count(*) FROM pinkas_snapshots)")

        assert cur.fetchone() == (0,)


@pytest.mark.parametrize(
    ("tz", "weekly", "day"),
    [(WARSAW, False, date(2026, 10, 22)), (UTC, False, date(2026, 10, 21)), (UTC, True, date(2026, 10, 19))],
    ids=["warsaw", "utc", "utc-week"],
)
def test_days_follow_the_zone(tz, weekly, day):
    assert stats.bucket(datetime(2026, 10, 21, 22, 30, tzinfo=UTC), tz, weekly) == day


def test_a_session_is_one_span_split_at_midnight():
    sessions = [
        {"character_id": 1, "started": at("2026-10-20T22:00"), "ended": at("2026-10-21T01:30")},
        {"character_id": 2, "started": at("2026-10-20T18:00"), "ended": at("2026-10-20T19:00")},
    ]

    assert stats.calendar(sessions, WARSAW) == {
        1: {date(2026, 10, 20): 2.0, date(2026, 10, 21): 1.5},
        2: {date(2026, 10, 20): 1.0},
    }


def test_a_span_across_the_dst_change_counts_real_hours():
    hours = stats.calendar([{"character_id": 1, "started": at("2026-10-24T23:00"), "ended": at("2026-10-25T04:00")}], WARSAW)

    assert hours == {1: {date(2026, 10, 24): 1.0, date(2026, 10, 25): 5.0}}


def point(day: int, level: int | None, xp: int = 0, xp_max: int = 1000) -> dict:
    return {"day": date(2026, 10, day), "level": level, "xp": xp, "xp_max": xp_max}


@pytest.mark.parametrize(
    ("points", "expected"),
    [
        ([point(1, 10), point(15, 24)], 36),
        ([point(15, 24), point(15, 24, 500)], 36 * 2 - 1),
        ([point(1, 10), point(15, 10)], None),
        ([point(1, 58), point(15, 60, 0, 0)], None),
        ([point(15, 24)], None),
        ([point(1, None)], None),
    ],
    ids=["pace", "same-day", "no-progress", "at-60", "one-point", "unknown"],
)
def test_days_to_max(points, expected):
    assert stats.days_to_max(points, date(2026, 10, 15)) == expected


def test_time_per_level_leaves_an_unknown_or_negative_time_out():
    levels = [event(1, level=3, played_level=1800), event(1, level=4, played_level=None), event(1, level=5, played_level=-500)]

    assert stats.time_per_level(levels) == {1: {2: 0.5}}


def test_deaths_group_killers_and_keep_player():
    died = [event(1, zone="Durotar", killer="player"), event(2, zone="Durotar", killer="player"),
            event(1, zone=None, killer=None), event(1, zone="The Barrens", killer="Falling")]

    result = stats.deaths(died)

    assert result["killers"] == [("player", 2), ("Falling", 1), ("unknown", 1)]
    assert result["zones"] == [("Durotar", 2), ("The Barrens", 1), ("unknown", 1)]


def test_ranked_folds_the_tail_into_other():
    assert stats.ranked({f"z{i}": 20 - i for i in range(12)}, top=3) == [("z0", 20), ("z1", 19), ("other", sum(range(9, 19)))]


def test_gathering_sums_sessions_and_survives_a_key_without_a_zone():
    result = stats.gathering([total(1, "skinning|Durotar", count=3), total(2, "skinning|Durotar", count=4),
                              total(1, "mining", count=1)])

    assert result["cells"] == {("Durotar", "skinning"): 7, ("unknown", "mining"): 1}
    assert result["top"] == 7


CHARS = {1: {"name": "Fakechar", "label": "Fakechar", "key": "Testrealm/Fakechar"},
         2: {"name": "Altchar", "label": "Altchar", "key": "Testrealm/Altchar"}}


def test_flows_conserve_items_and_hide_a_stranger():
    mails = [
        event(1, to="Testrealm/Altchar", items=[{"id": 2589, "name": "Linen Cloth", "count": 20}]),
        event(2, to="Testrealm/Stranger", items=[{"id": 1, "name": "Thing", "count": 3}]),
    ]

    result = stats.flows(mails, CHARS, {2589: (7, 5)})

    assert result["left"] == [(("Fakechar", "Cloth"), 20), (("Altchar", "Other"), 3)]
    assert result["right"] == [(("Cloth", "Altchar"), 20), (("Other", "other"), 3)]
    assert "Stranger" not in repr(result)


def test_gold_sources_sum_characters_largest_first():
    gold = stats.gold_sources([total(1, "loot", **{"in": 5, "out": 0}), total(2, "loot", **{"in": 7, "out": 0}),
                               total(1, "repair", **{"in": 0, "out": 30})])

    assert gold == [{"source": "repair", "in": 0, "out": 30}, {"source": "loot", "in": 12, "out": 0}]


def test_the_carried_account_total():
    account = stats.carried({1: [(date(2026, 10, 1), 100), (date(2026, 10, 3), 50)], 2: [(date(2026, 10, 2), 10)]})

    assert account == [(date(2026, 10, 1), 100), (date(2026, 10, 2), 110), (date(2026, 10, 3), 60)]


def test_a_run_ended_by_logout_has_no_time():
    runs = [event(1, instance="Ragefire Chasm", duration=1800, deaths=1, ended_by_logout=False),
            event(1, instance="Ragefire Chasm", duration=600, deaths=0, ended_by_logout=True),
            event(1, instance="Ragefire Chasm", duration=2400, deaths=2, ended_by_logout=False)]

    assert stats.dungeons(runs) == [{"instance": "Ragefire Chasm", "runs": 3, "deaths": 3, "average": 2100, "best": 1800}]


def test_combat_per_level_and_fights_per_death():
    combat = stats.combat(
        [total(1, "10", damage=300, healing=30, seconds=30, fights=4), total(1, "11", damage=5, seconds=0, fights=2)],
        [event(1, level=10), event(1, level=10)],
        {1: {10: 4.0}},
    )

    assert combat["levels"] == {1: {10: {"dps": 10, "hps": 1, "deaths_per_hour": 0.5}}}
    assert combat["fights_per_death"] == {1: 3}


def test_rested_share_without_xp_is_unknown():
    assert stats.rested_share([total(1, "", xp=200, rested_used=50), total(2, "", xp=0, rested_used=0)]) == {1: 0.25, 2: None}


@pytest.mark.parametrize(("given", "key"), [("30", "30"), ("all", "all"), ("7", "90"), (None, "90"), ("'; --", "90")])
def test_range_key(given, key):
    assert stats.range_key(given) == key


def test_a_ninth_character_is_other(conn):
    with conn, conn.cursor() as cur:
        cur.executemany("INSERT INTO pinkas_characters (realm, name) VALUES ('Testrealm', %s)", [(f"C{i}",) for i in range(9)])
        slots = [c["slot"] for c in stats.characters(cur).values()]

    assert slots == [1, 2, 3, 4, 5, 6, 7, 8, 0]


def test_an_empty_database_collects_empty_charts(conn):
    with conn, conn.cursor() as cur:
        result = stats.collect(cur, "90", NOW, WARSAW)

    assert result["characters"] == {} and result["flows"]["total"] == 0 and result["dungeons"] == []


def test_collect_over_weeks_of_play(played):
    with played, played.cursor() as cur:
        week = stats.collect(cur, "30", NOW, WARSAW)
        everything = stats.collect(cur, "all", NOW, WARSAW)

    assert {"Fakechar", "Zoë", "Herbchar"} <= {c["name"] for c in everything["characters"].values()}
    assert everything["deaths"]["count"] >= week["deaths"]["count"] > 0
    assert everything["flows"]["total"] > 0 and everything["gathering"]["top"] > 0
    assert all(d > NOW.date() - timedelta(days=60) for days in everything["calendar"].values() for d in days)
    assert any(v is None for v in everything["to_max"].values()), "Herbchar is at 60"


def test_the_page_view_over_weeks_of_play(played):
    with played, played.cursor() as cur:
        result = stats.collect(cur, "all", NOW, WARSAW)

    view = stats.view(result)
    at_max = {c["name"] for c in view["to_max"] if c["at_max"]}

    assert at_max and not at_max & {s["name"] for s in view["level"]["series"]}, "a character at 60 is listed, not drawn"
    assert all(len(s["points"]) > 1 for key in ("dps", "hps") for s in view[key]["series"]), "no lone points"
    assert view["gold"]["series"][0]["slot"] == 0, "a total is grey: colour means a character"
    assert {m["name"] for m in view["stockpiles"]} <= {"Herbs", "Cloth", "Leather", "Ore", "Enchanting"}
    assert len(view["calendar_table"]["rows"]) == len({d for days in result["calendar"].values() for d in days})
    assert {"level", "gold-total", "dps"} <= view["hover"].keys()
    assert view["calendar_main"] is None, "no role is main"


def test_the_main_strip_sits_beside_the_year_and_leaves_the_grid(played):
    with played, played.cursor() as cur:
        cur.execute("UPDATE pinkas_characters SET role = ' Main ' WHERE name = 'Fakechar'")
        result = stats.collect(cur, "90", NOW, WARSAW)

    view = stats.view(result)

    assert view["calendar_main"]["name"] == "Fakechar"
    assert "Fakechar" not in {s["name"] for s in view["calendar"]}


def test_a_name_on_two_realms_is_labelled_with_its_realm(conn):
    with conn, conn.cursor() as cur:
        cur.executemany("INSERT INTO pinkas_characters (realm, name) VALUES (%s, %s)",
                        [("Testrealm", "Fakechar"), ("Otherrealm", "Fakechar"), ("Testrealm", "Zoë")])
        labels = sorted(c["label"] for c in stats.characters(cur).values())

    assert labels == ["Fakechar (Otherrealm)", "Fakechar (Testrealm)", "Zoë"]


def test_the_sparklines_without_history_and_with_falling_gold(conn):
    export = account_v3()
    char = export["chars"][KEY]
    ingest(conn, with_export(export), NOW)
    later = account_v3()
    later["chars"][KEY].update(money=char["money"] - 30_000, logout_at=char["logout_at"] + 86400)
    ingest(conn, with_export(later), NOW)

    with conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM pinkas_characters")
        (cid,) = cur.fetchone()
        then = datetime.fromtimestamp(char["logout_at"], UTC) + timedelta(days=2)
        falling = stats.history(cur, cid, then, WARSAW)
        empty = stats.history(cur, cid, then + timedelta(days=90), WARSAW)

    assert falling["gold"]["change"] == "−3g in 30 d" and falling["level"]["change"] == "+0.0 in 30 d"
    assert empty["gold"] == {"spark": {"empty": True}, "now": "", "change": ""}


def snapshot(conn, now: datetime, logout: datetime, **fields) -> None:
    export = account_v3()
    export["chars"][KEY].update(logout_at=int(logout.timestamp()), **fields)
    ingest(conn, with_export(export), now)


@pytest.mark.parametrize(
    ("started", "logout"),
    [(datetime(2000, 1, 2, tzinfo=UTC), NOW - timedelta(hours=1)),
     (NOW - timedelta(hours=1), datetime(2099, 1, 1, tzinfo=UTC))],
    ids=["started decades ago", "logout in the future"],
)
def test_a_session_from_the_file_is_clamped_to_the_strip(conn, started, logout):
    snapshot(conn, NOW, logout, session_started_at=int(started.timestamp()))

    with conn, conn.cursor() as cur:
        days = [d for per_day in stats.collect(cur, "90", NOW, WARSAW)["calendar"].values() for d in per_day]

    assert len(days) <= stats.CALENDAR_WEEKS * 7 + 1


def test_the_pace_to_60_counts_real_days_on_a_weekly_range(conn):
    for days_ago, level in ((400, 5), (13, 11), (0, 24)):
        snapshot(conn, NOW, NOW - timedelta(days=days_ago, hours=1), level=level, xp=0)

    with conn, conn.cursor() as cur:
        result = stats.collect(cur, "all", NOW, WARSAW)

    assert list(result["to_max"].values()) == [36]


def test_a_null_total_field_counts_as_zero():
    nulls = [total(1, "loot", **{"in": None, "out": 5})]

    assert stats.gold_sources(nulls) == [{"source": "loot", "in": 0, "out": 5}]
    assert stats.rested_share([total(1, "", xp=None, rested_used=None)]) == {1: None}
    assert stats.gathering([total(1, "skinning|Durotar", count=None)])["top"] == 0


@pytest.mark.parametrize("key", ["²", "9" * 5000, "0", "61", "x"])
def test_a_combat_key_that_is_no_level_is_skipped(key):
    assert stats.combat([total(1, key, damage=10, healing=0, seconds=5, fights=1)], [], {})["levels"] == {}


def test_under_a_second_of_combat_draws_no_dps():
    result = stats.combat([total(1, "10", damage=9e15, healing=0, seconds=1e-300, fights=1)], [], {})

    assert result["levels"] == {}


def test_the_gathering_grid_folds_past_the_top_zones():
    cells = stats.gathering([total(1, f"skinning|zone {i}", count=i + 1) for i in range(500)])

    assert len(cells["zones"]) == stats.TOP and cells["zones"][-1] == "other"
    assert sum(cells["cells"].values()) == sum(range(1, 501))


def test_gold_sources_past_the_top_fold_into_other():
    rows = stats.gold_sources([total(1, f"source {i}", **{"in": i + 1}) for i in range(15)])

    assert len(rows) == stats.TOP and rows[-1] == {"source": "other", "in": sum(range(1, 7)), "out": 0}


def test_dungeons_past_the_limit_fold_and_a_negative_time_is_left_out():
    runs = [event(1, instance=f"instance {i}", deaths=1, duration=600, ended_by_logout=False) for i in range(40)]
    runs.append(event(1, instance="instance 0", deaths=0, duration=-30, ended_by_logout=False))
    table = stats.dungeons(runs)

    assert len(table) == stats.DUNGEON_LIMIT and table[-1]["instance"] == "other"
    assert table[0] == {"instance": "instance 0", "runs": 2, "deaths": 1, "average": 600, "best": 600}


def test_skill_charts_keep_the_most_shared_professions():
    days = [{"day": date(2026, 10, 1), "professions": [{"name": f"p{i}", "rank": 1} for i in range(20)]}]
    shared = [{"day": date(2026, 10, 1), "professions": [{"name": "p19", "rank": 5}]}]

    assert "p19" in stats.skills({1: days, 2: shared}) and len(stats.skills({1: days})) == stats.SKILL_CHARTS
