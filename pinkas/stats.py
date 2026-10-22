"""What /stats charts: events, session totals and the snapshots' generated columns, shaped per chart.

Every query is bounded by the range and reads small values: snapshot history comes from the generated
columns of migration 0003, never from `data`. Names in the result come from the game; the templates
escape them.
"""

import math
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from pinkas import charts
from pinkas.db import fetch
from pinkas.parse import KIND_LABELS, KIND_OF, ROUTE_KINDS

RANGES = {"30": 30, "90": 90, "365": 365, "all": None}
RANGE_LABELS = (("30", "30 days"), ("90", "90 days"), ("365", "365 days"), ("all", "all"))
SPARK_DAYS = 30
DEFAULT_RANGE = "90"
MAX_LEVEL = 60
# Categorical slots, by character id: a colour follows its character. Past these, grey "other" (0).
SLOTS = 8
TOP = 10
DUNGEON_LIMIT = 30
# Classic's nine primary and three secondary professions.
SKILL_CHARTS = 12
PACE_DAYS = 14
# Daily points up to a year; a longer range is drawn by the week.
DAILY_UP_TO = 365
CALENDAR_WEEKS = 53
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

CHARACTERS_SQL = "SELECT id, realm, name, role FROM pinkas_characters ORDER BY id"
# Both ends come from the file and calendar() steps through every day between them: clamped to the strip.
SESSIONS_SQL = """
SELECT character_id, greatest(session_started_at, %(since)s) AS started, least(max(logout_at), %(now)s) AS ended
FROM pinkas_snapshots WHERE logout_at >= %(since)s AND session_started_at IS NOT NULL
GROUP BY character_id, session_started_at
"""
DAILY_SQL = """
SELECT character_id, logout_at, level, xp, xp_max, money, professions, mats FROM pinkas_snapshots
WHERE logout_at >= %(since)s ORDER BY logout_at
"""
# The sparklines on a character's page; walks the (character_id, logout_at) index.
CHARACTER_DAILY_SQL = """
SELECT character_id, logout_at, level, xp, xp_max, money FROM pinkas_snapshots
WHERE character_id = %(character)s AND logout_at >= %(since)s ORDER BY logout_at
"""
EVENTS_SQL = "SELECT character_id, at, value FROM pinkas_events WHERE kind = %(kind)s AND at >= %(since)s ORDER BY at"
# Without a gather total's spots (up to 50 positions each): no chart reads them.
TOTALS_SQL = """
SELECT character_id, key, value - 'spots' AS value FROM pinkas_session_totals
WHERE kind = %(kind)s AND session_started_at >= %(since)s
"""


def amount(value: dict, field: str) -> float:
    """A total's number; the addon writes NaN and inf as null."""
    return value.get(field) or 0


def a_level(value) -> bool:
    return isinstance(value, int) and 1 <= value <= MAX_LEVEL


def range_key(value: str | None) -> str:
    return value if value in RANGES else DEFAULT_RANGE


def since(key: str, now: datetime) -> datetime:
    days = RANGES[key]

    return now - timedelta(days=days) if days else EPOCH


def characters(cur) -> dict[int, dict]:
    """Every character, archived too (history stays), with its colour slot by id and the label charts show:
    its name, with the realm when another realm has a character of the same name."""
    rows = fetch(cur, CHARACTERS_SQL)
    names = [r["name"] for r in rows]

    return {
        r["id"]: {**r, "key": f"{r['realm']}/{r['name']}", "slot": i + 1 if i < SLOTS else 0,
                  "label": r["name"] if names.count(r["name"]) == 1 else f"{r['name']} ({r['realm']})"}
        for i, r in enumerate(rows)
    }


# `tz` None is the server's own zone, DST included, as the page's other times (`moment.astimezone()`).
def bucket(moment: datetime, tz: tzinfo | None, weekly: bool) -> date:
    day = moment.astimezone(tz).date()

    return day - timedelta(days=day.weekday()) if weekly else day


