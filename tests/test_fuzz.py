"""Generated exports, hostile and often still parseable: the parser refuses what the contract doesn't allow,
and whatever it takes is stored and every page renders it, escaped.

    PINKAS_FUZZ=deep uv run pytest tests/test_fuzz.py      a longer search
"""

import os
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

import psycopg2
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pinkas.ingest import EARLIEST, LATEST, ingest
from pinkas.parse import LARGEST, TEXT_LIMIT, ParseError, parse
from tests.conftest import TABLES
from tests.files import KEY, account_v3, with_export
from tests.web_client import XSS

settings.register_profile("default", max_examples=60, deadline=None)
settings.register_profile("deep", max_examples=3000, deadline=None)
settings.load_profile(os.environ.get("PINKAS_FUZZ", "default"))

# `texts` start with it, so one that reaches an error message shows.
MARK = "§"
NOW = int(datetime.now(UTC).timestamp())
DAY = 86400
# Trade goods the routes know, so the stockpiles and the Sankey get material.
MAT_IDS = (2318, 2589, 2770, 2447, 10940)

# Values the contract refuses, and ones it takes that the pages must survive.
hostile = st.one_of(
    st.sampled_from((LARGEST + 1, -(2**31) - 1, 2**60, 1e308, -1e308, "x" * TEXT_LIMIT, True)),
    st.integers(-(2**60), 2**60),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=12),
    st.none(),
    st.lists(st.integers(), max_size=2),
    st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
)
ints = st.one_of(st.sampled_from((0, 1, -1, LARGEST, -LARGEST)), st.integers(-LARGEST, LARGEST))
int32s = st.one_of(st.sampled_from((0, 2**31 - 1, -(2**31))), st.integers(-(2**31), 2**31 - 1))
counts = st.one_of(st.sampled_from((0, 1, LARGEST)), st.integers(0, 50), st.integers(0, LARGEST))
nums = st.one_of(
    ints, st.floats(-LARGEST, LARGEST, allow_nan=False), st.sampled_from((5e-324, -5e-324, 0.5, 1e-300))
)
levels = st.one_of(st.integers(-2, 70), ints)
texts = st.one_of(st.sampled_from((XSS, "²", "Zoë", "", "x" * (TEXT_LIMIT - 1))), st.text(max_size=12)).map(
    lambda t: MARK + t
)


def mostly(valid, other=hostile):
    """`other` when a drawn 0–99 is 0, else `valid`: rare enough that most files still parse."""
    return st.integers(0, 99).flatmap(lambda n: other if n == 0 else valid)


def maybe(strategy):
    return st.one_of(st.none(), strategy)


# The last 400 days, which /stats shows, or the edges of the years ingest takes.
times = mostly(st.integers(NOW - 400 * DAY, NOW + DAY), st.sampled_from((EARLIEST - 1, EARLIEST, LATEST - 1, LATEST)))


def value(**fields):
    """Some of the fields, as the addon leaves out what it doesn't know, and sometimes one it never writes. A
    field may be null: the addon writes NaN and inf as null."""
    return st.fixed_dictionaries({}, optional={**{k: mostly(maybe(v)) for k, v in fields.items()}, MARK + "extra": hostile})


