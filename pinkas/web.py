"""The page: every character at a glance, behind one password.

    python3 -m pinkas.web                  serve
    python3 -m pinkas.web --hash-password  print a hash for PINKAS_PASSWORD_HASH

Config comes from the environment: DATABASE_URL (the app role), SESSION_SECRET,
PINKAS_PASSWORD_HASH, and optionally PINKAS_LISTEN (host:port, default 127.0.0.1:8094) and
PINKAS_GUIDE (a markdown file shown at /guide).

It speaks plain HTTP and expects a TLS proxy in front, the only client that can reach its address.
The Windows app (pinkas.desktop) serves the same pages through desktop_server: loopback only, its own
Host header only, and a one-time launch token from its window instead of the password.

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
import threading
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
from pinkas.parse import KIND_LABELS, KIND_OF, ROUTE_KINDS, TEXT_LIMIT, ParseError

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)
SCRIPT_PATH = "/static/pinkas.js"
SCRIPT = (ROOT / "static" / "pinkas.js").read_text(encoding="utf-8")
SCRIPT_ETAG = f'"{hashlib.sha256(SCRIPT.encode()).hexdigest()[:16]}"'
EDITOR = (ROOT / "static" / "vendor" / "tiny-mde.tiny.js").read_text(encoding="utf-8")
# Every file the app serves without a session: neither holds data.
STATIC = {
    SCRIPT_PATH: (SCRIPT, SCRIPT_ETAG),
    "/static/tiny-mde.js": (EDITOR, f'"{hashlib.sha256(EDITOR.encode()).hexdigest()[:16]}"'),
}
COOKIE = "pinkas"
COOKIE_MAX_AGE = 30 * 86400
PLAYING_HOURS = 12
FORM_LIMIT = 8192
# The guide editor's cap in UTF-8 bytes, as migration 0004's check.
GUIDE_LIMIT = 262144
# Room for the text encoded three times over (%XX): one over the cap comes back in the editor, not lost.
GUIDE_FORM_LIMIT = 4 * GUIDE_LIMIT
# The whole string must match: no "//" or scheme means no off-site redirect, and the charset keeps a
# decoded CR/LF out of the Location header.
SEGMENT = r"[A-Za-z0-9_.~%-]+"
NEXT_RE = re.compile(rf"/(?:c/{SEGMENT}/{SEGMENT}|search|guide|guide/edit|stats|settings(?:/game|/routes|/characters)?)?")
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
OWN_WINDOW = "Open pinkas from its own window."
# What a page says after a POST redirects to it with ?done=<key>: a fixed list, each on the one page its POST
# redirects to, so no text comes from the URL and no page shows another's message.
DONE = {
    "saved": ("/settings/characters", "ok", "Saved."),
    "chosen": ("/settings/game", "ok", "Game folder saved."),
    "not-game": ("/settings/game", "error",
                 "Not saved: that is not a game folder. Choose the one named _classic_ (or _retail_) inside World of Warcraft."),
    "no-window": ("/settings/game", "error",
                  "Failed: choosing a folder needs pinkas's own window, which needs Microsoft Edge WebView2."
                  " Install it from Microsoft, then start pinkas again."),
    "installed": ("/settings/game", "ok", "Addon {v} installed. Restart the game to load it."),
    "updated": ("/settings/game", "ok", "Addon updated to {v}. Type /reload in the game to load it."),
    "failed": ("/settings/game", "error",
               "Failed: Windows didn't let pinkas write into the game folder. Close the game and try again."),
    "refused": ("/settings/game", "error",
                "Not installed: the addon's folder in the game is a link, perhaps made by an addon manager, and pinkas"
                " won't write through it. Remove the link, then install again."),
    "guide": ("/guide", "ok", "Guide saved."),
    "routes": ("/settings/routes", "ok", "Routes saved. Type /reload in the game to use them."),
    "routes-later": ("/settings/routes", "ok",
                     "Routes saved. They reach the game with the addon: install or update it on the Game tab."),
    "routes-refused": ("/settings/routes", "error",
                       "Routes saved, but not written into the game: the addon's folder is a link, and pinkas won't"
                       " write through it. Remove the link, then install the addon on the Game tab."),
    "routes-failed": ("/settings/routes", "error",
                      "Routes saved, but Windows didn't let pinkas write them into the game's addon. Close the game,"
                      " then save again."),
}
# The profession that crafts with each route kind's materials, shown as a hint beside its recipient.
CRAFTS = {"herb": (171, "Alchemy"), "cloth": (197, "Tailoring"), "leather": (165, "Leatherworking"),
          "ore": (164, "Blacksmithing"), "enchanting": (333, "Enchanting")}


class Config(NamedTuple):
    dsn: str
    secret: str
    pw_hash: str
    guide: str = ""
    # The Windows app: the one Host header it answers; no password, a launch token instead (desktop_server).
    desktop_host: str = ""


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

    # The app's own window over plain HTTP: no Secure, and the cookie ends with the app, as its secret does.
    if conf.desktop_host:
        return f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict"

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


def item_kind(item: dict) -> str | None:
    return KIND_OF.get((item.get("class"), item.get("subclass")))


def grouped(items: list[dict], routes: dict, character: dict, in_bank: bool, bank_full: bool) -> list[dict]:
    """Items as the mail plan sees them: routed materials going to another character, materials whose
    route is this character (kept in the bank), junk for a vendor, and the rest. `slots` counts stacks."""
    groups: dict[tuple, dict] = {}

    for item in items:
        kind = item_kind(item)
        recipient = routes.get(kind) if kind else None

        if recipient and not is_recipient(character, recipient):
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
    plan = []

    for c in characters:
        data = c["data"]
        items = data["bags"] + (data["bank"]["items"] if data["bank"] else [])
        sends: dict[str, int] = {}

        for group in grouped(items, routes, c, in_bank=False, bank_full=False):
            if group["to"]:
                sends[group["to"]] = sends.get(group["to"], 0) + group["slots"]

        if sends:
            ordered = sorted(sends.items(), key=lambda s: -s[1])
            # A recipient typed without a realm is on the sender's, as the game reads it.
            near = sorted(characters, key=lambda other: other["realm"] != c["realm"])
            plan.append({"character": c, "sends": [(to, recipient_of(near, to), n) for to, n in ordered]})

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


def render_guide(text: str, down: int = 0, breaks: bool = False) -> str:
    """Raw HTML in the file becomes text, and markdown-it's link check drops javascript: and data: URLs.
    `down` moves every heading that many levels down, for a page that has its own h1. `breaks` keeps every line
    a line, for a guide written in the app's editor rather than as markdown.
    Built per call: sharing one parser across the server's threads is not documented as safe."""
    md = MarkdownIt("commonmark", {"html": False, "breaks": breaks}).enable("table")
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