def midnight_after(moment: datetime, tz: tzinfo | None) -> datetime:
    naive = datetime.combine(moment.date() + timedelta(days=1), time())

    return naive.astimezone() if tz is None else naive.replace(tzinfo=tz)


def calendar(sessions: list[dict], tz: tzinfo | None) -> dict[int, dict[date, float]]:
    """Hours played per character per local day. A session is one span from its start to its latest
    snapshot (a reload adds a snapshot, not a session), split at midnight."""
    hours: dict[int, dict[date, float]] = defaultdict(lambda: defaultdict(float))

    for s in sessions:
        start, end = s["started"].astimezone(tz), s["ended"]

        while start < end:
            cut = min(end, midnight_after(start, tz))
            # Timestamps: two times in one zone subtract by the wall clock, an hour off across a DST change.
            hours[s["character_id"]][start.date()] += (cut.timestamp() - start.timestamp()) / 3600
            start = cut.astimezone(tz)

    return {cid: dict(days) for cid, days in hours.items()}


def daily(rows: list[dict], tz: tzinfo | None, weekly: bool) -> dict[int, list[dict]]:
    """Each character's last snapshot per local day (or week), oldest first."""
    last: dict[int, dict[date, dict]] = defaultdict(dict)

    for r in rows:
        last[r["character_id"]][bucket(r["logout_at"], tz, weekly)] = r

    return {cid: [{**r, "day": d} for d, r in sorted(days.items())] for cid, days in last.items()}


def level_of(row: dict) -> float | None:
    """The level with the bar's share, so a day's progress shows between level-ups."""
    if row["level"] is None:
        return None
    if row["level"] >= MAX_LEVEL or not row["xp_max"] or row["xp"] is None:
        return float(row["level"])

    return row["level"] + min(row["xp"] / row["xp_max"], 0.999)


def days_to_max(points: list[dict], today: date) -> int | None:
    """Days to level 60 at the last PACE_DAYS days' pace; None without progress or at 60."""
    known = [(p["day"], level_of(p)) for p in points if level_of(p) is not None]

    if not known or known[-1][1] >= MAX_LEVEL:
        return None

    window = [k for k in known if k[0] >= today - timedelta(days=PACE_DAYS)]

    if len(window) < 2 or window[-1][1] <= window[0][1]:
        return None

    pace = (window[-1][1] - window[0][1]) / max((window[-1][0] - window[0][0]).days, 1)

    return round((MAX_LEVEL - known[-1][1]) / pace)


def carried(per_character: dict[int, list[tuple[date, float]]]) -> list[tuple[date, float]]:
    """The account's sum by day, each character counting its last known value from its first point on."""
    # shortcut: a character first seen mid-range adds its whole value that day, inflating the range's change;
    # seed it from its last earlier snapshot if that misleads.
    days = sorted({d for points in per_character.values() for d, _ in points})
    known: dict[int, float] = {}
    by_day = {cid: dict(points) for cid, points in per_character.items()}
    total = []

    for d in days:
        for cid, values in by_day.items():
            if d in values:
                known[cid] = values[d]

        total.append((d, sum(known.values())))

    return total


def time_per_level(levels: list[dict]) -> dict[int, dict[int, float]]:
    """Hours spent on each level, per character. A level event's played_level is the time on the level
    before it; null (no start seen) is left out, a gap rather than 0."""
    hours: dict[int, dict[int, float]] = defaultdict(dict)

    for e in levels:
        v = e["value"]

        if (v.get("played_level") or 0) > 0 and a_level(v.get("level")):
            hours[e["character_id"]][v["level"] - 1] = v["played_level"] / 3600

    return dict(hours)


