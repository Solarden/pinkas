"""The page: every character at a glance, behind one password.

    python3 -m pinkas.web                  serve
    python3 -m pinkas.web --hash-password  print a hash for PINKAS_PASSWORD_HASH

Config comes from the environment: DATABASE_URL (the app role), SESSION_SECRET,
PINKAS_PASSWORD_HASH, and optionally PINKAS_LISTEN (host:port, default 127.0.0.1:8094) and
PINKAS_GUIDE (a markdown file shown at /guide).

It speaks plain HTTP and expects a TLS proxy in front, the only client that can reach its address.

Everything shown comes from the game or from the pulled file, so every template is autoescaped and
the one script is static/pinkas.js, served by the app itself, which sets game text with textContent only.
The one exception to autoescaping is the guide, rendered with raw HTML off.
"""

import getpass
import hashlib
import hmac
import http.server
import json
import math
import os
import re
import secrets
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, quote, unquote, urlparse

import psycopg2
from jinja2 import Environment, FileSystemLoader
from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from psycopg2.extras import execute_values

from pinkas import rested, stats
from pinkas.db import NEWEST, fetch
from pinkas.ingest import ts
from pinkas.parse import KIND_LABELS, KIND_OF, ROUTE_KINDS, ParseError

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)
SCRIPT_PATH = "/static/pinkas.js"
SCRIPT = (ROOT / "static" / "pinkas.js").read_text(encoding="utf-8")
SCRIPT_ETAG = f'"{hashlib.sha256(SCRIPT.encode()).hexdigest()[:16]}"'
COOKIE = "pinkas"
COOKIE_MAX_AGE = 30 * 86400
PLAYING_HOURS = 12
FORM_LIMIT = 8192
# The whole string must match: no "//" or scheme means no off-site redirect, and the charset keeps a
# decoded CR/LF out of the Location header.
SEGMENT = r"[A-Za-z0-9_.~%-]+"
NEXT_RE = re.compile(rf"/(?:c/{SEGMENT}/{SEGMENT}|search|guide|stats|settings)?")
TARGET_SKILLS = (150, 225, 300)
# Common (1) is plain text; the word is for screen readers, the colour is in base.html.
QUALITY_WORDS = {0: "poor", 2: "uncommon", 3: "rare", 4: "epic", 5: "legendary"}
# Cooking, Fishing and First Aid: every character can learn them beside its two professions.
SECONDARY_SKILL_LINES = {185, 356, 129}
ROLE_LIMIT = 40
SORT_LIMIT = 10000
# A bank or mailbox seen longer ago than this is marked stale.
STALE_AFTER = timedelta(days=3)
HEADERS = (
    (
        "Content-Security-Policy",
        # The skin is an inline <style>: sync.py keeps it inside templates/base.html.
        "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src data:;"
        " form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    # Honoured only over HTTPS (through the proxy); then the browser refuses plain HTTP for a year.
    ("Strict-Transport-Security", "max-age=31536000"),
)
# Every interface would put the plain-HTTP port on the network, around the proxy.
WILDCARD_HOSTS = ("", "0.0.0.0", "::", "[::]")


class Config(NamedTuple):
    dsn: str
    secret: str
    pw_hash: str
    guide: str = ""


def load_config(env) -> Config:
    """Names a missing variable, never a value."""
    names = ("DATABASE_URL", "SESSION_SECRET", "PINKAS_PASSWORD_HASH")
    missing = [name for name in names if not env.get(name)]

    if missing:
        raise SystemExit(f"missing from the environment: {', '.join(missing)}")

    return Config(*(env[name] for name in names), guide=env.get("PINKAS_GUIDE", ""))


# ":" separators: a "$" would be expanded if the env file were sourced by a shell.
def listen_address(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")

    if host in WILDCARD_HOSTS:
        raise SystemExit("PINKAS_LISTEN: bind to loopback or the proxy's address, never every interface")

    return host.strip("[]"), int(port)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p)

    return f"scrypt:{n}:{r}:{p}:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split(":")

        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except ValueError:
        return False

    return same(got.hex(), digest)


def same(given: str, expected: str) -> bool:
    """Constant time; bytes, because compare_digest raises on a non-ASCII str and a client controls `given`."""
    return hmac.compare_digest(given.encode(), expected.encode())


def sign(secret: str, msg: str) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def session_cookie(conf: Config, now: float) -> str:
    """Signing the password hash too means a new password ends every session."""
    expiry = int(now) + COOKIE_MAX_AGE
    value = f"{expiry}|{sign(conf.secret, f'session|{expiry}|{conf.pw_hash}')}"

    return f"{COOKIE}={value}; Max-Age={COOKIE_MAX_AGE}; Path=/; Secure; HttpOnly; SameSite=Lax"


# Deletes this browser's cookie only: sessions are not stored, so a copied cookie stays valid until it
# expires or the password hash changes.
LOGOUT_COOKIE = f"{COOKIE}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Lax"


def valid_session(conf: Config, cookie_header: str | None, now: float) -> bool:
    for part in (cookie_header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        expiry, _, sig = value.partition("|")

        # The length first: int() refuses a string of over 4300 digits.
        if name != COOKIE or not sep or not (expiry.isascii() and expiry.isdigit() and len(expiry) <= 12):
            continue

        if int(expiry) <= now:
            continue

        if same(sig, sign(conf.secret, f"session|{expiry}|{conf.pw_hash}")):
            return True

    return False


def form_token(conf: Config) -> str:
    """SameSite=Lax still sends the cookie on a POST from a sibling app on the same site; this token
    is not sent."""
    return sign(conf.secret, f"form|{conf.pw_hash}")


def safe_next(target: str) -> str:
    return target if NEXT_RE.fullmatch(target) else "/"


def pathpart(text: str) -> str:
    """One path segment: Jinja's urlencode keeps "/", which would split a name holding one."""
    return quote(text, safe="")


def gold(copper: int | None) -> str:
    """Zero units left out; thousands of gold grouped by a narrow no-break space."""
    if copper is None:
        return "?"

    g, s, c = copper // 10000, copper // 100 % 100, copper % 100
    parts = [f"{g:,}g".replace(",", "\u202f")] if g else []
    parts += [f"{s}s"] if s else []
    parts += [f"{c}c"] if c or not parts else []

    return " ".join(parts)


def epoch(seconds: int | None) -> datetime | None:
    """A time inside a snapshot; one out of range shows as unknown instead of failing the page."""
    try:
        return ts(seconds)
    except ParseError:
        return None


def ago(then: datetime | None) -> str:
    if then is None:
        return "?"
    seconds = (datetime.now(UTC) - then).total_seconds()

    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"

    return f"{int(seconds // 86400)} d ago"


def stale(then: datetime | None) -> bool:
    return then is not None and datetime.now(UTC) - then > STALE_AFTER


def duration(seconds: float) -> str:
    if seconds < 86400:
        return f"{math.ceil(seconds / 3600)} h"

    return f"{math.ceil(seconds / 86400)} d"


def minutes(seconds: float | None) -> str:
    if seconds is None:
        return "—"

    total = round(seconds / 60)

    return f"{total} min" if total < 60 else f"{total // 60} h {total % 60} min"


def local(moment: datetime | None) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z") if moment else "—"


TEMPLATES.filters.update(
    pathpart=pathpart, gold=gold, epoch=epoch, ago=ago, stale=stale, duration=duration, minutes=minutes, local=local
)
TEMPLATES.globals.update(range_labels=stats.RANGE_LABELS, one_decimal="{:.1f}".format, percent="{}%".format)
TEMPLATES.globals["quality_words"] = QUALITY_WORDS
TEMPLATES.globals["rested"] = {
    "share": rested.BAR_SHARE_PER_PERIOD,
    "hours": rested.PERIOD_SECONDS // 3600,
    "elsewhere": rested.NOT_RESTING_SHARE,
    "cap": rested.CAP_BARS,
    "per_rank": rested.WELL_RESTED_PER_RANK,
    "max_level": rested.MAX_LEVEL,
}


def render(name: str, **ctx) -> str:
    return TEMPLATES.get_template(name).render(**ctx)


def connect(dsn: str):
    return psycopg2.connect(dsn, connect_timeout=5)


CHARACTERS_SQL = f"""
SELECT c.id, c.realm, c.name, c.role, c.well_rested_rank, s.logout_at, s.data,
       coalesce(c.playing_since > now() - make_interval(hours => %(hours)s)
                AND NOT EXISTS (SELECT 1 FROM pinkas_snapshots o
                                WHERE o.character_id <> c.id AND o.session_started_at > c.playing_since),
                false) AS playing
FROM pinkas_characters c
{NEWEST}
"""

# The newest snapshot overall is the newest of each character's, so the unique index serves it.
NERD_SQL = f"""
SELECT p.status, p.since, f.sha256, f.size, f.fetched_at, s.export,
       (SELECT count(*) FROM pinkas_snapshots) AS snapshots, (SELECT count(*) FROM pinkas_events) AS events
FROM (SELECT 1) one
LEFT JOIN pinkas_pull_state p ON true
LEFT JOIN LATERAL (SELECT sha256, octet_length(body) AS size, fetched_at FROM pinkas_files
                   ORDER BY fetched_at DESC LIMIT 1) f ON true
LEFT JOIN LATERAL (SELECT s.data->'export' AS export FROM pinkas_characters c {NEWEST}
                   ORDER BY s.logout_at DESC LIMIT 1) s ON true
"""


# Items of each character's newest snapshot only; ingest has already carried a bank or mailbox the
# export left null.
SEARCH_SQL = f"""
SELECT c.realm, c.name, x.container, x.item->>'name' AS item, (x.item->>'id')::int AS item_id,
       max((x.item->>'quality')::int) AS quality, sum((x.item->>'count')::int) AS count
FROM pinkas_characters c
{NEWEST}
CROSS JOIN LATERAL (
    SELECT 'bags', i FROM jsonb_array_elements(s.data->'bags') i
    UNION ALL SELECT 'bank', i FROM jsonb_array_elements(s.data->'bank'->'items') i
    UNION ALL SELECT 'mailbox', i FROM jsonb_array_elements(s.data->'mailbox'->'mails') m,
                                       jsonb_array_elements(m->'items') i
) x(container, item)
WHERE c.archived_at IS NULL AND x.item->>'name' ILIKE %(pattern)s
GROUP BY c.id, c.realm, c.name, c.sort, x.container, item, item_id
ORDER BY item, item_id, c.sort NULLS LAST, c.name
"""

# The skill line -> name pairs the game reported in every character's newest snapshot. A line reported
# under two names (clients in two languages) keeps the first name alphabetically, every time.
PROFESSIONS_SQL = f"""
SELECT DISTINCT ON (skill_line) (p->>'skill_line')::int AS skill_line, p->>'name' AS name
FROM pinkas_characters c
{NEWEST}
CROSS JOIN LATERAL jsonb_array_elements(s.data->'professions') p
WHERE p->>'skill_line' IS NOT NULL
ORDER BY skill_line, name
"""


def professions(cur) -> dict[int, str]:
    return {row["skill_line"]: row["name"] or f"profession {row['skill_line']}" for row in fetch(cur, PROFESSIONS_SQL)}


def nerd_stats(cur) -> dict:
    stats = fetch(cur, NERD_SQL)[0]
    stats["export"] = stats["export"] or {}

    return stats


def next_targets(cur) -> dict[tuple[int, int], list[int]]:
    targets: dict[tuple[int, int], list[int]] = {}

    for row in fetch(cur, "SELECT character_id, skill_line, skill FROM pinkas_targets ORDER BY skill"):
        targets.setdefault((row["character_id"], row["skill_line"]), []).append(row["skill"])

    return targets


def summary(row: dict, targets: dict, now: datetime) -> dict:
    """What a row on `/` shows. The playing character is online, so its rested is the snapshot's."""
    data, rank = row["data"], row["well_rested_rank"]
    level, xp_max, resting = data["level"], data["xp_max"], bool(data["resting"])
    current = rested.rested_now(
        level, xp_max, data["rested"], resting, row["logout_at"], row["logout_at"] if row["playing"] else now, rank
    )
    professions = []

    for p in data["professions"]:
        skills = targets.get((row["id"], p["skill_line"]), [])
        target = next((s for s in skills if s > (p["rank"] or 0)), None)
        met = skills[-1] if skills and target is None else None
        # The rank's cap reached with nothing set beyond it: the next rank needs a trainer.
        at_cap = (
            target is None and met is None and p["rank"] is not None and p["max"] is not None
            and p["rank"] >= p["max"] < TARGET_SKILLS[-1]
        )
        professions.append(
            {**p, "target": target, "met": met, "at_cap": at_cap, "secondary": p["skill_line"] in SECONDARY_SKILL_LINES}
        )

    return {
        **row,
        "class": data["class"],
        "level": level,
        "money": data["money"],
        "rested": current,
        "cap": rested.rate_and_cap(xp_max, resting, rank)[1] if xp_max else None,
        "bar_pct": round(100 * current / xp_max) if current is not None and xp_max else None,
        "full_in": rested.full_in(level, xp_max, current, resting, rank),
        "xp": data["xp"] if level is not None and level < rested.MAX_LEVEL and xp_max else None,
        "xp_max": xp_max,
        "professions": professions,
    }


def account_summary(characters: list[dict]) -> dict:
    """Gold summed over the listed characters, and each profession with every character that has it:
    primary professions first, then secondary, each by name; within one, the highest rank first."""
    by_name: dict[tuple, list[dict]] = {}

    for c in characters:
        for p in c["professions"]:
            entry = {**p, "character": c["name"], "realm": c["realm"]}
            by_name.setdefault((p["secondary"], p["name"] or "?"), []).append(entry)

    return {
        "gold": sum(c["money"] or 0 for c in characters),
        "professions": [
            {"name": name, "characters": sorted(entries, key=lambda e: -(e["rank"] or 0))}
            for (_, name), entries in sorted(by_name.items())
        ],
    }


def rested_share(c: dict) -> float | None:
    """Rested XP as a share of the character's level; None when unknown or at max level."""
    if c["rested"] is None or not c["xp_max"] or c["level"] is None or c["level"] >= rested.MAX_LEVEL:
        return None

    return c["rested"] / c["xp_max"]


def next_up(characters: list[dict]) -> int | None:
    """The most rested character that isn't playing; none when no one has rested XP."""
    waiting = [c for c in characters if not c["playing"] and rested_share(c)]

    return max(waiting, key=rested_share)["id"] if waiting else None


def card_keys(characters: list[dict]) -> dict[str, dict]:
    """The sort and filter keys static/pinkas.js reads, by card id."""
    return {
        f"c{c['id']}": {
            "name": c["name"],
            "role": c["role"],
            "professions": [p["name"] for p in c["professions"] if p["name"]],
            "level": c["level"],
            "rested": rested_share(c),
            "seen": int(c["logout_at"].timestamp()),
            "playing": c["playing"],
        }
        for c in characters
    }


def summed(items: list[dict]) -> list[dict]:
    """Stacks of the same item as one line, by name. Items exported before contract v3 have no quality."""
    lines: dict[tuple, dict] = {}

    for item in items:
        key = (item["name"] or "", item["id"])
        line = lines.setdefault(key, {"name": item["name"], "id": item["id"], "count": 0, "quality": item.get("quality")})
        line["count"] += item["count"]

    return [lines[key] for key in sorted(lines)]


def own_name(recipient: str) -> str:
    """The character name in a route's recipient, which may be typed as Name-Realm.

    shortcut: ignores the realm; enough while every alt shares one realm.
    """
    return recipient.split("-", 1)[0].casefold()


def item_kind(item: dict) -> str | None:
    return KIND_OF.get((item.get("class"), item.get("subclass")))


def grouped(items: list[dict], routes: dict, character: str, in_bank: bool, bank_full: bool) -> list[dict]:
    """Items as the mail plan sees them: routed materials going to another character, materials whose
    route is this character (kept in the bank), junk for a vendor, and the rest. `slots` counts stacks."""
    groups: dict[tuple, dict] = {}

    for item in items:
        kind = item_kind(item)
        recipient = routes.get(kind) if kind else None

        if recipient and own_name(recipient) != character.casefold():
            key, label, to = (0, ROUTE_KINDS.index(kind)), f"{KIND_LABELS[kind]} →", recipient
        elif recipient:
            where = "own mats" if in_bank else "bank full" if bank_full else None
            key, to = (1, ROUTE_KINDS.index(kind)), None
            label = f"{KIND_LABELS[kind]} · {where}" if where else f"{KIND_LABELS[kind]} → bank"
        elif item.get("quality") == 0:
            key, label, to = (2, 0), "Junk → vendor", None
        else:
            key, label, to = (3, 0), "Other", None

        group = groups.setdefault(key, {"label": label, "to": to, "entries": []})
        group["entries"].append(item)

    return [
        {"label": g["label"], "to": g["to"], "slots": len(g["entries"]), "items": summed(g["entries"])}
        for _, g in sorted(groups.items())
    ]


def slots(items: list[dict], total: int | None) -> dict:
    """Used and total slots; `low` and `high` turn the meter yellow at 70% and red at 90%."""
    used = len(items)

    return {
        "used": used,
        "total": total,
        "low": round(total * 0.7, 1) if total else None,
        "high": round(total * 0.9, 1) if total else None,
        "level": "neg" if total and used >= total * 0.9 else "warn" if total and used >= total * 0.7 else "",
    }


def mail_plan(characters: list[dict], routes: dict) -> list[dict]:
    """Per character, the slots of routed materials (bags and bank) to mail to each other character,
    each recipient with its listed character when it is one, for the link."""
    listed = {c["name"].casefold(): c for c in characters}
    plan = []

    for c in characters:
        data = c["data"]
        items = data["bags"] + (data["bank"]["items"] if data["bank"] else [])
        sends: dict[str, int] = {}

        for group in grouped(items, routes, c["name"], in_bank=False, bank_full=False):
            if group["to"]:
                sends[group["to"]] = sends.get(group["to"], 0) + group["slots"]

        if sends:
            ordered = sorted(sends.items(), key=lambda s: -s[1])
            plan.append({"character": c, "sends": [(to, listed.get(own_name(to)), n) for to, n in ordered]})

    return plan


def like_pattern(text: str) -> str:
    """A substring match in which the user's %, _ and backslash are literal (backslash is LIKE's
    default escape)."""
    return "%" + re.sub(r"([\\%_])", r"\\\1", text) + "%"


def found(rows: list[dict]) -> list[dict]:
    """Search rows as one entry per item: its total, then each character's count per container."""
    items: dict[tuple, dict] = {}

    for row in rows:
        item = items.setdefault(
            (row["item"], row["item_id"]),
            {"name": row["item"], "id": row["item_id"], "quality": row["quality"], "total": 0, "characters": {}},
        )
        who = item["characters"].setdefault(
            (row["realm"], row["name"]), {"realm": row["realm"], "name": row["name"], "bags": 0, "bank": 0, "mailbox": 0}
        )
        who[row["container"]] += row["count"]
        # Mail items carry no quality: any container that knows it names it.
        item["quality"] = item["quality"] if item["quality"] is not None else row["quality"]
        item["total"] += row["count"]

    return [{**item, "characters": list(item["characters"].values())} for item in items.values()]


def table_open(renderer, tokens, idx, options, env) -> str:
    """A table scrolls inside a focusable region, named after the heading above it, so a keyboard can
    scroll it; scrolling the table itself would drop its table semantics in some browsers."""
    name = next((tokens[i + 1].content for i in range(idx - 1, -1, -1) if tokens[i].type == "heading_open"), "Table")

    return f'<div class="table-box" tabindex="0" role="region" aria-label="{escapeHtml(name)}">\n<table>\n'


def render_guide(text: str, down: int = 0) -> str:
    """Raw HTML in the file becomes text, and markdown-it's link check drops javascript: and data: URLs.
    `down` moves every heading that many levels down, for a page that has its own h1.
    Built per call: sharing one parser across the server's threads is not documented as safe."""
    md = MarkdownIt("commonmark", {"html": False}).enable("table")
    md.add_render_rule("table_open", table_open)
    md.add_render_rule("table_close", lambda *args: "</table>\n</div>\n")
    tokens = md.parse(text)

    for token in tokens:
        if token.type in ("heading_open", "heading_close"):
            token.tag = f"h{min(6, int(token.tag[1]) + down)}"

    html = md.renderer.render(tokens, md.options, {})

    # Task lists aren't CommonMark; their markers stay text, shown as boxes.
    for marker, box in (("[ ]", "☐"), ("[x]", "☑"), ("[X]", "☑")):
        html = html.replace(f"<li>{marker} ", f'<li class="task">{box} ')

    return html


def settings_rows(cur) -> list[dict]:
    """Every character, archived ones last, each with its professions: learned or targeted ones first."""
    known = professions(cur)
    targets = next_targets(cur)
    characters = fetch(
        cur,
        "SELECT c.id, c.realm, c.name, c.role, c.sort, c.well_rested_rank, c.archived_at,"
        f" s.data->'professions' AS professions FROM pinkas_characters c {NEWEST}"
        " ORDER BY c.archived_at IS NOT NULL, c.sort NULLS LAST, c.name",
    )

    for c in characters:
        learned = {p["skill_line"]: p["rank"] for p in c["professions"] if p["skill_line"] is not None}
        mine = {line for (cid, line) in targets if cid == c["id"]}
        c["first"], c["rest"] = [], []

        # Lines no snapshot names any more (a dropped profession that still has targets) go last.
        for line in sorted(known.keys() | mine, key=lambda line: (line not in known, known.get(line, ""))):
            upto = targets.get((c["id"], line), [None])[-1]
            row = {"skill_line": line, "name": known.get(line, f"profession {line}"), "rank": learned.get(line), "upto": upto}
            c["first" if line in learned or upto else "rest"].append(row)

    return characters


def save_settings(conn, form: dict[str, list[str]]) -> bool:
    """Every field is checked before anything is written, and False means nothing was."""
    def first(name: str) -> str:
        return form.get(name, [""])[0]

    character, role, sort, rank = first("character"), first("role").strip(), first("sort").strip(), first("rank")

    if not (character.isascii() and character.isdigit()) or len(role) > ROLE_LIMIT or rank not in tuple("012345"):
        return False

    if sort and not (re.fullmatch(r"-?[0-9]{1,5}", sort) and abs(int(sort)) <= SORT_LIMIT):
        return False

    character_id = int(character)

    with conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM pinkas_characters WHERE id = %s FOR UPDATE", (character_id,))

        if cur.fetchone() is None:
            return False

        known = professions(cur).keys() | {line for (cid, line) in next_targets(cur) if cid == character_id}
        upto: dict[int, int] = {}

        for key, values in form.items():
            if not key.startswith("target-"):
                continue

            line, value = key.removeprefix("target-"), values[0]

            if not (line.isascii() and line.isdigit() and int(line) in known and value in ("", *map(str, TARGET_SKILLS))):
                return False

            upto[int(line)] = int(value or 0)

        archived = "archived" in form
        cur.execute(
            "UPDATE pinkas_characters SET role = %s, sort = %s, well_rested_rank = %s,"
            " archived_at = CASE WHEN %s THEN coalesce(archived_at, now()) END,"
            " playing_since = CASE WHEN %s THEN NULL ELSE playing_since END WHERE id = %s",
            (role or None, int(sort) if sort else None, int(rank), archived, archived, character_id),
        )
        cur.execute(
            "DELETE FROM pinkas_targets WHERE character_id = %s AND skill_line = ANY(%s)", (character_id, list(upto))
        )
        execute_values(
            cur,
            "INSERT INTO pinkas_targets (character_id, skill_line, skill) VALUES %s",
            [(character_id, line, skill) for line, top in upto.items() for skill in TARGET_SKILLS if skill <= top],
        )

    return True


def set_playing(conn, character_id: int | None) -> bool:
    """False for an unknown or archived character, which `/` doesn't show. The row locks serialise
    two devices, so the partial unique index (one playing character) is never tripped."""
    with conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM pinkas_characters WHERE archived_at IS NULL FOR UPDATE")

        if character_id is not None and character_id not in {row[0] for row in cur.fetchall()}:
            return False

        cur.execute(
            "UPDATE pinkas_characters SET playing_since = NULL"
            " WHERE playing_since IS NOT NULL AND id IS DISTINCT FROM %s",
            (character_id,),
        )

        if character_id is not None:
            cur.execute("UPDATE pinkas_characters SET playing_since = now() WHERE id = %s", (character_id,))

    return True


class Handler(http.server.BaseHTTPRequestHandler):
    # An idle client can't hold a thread for longer than this.
    timeout = 30
    server_version = "pinkas"
    sys_version = ""
    # Back after "log out" must not show a page; only the script may be kept, and it is revalidated.
    cache_control = "no-store"

    def log_request(self, code="-", size="-") -> None:
        # The method and status only: a path names a character, and the log keeps no file content.
        self.log_message("%s %s", self.command, code)

    def end_headers(self) -> None:
        # Here, so the server's own error responses (a bad request line, an unknown method) get them too.
        for key, value in (*HEADERS, ("Cache-Control", self.cache_control)):
            self.send_header(key, value)

        super().end_headers()

    def send(
        self, code: int, body: str = "", headers: tuple[tuple[str, str], ...] = (), kind: str = "text/html"
    ) -> None:
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))

        for key, value in headers:
            self.send_header(key, value)

        self.end_headers()

        if self.command != "HEAD":
            self.wfile.write(payload)

    def redirect(self, location: str, *headers: tuple[str, str]) -> None:
        # Callers build `location` from fixed paths or safe_next(), never from the raw request.
        self.send(303, "", (*headers, ("Location", location)))

    def form_length(self) -> int:
        try:
            # max(): a negative Content-Length would make read() block until the client hangs up.
            return max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            return 0

    def read_form(self) -> dict[str, list[str]]:
        return parse_qs(self.rfile.read(self.form_length()).decode("utf-8", "replace"), keep_blank_values=True)

    def do_GET(self) -> None:
        self.respond(post=False)

    do_HEAD = do_GET

    def do_POST(self) -> None:
        # Refused whole, never cut: a form missing a field would save that field as cleared.
        if self.form_length() > FORM_LIMIT:
            return self.send(413, render("error.html", message="Form too large."))

        self.respond(post=True)

    def respond(self, post: bool) -> None:
        # Per request: a keep-alive connection must not carry the script's no-cache onto a page.
        self.cache_control = Handler.cache_control
        conf: Config = self.server.conf
        url = urlparse(self.path)

        if url.path == "/login":
            return self.login(conf) if post else self.login_page(url)

        # Before the session check: the script holds no data, and a redirect in its place would be refused.
        if url.path == SCRIPT_PATH and not post:
            self.cache_control = "no-cache"

            if self.headers.get("If-None-Match") == SCRIPT_ETAG:
                return self.send(304, headers=(("ETag", SCRIPT_ETAG),))

            return self.send(200, SCRIPT, (("ETag", SCRIPT_ETAG),), kind="text/javascript")

        if post and url.path == "/logout":
            if not same(self.read_form().get("t", [""])[0], form_token(conf)):
                return self.send(400, render("error.html", message="Stale form: reload and try again."))

            return self.redirect("/login", ("Set-Cookie", LOGOUT_COOKIE))

        if not valid_session(conf, self.headers.get("Cookie"), time.time()):
            # A POST can't be replayed after login, so it lands on the list instead.
            target = "/" if post else safe_next(url.path)

            return self.redirect("/login?next=" + quote(target, safe="/"))

        cookie = ("Set-Cookie", session_cookie(conf, time.time()))
        conn = None

        try:
            conn = connect(conf.dsn)
            ctx = {"token": form_token(conf)}

            if post:
                self.post(conn, url.path, ctx, cookie)
            else:
                self.get(conn, url.path, url.query, ctx, cookie)
        # The client left mid-response: nothing to answer.
        except ConnectionError:
            pass
        # A page, never a crashed handler. The class and SQLSTATE only: a driver message can quote the DSN.
        except Exception as exc:
            print(f"pinkas: {type(exc).__name__} {getattr(exc, 'pgcode', '') or ''}", file=sys.stderr, flush=True)
            database = isinstance(exc, psycopg2.Error)
            message = "Database unavailable." if database else "Server error: see the log."
            self.send(503 if database else 500, render("error.html", message=message))
        finally:
            if conn is not None:
                conn.close()

    def login_page(self, url) -> None:
        target = safe_next(parse_qs(url.query).get("next", ["/"])[0])
        self.send(200, render("login.html", next=target, error=None))

    def login(self, conf: Config) -> None:
        form = self.read_form()
        target = safe_next(form.get("next", ["/"])[0])
        # shortcut: no rate limit; the page is LAN-only and each try costs a scrypt. Add one if it is ever exposed.
        if verify_password(form.get("password", [""])[0], conf.pw_hash):
            return self.redirect(target, ("Set-Cookie", session_cookie(conf, time.time())))

        self.send(401, render("login.html", next=target, error="Wrong password."))

    def page(self, conn, code: int, name: str, ctx: dict, cookie: tuple[str, str], nerd=None, **more) -> None:
        """A rendered page, the nerd panel included: only authenticated handlers call this."""
        if nerd is None:
            with conn, conn.cursor() as cur:
                nerd = nerd_stats(cur)

        self.send(code, render(name, **ctx, nerd=nerd, **more), (cookie,))

    def not_found(self, conn, ctx: dict, cookie: tuple[str, str]) -> None:
        self.page(conn, 404, "error.html", ctx, cookie, message="Not found.")

    def guide(self, down: int = 0) -> dict:
        """The guide's context: the rendered file, or why there is none."""
        path = self.server.conf.guide

        if not path:
            return {"html": None, "message": "No guide set: PINKAS_GUIDE names a markdown file."}

        # shortcut: no size cap; the file is the owner's own. Cap it if the path ever comes from elsewhere.
        try:
            text = Path(path).read_text(encoding="utf-8")
        # The class only: the message names the path.
        except (OSError, UnicodeDecodeError) as exc:
            print(f"pinkas: guide {type(exc).__name__}", file=sys.stderr, flush=True)

            return {"html": None, "message": "The guide can't be read."}

        return {"html": render_guide(text, down), "message": None}

    def get(self, conn, path: str, query: str, ctx: dict, cookie: tuple[str, str]) -> None:
        now = datetime.now(UTC)

        if path == "/":
            with conn, conn.cursor() as cur:
                rows = fetch(cur, CHARACTERS_SQL + " WHERE c.archived_at IS NULL ORDER BY c.sort NULLS LAST, c.name",
                             {"hours": PLAYING_HOURS})
                targets = next_targets(cur)
                nerd = nerd_stats(cur)

            characters = [summary(row, targets, now) for row in rows]
            characters.sort(key=lambda c: not c["playing"])
            playing = characters[0] if characters and characters[0]["playing"] else None

            routes = nerd["export"].get("routes", {})

            for c in characters:
                c["slots"] = slots(c["data"]["bags"], c["data"].get("bag_slots"))

            return self.page(conn, 200, "index.html", ctx, cookie, nerd, characters=characters, playing=playing,
                             routes=[(kind, routes[kind]) for kind in ROUTE_KINDS if routes.get(kind)],
                             plan=mail_plan(characters, routes),
                             account=account_summary(characters),
                             next_up=next_up(characters), cards=card_keys(characters))

        if path == "/search":
            q = parse_qs(query).get("q", [""])[0].strip()
            results = None

            if q:
                with conn, conn.cursor() as cur:
                    results = found(fetch(cur, SEARCH_SQL, {"pattern": like_pattern(q)}))

            return self.page(conn, 200, "search.html", ctx, cookie, q=q, results=results)

        if path == "/stats":
            key = stats.range_key(parse_qs(query).get("range", [None])[0])

            with conn, conn.cursor() as cur:
                # The charts change with an ingest (a new file), a character's role or name, and the day.
                cur.execute("SELECT max(id) FROM pinkas_files")
                (newest,) = cur.fetchone()
                cur.execute(stats.CHARACTERS_SQL)
                mark = (newest, now.astimezone().date(), tuple(cur.fetchall()))
                cached = self.server.stats_cache.get(key)

                if cached and cached[0] == mark:
                    view = cached[1]
                else:
                    view = stats.view(stats.collect(cur, key, now))
                    self.server.stats_cache[key] = (mark, view)

                nerd = nerd_stats(cur)

            return self.page(conn, 200, "stats.html", ctx, cookie, nerd, v=view)

        if path == "/guide":
            return self.page(conn, 200, "guide.html", ctx, cookie, **self.guide(down=1))

        if path == "/settings":
            with conn, conn.cursor() as cur:
                characters = settings_rows(cur)

            return self.page(conn, 200, "settings.html", ctx, cookie, characters=characters, skills=TARGET_SKILLS)

        # Split before unquoting, so a name holding "/" (sent as %2F) stays one segment.
        parts = path.split("/")

        if len(parts) == 4 and parts[1] == "c":
            realm, name = unquote(parts[2]), unquote(parts[3])

            with conn, conn.cursor() as cur:
                rows = fetch(cur, CHARACTERS_SQL + " WHERE c.realm = %(realm)s AND c.name = %(name)s",
                             {"hours": PLAYING_HOURS, "realm": realm, "name": name})
                targets = next_targets(cur)
                learned = {p["skill_line"] for p in rows[0]["data"]["professions"]} if rows else set()
                unlearned = {line: skills[-1] for (cid, line), skills in targets.items()
                             if rows and cid == rows[0]["id"] and line not in learned}
                # Every character's newest snapshot is read only when a name is needed.
                known = professions(cur) if unlearned else {}
                cur.execute("SELECT name FROM pinkas_characters WHERE realm = %s", (realm,))
                alts = {name.casefold(): name for (name,) in cur.fetchall()}
                history = stats.history(cur, rows[0]["id"], now) if rows else None

            if rows:
                row = summary(rows[0], targets, now)
                data = row["data"]
                planned = sorted(
                    ({"name": known.get(line, f"profession {line}"), "upto": upto} for line, upto in unlearned.items()),
                    key=lambda p: p["name"],
                )

                routes = data.get("export", {}).get("routes", {})
                bank = data["bank"]
                bank_slots = slots(bank["items"], bank.get("slots")) if bank else None
                bank_full = bool(bank_slots and bank_slots["total"] and bank_slots["used"] >= bank_slots["total"])
                bag_groups = grouped(data["bags"], routes, row["name"], in_bank=False, bank_full=bank_full)
                bank_groups = grouped(bank["items"], routes, row["name"], in_bank=True, bank_full=False) if bank else None

                # A recipient that is a character of this realm gets a link to its page.
                for group in bag_groups + (bank_groups or []):
                    group["who"] = alts.get(own_name(group["to"])) if group["to"] else None

                return self.page(
                    conn, 200, "character.html", ctx, cookie, c=row, data=data, planned=planned, **self.guide(down=1),
                    bags=bag_groups, bag_slots=slots(data["bags"], data.get("bag_slots")),
                    bank=bank_groups, bank_slots=bank_slots, history=history,
                    raw=json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True),
                )

        self.not_found(conn, ctx, cookie)

    def post(self, conn, path: str, ctx: dict, cookie: tuple[str, str]) -> None:
        form = self.read_form()

        if not same(form.get("t", [""])[0], ctx["token"]):
            return self.page(conn, 400, "error.html", ctx, cookie, message="Stale form: reload and try again.")

        if path == "/playing":
            chosen = form.get("character", [""])[0]

            if chosen == "" or (chosen.isascii() and chosen.isdigit()):
                if set_playing(conn, int(chosen) if chosen else None):
                    return self.redirect("/", cookie)

            return self.page(conn, 400, "error.html", ctx, cookie, message="Unknown character.")

        if path == "/settings":
            if save_settings(conn, form):
                return self.redirect("/settings", cookie)

            return self.page(conn, 400, "error.html", ctx, cookie, message="Not saved: a value is out of range.")

        self.not_found(conn, ctx, cookie)


def main(argv: list[str]) -> int:
    if argv == ["--hash-password"]:
        password = getpass.getpass("password: ")

        if not password or password != getpass.getpass("again: "):
            raise SystemExit("empty, or the two differ")

        print(hash_password(password))

        return 0

    conf = load_config(os.environ)
    host, port = listen_address(os.environ.get("PINKAS_LISTEN", "127.0.0.1:8094"))
    server = http.server.ThreadingHTTPServer((host, port), Handler)
    server.conf = conf
    # The /stats view per range, until the data behind it changes.
    server.stats_cache = {}
    print(f"pinkas: listening on {host}:{port}", flush=True)
    server.serve_forever()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