def route_choices(cur) -> list[dict]:
    """The characters a route can go to: every one not archived, and any archived one a route still names."""
    rows = fetch(
        cur,
        f"SELECT c.id, c.realm, c.name, s.data->'professions' AS professions FROM pinkas_characters c {NEWEST}"
        " WHERE c.archived_at IS NULL OR c.id IN (SELECT character_id FROM pinkas_routes) ORDER BY c.realm, c.name",
    )

    # The export carries the route back as Name-Realm, and the pull refuses a longer one: only a forged name gets here.
    return [c for c in rows if len(c["name"]) + 1 + len(plain_realm(c["realm"])) <= TEXT_LIMIT]


def plain_realm(realm: str) -> str:
    """A realm as the game writes it after a name: without its spaces."""
    return re.sub(r"\s", "", realm)


def is_recipient(character: dict, to: str) -> bool:
    """Whether a route's recipient, "Name" or "Name-Realm" in any case, is this character."""
    name, _, realm = to.partition("-")

    return character["name"].casefold() == name.casefold() and (
        not realm or plain_realm(character["realm"]).casefold() == plain_realm(realm).casefold())


def recipient_of(characters: list[dict], to: str) -> dict | None:
    """The character a route's recipient names, the first one when it gives no realm and several share the name."""
    return next((c for c in characters if is_recipient(c, to)), None)


def route_recipients(cur) -> dict[str, str]:
    """Each route's recipient as the addon takes it: Name-Realm."""
    cur.execute("SELECT r.kind, c.name, c.realm FROM pinkas_routes r JOIN pinkas_characters c ON c.id = r.character_id")

    return {kind: name + "-" + plain_realm(realm) for kind, name, realm in cur.fetchall()}