def ranked(counts: dict[str, float], top: int = TOP) -> list[tuple[str, float]]:
    """Largest first; past `top`, the rest as one "other" row."""
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    if len(rows) > top:
        rows = rows[: top - 1] + [("other", sum(n for _, n in rows[top - 1 :]))]

    return rows


def deaths(events: list[dict]) -> dict:
    zones: dict[str, int] = defaultdict(int)
    killers: dict[str, int] = defaultdict(int)

    for e in events:
        zones[e["value"].get("zone") or "unknown"] += 1
        killers[e["value"].get("killer") or "unknown"] += 1

    return {"zones": ranked(zones), "killers": ranked(killers), "count": len(events)}


def largest(cells: dict[tuple[str, str], float], axis: int) -> list[str]:
    sums: dict[str, float] = defaultdict(float)

    for k, n in cells.items():
        sums[k[axis]] += n

    return [name for name, _ in sorted(sums.items(), key=lambda kv: (kv[0] == "other", -kv[1], kv[0]))]


def folded(cells: dict[tuple[str, str], float], axis: int) -> dict[tuple[str, str], float]:
    """Past `TOP` names on one axis, the rest as "other": the grid is drawn whole, a cell per pair."""
    names = largest(cells, axis)

    if len(names) <= TOP:
        return cells

    kept = set(names[: TOP - 1])
    out: dict[tuple[str, str], float] = defaultdict(float)

    for key, n in cells.items():
        name = key[axis] if key[axis] in kept else "other"
        out[(name, key[1]) if axis == 0 else (key[0], name)] += n

    return out


def gathering(totals: list[dict]) -> dict:
    """Counts by zone × profession, both largest first. The key is "profession|zone"."""
    cells: dict[tuple[str, str], float] = defaultdict(float)

    for t in totals:
        profession, _, zone = t["key"].partition("|")
        cells[(zone or "unknown", profession or "unknown")] += amount(t["value"], "count")

    cells = folded(folded(cells, 0), 1)

    return {"zones": largest(cells, 0), "professions": largest(cells, 1), "cells": dict(cells),
            "top": max(cells.values(), default=0)}


def material(item_id: int, item_classes: dict[int, tuple[int, int]]) -> str:
    kind = KIND_OF.get(item_classes.get(item_id, (None, None)))

    return KIND_LABELS[kind] if kind else "Other"


def flows(mails: list[dict], chars: dict[int, dict], item_classes: dict[int, tuple[int, int]]) -> dict:
    """The Sankey's links: sender → material and material → recipient, in items.

    A recipient that isn't one of the account's characters is "other", so the page never names a stranger.
    """
    by_key = {c["key"]: c for c in chars.values()}
    left: dict[tuple, int] = defaultdict(int)
    right: dict[tuple, int] = defaultdict(int)

    for e in mails:
        sender = chars.get(e["character_id"])
        to = by_key.get(e["value"].get("to"))
        source = sender["label"] if sender else "other"
        target = to["label"] if to else "other"

        for item in e["value"].get("items") or []:
            kind, count = material(item.get("id"), item_classes), item.get("count") or 0
            left[(source, kind)] += count
            right[(kind, target)] += count

    return {
        "left": sorted(left.items(), key=lambda kv: -kv[1]),
        "right": sorted(right.items(), key=lambda kv: -kv[1]),
        "total": sum(left.values()),
    }


def gold_sources(totals: list[dict]) -> list[dict]:
    sums: dict[str, dict[str, int]] = defaultdict(lambda: {"in": 0, "out": 0})

    for t in totals:
        for way in ("in", "out"):
            sums[t["key"]][way] += amount(t["value"], way)

    rows = sorted(({"source": k, **v} for k, v in sums.items()), key=lambda s: -(s["in"] + s["out"]))

    if len(rows) > TOP:
        rest = rows[TOP - 1 :]
        rows = rows[: TOP - 1] + [{"source": "other", "in": sum(r["in"] for r in rest), "out": sum(r["out"] for r in rest)}]

    return rows


