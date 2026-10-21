import copy
import http.client
import http.server
import socket
import threading
import time
from datetime import UTC, datetime
from typing import NamedTuple
from urllib.parse import quote, urlencode

import psycopg2
import pytest

from pinkas import web
from pinkas.ingest import ingest
from tests.db import DSN
from tests.files import KEY, account, fixture, with_export

PASSWORD = "correct horse"
CONF = web.Config(DSN, "test-secret", web.hash_password(PASSWORD))
TOKEN = web.form_token(CONF)
FETCHED = datetime(2026, 10, 21, 10, 30, tzinfo=UTC)
NERD = "Stats for nerds"
XSS = "</script><img src=x onerror=alert(1)>"
XSS_ESCAPED = "&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;"


class Response(NamedTuple):
    status: int
    headers: http.client.HTTPMessage
    body: str


class Client:
    def __init__(self, port: int):
        self.port = port
        self.cookie: str | None = None

    def request(self, method: str, path: str, form: dict | None = None, cookie: str | None = None) -> Response:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}

        if cookie or self.cookie:
            headers["Cookie"] = cookie or self.cookie

        body = None

        if form is not None:
            body = urlencode(form)
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        result = Response(response.status, response.headers, response.read().decode())
        conn.close()

        return result

    def get(self, path: str, **kw) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, form: dict, **kw) -> Response:
        return self.request("POST", path, form, **kw)

    def login(self) -> None:
        response = self.post("/login", {"password": PASSWORD, "next": "/"})
        assert response.status == 303
        self.cookie = response.headers["Set-Cookie"].split(";")[0]


@pytest.fixture
def client(conn, monkeypatch):
    def as_app_role(dsn: str):
        c = psycopg2.connect(dsn)

        with c.cursor() as cur:
            cur.execute("SET ROLE pinkas")

        c.commit()

        return c

    monkeypatch.setattr(web, "connect", as_app_role)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    server.conf = CONF
    # A short poll, so shutdown() doesn't wait the default half second per test.
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    yield Client(server.server_address[1])
    server.shutdown()
    server.server_close()


@pytest.fixture
def logged_in(client):
    client.login()

    return client


def store(conn, export: dict) -> None:
    ingest(conn, with_export(export), FETCHED)


def with_alt(**alt_changes) -> dict:
    """The beta account plus a second character, Altchar."""
    export = account()
    alt = copy.deepcopy(export["chars"][KEY])
    alt.update(name="Altchar", level=20, xp_max=14000, rested=0, **alt_changes)
    export["chars"]["Testrealm/Altchar"] = alt

    return export


def ids(owner) -> dict[str, int]:
    with owner.cursor() as cur:
        cur.execute("SELECT name, id FROM pinkas_characters")

        return dict(cur.fetchall())


def playing(owner) -> list[str]:
    with owner.cursor() as cur:
        cur.execute("SELECT name FROM pinkas_characters WHERE playing_since IS NOT NULL")

        return [row[0] for row in cur.fetchall()]


# Login and session -------------------------------------------------------------------------------


def test_the_password_hash_round_trips():
    stored = web.hash_password("pw")

    assert "$" not in stored
    assert web.verify_password("pw", stored) and not web.verify_password("other", stored)
    assert not web.verify_password("pw", "garbage") and not web.verify_password("pw", "md5:1:1:1:00:00")


def test_a_wrong_password_sets_no_cookie(client):
    response = client.post("/login", {"password": "wrong"})

    assert response.status == 401 and "Set-Cookie" not in response.headers
    assert 'role="alert">Wrong password.' in response.body


def test_the_right_password_sets_a_locked_down_cookie(client):
    response = client.post("/login", {"password": PASSWORD, "next": "/c/Testrealm/Fakechar"})

    assert response.status == 303 and response.headers["Location"] == "/c/Testrealm/Fakechar"
    assert all(flag in response.headers["Set-Cookie"] for flag in ("Secure", "HttpOnly", "SameSite=Lax"))


