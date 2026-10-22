import itertools
import math
import re
from datetime import date

import pytest

from pinkas import charts


def numbers(value) -> list[float]:
    """Every number in a chart's geometry, the path strings included."""
    if isinstance(value, dict):
        return [n for v in value.values() for n in numbers(v)]
    if isinstance(value, list | tuple):
        return [n for v in value for n in numbers(v)]
    if isinstance(value, float | int) and not isinstance(value, bool):
        return [value]
    if isinstance(value, str):
        return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", value)]

    return []


def series(name: str, slot: int, points: list) -> dict:
    return {"name": name, "slot": slot, "points": points}


@pytest.mark.parametrize(("lo", "hi"), [(0, 60), (0, 0.4), (3, 3), (0, 12345)])
def test_ticks_cover_the_range_in_round_steps(lo, hi):
    ticks = charts.nice_ticks(lo, hi)

    assert ticks[0] <= lo and ticks[-1] >= hi and len(set(ticks)) == len(ticks) >= 2


@pytest.mark.parametrize(
    "points",
    [[(date(2026, 10, 1), 5)], [(date(2026, 10, 1), 0), (date(2026, 10, 2), 0)], [(3, 1.5), (3, 1.5)]],
    ids=["one-point", "all-zero", "one-x"],
)
def test_a_degenerate_line_chart_has_no_nan(points):
    kind = "date" if isinstance(points[0][0], date) else "level"
    chart = charts.line_chart([series("Fakechar", 1, points)], kind)

    assert not chart["empty"] and not any(math.isnan(n) or math.isinf(n) for n in numbers(chart))


def test_no_points_is_empty():
    assert charts.line_chart([series("Fakechar", 1, [(1, None)])], "level") == {"empty": True}


def test_a_missing_level_lifts_the_pen():
    chart = charts.line_chart([series("Fakechar", 1, [(1, 1.0), (2, 2.0), (5, 1.0)])], "level")

    assert chart["series"][0]["d"].count("M") == 2


def test_end_labels_that_would_collide_are_dropped_not_stacked():
    close = [series("A", 1, [(1, 10.0), (2, 10.0)]), series("B", 2, [(1, 10.1), (2, 10.1)]),
             series("C", 3, [(1, 0.0), (2, 0.0)])]

    labels = [s["end"]["label"] for s in charts.line_chart(close, "level")["series"]]

    assert labels.count(None) == 1 and "C" in labels


def test_the_readout_carries_a_series_between_its_days_but_not_past_its_last():
    days = [date(2026, 10, d) for d in (1, 2, 3)]
    chart = charts.line_chart(
        [series("Fakechar", 1, [(days[0], 1.0), (days[2], 3.0)]), series("Zoë", 2, [(days[1], 2.0)])], "date"
    )

    readout = {row["label"]: [v["name"] for v in row["values"]] for row in chart["hover"]}

    assert readout == {"1 Oct": ["Fakechar"], "2 Oct": ["Fakechar", "Zoë"], "3 Oct": ["Fakechar"]}


def test_over_levels_the_readout_shows_measured_values_only():
    chart = charts.line_chart([series("Fakechar", 1, [(1, 1.0), (3, 3.0)]), series("Zoë", 2, [(2, 2.0)])], "level")

    assert [[v["name"] for v in row["values"]] for row in chart["hover"]] == [["Fakechar"], ["Zoë"], ["Fakechar"]]


def test_sankey_columns_carry_the_same_items_and_labels_never_overlap():
    # Two small senders side by side: drawn to scale they would sit a few pixels apart.
    left = [(("Fakechar", "Cloth"), 500), (("Zoë", "Cloth"), 3), (("Newchar", "Leather"), 2)]
    right = [(("Cloth", "Altchar"), 503), (("Leather", "Fakechar"), 2)]

    chart = charts.sankey(left, right, {"Fakechar": 1, "Zoë": 2, "Altchar": 3, "Newchar": 4})

    for col in range(3):
        nodes = sorted((n for n in chart["nodes"] if n["col"] == col), key=lambda n: n["y"])

        assert sum(n["value"] for n in nodes) == 505
        assert all(b["y"] - a["y"] >= charts.LABEL_MIN for a, b in itertools.pairwise(nodes))

    assert not any(math.isnan(n) for n in numbers(chart))
    assert {link["slot"] for link in chart["links"]} == {1, 2, 3, 4}, "bands take the sender's, then the recipient's colour"


def test_a_sankey_without_mail_is_empty():
    assert charts.sankey([], [], {}) == {"empty": True}


def test_a_calendar_strip_ends_with_the_given_day_and_steps_by_hours():
    end = date(2026, 10, 22)
    strip = charts.calendar_strip({end: 3.5, date(2026, 10, 1): 0.2}, end, weeks=4)

    by_day = {c["title"].split(" · ")[0]: c["step"] for c in strip["cells"]}

    assert by_day["Thu 22 Oct"] == 4 and by_day["Thu 1 Oct"] == 1 and "Fri 23 Oct" not in by_day
    assert strip["total"] == 3.7 and len(strip["cells"]) == 3 * 7 + 4


def test_a_strip_counts_only_the_days_it_draws_and_its_months_span_every_week():
    end = date(2026, 10, 22)
    strip = charts.calendar_strip({end: 2.0, date(2025, 1, 1): 9.0}, end, weeks=53, labels=False)

    assert strip["total"] == 2.0
    assert sum(s["weeks"] for s in strip["spans"]) == 53 and strip["spans"][-1]["label"] == "Oct"
    assert strip["cells"][0]["x"] == 0, "no room for labels: they are HTML beside the stretched strip"


@pytest.mark.parametrize(("hours", "step"), [(0, 0), (0.1, 1), (1, 2), (2.5, 3), (3.9, 4), (9, 5)])
def test_hour_steps(hours, step):
    assert charts.hour_step(hours) == step


@pytest.mark.parametrize(("value", "step"), [(0, 0), (1, 1), (50, 3), (100, 5)])
def test_heat_steps_are_fifths_of_the_largest(value, step):
    assert charts.heat_step(value, 100) == step


def test_a_one_value_sparkline_is_a_flat_line():
    spark = charts.spark([7])

    assert spark["points"].count(",") == 2 and spark["end"][0] == spark["width"]


def test_a_level_axis_labels_whole_levels_only():
    chart = charts.line_chart([series("a", 1, [(10, 1.0), (11, 2.0), (12, 3.0)])], "level")

    assert [t["label"] for t in chart["x_ticks"]] == ["10", "11", "12"]


def test_a_tiny_span_still_gets_ticks():
    assert charts.nice_ticks(0, 1e-323)[-1] >= 1


def test_an_empty_series_beside_a_drawn_one_is_skipped():
    chart = charts.line_chart([series("a", 1, [(date(2026, 10, 1), 1.0), (date(2026, 10, 2), 2.0)]), series("b", 2, [])], "date")

    assert [s["name"] for s in chart["series"]] == ["a"]