def dungeons(runs: list[dict]) -> list[dict]:
    """Per instance: runs, deaths, and average and best time over runs that ended inside (a run closed
    by a logout lasted until the logout)."""
    table: dict[str, dict] = {}

    for e in runs:
        v = e["value"]
        row = table.setdefault(v.get("instance") or "unknown", {"runs": 0, "deaths": 0, "times": []})
        row["runs"] += 1
        row["deaths"] += v.get("deaths") or 0

        if not v.get("ended_by_logout") and (v.get("duration") or -1) >= 0:
            row["times"].append(v["duration"])

    if len(table) > DUNGEON_LIMIT:
        kept = sorted(table, key=lambda name: (-table[name]["runs"], name))[: DUNGEON_LIMIT - 1]
        other = {"runs": 0, "deaths": 0, "times": []}

        for name in [n for n in table if n not in kept]:
            row = table.pop(name)
            other["runs"] += row["runs"]
            other["deaths"] += row["deaths"]
            other["times"] += row["times"]

        table["other"] = other

    return [
        {"instance": name, "runs": r["runs"], "deaths": r["deaths"],
         "average": sum(r["times"]) / len(r["times"]) if r["times"] else None, "best": min(r["times"], default=None)}
        for name, r in sorted(table.items(), key=lambda kv: (kv[0] == "other", -kv[1]["runs"], kv[0]))
    ]


def combat(totals: list[dict], death_events: list[dict], hours: dict[int, dict[int, float]]) -> dict:
    """Per character and level: damage and healing per second of combat, deaths per hour played on that
    level; and per character, fights per death."""
    sums: dict[tuple[int, int], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    fights: dict[int, float] = defaultdict(float)
    died: dict[tuple[int, int], int] = defaultdict(int)
    died_total: dict[int, int] = defaultdict(int)

    for t in totals:
        # The key is the level as text. str.isdigit() alone takes "²", which int() refuses, and the
        # length keeps int() off a key of thousands of digits.
        level = int(t["key"]) if t["key"].isascii() and t["key"].isdigit() and len(t["key"]) <= 2 else None

        if not a_level(level):
            continue

        for field in ("damage", "healing", "seconds"):
            sums[(t["character_id"], level)][field] += amount(t["value"], field)

        fights[t["character_id"]] += amount(t["value"], "fights")

    for e in death_events:
        died_total[e["character_id"]] += 1

        if a_level(e["value"].get("level")):
            died[(e["character_id"], e["value"]["level"])] += 1

    per_level: dict[int, dict[int, dict]] = defaultdict(dict)

    # Under a second of combat on a level says nothing, and a tiny divisor would overflow the chart.
    for (cid, level), s in sums.items():
        if s["seconds"] >= 1:
            per_level[cid][level] = {"dps": s["damage"] / s["seconds"], "hps": s["healing"] / s["seconds"]}

    for cid, levels in hours.items():
        for level, h in levels.items():
            if h > 0:
                per_level[cid].setdefault(level, {})["deaths_per_hour"] = died[(cid, level)] / h

    return {
        "levels": {cid: dict(sorted(v.items())) for cid, v in per_level.items()},
        "fights_per_death": {cid: (fights[cid] / died_total[cid] if died_total[cid] else None) for cid in fights},
    }


def rested_share(totals: list[dict]) -> dict[int, float | None]:
    xp: dict[int, float] = defaultdict(float)
    used: dict[int, float] = defaultdict(float)

    for t in totals:
        xp[t["character_id"]] += amount(t["value"], "xp")
        used[t["character_id"]] += amount(t["value"], "rested_used")

    return {cid: (used[cid] / xp[cid] if xp[cid] else None) for cid in xp}


def skills(points: dict[int, list[dict]]) -> dict[str, dict[int, list[tuple[date, int]]]]:
    """Each profession's rank over time, per character that has it."""
    out: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))

    for cid, days in points.items():
        for p in days:
            for prof in p["professions"] or []:
                name = prof.get("name") or f"profession {prof.get('skill_line')}"

                if prof.get("rank") is not None:
                    out[name][cid].append((p["day"], prof["rank"]))

    shown = sorted(out, key=lambda name: (-len(out[name]), name))[:SKILL_CHARTS]

    return {name: dict(out[name]) for name in sorted(shown)}