mailed = st.lists(mostly(value(id=st.one_of(st.sampled_from(MAT_IDS), ints), name=texts, count=counts)), max_size=3)
EVENTS_BY_KIND = {
    "level": value(level=levels, played_level=ints, zone=texts),
    "death": value(level=levels, zone=texts, killer=st.one_of(texts, st.just("player"))),
    "quest": value(quest=ints, xp=counts),
    "mail": value(to=st.one_of(st.just(KEY), texts), items=mailed),
    "dungeon": value(instance=texts, deaths=counts, duration=nums, ended_by_logout=st.booleans()),
}
# Every field present, and a positive time in combat, sometimes tiny, unless `mostly` draws a hostile one.
fought = st.fixed_dictionaries({
    "damage": mostly(nums),
    "healing": mostly(nums),
    "seconds": mostly(st.one_of(st.floats(1e-320, 3600), st.sampled_from((5e-324, 1e-300, 0.5, 1, 60)))),
    "fights": mostly(counts),
})
# Kind -> (key, value).
TOTALS_BY_KIND = {
    "gold": (texts, value(**{"in": counts, "out": counts})),
    "combat": (mostly(st.sampled_from(("10", "11", "12")), st.sampled_from(("1", "60", "61", "0", "²", "007"))), fought),
    "gather": (
        st.builds("{}|{}".format, st.sampled_from(("skinning", "herbalism", "")), texts),
        value(count=counts, spots=st.lists(hostile, max_size=2)),
    ),
    "rested": (st.just(""), value(xp=counts, rested_used=counts)),
}
events = st.one_of(*(
    st.fixed_dictionaries({"id": ints, "at": times, "kind": mostly(st.just(kind)), "value": v})
    for kind, v in EVENTS_BY_KIND.items()
))
totals = st.one_of(*(
    st.fixed_dictionaries({"session": times, "kind": mostly(st.just(kind)), "key": key, "value": v})
    for kind, (key, v) in TOTALS_BY_KIND.items()
))
items = st.fixed_dictionaries({
    "id": mostly(st.one_of(st.sampled_from(MAT_IDS), int32s)),
    "name": mostly(maybe(texts)),
    "count": mostly(st.one_of(st.integers(1, 200), int32s)),
    "quality": mostly(maybe(st.integers(0, 5))),
    "class": mostly(maybe(st.sampled_from((7, 2, 4)))),
    "subclass": mostly(maybe(st.sampled_from((5, 6, 7, 9, 12, 0)))),
})
professions = st.fixed_dictionaries({
    "skill_line": mostly(maybe(st.one_of(st.sampled_from((393, 182, 186, 171, 129, 185, 356)), int32s))),
    "name": mostly(maybe(texts)),
    "rank": mostly(maybe(counts)),
    "max": mostly(maybe(st.integers(0, 300))),
})


@st.composite
def exports(draw) -> dict:
    """The beta account's one character, renamed, with generated state, bags, events and totals."""
    export = account_v3()
    char = export["chars"].pop(KEY)
    char.update(
        name=draw(texts),
        realm=draw(st.one_of(st.just("Testrealm"), texts)),
        level=draw(mostly(maybe(levels))),
        xp=draw(mostly(maybe(st.one_of(st.integers(0, 10**5), ints)))),
        money=draw(mostly(maybe(st.one_of(st.integers(0, 10**9), ints)))),
        zone=draw(mostly(maybe(texts))),
        logout_at=draw(mostly(times, st.none())),
        session_started_at=draw(mostly(times, st.none())),
        professions=draw(st.lists(professions, max_size=4)),
        bags=draw(st.lists(items, max_size=6)),
        events=draw(st.lists(events, max_size=8)),
        totals=draw(st.lists(totals, max_size=10)),
    )

    # A session across two levels, which a list of random totals rarely holds: the dps lines need it.
    if draw(st.booleans()):
        session = draw(times)
        char["totals"] += [{"session": session, "kind": "combat", "key": key, "value": draw(fought)} for key in ("13", "14")]

    export["chars"][f"{char['realm']}/{char['name']}"] = char

    return export


@given(exports())
def test_the_parser_refuses_without_naming_what_it_read(export):
    try:
        parse(with_export(export))
    except ParseError as exc:
        assert MARK not in str(exc)


def stores(conn, body: bytes) -> bool:
    """Whether ingest takes the file; a value the parser, the driver or Postgres refuses fails it, as in the pull."""
    try:
        ingest(conn, body, datetime.now(UTC))
    except (ValueError, psycopg2.DataError):
        conn.rollback()

        return False

    return True


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(st.lists(exports(), min_size=1, max_size=3))
def test_whatever_is_stored_renders_on_every_page(logged_in, conn, database, exported):
    with database.cursor() as cur:
        cur.execute(f"TRUNCATE {TABLES} RESTART IDENTITY")

    # The ids restart, and the /stats cache takes an unchanged newest file id as unchanged data.
    logged_in.server.stats_cache.clear()
    stored = [e["chars"] for e in exported if stores(conn, with_export(e))]
    pages = ["/", "/stats", "/stats?range=all", "/stats?range=30", "/settings"]

    # Ingest skips a character that never logged out with the addon: it has no page yet.
    for chars in stored:
        for char in (c for c in chars.values() if c["logout_at"] is not None):
            pages.append(f"/c/{quote(char['realm'], safe='')}/{quote(char['name'], safe='')}")
            pages.append("/search?" + urlencode({"q": char["name"][1:4] or "x"}))

    for page in pages:
        response = logged_in.get(page)

        assert response.status == 200, page
        assert XSS not in response.body and "<img" not in response.body, page

