"""Weeks of made-up play for six made-up characters, as the pulled files the addon would write.

The beta file holds no events or totals; /stats needs both, and a history of logouts. `play()` walks
day by day, plays some characters each evening and returns one export per logout (a reload included),
the way the pull would fetch them. Seeded: the same arguments give the same account.
"""

import copy
import random
from datetime import UTC, datetime, timedelta

from tests.files import account_v3

REALM = "Testrealm"
START = datetime(2026, 10, 1, tzinfo=UTC)
# name, class, race, level, (skill line, name, rank)…, chance to play on a given day
CHARACTERS = (
    ("Fakechar", "ROGUE", "Orc", 14, ((393, "Skinning", 95), (165, "Leatherworking", 60), (185, "Cooking", 40)), 0.8),
    ("Altchar", "MAGE", "Troll", 11, ((197, "Tailoring", 40), (186, "Mining", 20)), 0.45),
    ("Herbchar", "DRUID", "Tauren", 60, ((182, "Herbalism", 300), (171, "Alchemy", 230)), 0.15),
    ("Minechar", "WARRIOR", "Orc", 30, ((186, "Mining", 150), (164, "Blacksmithing", 120)), 0.3),
    ("Zoë", "SHAMAN", "Troll", 1, ((393, "Skinning", 1), (356, "Fishing", 5)), 0.5),
    ("Newchar", "PRIEST", "Undead", 1, (), 0.04),
)
# id, name, item class, subclass: the trade goods the routes know (parse.KIND_OF).
MATS = (
    (2318, "Light Leather", 7, 6), (2589, "Linen Cloth", 7, 5), (2770, "Copper Ore", 7, 7),
    (2447, "Peacebloom", 7, 9), (10940, "Strange Dust", 7, 12), (4306, "Silk Cloth", 7, 5),
)
OTHER_ITEM = (6948, "Hearthstone", 15, 0)
# Gear and odds to fill bags and bank to a real snapshot's size (5–15 KB), so a timing over them means something.
FILLER = [(20000 + i, f"Made-up Gear Piece of the Owl {i}", 4, 2) for i in range(60)]
ZONES = ("Durotar", "The Barrens", "Stonetalon Mountains", "Ashenvale", "Mulgore")
KILLERS = ("Razormane Hunter", "Kolkar Wrangler", "Falling", "player", None, "Drowning", "Bristleback Quilboar")
DUNGEONS = ("Ragefire Chasm", "Wailing Caverns")
GOLD_SOURCES = ("loot", "vendor", "quest", "trade", "auction", "mail", "repair", "taxi", "other")
# Money in or out by source; the rest go either way.
GOLD_SIGN = {"loot": 1, "quest": 1, "trade": 1, "repair": -1, "taxi": -1, "mail": -1}
# Skill line: the gathering profession and the index in MATS of what it gathers.
GATHERING = {393: ("skinning", 0), 182: ("herbalism", 3), 186: ("mining", 2)}


def xp_max(level: int) -> int:
    return 400 + 180 * level * level // 10


class Character:
    def __init__(self, spec):
        self.name, self.cls, self.race, level, profs, self.chance = spec
        self.key = f"{REALM}/{self.name}"
        self.level, self.xp, self.rested = level, 0, 0
        self.money = 10_000 * level
        self.played, self.level_start = 3600 * level, 3600 * level - 600
        self.professions = [list(p) for p in profs]
        self.stock = {m[0]: 0 for m in MATS}
        self.events, self.next_event = [], 1
        self.logout = None

    def add_event(self, at: int, kind: str, value: dict) -> None:
        self.events.append({"id": self.next_event, "at": at, "kind": kind, "value": value})
        self.next_event += 1


class Hostile:
    """The hostile name, handed out once per kind of game text."""

    def __init__(self, name: str | None):
        self.name = name
        self.unused = {"death", "gather", "mail", "dungeon"} if name else set()

    def take(self, kind: str) -> str | None:
        if kind not in self.unused:
            return None

        self.unused.discard(kind)

        return self.name