def stockpiles(points: dict[int, list[dict]]) -> dict[str, list[tuple[date, float]]]:
    """The account's count of each route kind's materials (bags and bank), by day."""
    per_kind: dict[str, dict[int, list]] = {KIND_LABELS[k]: defaultdict(list) for k in ROUTE_KINDS}

    for cid, days in points.items():
        for p in days:
            counts = dict.fromkeys(per_kind, 0)

            for m in p["mats"] or []:
                kind = KIND_OF.get((m.get("class"), m.get("subclass")))

                if kind:
                    counts[KIND_LABELS[kind]] += m.get("count") or 0

            for label, n in counts.items():
                per_kind[label][cid].append((p["day"], n))

    return {label: carried(v) for label, v in per_kind.items() if any(n for pts in v.values() for _, n in pts)}


def collect(cur, key: str, now: datetime, tz: tzinfo | None = None) -> dict:
    """Everything /stats shows for one range, in one read."""
    args = {"since": since(key, now)}
    chars = characters(cur)

    def events(kind: str) -> list[dict]:
        return fetch(cur, EVENTS_SQL, {**args, "kind": kind})

    def totals(kind: str) -> list[dict]:
        return fetch(cur, TOTALS_SQL, {**args, "kind": kind})

    rows = fetch(cur, DAILY_SQL, args)
    weekly = bool(rows) and (now - rows[0]["logout_at"]).days > DAILY_UP_TO
    points = daily(rows, tz, weekly)
    # The pace needs real days: a weekly point is dated to its Monday.
    by_day = daily(rows, tz, False) if weekly else points
    # The account's strip always shows CALENDAR_WEEKS; a character's strip shows the range within them.
    # shortcut: no calendar further back than CALENDAR_WEEKS; an older year would need paging.
    sessions = fetch(cur, SESSIONS_SQL, {"since": now - timedelta(weeks=CALENDAR_WEEKS), "now": now})
    level_hours = time_per_level(events("level"))
    death_events = events("death")
    # Mail items carry no class: a mailed item id is known from the trade goods seen in the range.
    item_classes = {m.get("id"): (m.get("class"), m.get("subclass")) for r in rows for m in r["mats"] or []}
    today = now.astimezone(tz).date()
    money = {cid: [(p["day"], p["money"]) for p in days if p["money"] is not None] for cid, days in points.items()}

    return {
        "range": key,
        "today": today,
        "characters": chars,
        "calendar": calendar(sessions, tz),
        "level": {cid: [(p["day"], level_of(p)) for p in days if p["level"] is not None] for cid, days in points.items()},
        "to_max": {cid: days_to_max(days, today) for cid, days in by_day.items()},
        "time_per_level": level_hours,
        "deaths": deaths(death_events),
        "gathering": gathering(totals("gather")),
        "flows": flows(events("mail"), chars, item_classes),
        "gold_sources": gold_sources(totals("gold")),
        "gold": carried(money),
        "dungeons": dungeons(events("dungeon")),
        "combat": combat(totals("combat"), death_events, level_hours),
        "skills": skills(points),
        "stockpiles": stockpiles(points),
        "rested": rested_share(totals("rested")),
    }


def series_table(series: list[dict], x_head: str, fmt) -> dict:
    """A line chart's table view: one row per x, one column per series."""
    xs = sorted({x for s in series for x, _ in s["points"]})
    values = [dict(s["points"]) for s in series]
    rows = []

    for x in xs:
        label = charts.short_date(x) if isinstance(x, date) else x
        rows.append([label, *(fmt(v[x]) if v.get(x) is not None else "—" for v in values)])

    return {"head": [x_head, *(s["name"] for s in series)], "rows": rows}