def store_routes(conn, form: dict[str, list[str]]) -> bool:
    """Every kind's field is checked before anything is written; False means nothing was."""
    if any(key.startswith("route-") and key.removeprefix("route-") not in ROUTE_KINDS for key in form):
        return False

    picks = {kind: form.get(f"route-{kind}", [None])[0] for kind in ROUTE_KINDS}

    if not all(pick == "" or (pick and pick.isascii() and pick.isdigit()) for pick in picks.values()):
        return False

    with conn, conn.cursor() as cur:
        if not {int(p) for p in picks.values() if p} <= {c["id"] for c in route_choices(cur)}:
            return False

        cur.execute("DELETE FROM pinkas_routes WHERE kind = ANY(%s)", ([k for k, p in picks.items() if not p],))

        for kind, pick in picks.items():
            if pick:
                cur.execute(
                    "INSERT INTO pinkas_routes (kind, character_id) VALUES (%s, %s)"
                    " ON CONFLICT (kind) DO UPDATE SET character_id = EXCLUDED.character_id",
                    (kind, int(pick)),
                )

    return True


def read_recipients(conn):
    """The routes' reader for the Windows app, which calls it while it holds the addon's lock."""
    def read() -> dict[str, str]:
        with conn, conn.cursor() as cur:
            return route_recipients(cur)

    return read


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
        limit = GUIDE_FORM_LIMIT if urlparse(self.path).path == "/guide/edit" else FORM_LIMIT

        # Refused whole, never cut: a form missing a field would save that field as cleared.
        if self.form_length() > limit:
            return self.send(413, render("error.html", message="Form too large."))

        self.respond(post=True)

    def respond(self, post: bool) -> None:
        # Per request: a keep-alive connection must not carry the script's no-cache onto a page.
        self.cache_control = Handler.cache_control
        conf: Config = self.server.conf
        url = urlparse(self.path)

        if conf.desktop_host:
            # Any other name is a page elsewhere reaching this port through DNS rebinding.
            if self.headers.get("Host") != conf.desktop_host:
                return self.send(400, render("error.html", message="Bad request."))

            if url.path == "/start" and not post:
                return self.start(conf, url)

            if url.path in ("/login", "/logout"):
                return self.send(404, render("error.html", message="Not found."))

        if url.path == "/login":
            return self.login(conf) if post else self.login_page(url)

        # Before the session check: the scripts hold no data, and a redirect in their place would be refused.
        if url.path in STATIC and not post:
            body, etag = STATIC[url.path]
            self.cache_control = "no-cache"

            if self.headers.get("If-None-Match") == etag:
                return self.send(304, headers=(("ETag", etag),))

            return self.send(200, body, (("ETag", etag),), kind="text/javascript")

        if post and url.path == "/logout":
            if not same(self.read_form().get("t", [""])[0], form_token(conf)):
                return self.send(400, render("error.html", message="Stale form: reload and try again."))

            return self.redirect("/login", ("Set-Cookie", LOGOUT_COOKIE))

        if not valid_session(conf, self.headers.get("Cookie"), time.time()):
            if conf.desktop_host:
                return self.send(403, render("error.html", message=OWN_WINDOW))

            # A POST can't be replayed after login, so it lands on the list instead.
            target = "/" if post else safe_next(url.path)

            return self.redirect("/login?next=" + quote(target, safe="/"))

        cookie = ("Set-Cookie", session_cookie(conf, time.time()))
        conn = None

        try:
            conn = connect(conf.dsn)
            ctx = {"token": form_token(conf), "desktop": bool(conf.desktop_host), "flash": None}
            done = DONE.get(parse_qs(url.query).get("done", [""])[0])

            if done and not post and done[0] == url.path:
                ctx["flash"] = done[1:]

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

    def start(self, conf: Config, url) -> None:
        """The window's first request: its token, used once, becomes the session cookie."""
        given = parse_qs(url.query).get("token", [""])[0]

        with self.server.launch_lock:
            expected = self.server.launch_token
            valid = expected is not None and same(given, expected)

            if valid:
                self.server.launch_token = None

        if not valid:
            return self.send(403, render("error.html", message=OWN_WINDOW))

        self.redirect(self.server.first_page, ("Set-Cookie", session_cookie(conf, time.time())))

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

    def guide(self, conn, down: int = 0) -> dict:
        """The guide's context: the file PINKAS_GUIDE names, else the one written in the app's editor."""
        path = self.server.conf.guide

        if not path:
            text = saved_guide(conn)
            html = render_guide(text, down, breaks=True) if text.strip() else None

            return {"html": html, "message": None, "editable": True}

        # shortcut: no size cap; the file is the owner's own. Cap it if the path ever comes from elsewhere.
        try:
            text = Path(path).read_text(encoding="utf-8")
        # The class only: the message names the path.
        except (OSError, UnicodeDecodeError) as exc:
            print(f"pinkas: guide {type(exc).__name__}", file=sys.stderr, flush=True)

            return {"html": None, "message": "The guide can't be read.", "editable": False}

        return {"html": render_guide(text, down), "message": None, "editable": False}

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

            # A recipient that is a listed character by its name, as the mail plan shows it.
            return self.page(conn, 200, "index.html", ctx, cookie, nerd, characters=characters, playing=playing,
                             routes=[(kind, (recipient_of(characters, routes[kind]) or {"name": routes[kind]})["name"])
                                     for kind in ROUTE_KINDS if routes.get(kind)],
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
            return self.page(conn, 200, "guide.html", ctx, cookie, **self.guide(conn, down=1))

        # A file named by PINKAS_GUIDE is edited where it lives.
        if path == "/guide/edit" and not self.server.conf.guide:
            return self.page(conn, 200, "guide_edit.html", ctx, cookie, text=saved_guide(conn), error=None)

        if path == "/settings":
            return self.redirect("/settings/game" if self.server.conf.desktop_host else "/settings/routes")

        if path == "/settings/game" and self.server.conf.desktop_host:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT EXISTS (SELECT 1 FROM pinkas_characters)")
                (any_character,) = cur.fetchone()

            app = self.server.app
            game, addon, files = app.game(), app.addon(), app.files()
            # The first run's checklist, until a character is read; a folder lost later is the missing state instead.
            setup = None if any_character else {"game": game, "addon": addon["installed"]}
            done = ctx["flash"]

            if done and "{v}" in done[1]:
                ctx["flash"] = (done[0], done[1].format(v=addon["newest"]))

            return self.page(conn, 200, "settings_game.html", ctx, cookie, tab="game", game=game, files=files,
                             addon=addon, candidates=[] if game else app.candidates(), setup=setup,
                             missing=app.missing())

        if path == "/settings/routes" and self.server.conf.desktop_host:
            with conn, conn.cursor() as cur:
                choices = route_choices(cur)
                cur.execute("SELECT kind, character_id FROM pinkas_routes")
                picked = dict(cur.fetchall())
                nerd = nerd_stats(cur)

            # None ever set here: the game's routes apply, so the form starts from them where a route names one character.
            from_game = not picked and not self.server.app.routes_set_here()

            if from_game:
                for kind, to in nerd["export"].get("routes", {}).items():
                    matches = [c["id"] for c in choices if to and is_recipient(c, to)]

                    if len(matches) == 1:
                        picked[kind] = matches[0]

            routes = []

            for kind in ROUTE_KINDS:
                line, craft = CRAFTS[kind]
                ranks = sorted(((p["rank"], c["name"]) for c in choices for p in c["professions"]
                                if p["skill_line"] == line and p["rank"]), reverse=True)
                hint = ", ".join(f"{name} {rank}" for rank, name in ranks) or "nobody has it yet"
                routes.append({"kind": kind, "label": KIND_LABELS[kind], "picked": picked.get(kind), "hint": f"{craft}: {hint}"})

            realms = {}

            for c in choices:
                realms.setdefault(c["realm"], []).append(c)

            return self.page(conn, 200, "settings_routes.html", ctx, cookie, nerd, tab="routes", routes=routes,
                             realms=realms, from_game=from_game and bool(picked))

        if path == "/settings/routes":
            with conn, conn.cursor() as cur:
                nerd = nerd_stats(cur)

            routes = nerd["export"].get("routes", {})

            return self.page(conn, 200, "settings_routes.html", ctx, cookie, nerd, tab="routes",
                             routes=[(KIND_LABELS[kind], routes.get(kind)) for kind in ROUTE_KINDS])

        if path == "/settings/characters":
            with conn, conn.cursor() as cur:
                characters = settings_rows(cur)

            return self.page(conn, 200, "settings_characters.html", ctx, cookie, tab="characters",
                             characters=characters, skills=TARGET_SKILLS)

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
                # This realm's first, for a recipient typed without one.
                alts = fetch(cur, "SELECT name, realm FROM pinkas_characters ORDER BY realm <> %(realm)s",
                             {"realm": realm})
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
                bag_groups = grouped(data["bags"], routes, row, in_bank=False, bank_full=bank_full)
                bank_groups = grouped(bank["items"], routes, row, in_bank=True, bank_full=False) if bank else None

                # A recipient that is one of the characters gets a link to its page.
                for group in bag_groups + (bank_groups or []):
                    group["who"] = recipient_of(alts, group["to"]) if group["to"] else None

                return self.page(
                    conn, 200, "character.html", ctx, cookie, c=row, data=data, planned=planned, **self.guide(conn, down=1),
                    bags=bag_groups, bag_slots=slots(data["bags"], data.get("bag_slots")),
                    bank=bank_groups, bank_slots=bank_slots, history=history,
                    raw=json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True),
                )

        self.not_found(conn, ctx, cookie)

    def post(self, conn, path: str, ctx: dict, cookie: tuple[str, str]) -> None:
        form = self.read_form()

        if not same(form.get("t", [""])[0], ctx["token"]):
            # The guide's text comes back in the editor: an error page would lose it.
            if path == "/guide/edit" and not self.server.conf.guide:
                return self.page(conn, 400, "guide_edit.html", ctx, cookie, text=form.get("text", [""])[0],
                                 error="Not saved: the page was open too long. Press Save guide again.")

            return self.page(conn, 400, "error.html", ctx, cookie, message="Stale form: reload and try again.")

        if path == "/playing":
            chosen = form.get("character", [""])[0]

            if chosen == "" or (chosen.isascii() and chosen.isdigit()):
                if set_playing(conn, int(chosen) if chosen else None):
                    return self.redirect("/", cookie)

            return self.page(conn, 400, "error.html", ctx, cookie, message="Unknown character.")

        if path == "/settings/characters":
            if save_settings(conn, form):
                return self.redirect("/settings/characters?done=saved", cookie)

            return self.page(conn, 400, "error.html", ctx, cookie, message="Not saved: a value is out of range.")

        if path == "/guide/edit" and not self.server.conf.guide:
            # Postgres can't store a NUL, and one never belongs in notes.
            text = form.get("text", [""])[0].replace("\r\n", "\n").replace("\x00", "")

            if len(text.encode()) > GUIDE_LIMIT:
                return self.page(conn, 413, "guide_edit.html", ctx, cookie, text=text,
                                 error="Not saved: the guide is over 256 KB. Shorten it, then save again.")

            save_guide(conn, text)

            return self.redirect("/guide?done=guide", cookie)

        if path == "/settings/game" and self.server.conf.desktop_host:
            app = self.server.app

            if "log" in form:
                app.open_log()

                return self.redirect("/settings/game", cookie)

            if "choose" in form:
                outcome = app.choose()

                return self.redirect("/settings/game" + ("" if outcome == "cancelled" else f"?done={outcome}"), cookie)

            pick = form.get("game", [""])[0]

            if pick.isascii() and pick.isdigit() and app.use(int(pick)):
                return self.redirect("/settings/game?done=chosen", cookie)

            return self.page(conn, 400, "error.html", ctx, cookie, message="Unknown game folder.")

        if path == "/settings/routes" and self.server.conf.desktop_host:
            if not store_routes(conn, form):
                return self.page(conn, 400, "error.html", ctx, cookie, message="Not saved: reload the page and try again.")

            return self.redirect(f"/settings/routes?done={self.server.app.write_routes(read_recipients(conn))}", cookie)

        if path == "/settings/addon" and self.server.conf.desktop_host:
            outcome = self.server.app.install(read_recipients(conn))

            # A folder gone since the page was shown: the page now says it is missing.
            return self.redirect("/settings/game" + ("" if outcome == "no-game" else f"?done={outcome}"), cookie)

        self.not_found(conn, ctx, cookie)