def forged(sig_secret: str = CONF.secret, pw_hash: str = CONF.pw_hash, expiry: int | None = None) -> str:
    expiry = expiry or int(time.time()) + 600

    return f"pinkas={expiry}|{web.sign(sig_secret, f'session|{expiry}|{pw_hash}')}"


@pytest.mark.parametrize(
    "cookie",
    [
        None,
        "pinkas=9999999999|bad",
        forged(sig_secret="other secret"),
        forged(pw_hash=web.hash_password("an older password")),
        forged(expiry=int(time.time()) - 1),
        "pinkas=9999999999|zé",
        "pinkas=²|x",
        "pinkas=" + "9" * 5000 + "|x",
    ],
    ids=[
        "no cookie", "bad signature", "other secret", "old password", "expired", "non-ASCII", "superscript expiry",
        "huge expiry",
    ],
)
def test_without_a_valid_session_the_page_sends_you_to_login(client, cookie):
    response = client.get("/c/Testrealm/Fakechar", cookie=cookie)

    assert response.status == 303
    assert response.headers["Location"] == "/login?next=/c/Testrealm/Fakechar"
    assert response.body == ""


def test_a_forged_cookie_with_the_right_secret_works(client, conn):
    store(conn, account())

    assert client.get("/", cookie=forged()).status == 200


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/", "/"),
        ("/c/Testrealm/Fakechar", "/c/Testrealm/Fakechar"),
        ("/c/Zo%C3%AB/x", "/c/Zo%C3%AB/x"),
        ("//evil.example", "/"),
        ("http://evil.example/", "/"),
        ("/c/a/b\r\nSet-Cookie: x=1", "/"),
        ("/c/a", "/"),
        ("/elsewhere", "/"),
    ],
)
def test_safe_next(target, expected):
    assert web.safe_next(target) == expected


def test_logout_needs_the_form_token(logged_in):
    assert logged_in.post("/logout", {"t": "wrong"}).status == 400
    response = logged_in.post("/logout", {"t": TOKEN})

    assert response.status == 303 and response.headers["Set-Cookie"].startswith("pinkas=; Max-Age=0;")


# "Playing" ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("form", [{}, {"t": "wrong"}, {"t": "é"}], ids=["no token", "wrong token", "non-ASCII token"])
def test_a_post_without_the_form_token_changes_nothing(logged_in, conn, owner, form):
    store(conn, with_alt())
    response = logged_in.post("/playing", {**form, "character": ids(owner)["Altchar"]})

    assert response.status == 400 and playing(owner) == []


def test_one_character_plays_at_a_time(logged_in, conn, owner):
    store(conn, with_alt())
    known = ids(owner)

    assert logged_in.post("/playing", {"t": TOKEN, "character": known["Fakechar"]}).status == 303
    assert playing(owner) == ["Fakechar"]
    logged_in.post("/playing", {"t": TOKEN, "character": known["Altchar"]})
    assert playing(owner) == ["Altchar"]
    logged_in.post("/playing", {"t": TOKEN, "character": ""})
    assert playing(owner) == []


def test_two_devices_choosing_at_once_both_succeed(conn, owner):
    """One request holds the rows and picks Fakechar; the other, picking Altchar, waits and wins."""
    store(conn, with_alt())
    known = ids(owner)
    first = psycopg2.connect(DSN)
    second = psycopg2.connect(DSN)

    with second.cursor() as cur:
        cur.execute("SET ROLE pinkas")
    second.commit()
    errors = []

    def choose_altchar():
        try:
            web.set_playing(second, known["Altchar"])
        except psycopg2.Error as exc:
            errors.append(type(exc).__name__)

    with first.cursor() as cur:
        cur.execute("SELECT id FROM pinkas_characters FOR UPDATE")
        waiting = threading.Thread(target=choose_altchar)
        waiting.start()
        time.sleep(0.3)
        cur.execute("UPDATE pinkas_characters SET playing_since = now() WHERE id = %s", (known["Fakechar"],))

    first.commit()
    waiting.join(5)
    first.close()
    second.close()

    assert errors == [] and playing(owner) == ["Altchar"]