def line(series: list[dict], x_kind: str, x_head: str, fmt=charts.number) -> dict:
    return {**charts.line_chart(series, x_kind, fmt), "table": series_table(series, x_head, fmt),
            "legend": [{"name": s["name"], "slot": s["slot"]} for s in series if s["points"]]}


def per_character(values: dict[int, list], chars: dict[int, dict]) -> list[dict]:
    return [{"name": chars[cid]["label"], "slot": chars[cid]["slot"], "points": pts}
            for cid, pts in sorted(values.items()) if cid in chars and pts]


def grouped(value: float) -> str:
    return f"{value:,.0f}".replace(",", "\u202f")


def history(cur, character_id: int, now: datetime, tz: tzinfo | None = None) -> dict:
    """Level and gold over the last SPARK_DAYS days, a point per day, for the character page."""
    rows = fetch(cur, CHARACTER_DAILY_SQL, {"character": character_id, "since": now - timedelta(days=SPARK_DAYS)})
    points = daily(rows, tz, False).get(character_id, [])
    levels = [level_of(p) for p in points if p["level"] is not None]
    golds = [p["money"] / 10000 for p in points if p["money"] is not None]
    level = {"spark": charts.spark(levels), "now": "", "change": ""}
    gold = {"spark": charts.spark(golds), "now": "", "change": ""}

    if levels:
        level["now"] = f"level {levels[-1]:.1f}"
        level["change"] = f"{levels[-1] - levels[0]:+.1f} in {SPARK_DAYS} d"

    if golds:
        sign = "+" if golds[-1] >= golds[0] else "−"
        gold["now"] = f"{grouped(golds[-1])}g"
        gold["change"] = f"{sign}{grouped(abs(golds[-1] - golds[0]))}g in {SPARK_DAYS} d"

    return {"level": level, "gold": gold}


def is_main(character: dict) -> bool:
    return (character.get("role") or "").strip().casefold() == "main"


def as_hours(value: float) -> str:
    return f"{value:.1f} h"


