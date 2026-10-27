from datetime import UTC, datetime, timedelta

import pytest

from pinkas.rested import full_in, rested_now

LOGOUT = datetime(2026, 10, 21, 2, 30, tzinfo=UTC)


def later(hours: float) -> datetime:
    return LOGOUT + timedelta(hours=hours)


@pytest.mark.parametrize(
    ("hours", "rested", "resting", "rank", "expected"),
    [
        (0, 100, True, 0, 100),
        (8, 0, True, 0, 380),
        (8, 0, False, 0, 95),
        (8, 0, True, 5, 456),
        (400, 0, True, 0, 11400),
        (400, 0, True, 5, 13680),
        (8, None, True, 0, 380),
    ],
    ids=["no time", "8 h at an inn", "8 h outside", "Well Rested 5", "the cap", "the raised cap", "nil rested"],
)
def test_rested_at_level_ten(hours, rested, resting, rank, expected):
    assert rested_now(10, 7600, rested, resting, LOGOUT, later(hours), rank) == expected


# Measured in the Forever beta on the night of 2026-10-21. The game grows
# rested in whole ticks, so the formula may land a point or two above the measurement.
@pytest.mark.parametrize(
    ("xp_max", "start", "resting", "hours", "measured"),
    [(400, 0, True, 7.72, 18), (900, 0, False, 8.13, 10)],
    ids=["inn", "open field"],
)
def test_beta_overnight(xp_max, start, resting, hours, measured):
    estimate = rested_now(2, xp_max, start, resting, LOGOUT, later(hours))

    assert measured <= estimate <= measured + 2


def test_max_level_has_no_rested():
    assert rested_now(60, 0, None, True, LOGOUT, later(8)) == 0


@pytest.mark.parametrize(("level", "xp_max"), [(10, None), (None, 7600)], ids=["bar", "level"])
def test_unknown_is_unknown_not_zero(level, xp_max):
    assert rested_now(level, xp_max, 100, True, LOGOUT, later(8)) is None


def test_a_clock_behind_the_logout_adds_nothing():
    assert rested_now(10, 7600, 100, True, LOGOUT, later(-1)) == 100


@pytest.mark.parametrize(
    ("level", "xp_max", "rested", "resting", "rank", "hours"),
    [
        (10, 7600, 0, True, 0, 240),
        (10, 7600, 5700, True, 0, 120),
        (10, 7600, 11400, True, 0, 0),
        (10, 7600, 0, False, 0, 960),
        (10, 7600, 0, True, 5, 240),
        (10, 7601, 11857, True, 1, 0),
    ],
    ids=["empty at an inn", "half", "at the cap", "empty outside", "Well Rested 5", "at a fractional cap"],
)
def test_full_in(level, xp_max, rested, resting, rank, hours):
    assert full_in(level, xp_max, rested, resting, rank) == pytest.approx(hours * 3600)


@pytest.mark.parametrize(
    ("level", "xp_max", "rested"), [(60, 0, 0), (None, 7600, 0), (10, None, 0), (10, 7600, None)],
    ids=["max level", "unknown level", "unknown bar", "unknown rested"],
)
def test_full_in_has_no_answer(level, xp_max, rested):
    assert full_in(level, xp_max, rested, True) is None