def test_an_archived_character_cannot_play(logged_in, conn, owner):
    store(conn, with_alt())

    with owner.cursor() as cur:
        cur.execute("UPDATE pinkas_characters SET archived_at = now() WHERE name = 'Altchar'")

    assert logged_in.post("/playing", {"t": TOKEN, "character": ids(owner)["Altchar"]}).status == 400
    assert playing(owner) == []


@pytest.mark.parametrize("chosen", ["999999", "abc", "²"])
def test_an_unknown_character_cannot_play(logged_in, conn, owner, chosen):
    store(conn, with_alt())

    assert logged_in.post("/playing", {"t": TOKEN, "character": chosen}).status == 400
    assert playing(owner) == []


def test_the_playing_character_sits_on_top_with_its_mail(logged_in, conn, owner):
    export = with_alt()
    # Days since the logout would have built rested, but the playing character is online.
    export["chars"][KEY].update(rested=0, logout_at=export["chars"][KEY]["logout_at"] - 3 * 86400)
    export["routes"] = {"herb": "Altchar", "cloth": "Fakechar-Testrealm", "ore": "Otheralt"}
    store(conn, export)
    logged_in.post("/playing", {"t": TOKEN, "character": ids(owner)["Fakechar"]})
    body = logged_in.get("/").body

    assert body.index("Playing Fakechar") < body.index(">Fakechar<") < body.index(">Altchar<"), "by name, Altchar leads"
    assert "herb → Altchar" in body and "ore → Otheralt" in body
    assert "cloth →" not in body, "a route to the playing character itself is hidden"
    assert "0% of a level, playing" in body


def set_playing_since(owner, name: str, sql_time: str) -> None:
    with owner.cursor() as cur:
        cur.execute(f"UPDATE pinkas_characters SET playing_since = {sql_time} WHERE name = %s", (name,))


def test_playing_ends_after_twelve_hours(logged_in, conn, owner):
    # Altchar's session started days ago, so only the age of playing_since can end it.
    store(conn, with_alt(session_started_at=account()["chars"][KEY]["session_started_at"] - 3 * 86400))
    set_playing_since(owner, "Fakechar", "now() - interval '13 hours'")

    assert "Playing" not in logged_in.get("/").body
    set_playing_since(owner, "Fakechar", "now() - interval '11 hours'")
    assert "Playing Fakechar" in logged_in.get("/").body


def test_playing_ends_when_another_character_starts_a_session(logged_in, conn, owner):
    store(conn, with_alt())
    logged_in.post("/playing", {"t": TOKEN, "character": ids(owner)["Fakechar"]})
    assert "Playing Fakechar" in logged_in.get("/").body
    later = int(time.time()) + 60
    export = with_alt(session_started_at=later, logout_at=later + 600)
    export["exported_at"] = later + 600
    store(conn, export)

    assert "Playing" not in logged_in.get("/").body


# Pages -------------------------------------------------------------------------------------------


def test_the_list_shows_every_character(logged_in, conn):
    store(conn, with_alt())
    body = logged_in.get("/").body

    assert ">Fakechar<" in body and ">Altchar<" in body
    assert 'aria-label="Play Altchar"' in body and 'aria-label="rested XP of Altchar"' in body
    assert "10g 16s 94c" in body and "Skinning 75/75" in body and "% of a level" in body


def test_an_empty_database_renders(logged_in):
    body = logged_in.get("/").body

    assert "No characters yet" in body and NERD in body and "none yet" in body


def test_a_character_page_sums_stacks_and_dates_bank_and_mail(logged_in, conn):
    export = account()
    export["chars"][KEY]["bank"] = {"as_of": 1792570000, "items": [{"id": 2589, "name": "Linen Cloth", "count": 5}]}
    export["chars"][KEY]["mailbox"] = {
        "as_of": 1792570000, "mails": [{"from": "Altchar", "money": 500, "items": []}],
    }
    store(conn, export)
    body = logged_in.get("/c/Testrealm/Fakechar").body

    assert "Linen Cloth<span class=\"count\">32</span>" in body, "20 + 12 in the bags"
    assert "Linen Cloth<span class=\"count\">5</span>" in body
    assert body.count(", as of") == 2 and "from Altchar, 0g 5s 0c" in body


