"""Chart geometry for /stats: coordinates, paths and tick labels as numbers and plain strings.

No markup is built here. The templates draw the SVG from these values and escape every label, so a
name from the game never reaches the page unescaped.
"""

import math
from datetime import date, timedelta

# One viewBox for every line chart; at 375 px its box scrolls instead of shrinking the text.
WIDTH, HEIGHT = 640, 260
LEFT, RIGHT, TOP, BOTTOM = 48, 104, 14, 30
END_LABEL_GAP = 15
# A Sankey node is placed at least this tall, so neighbouring labels never overlap.
LABEL_MIN = 16
CELL, CELL_GAP = 11, 2
# Hours a day per calendar step: none, under 1, 1–2, 2–3, 3–4, 4 or more.
HOUR_STEPS = (1, 2, 3, 4)
HEAT_STEPS = 5


def nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    # A span under 1e-9 would make the step 0.
    if hi - lo <= 1e-9:
        hi = lo + 1

    raw = (hi - lo) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    first = math.floor(lo / step) * step

    return [first + i * step for i in range(int(math.ceil((hi - first) / step)) + 1)]


def short_date(d: date) -> str:
    return f"{d.day} {d:%b}"


def number(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value / 1000:.3g}k"

    return f"{value:.3g}" if value != int(value) else str(int(value))


def line_chart(series: list[dict], x_kind: str, y_label=number) -> dict:
    """`series`: [{name, slot, points: [(x, y), …]}] with x a date or a level. Returns the plot's ticks,
    each series' path, its points (for the hover titles) and the end labels that don't collide."""
    points = [(x, y) for s in series for x, y in s["points"] if y is not None]

    if not points:
        return {"empty": True}

    def xv(x) -> float:
        return x.toordinal() if x_kind == "date" else x

    xs, ys = [xv(x) for x, _ in points], [y for _, y in points]
    x_lo, x_hi = min(xs), max(xs)
    y_ticks = nice_ticks(min(0, min(ys)), max(ys))
    y_lo, y_hi = y_ticks[0], y_ticks[-1]
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM

    def px(x) -> float:
        return round(LEFT + (xv(x) - x_lo) / ((x_hi - x_lo) or 1) * plot_w, 1)

    def x_label(x) -> str:
        return short_date(x) if x_kind == "date" else f"level {x}"

    def py(y: float) -> float:
        return round(TOP + (1 - (y - y_lo) / ((y_hi - y_lo) or 1)) * plot_h, 1)

    if x_kind == "date":
        span = x_hi - x_lo
        step = max(1, math.ceil(span / 5))
        x_ticks = [date.fromordinal(x_lo + i) for i in range(0, span + 1, step)]
        x_labels = [{"x": px(d), "label": short_date(d)} for d in x_ticks]
    else:
        # Levels are whole: a step of 0.5 would label levels that don't exist.
        ticks = [t for t in nice_ticks(x_lo, x_hi, 5) if x_lo <= t <= x_hi and t == int(t)]
        x_labels = [{"x": px(t), "label": number(t)} for t in ticks]

    drawn = []

    for s in series:
        pts = [(x, y) for x, y in s["points"] if y is not None]

        if not pts:
            continue

        path, previous = [], None

        for x, y in pts:
            # A missing level (x_kind "level") leaves a gap: the path lifts the pen.
            jump = x_kind == "level" and previous is not None and x - previous > 1
            path.append(f"{'M' if not path or jump else 'L'}{px(x)},{py(y)}")
            previous = x

        last = pts[-1]
        drawn.append({
            "name": s["name"], "slot": s["slot"], "d": " ".join(path),
            "points": [{"x": px(x), "y": py(y), "title": f"{s['name']} · {x_label(x)} · {y_label(y)}"}
                       for x, y in pts],
            "end": {"x": px(last[0]), "y": py(last[1]), "label": None},
        })

    # Direct labels only where they don't collide; the legend names every series anyway.
    taken: list[float] = []

    for s in sorted(drawn, key=lambda s: s["end"]["y"]):
        if all(abs(s["end"]["y"] - t) >= END_LABEL_GAP for t in taken):
            s["end"]["label"] = s["name"]
            taken.append(s["end"]["y"])

    # The crosshair's readout per x, for static/pinkas.js. A day without a logout changed nothing, so over
    # dates a series holds its last value up to its last point; over levels only a measured value shows.
    hover = []
    known = [(s, [(sx, sy) for sx, sy in s["points"] if sy is not None]) for s in series]

    for x in sorted({x for x, _ in points}):
        values = []

        for s, pts in known:
            before = [sy for sx, sy in pts if sx <= x]

            if not before:
                continue

            held = x_kind == "date" and x <= pts[-1][0]

            if held or any(sx == x for sx, _ in pts):
                values.append({"name": s["name"], "slot": s["slot"], "value": y_label(before[-1])})

        hover.append({"x": px(x), "label": x_label(x), "values": values})

    return {
        "empty": False, "width": WIDTH, "height": HEIGHT,
        "hover": hover,
        "plot": {"x0": LEFT, "x1": WIDTH - RIGHT, "y0": TOP, "y1": HEIGHT - BOTTOM},
        "x_ticks": x_labels, "y_ticks": [{"y": py(t), "label": number(t)} for t in y_ticks],
        "series": drawn,
    }