def saved_guide(conn) -> str:
    with conn, conn.cursor() as cur:
        cur.execute("SELECT text FROM pinkas_guide")
        row = cur.fetchone()

    return row[0] if row else ""


def save_guide(conn, text: str) -> None:
    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pinkas_guide (text, updated_at) VALUES (%s, now())"
            " ON CONFLICT (id) DO UPDATE SET text = excluded.text, updated_at = excluded.updated_at",
            (text,),
        )


def desktop_server(dsn: str, app, first_page: str = "/") -> tuple[http.server.ThreadingHTTPServer, str]:
    """The Windows app's server on a free loopback port, and the one-time URL its window opens.
    `app` is pinkas.desktop.App: the game folder, the files read, the addon."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host = f"127.0.0.1:{server.server_address[1]}"
    server.conf = Config(dsn, secrets.token_hex(32), "", desktop_host=host)
    server.app = app
    server.first_page = first_page
    server.stats_cache = {}
    server.launch_lock = threading.Lock()

    return server, launch_url(server)


def launch_url(server) -> str:
    """A new one-time URL for the window, or for the browser standing in for it; the previous token stops working."""
    with server.launch_lock:
        server.launch_token = secrets.token_urlsafe(32)

        return f"http://{server.conf.desktop_host}/start?token={server.launch_token}"


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