def test_a_character_without_bank_or_mail_says_so(logged_in, conn):
    store(conn, account())

    assert logged_in.get("/c/Testrealm/Fakechar").body.count("Not seen yet") == 2


def test_a_time_out_of_range_shows_as_unknown(logged_in, conn):
    export = account()
    export["chars"][KEY]["bank"] = {"as_of": 10**12, "items": []}
    store(conn, export)
    response = logged_in.get("/c/Testrealm/Fakechar")

    assert response.status == 200 and "Bank, as of <span title=\"—\">?</span>" in response.body


def test_a_non_ascii_name_has_its_page(logged_in, conn):
    ingest(conn, fixture("fake_nonascii.lua"), FETCHED)
    body = logged_in.get("/").body

    assert 'href="/c/Testrealm/Zo%C3%AB"' in body
    assert logged_in.get("/c/Testrealm/Zo%C3%AB").status == 200


@pytest.mark.parametrize("path", ["/c/Testrealm/Nobody", "/c/Testrealm", "/nothing", "/c/a/b/c"])
def test_an_unknown_page_is_404(logged_in, conn, path):
    store(conn, account())

    assert logged_in.get(path).status == 404


# The nerd panel ----------------------------------------------------------------------------------


def test_the_nerd_panel_shows_after_login(logged_in, conn):
    ingest(conn, fixture("fake_account.lua"), FETCHED)
    index, character, missing = (logged_in.get(p) for p in ("/", "/c/Testrealm/Fakechar", "/nothing"))

    for response in (index, character, missing):
        assert NERD in response.body

    assert "1.60.1.70450" in index.body and "Fakechar 0" in index.body
    assert "&#34;xp_max&#34;: 7600" in character.body, "the raw snapshot, escaped"


def test_the_nerd_panel_is_never_shown_before_login(client, conn):
    ingest(conn, fixture("fake_account.lua"), FETCHED)
    responses = [
        client.get("/login"),
        client.post("/login", {"password": "wrong"}),
        client.get("/"),
        client.get("/c/Testrealm/Fakechar"),
        client.post("/playing", {"t": TOKEN, "character": ""}),
        client.post("/logout", {"t": "wrong"}),
    ]

    for response in responses:
        assert NERD not in response.body and "1.60.1.70450" not in response.body


# Escaping ----------------------------------------------------------------------------------------


def test_game_strings_stay_inert(logged_in, conn, owner):
    export = with_alt()
    char = export["chars"].pop(KEY)
    char.update(name=XSS, zone=XSS)
    char["bags"][0]["name"] = XSS
    char["professions"][0]["name"] = XSS
    char["bank"] = {"as_of": 1792570000, "items": [{"id": 1, "name": XSS, "count": 1}]}
    mail = {"from": XSS, "money": 0, "items": [{"id": 1, "name": XSS, "count": 1}]}
    char["mailbox"] = {"as_of": 1792570000, "mails": [mail]}
    char["events"] = [{"id": 1, "at": 1792570500, "kind": "death", "value": {"killer": XSS}}]
    export["chars"][f"Testrealm/{XSS}"] = char
    export["routes"] = {"herb": XSS}
    store(conn, export)
    logged_in.post("/playing", {"t": TOKEN, "character": ids(owner)["Altchar"]})

    index = logged_in.get("/").body
    character = logged_in.get(f"/c/Testrealm/{quote(XSS, safe='')}").body

    for body in (index, character):
        assert XSS not in body and "<img" not in body

    assert XSS_ESCAPED in index and f"herb → {XSS_ESCAPED}" in index
    assert f'href="/c/Testrealm/{quote(XSS, safe="")}"' in index, "the name's '/' is encoded too"
    assert character.count(XSS_ESCAPED) >= 6, "name, zone, bag, bank, mail sender and item, profession"
    assert character.split(NERD)[1].count(XSS_ESCAPED) >= 5, "the raw snapshot in the panel"