def hour_step(hours: float) -> int:
    if hours <= 0:
        return 0

    return 1 + sum(hours >= h for h in HOUR_STEPS)


def calendar_strip(hours: dict[date, float], end: date, weeks: int, labels: bool = True) -> dict:
    """One week per column, Monday on top, ending with `end`'s week; months labelled where they start.

    Without `labels` the SVG holds the cells only (the year view puts its labels in HTML beside them);
    `spans` gives each month's width in weeks for those labels."""
    first = end - timedelta(days=end.weekday()) - timedelta(weeks=weeks - 1)
    left, top = (28, 16) if labels else (0, 0)
    cells, months, starts, seen = [], [], [], None

    for i in range(weeks * 7):
        day = first + timedelta(days=i)

        if day > end:
            break

        col, row = i // 7, i % 7
        x, y = left + col * (CELL + CELL_GAP), top + row * (CELL + CELL_GAP)
        h = hours.get(day, 0)
        title = f"{day:%a} {short_date(day)} · {h:.1f} h"
        cells.append({"x": x, "y": y, "step": hour_step(h), "hours": h, "title": title})

        if day.day <= 7 and row == 0 and day.month != seen:
            months.append({"x": x, "label": f"{day:%b}"})
            starts.append((col, f"{day:%b}"))
            seen = day.month

    columns = math.ceil(len(cells) / 7)
    spans, start, label = [], 0, ""

    for col, month in starts:
        if col > start:
            spans.append({"label": label, "weeks": col - start})

        start, label = col, month

    spans.append({"label": label, "weeks": columns - start})

    return {
        "width": left + weeks * (CELL + CELL_GAP) - (0 if labels else CELL_GAP),
        "height": top + 7 * (CELL + CELL_GAP) - (0 if labels else CELL_GAP),
        "cells": cells, "months": months, "spans": spans,
        "days": [{"y": top + r * (CELL + CELL_GAP) + CELL - 2, "label": label}
                 for r, label in ((0, "Mon"), (2, "Wed"), (4, "Fri"))],
        # The drawn days only: the hours given may reach past the strip.
        "total": round(sum(c["hours"] for c in cells), 1),
    }


def sankey(left: list[tuple], right: list[tuple], slot_of: dict[str, int], height: int = 300) -> dict:
    """Three columns, sender → material → recipient. `left` holds ((sender, material), items), `right`
    ((material, recipient), items); bands are stacked on each node in size order."""
    total = sum(n for _, n in left)

    if not total:
        return {"empty": True}

    node_w, gap, x = 12, 8, (120, 314, 508)

    def column(pairs: list[tuple], index: int) -> dict[str, float]:
        sums: dict[str, float] = {}

        for key, n in pairs:
            sums[key[index]] = sums.get(key[index], 0) + n

        return dict(sorted(sums.items(), key=lambda kv: -kv[1]))

    columns = [column(left, 0), column(left, 1), column(right, 1)]
    scale = min((height - gap * (len(c) - 1)) / total for c in columns)
    nodes: dict[tuple[int, str], dict] = {}
    bottom = 0.0

    for i, col in enumerate(columns):
        y = 0.0

        for name, n in col.items():
            nodes[(i, name)] = {"col": i, "x": x[i], "y": round(y, 1), "h": round(max(n * scale, 1), 1),
                                "label": name, "value": n, "slot": slot_of.get(name, 0) if i != 1 else None,
                                "out": y, "in": y}
            y += max(n * scale, LABEL_MIN) + gap

        bottom = max(bottom, y - gap)

    links = []

    for col, pairs in ((0, left), (1, right)):
        for (a, b), n in pairs:
            src, dst, h = nodes[(col, a)], nodes[(col + 1, b)], n * scale
            x0, x1 = src["x"] + node_w, dst["x"]
            y0, y1 = src["out"], dst["in"]
            mid = (x0 + x1) / 2
            links.append({
                "d": f"M{x0},{y0:.1f} C{mid},{y0:.1f} {mid},{y1:.1f} {x1},{y1:.1f} L{x1},{y1 + h:.1f}"
                     f" C{mid},{y1 + h:.1f} {mid},{y0 + h:.1f} {x0},{y0 + h:.1f} Z",
                "slot": slot_of.get(a if col == 0 else b, 0), "from": a, "to": b, "value": n,
            })
            src["out"] += h
            dst["in"] += h

    return {
        "empty": False, "width": WIDTH, "height": round(bottom), "node_w": node_w,
        "nodes": [{k: v for k, v in n.items() if k not in ("out", "in")} for n in nodes.values()], "links": links,
    }


def heat_step(value: float, top: float) -> int:
    return 0 if not value or not top else max(1, math.ceil(HEAT_STEPS * value / top))


def spark(values: list[float], width: int = 120, height: int = 28) -> dict:
    """A sparkline's polyline, no axes; one value draws a flat line."""
    if not values:
        return {"empty": True}

    lo, hi = min(values), max(values)
    step = width / max(len(values) - 1, 1)
    points = [(round(i * step, 1), round(height - 2 - (v - lo) / ((hi - lo) or 1) * (height - 4), 1))
              for i, v in enumerate(values)]

    if len(points) == 1:
        points.append((width, points[0][1]))

    return {"empty": False, "width": width, "height": height,
            "points": " ".join(f"{x},{y}" for x, y in points), "end": points[-1]}