def view(result: dict) -> dict:
    """The page's charts: geometry from pinkas.charts, a legend and a table view for each."""
    today = result["today"]
    chars = result["characters"]
    slot_of = {c["label"]: c["slot"] for c in chars.values()}
    days = RANGES[result["range"]]
    weeks = min(CALENDAR_WEEKS, math.ceil(days / 7)) if days else CALENDAR_WEEKS
    account: dict[date, float] = defaultdict(float)

    for per_day in result["calendar"].values():
        for d, h in per_day.items():
            account[d] += h

    def tagged(cid: int) -> dict:
        return {"name": chars[cid]["label"], "slot": chars[cid]["slot"]}

    def largest_first(values: dict[int, float | None], field: str) -> list[dict]:
        ordered = sorted(values.items(), key=lambda kv: -(kv[1] or 0))

        return [{**tagged(cid), field: v} for cid, v in ordered if cid in chars]

    def at_max(cid: int) -> bool:
        levels = result["level"].get(cid)

        return bool(levels) and levels[-1][1] >= MAX_LEVEL

    per_level = result["combat"]["levels"]

    def by_level(field: str) -> list[dict]:
        series = {cid: [(lvl, v[field]) for lvl, v in levels.items() if field in v] for cid, levels in per_level.items()}

        return per_character({cid: pts for cid, pts in series.items() if len(pts) > 1}, chars)

    # The owner's role "main" puts that character's strip beside the account's year.
    strips = [{**tagged(cid), "main": is_main(chars[cid]), **charts.calendar_strip(per_day, today, weeks)}
              for cid, per_day in sorted(result["calendar"].items()) if cid in chars]
    levelling = {cid: pts for cid, pts in result["level"].items() if pts and pts[0][1] < MAX_LEVEL}
    gather = result["gathering"]
    heat_rows = []

    for zone in gather["zones"]:
        counts = [gather["cells"].get((zone, p), 0) for p in gather["professions"]]
        heat_rows.append({"zone": zone, "cells": [{"value": n, "step": charts.heat_step(n, gather["top"])} for n in counts]})

    flow = result["flows"]
    flow_rows = [[a, b, "", n] for (a, b), n in flow["left"]] + [["", a, b, n] for (a, b), n in flow["right"]]
    stock = [{"name": label, "spark": charts.spark([n for _, n in pts]), "now": pts[-1][1],
              "change": pts[-1][1] - pts[0][1]} for label, pts in result["stockpiles"].items()]
    fights = result["combat"]["fights_per_death"]

    page = {
        "range": result["range"],
        "calendar_year": charts.calendar_strip(account, today, CALENDAR_WEEKS, labels=False),
        "calendar_main": next((s for s in strips if s["main"]), None),
        "calendar": [s for s in strips if not s["main"]],
        "level": line(per_character(levelling, chars), "date", "Day", lambda v: f"{v:.1f}"),
        "to_max": [{**tagged(cid), "days": n, "at_max": at_max(cid)}
                   for cid, n in sorted(result["to_max"].items()) if cid in chars],
        "time_per_level": line(per_character({cid: sorted(v.items()) for cid, v in result["time_per_level"].items()}, chars),
                               "level", "Level", as_hours),
        "deaths": result["deaths"],
        "gathering": {**gather, "professions": [p.capitalize() for p in gather["professions"]], "rows": heat_rows},
        "sankey": {**charts.sankey(flow["left"], flow["right"], slot_of),
                   "table": {"head": ["From", "Material", "To", "Items"], "rows": flow_rows}},
        "stockpiles": stock,
        "gold_sources": result["gold_sources"],
        "gold_top": max((max(s["in"], s["out"]) for s in result["gold_sources"]), default=0),
        "gold": line([{"name": "All characters", "slot": 0, "points": [(d, v / 10000) for d, v in result["gold"]]}],
                     "date", "Day", lambda v: f"{v:,.0f}g"),
        "dungeons": result["dungeons"],
        "dps": line(by_level("dps"), "level", "Level", lambda v: f"{v:.1f}"),
        "hps": line(by_level("hps"), "level", "Level", lambda v: f"{v:.1f}"),
        "deaths_per_hour": line(by_level("deaths_per_hour"), "level", "Level", lambda v: f"{v:.2f}"),
        "fights_per_death": largest_first(fights, "value"),
        "fights_top": max((v for v in fights.values() if v is not None), default=0),
        "skills": [{"name": name, **line(per_character(per, chars), "date", "Day")}
                   for name, per in result["skills"].items()],
        "rested": largest_first(result["rested"], "share"),
    }
    with_play = [cid for cid in sorted(result["calendar"]) if cid in chars]
    page["calendar_table"] = {
        "head": ["Day", "All characters", *(chars[cid]["label"] for cid in with_play)],
        "rows": [[f"{d:%a} {charts.short_date(d)}", as_hours(account[d]),
                  *(as_hours(result["calendar"][cid][d]) if result["calendar"][cid].get(d) else "—" for cid in with_play)]
                 for d in sorted(account, reverse=True)],
    }
    # The crosshair readouts, by the figure ids stats.html gives the line charts.
    lines = {"level": "level", "time-per-level": "time_per_level", "gold-total": "gold",
             "dps": "dps", "hps": "hps", "deaths-per-hour": "deaths_per_hour"}
    page["hover"] = {fid: page[key].get("hover", []) for fid, key in lines.items()} | {
        f"skill-{i}": s.get("hover", []) for i, s in enumerate(page["skills"], 1)}

    return page