class Totals(dict):
    def add(self, kind: str, key: str, **fields) -> None:
        value = self.setdefault((kind, key), {})

        for field, amount in fields.items():
            value[field] = value.get(field, 0) + amount

    def rows(self, session: int) -> list[dict]:
        return [{"session": session, "kind": k, "key": key, "value": v} for (k, key), v in self.items()]


def items(stock: dict, extra=()) -> list[dict]:
    names = {m[0]: m for m in (*MATS, OTHER_ITEM, *FILLER)}
    lines = [(i, n) for i, n in stock.items() if n] + list(extra)

    return [
        {"id": i, "name": names[i][1], "count": n, "quality": 1, "class": names[i][2], "subclass": names[i][3]}
        for i, n in lines
    ]


def export_of(chars: list[Character], session: dict, exported_at: int) -> dict:
    """Every character's entry, as the account's one file holds them; only the one that played carries
    this session's events and totals (ingest keeps earlier ones by their keys)."""
    export = account_v3()
    template = export["chars"].pop(f"{REALM}/Fakechar")
    export["exported_at"] = exported_at
    export["routes"] = {"cloth": "Altchar", "leather": "Fakechar", "ore": "Minechar", "herb": "Herbchar"}

    for c in chars:
        if c.logout is None:
            continue

        entry = copy.deepcopy(template)
        entry.update(
            name=c.name, realm=REALM, **{"class": c.cls}, race=c.race, level=c.level,
            xp=c.xp if c.level < 60 else 0, xp_max=xp_max(c.level) if c.level < 60 else 0, rested=c.rested,
            resting=True, money=c.money, zone="Orgrimmar", session_started_at=c.logout[0], logout_at=c.logout[1],
            played={"total": c.played, "level": c.played - c.level_start, "at": c.logout[1]},
            professions=[{"skill_line": s, "name": n, "rank": r, "max": 75 * (1 + r // 75) if r < 300 else 300}
                         for s, n, r in c.professions],
            bags=items(c.stock, [(OTHER_ITEM[0], 1), *((f[0], 1) for f in FILLER[:25])]), bag_slots=80,
            bank={"as_of": c.logout[1], "slots": 48,
                  "items": items({i: n // 2 for i, n in c.stock.items()}, [(f[0], 1) for f in FILLER[25:]])},
            events=session["events"] if session["who"] is c else [],
            totals=session["totals"] if session["who"] is c else [],
        )
        export["chars"][c.key] = entry

    return export


def ten_minutes(c: Character, at: int, rng: random.Random, hostile: Hostile, totals: Totals) -> None:
    gained = rng.randint(80, 400) if c.level < 60 else 0
    used = min(c.rested, gained // 2)
    c.rested -= used
    totals.add("rested", "", xp=gained, rested_used=used)
    fight = rng.randint(8, 50)
    totals.add("combat", str(c.level), damage=fight * rng.randint(4, 6 + c.level), healing=fight * rng.randint(0, 3),
               taken=fight * 3, avoidable=0, seconds=fight, fights=1)
    c.xp += gained
    c.played += 600

    while c.level < 60 and c.xp >= xp_max(c.level):
        c.xp -= xp_max(c.level)
        c.level += 1
        start = c.played - rng.randint(0, 300)
        c.add_event(at, "level", {"level": c.level, "zone": rng.choice(ZONES), "played_total": c.played,
                                  "played_level": start - c.level_start})
        c.level_start = start

    if rng.random() < 0.12:
        zone, killer = rng.choice(ZONES), rng.choice(KILLERS)

        if name := hostile.take("death"):
            zone = killer = name

        c.add_event(at + 30, "death", {"level": c.level, "zone": zone, "map": 1411, "x": rng.random(),
                                       "y": rng.random(), "killer": killer, "recap_id": c.next_event})

    if rng.random() < 0.3:
        c.add_event(at + 60, "quest", {"quest": rng.randint(100, 900), "xp": 250, "money": 120})

    for skill_line, (gathering, mat) in GATHERING.items():
        prof = next((p for p in c.professions if p[0] == skill_line), None)

        if prof and rng.random() < 0.6:
            zone = hostile.take("gather") or rng.choice(ZONES[:3])
            totals.add("gather", f"{gathering}|{zone}", count=rng.randint(1, 4))
            prof[2] = min(300, prof[2] + (rng.random() < 0.3))
            c.stock[MATS[mat][0]] += rng.randint(1, 3)

    c.stock[MATS[1][0]] += rng.randint(0, 3)


def gold(c: Character, rng: random.Random, totals: Totals) -> None:
    for source in rng.sample(GOLD_SOURCES, 4):
        amount = rng.randint(50, 30_000)
        sign = GOLD_SIGN.get(source) or rng.choice((1, -1))
        amount = min(amount, c.money) if sign < 0 else amount
        totals.add("gold", source, **{"in" if sign > 0 else "out": amount})
        c.money += sign * amount


def mail(c: Character, chars: list[Character], at: int, rng: random.Random, hostile: Hostile) -> None:
    others = [o for o in chars if o is not c and o.logout]

    if not others:
        return

    to = rng.choice(others)
    sent = [(i, n) for i, n in c.stock.items() if n >= 5][:3]
    mailed = [{"id": i, "name": next(m[1] for m in MATS if m[0] == i), "count": n} for i, n in sent]

    if name := hostile.take("mail"):
        mailed.append({"id": 99999, "name": name, "count": 1})

    if not mailed:
        return

    for i, n in sent:
        c.stock[i] -= n
        to.stock[i] += n

    c.add_event(at, "mail", {"to": to.key, "items": mailed})


def dungeon(c: Character, started: int, at: int, rng: random.Random, hostile: Hostile) -> None:
    instance = hostile.take("dungeon") or rng.choice(DUNGEONS)
    logout = rng.random() < 0.2
    c.add_event(at, "dungeon", {
        "instance": instance, "level": c.level, "started": started, "duration": rng.randint(1500, 4500),
        "deaths": rng.randint(0, 3), "ended_by_logout": logout,
    })


def session(c: Character, chars: list[Character], started: int, seconds: int, rng: random.Random,
            hostile: Hostile) -> dict:
    totals = Totals()
    events_before = len(c.events)

    for minute in range(0, seconds // 60, 10):
        ten_minutes(c, started + minute * 60, rng, hostile, totals)

    gold(c, rng, totals)

    if rng.random() < 0.35:
        mail(c, chars, started + seconds - 120, rng, hostile)

    if c.level >= 13 and rng.random() < 0.15:
        dungeon(c, started, started + seconds - 60, rng, hostile)

    return {"who": c, "events": c.events[events_before:], "totals": totals.rows(started)}


def fetched(chars: list[Character], session: dict, started: int, logout_at: int) -> tuple[datetime, dict]:
    session["who"].logout = (started, logout_at)

    return datetime.fromtimestamp(logout_at + 60, UTC), export_of(chars, session, logout_at)


def play(days: int = 21, seed: int = 8, hostile: str | None = None, first_day: datetime = START) -> list[tuple[datetime, dict]]:
    """(fetched_at, export) per logout, oldest first. `hostile`, when given, names one killer, death and
    gathering zone, mailed item and dungeon."""
    rng = random.Random(seed)
    names = Hostile(hostile)
    chars = [Character(spec) for spec in CHARACTERS]
    files = []

    for day in range(days):
        evening = first_day + timedelta(days=day, hours=17 + rng.randint(0, 4))

        for c in chars:
            if rng.random() > c.chance:
                continue

            started = int(evening.timestamp()) + rng.randint(0, 3600)
            seconds = rng.randint(1200, 4 * 3600)
            evening += timedelta(seconds=seconds + 600)
            played = session(c, chars, started, seconds, rng, names)

            # A reload halfway: a second snapshot of the same session.
            if rng.random() < 0.3:
                files.append(fetched(chars, played, started, started + seconds // 2))

            c.rested += 4 * xp_max(c.level) // 100 if c.level < 60 else 0
            files.append(fetched(chars, played, started, started + seconds))

    return files
