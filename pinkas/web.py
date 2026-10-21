"""The page: every character at a glance, behind one password.

    python3 -m pinkas.web                  serve
    python3 -m pinkas.web --hash-password  print a hash for PINKAS_PASSWORD_HASH

Config comes from the environment: DATABASE_URL (the app role), SESSION_SECRET,
PINKAS_PASSWORD_HASH, and optionally PINKAS_LISTEN (host:port, default 127.0.0.1:8094).

It speaks plain HTTP and expects a TLS proxy in front, the only client that can reach its address.

Everything shown comes from the game or from the pulled file, so every template is autoescaped and
no page runs a script.
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
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, quote, unquote, urlparse

import psycopg2
from jinja2 import Environment, FileSystemLoader

from pinkas import rested
from pinkas.ingest import ts
from pinkas.parse import ROUTE_KINDS, ParseError

TEMPLATES = Environment(loader=FileSystemLoader(Path(__file__).resolve().parent.parent / "templates"), autoescape=True)
COOKIE = "pinkas"
COOKIE_MAX_AGE = 30 * 86400
PLAYING_HOURS = 12
FORM_LIMIT = 8192
# The whole string must match: no "//" or scheme means no off-site redirect, and the charset keeps a
# decoded CR/LF out of the Location header.
SEGMENT = r"[A-Za-z0-9_.~%-]+"
NEXT_RE = re.compile(rf"/(?:c/{SEGMENT}/{SEGMENT})?")
HEADERS = (
    (
        "Content-Security-Policy",
        # The skin is an inline <style>: sync.py keeps it inside templates/base.html.
        "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src data:;"
        " form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    # Back after "log out" must not show the page.
    ("Cache-Control", "no-store"),
    # Honoured only over HTTPS (through the proxy); then the browser refuses plain HTTP for a year.
    ("Strict-Transport-Security", "max-age=31536000"),
)
# Every interface would put the plain-HTTP port on the network, around the proxy.
WILDCARD_HOSTS = ("", "0.0.0.0", "::", "[::]")


class Config(NamedTuple):
    dsn: str
    secret: str
    pw_hash: str


def load_config(env) -> Config:
    """Names a missing variable, never a value."""
    names = ("DATABASE_URL", "SESSION_SECRET", "PINKAS_PASSWORD_HASH")
    missing = [name for name in names if not env.get(name)]

    if missing:
        raise SystemExit(f"missing from the environment: {', '.join(missing)}")

    return Config(*(env[name] for name in names))


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
    if copper is None:
        return "?"

    return f"{copper // 10000}g {copper // 100 % 100}s {copper % 100}c"


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


def duration(seconds: float) -> str:
    if seconds < 86400:
        return f"{math.ceil(seconds / 3600)} h"

    return f"{math.ceil(seconds / 86400)} d"


def local(moment: datetime | None) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z") if moment else "—"


TEMPLATES.filters.update(pathpart=pathpart, gold=gold, epoch=epoch, ago=ago, duration=duration, local=local)
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


def fetch(cur, sql: str, args=()) -> list[dict]:
    cur.execute(sql, args)
    names = [d.name for d in cur.description]

    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


# The newest snapshot per character, walking the (character_id, logout_at) unique index.
CHARACTERS_SQL = """
SELECT c.id, c.realm, c.name, c.well_rested_rank, s.logout_at, s.data,
       coalesce(c.playing_since > now() - make_interval(hours => %(hours)s)
                AND NOT EXISTS (SELECT 1 FROM pinkas_snapshots o
                                WHERE o.character_id <> c.id AND o.session_started_at > c.playing_since),
                false) AS playing
FROM pinkas_characters c
CROSS JOIN LATERAL (SELECT logout_at, data FROM pinkas_snapshots
                    WHERE character_id = c.id ORDER BY logout_at DESC LIMIT 1) s
"""

# The newest snapshot overall is the newest of each character's, so the unique index serves it.
NERD_SQL = """
SELECT p.status, p.since, f.sha256, f.size, f.fetched_at, s.export,
       (SELECT count(*) FROM pinkas_snapshots) AS snapshots, (SELECT count(*) FROM pinkas_events) AS events
FROM (SELECT 1) one
LEFT JOIN pinkas_pull_state p ON true
LEFT JOIN LATERAL (SELECT sha256, octet_length(body) AS size, fetched_at FROM pinkas_files
                   ORDER BY fetched_at DESC LIMIT 1) f ON true