# Headers and failures ----------------------------------------------------------------------------


def test_every_response_carries_the_security_headers(logged_in, conn):
    store(conn, account())
    responses = [
        logged_in.get("/login"),
        logged_in.get("/"),
        logged_in.get("/nothing"),
        logged_in.post("/playing", {"t": TOKEN, "character": ""}),
        logged_in.request("PUT", "/"),
        Client(logged_in.port).get("/"),
    ]

    for response in responses:
        assert response.headers["Content-Security-Policy"].startswith("default-src 'none'; script-src 'self';")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Strict-Transport-Security"] == "max-age=31536000"

    assert responses[4].status == 501


def test_a_database_failure_names_only_the_class(logged_in, monkeypatch, capsys):
    def unreachable(dsn):
        return psycopg2.connect("postgresql://who:hunter2@127.0.0.1:1/nothing", connect_timeout=1)

    monkeypatch.setattr(web, "connect", unreachable)
    response = logged_in.get("/")
    err = capsys.readouterr().err

    assert response.status == 503 and "OperationalError" in err
    assert "hunter2" not in response.body + err and "127.0.0.1:1" not in response.body + err


def raw_post(port: int, path: str, headers: str, body: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(f"POST {path} HTTP/1.1\r\nHost: x\r\n{headers}\r\n".encode() + body)

        return sock.recv(65536)


def test_a_form_is_read_up_to_its_cap(client):
    body = b"x=" + b"a" * web.FORM_LIMIT + f"&password={PASSWORD}".encode()

    assert raw_post(client.port, "/login", f"Content-Length: {len(body)}\r\n", body).startswith(b"HTTP/1.0 401")


def test_a_negative_content_length_reads_nothing(client):
    assert raw_post(client.port, "/login", "Content-Length: -1\r\n", b"").startswith(b"HTTP/1.0 401")


def test_a_code_failure_is_not_blamed_on_the_database(logged_in, conn, monkeypatch, capsys):
    store(conn, account())

    def broken(*args):
        raise TypeError("a bug")

    monkeypatch.setattr(web, "summary", broken)
    response = logged_in.get("/")

    assert response.status == 500 and "Server error" in response.body
    assert "TypeError" in capsys.readouterr().err


def test_a_client_that_left_gets_no_second_answer(logged_in, monkeypatch, capsys):
    def gone(*args):
        raise ConnectionResetError

    monkeypatch.setattr(web.Handler, "get", gone)

    with pytest.raises(http.client.RemoteDisconnected):
        logged_in.get("/")

    assert "pinkas:" not in capsys.readouterr().err


def test_the_log_never_names_a_character(logged_in, conn, capsys):
    store(conn, account())
    logged_in.get("/c/Testrealm/Fakechar")
    err = capsys.readouterr().err

    assert "GET 200" in err and "Fakechar" not in err


def test_an_idle_client_is_dropped(client, monkeypatch):
    assert web.Handler.timeout == 30
    monkeypatch.setattr(web.Handler, "timeout", 0.3)

    with socket.create_connection(("127.0.0.1", client.port), timeout=5) as sock:
        sock.sendall(b"GET / HTTP/1.1\r\n")

        assert sock.recv(1024) == b""


@pytest.mark.parametrize("value", ["0.0.0.0:8094", ":8094", "[::]:8094"])
def test_plain_http_is_never_bound_to_every_interface(value):
    with pytest.raises(SystemExit, match="never every interface"):
        web.listen_address(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("127.0.0.1:8094", ("127.0.0.1", 8094)), ("172.17.0.1:8094", ("172.17.0.1", 8094)), ("[::1]:8094", ("::1", 8094))],
)
def test_one_named_address_is_bound(value, expected):
    assert web.listen_address(value) == expected


def test_a_missing_setting_is_named_without_values():
    with pytest.raises(SystemExit, match="SESSION_SECRET, PINKAS_PASSWORD_HASH") as caught:
        web.load_config({"DATABASE_URL": "postgresql://who:hunter2@db/x"})

    assert "hunter2" not in str(caught.value)
