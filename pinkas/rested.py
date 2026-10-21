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
    bonus = 1 + WELL_RESTED_PER_RANK * well_rested_rank
    share = 1 if resting else NOT_RESTING_SHARE
    per_second = xp_max * BAR_SHARE_PER_PERIOD * share * bonus / PERIOD_SECONDS
    seconds = max(0.0, (now - logout_at).total_seconds())

    return int(min(CAP_BARS * xp_max * bonus, (rested or 0) + per_second * seconds))