LEFT JOIN LATERAL (SELECT n.export FROM pinkas_characters c
                   CROSS JOIN LATERAL (SELECT logout_at, data->'export' AS export FROM pinkas_snapshots
                                       WHERE character_id = c.id ORDER BY logout_at DESC LIMIT 1) n
                   ORDER BY n.logout_at DESC LIMIT 1) s ON true
"""


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
    professions = [
        {**p, "target": next((s for s in targets.get((row["id"], p["skill_line"]), []) if s > (p["rank"] or 0)), None)}
        for p in data["professions"]
    ]

    return {
        **row,
        "class": data["class"],
        "level": level,
        "money": data["money"],
        "rested": current,
        "cap": rested.rate_and_cap(xp_max, resting, rank)[1] if xp_max else None,
        "bar_pct": round(100 * current / xp_max) if current is not None and xp_max else None,
        "full_in": rested.full_in(level, xp_max, current, resting, rank),
        "professions": professions,
    }


def mail_routes(export: dict, playing: dict | None) -> list[tuple[str, str]]:
    """The account's routes, minus one pointing at the playing character itself.

    shortcut: compares the name before any "-Realm" and ignores the realm; enough while every alt
    shares one realm.
    """
    if playing is None:
        return []

    routes = export.get("routes", {})
    own = playing["name"].casefold()

    return [
        (kind, routes[kind]) for kind in ROUTE_KINDS
        if routes.get(kind) and routes[kind].split("-", 1)[0].casefold() != own
    ]


def summed(items: list[dict]) -> list[dict]:
    """Stacks of the same item as one line, by name."""
    counts: dict[tuple, int] = {}

    for item in items:
        key = (item["name"] or "", item["id"])
        counts[key] = counts.get(key, 0) + item["count"]

    return [{"name": name, "id": item_id, "count": count} for (name, item_id), count in sorted(counts.items())]


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

    def log_request(self, code="-", size="-") -> None:
        # The method and status only: a path names a character, and the log keeps no file content.
        self.log_message("%s %s", self.command, code)

    def end_headers(self) -> None:
        # Here, so the server's own error responses (a bad request line, an unknown method) get them too.
        for key, value in HEADERS:
            self.send_header(key, value)

        super().end_headers()

    def send(self, code: int, body: str = "", headers: tuple[tuple[str, str], ...] = ()) -> None:
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))

        for key, value in headers:
            self.send_header(key, value)

        self.end_headers()

        if self.command != "HEAD":
            self.wfile.write(payload)

    def redirect(self, location: str, *headers: tuple[str, str]) -> None:
        # Callers build `location` from fixed paths or safe_next(), never from the raw request.
        self.send(303, "", (*headers, ("Location", location)))

    def read_form(self) -> dict[str, list[str]]:
        try:
            # max(): a negative Content-Length would make read() block until the client hangs up.
            length = max(0, min(int(self.headers.get("Content-Length") or 0), FORM_LIMIT))

            return parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
        except (TypeError, ValueError):
            return {}

    def do_GET(self) -> None:
        self.respond(post=False)

    do_HEAD = do_GET

    def do_POST(self) -> None:
        self.respond(post=True)

    def respond(self, post: bool) -> None:
        conf: Config = self.server.conf
        url = urlparse(self.path)

        if url.path == "/login":
            return self.login(conf) if post else self.login_page(url)

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
                self.get(conn, url.path, ctx, cookie)
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

    def get(self, conn, path: str, ctx: dict, cookie: tuple[str, str]) -> None:
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

            return self.page(conn, 200, "index.html", ctx, cookie, nerd, characters=characters, playing=playing,
                             routes=mail_routes(nerd["export"], playing))

        # Split before unquoting, so a name holding "/" (sent as %2F) stays one segment.
        parts = path.split("/")

        if len(parts) == 4 and parts[1] == "c":
            realm, name = unquote(parts[2]), unquote(parts[3])

            with conn, conn.cursor() as cur:
                rows = fetch(cur, CHARACTERS_SQL + " WHERE c.realm = %(realm)s AND c.name = %(name)s",
                             {"hours": PLAYING_HOURS, "realm": realm, "name": name})

            if rows:
                row = rows[0]
                data = row["data"]

                return self.page(
                    conn, 200, "character.html", ctx, cookie, c=row, data=data, bags=summed(data["bags"]),
                    bank=summed(data["bank"]["items"]) if data["bank"] else None,
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
    print(f"pinkas: listening on {host}:{port}", flush=True)
    server.serve_forever()

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
