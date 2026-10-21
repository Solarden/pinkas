"""Rested XP built up since a logout, at the Classic rates the Forever beta confirmed overnight."""

from datetime import datetime

MAX_LEVEL = 60
# Resting (an inn or a city) builds 5% of the level's XP bar every 8 hours; anywhere else, a camp
# included, a quarter of that.
BAR_SHARE_PER_PERIOD = 0.05
PERIOD_SECONDS = 8 * 3600
NOT_RESTING_SHARE = 0.25
CAP_BARS = 1.5
# The Legacy perk Well Rested raises both the rate and the cap by 4% a rank.
WELL_RESTED_PER_RANK = 0.04


def rate_and_cap(xp_max: int, resting: bool, well_rested_rank: int = 0) -> tuple[float, float]:
    """Rested XP gained per second, and the most there can be."""
    bonus = 1 + WELL_RESTED_PER_RANK * well_rested_rank
    share = 1 if resting else NOT_RESTING_SHARE

    return xp_max * BAR_SHARE_PER_PERIOD * share * bonus / PERIOD_SECONDS, CAP_BARS * xp_max * bonus


def rested_now(
    level: int | None,
    xp_max: int | None,
    rested: int | None,
    resting: bool,
    logout_at: datetime,
    now: datetime,
    well_rested_rank: int = 0,
) -> int | None:
    """None when the level or its bar is unknown; the game reports no rested as nil, read here as 0."""
    if level is None:
        return None
    if level >= MAX_LEVEL:
        return 0
    if not xp_max:
        return None
    per_second, cap = rate_and_cap(xp_max, resting, well_rested_rank)
    seconds = max(0.0, (now - logout_at).total_seconds())

    return int(min(cap, (rested or 0) + per_second * seconds))


def full_in(
    level: int | None, xp_max: int | None, rested: int | None, resting: bool, well_rested_rank: int = 0
) -> float | None:
    """Seconds until `rested` (as of now) reaches the cap; None at the max level or when unknown."""
    if level is None or level >= MAX_LEVEL or not xp_max or rested is None:
        return None
    per_second, cap = rate_and_cap(xp_max, resting, well_rested_rank)

    # int(): rested_now() truncates, so a character at a fractional cap would never read as full.
    return max(0.0, (int(cap) - rested) / per_second)
