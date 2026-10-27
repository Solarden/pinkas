import copy
import http.client
import json
import re
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

import psycopg2
import pytest

from pinkas import stats, web
from pinkas.ingest import ingest
from tests.db import DSN
from tests.files import KEY, account, account_v3, fixture, with_export
from tests.web_client import CONF, PASSWORD, TOKEN, XSS, XSS_ESCAPED, Client

FETCHED = datetime(2026, 10, 21, 10, 30, tzinfo=UTC)
NERD = "Stats for nerds"


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
        ("/search", "/search"),
        ("/guide", "/guide"),
        ("/settings", "/settings"),
        ("/stats", "/stats"),
        ("/settingsx", "/"),
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


PLAYING = '<span class="pill pos">playing</span>'


def test_the_playing_character_sits_on_top(logged_in, conn, owner):
    export = with_alt()
    # Days since the logout would have built rested, but the playing character is online.
    export["chars"][KEY].update(rested=0, logout_at=export["chars"][KEY]["logout_at"] - 3 * 86400)
    store(conn, export)
    logged_in.post("/playing", {"t": TOKEN, "character": ids(owner)["Fakechar"]})
    body = logged_in.get("/").body

    assert body.count(PLAYING) == 1
    assert body.index('class="name" href="/c/Testrealm/Fakechar"') < body.index('class="name" href="/c/Testrealm/Altchar"'), (
        "by name, Altchar leads"
    )
    assert "0% of a level · playing" in body and "<b>Playing now</b>" in body


def set_playing_since(owner, name: str, sql_time: str) -> None:
    with owner.cursor() as cur:
        cur.execute(f"UPDATE pinkas_characters SET playing_since = {sql_time} WHERE name = %s", (name,))


def test_playing_ends_after_twelve_hours(logged_in, conn, owner):
    # Altchar's session started days ago, so only the age of playing_since can end it.
    store(conn, with_alt(session_started_at=account()["chars"][KEY]["session_started_at"] - 3 * 86400))
    set_playing_since(owner, "Fakechar", "now() - interval '13 hours'")

    assert PLAYING not in logged_in.get("/").body
    set_playing_since(owner, "Fakechar", "now() - interval '11 hours'")

    assert PLAYING in logged_in.get("/").body


def test_playing_ends_when_another_character_starts_a_session(logged_in, conn, owner):
    store(conn, with_alt())
    logged_in.post("/playing", {"t": TOKEN, "character": ids(owner)["Fakechar"]})
    assert PLAYING in logged_in.get("/").body
    later = int(time.time()) + 60
    export = with_alt(session_started_at=later, logout_at=later + 600)
    export["exported_at"] = later + 600
    store(conn, export)

    assert PLAYING not in logged_in.get("/").body


# Pages -------------------------------------------------------------------------------------------


def test_the_list_shows_every_character(logged_in, conn):
    store(conn, with_alt())
    body = logged_in.get("/").body

    assert ">Fakechar<" in body and ">Altchar<" in body
    assert 'aria-label="Play Altchar"' in body and 'aria-label="Rested XP of Altchar"' in body
    assert "Gold <b>10g 16s 94c</b>" in body and "% of a level" in body
    assert '<span class="n">Skinning</span><span class="count">75/75 · train</span>' in body


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

    assert '<span class="n">Linen Cloth</span><span class="count">32</span>' in body, "20 + 12 in the bags"
    assert '<span class="n">Linen Cloth</span><span class="count">5</span>' in body
    assert body.count(", as of") == 2 and '<span class="n">from Altchar: 5s</span>' in body


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
    index, character, missing, *others = (
        logged_in.get(p) for p in ("/", "/c/Testrealm/Fakechar", "/nothing", "/search?q=leather", "/guide",
                                   "/settings/routes", "/settings/characters", "/guide/edit", "/stats")
    )

    for response in (index, character, missing, *others):
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
        client.get("/search?q=leather"),
        client.get("/guide"),
        client.get("/settings/characters"),
        client.post("/settings/characters", {"t": TOKEN, "character": "1", "rank": "0"}),
    ]

    for response in responses:
        assert NERD not in response.body and "1.60.1.70450" not in response.body

    assert [r.status for r in responses[-4:]] == [303, 303, 303, 303]


# Escaping ----------------------------------------------------------------------------------------


def test_game_strings_stay_inert(logged_in, conn, owner, tmp_path):
    export = with_alt()
    char = export["chars"].pop(KEY)
    char.update(name=XSS, zone=XSS)
    char["bags"][0]["name"] = XSS
    char["professions"][0]["name"] = XSS
    char["bank"] = {"as_of": 1792570000, "items": [{"id": 1, "name": XSS, "count": 1}]}
    mail = {"from": XSS, "money": 0, "items": [{"id": 1, "name": XSS, "count": 1}]}
    char["mailbox"] = {"as_of": 1792570000, "mails": [mail]}
    char["events"] = [
        {"id": 1, "at": 1792570500, "kind": "death", "value": {"killer": XSS, "zone": XSS, "level": 10}},
        {"id": 2, "at": 1792570600, "kind": "dungeon",
         "value": {"instance": XSS, "level": 10, "started": 1792570000, "duration": 600, "deaths": 0, "ended_by_logout": False}},
    ]
    char["totals"] = [{"session": char["session_started_at"], "kind": "gather", "key": f"skinning|{XSS}", "value": {"count": 2}},
                      {"session": char["session_started_at"], "kind": "gold", "key": XSS, "value": {"in": 5}}]
    # Altchar mails the hostile character an item with a hostile name; the recipient reaches the Sankey.
    export["chars"]["Testrealm/Altchar"]["events"] = [
        {"id": 1, "at": 1792570700, "kind": "mail", "value": {"to": f"Testrealm/{XSS}", "items": [{"id": 9, "name": XSS, "count": 3}]}},
    ]
    export["chars"][f"Testrealm/{XSS}"] = char
    export["routes"] = {"herb": XSS}
    store(conn, export)
    logged_in.post("/playing", {"t": TOKEN, "character": ids(owner)["Altchar"]})

    with owner.cursor() as cur:
        cur.execute("UPDATE pinkas_characters SET role = %s", (XSS,))

    guide = tmp_path / "guide.md"
    guide.write_text(f"# Plan\n\n{XSS}\n\n<div>{XSS}</div>\n\ninline <b>bold</b> {XSS}\n", encoding="utf-8")
    logged_in.server.conf = CONF._replace(guide=str(guide))

    index = logged_in.get("/").body
    character = logged_in.get(f"/c/Testrealm/{quote(XSS, safe='')}").body
    search = logged_in.get("/search?" + urlencode({"q": XSS})).body
    settings = logged_in.get("/settings/characters").body
    handbook = logged_in.get("/guide").body
    charts = logged_in.get("/stats?range=all").body

    for body in (index, character, search, settings, handbook, charts):
        assert XSS not in body and "<img" not in body

    assert charts.count(XSS_ESCAPED) >= 7, "killers, zones, gathering, dungeons, gold sources, the Sankey, legends"
    block = charts.split('<script type="application/json" id="charts">')[1].split("</script>")[0]

    assert "<" not in block and XSS in json.dumps(json.loads(block), ensure_ascii=False), "the readout's names, inert"

    assert search.count(XSS_ESCAPED) >= 3, "the item, its character and the query in the box"
    assert settings.count(XSS_ESCAPED) >= 2, "the name and the role"
    assert handbook.count(XSS_ESCAPED) == 3 and "&lt;div&gt;" in handbook and "&lt;b&gt;bold&lt;/b&gt;" in handbook
    assert "<div>" not in handbook and "<b>" not in handbook

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


def test_a_form_over_its_cap_is_refused_whole(client):
    body = b"x=" + b"a" * web.FORM_LIMIT + f"&password={PASSWORD}".encode()

    assert raw_post(client.port, "/login", f"Content-Length: {len(body)}\r\n", body).startswith(b"HTTP/1.0 413")


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
    [("127.0.0.1:8094", ("127.0.0.1", 8094)), ("192.0.2.1:8094", ("192.0.2.1", 8094)), ("[::1]:8094", ("::1", 8094))],
)
def test_one_named_address_is_bound(value, expected):
    assert web.listen_address(value) == expected


def test_a_missing_setting_is_named_without_values():
    with pytest.raises(SystemExit, match="SESSION_SECRET, PINKAS_PASSWORD_HASH") as caught:
        web.load_config({"DATABASE_URL": "postgresql://who:hunter2@db/x"})

    assert "hunter2" not in str(caught.value)


# Bank and mailbox ---------------------------------------------------------------------------------


def test_a_logout_without_a_bank_visit_keeps_the_last_bank(logged_in, conn):
    export = account()
    export["chars"][KEY]["bank"] = {"as_of": 1792530000, "items": [{"id": 2589, "name": "Linen Cloth", "count": 5}]}
    store(conn, export)
    later = account()
    later["exported_at"] += 3600
    later["chars"][KEY]["logout_at"] += 3600
    store(conn, later)

    body = logged_in.get("/c/Testrealm/Fakechar").body
    found = logged_in.get("/search?q=linen").body

    assert '<span class="n">Linen Cloth</span><span class="count">5</span>' in body and "Bank, as of" in body
    assert "bags 32 · bank 5</span>" in found, "no separator after the last non-empty container"


# Search -------------------------------------------------------------------------------------------


def searched(logged_in, q: str) -> str:
    return logged_in.get("/search?" + urlencode({"q": q})).body


def test_search_counts_per_character_and_container(logged_in, conn):
    export = with_alt()
    export["chars"][KEY]["mailbox"] = {
        "as_of": 1792570000,
        "mails": [{"from": "other", "money": 0, "items": [{"id": 2589, "name": "Linen Cloth", "count": 3}]}] * 2,
    }
    store(conn, export)
    body = searched(logged_in, "LINEN")

    assert '<h3><span class="n">Linen Cloth</span><span class="sr">: </span><span class="count">70</span></h3>' in body, (
        "32 + 6 + 32"
    )
    assert "bags 32 · mailbox 6" in body and body.count("bags 32") == 2


def test_search_reads_only_the_newest_snapshot_of_listed_characters(logged_in, conn, owner):
    store(conn, with_alt())
    newer = with_alt()
    newer["exported_at"] += 3600
    newer["chars"][KEY]["logout_at"] += 3600
    newer["chars"][KEY]["bags"] = [b for b in newer["chars"][KEY]["bags"] if b["name"] != "Linen Cloth"]
    store(conn, newer)

    with owner.cursor() as cur:
        cur.execute("UPDATE pinkas_characters SET archived_at = now() WHERE name = 'Altchar'")

    assert "Nothing called" in searched(logged_in, "linen")


@pytest.mark.parametrize("q", ["%", "_", "\\", "x' OR '1'='1", "%%linen"])
def test_search_takes_the_query_literally(logged_in, conn, q):
    store(conn, account())

    assert "Nothing called" in searched(logged_in, q)


def test_an_empty_search_shows_the_box_only(logged_in, conn):
    store(conn, account())
    body = logged_in.get("/search?q=+").body

    assert "Search every character" in body and "Nothing called" not in body


def test_search_finds_a_non_ascii_name(logged_in, conn):
    export = account()
    export["chars"][KEY]["bags"][0]["name"] = "Zoë's Ring"
    store(conn, export)

    assert "Zoë&#39;s Ring" in searched(logged_in, "zoë")


# Guide --------------------------------------------------------------------------------------------


def test_without_a_guide_file_the_guide_is_written_in_the_app(logged_in):
    body = logged_in.get("/guide").body

    assert "No guide yet" in body and 'href="/guide/edit"' in body


def test_an_unreadable_guide_names_only_the_class(logged_in, tmp_path, capsys):
    logged_in.server.conf = CONF._replace(guide=str(tmp_path / "missing.md"))
    body = logged_in.get("/guide").body

    err = capsys.readouterr().err

    assert "The guide can&#39;t be read." in body
    assert "pinkas: guide FileNotFoundError" in err and "missing.md" not in err


def test_the_guide_renders_markdown_without_unsafe_links(logged_in, tmp_path):
    guide = tmp_path / "guide.md"
    guide.write_text("# Alts\n\n| who | craft |\n|---|---|\n| one | Tailoring |\n\n"
                     "[ok](https://example.com) [bad](javascript:alert(1))\n\n- [ ] open\n- [x] done\n", encoding="utf-8")
    logged_in.server.conf = CONF._replace(guide=str(guide))
    body = logged_in.get("/guide").body

    assert "<h1>Guide</h1>" in body and "<h2>Alts</h2>" in body and body.count("<h1") == 1 and "<td>Tailoring</td>" in body
    assert '<div class="table-box" tabindex="0" role="region" aria-label="Alts">\n<table>' in body
    assert '<a href="https://example.com">ok</a>' in body and 'href="javascript:' not in body
    assert '<li class="task">☐ open</li>' in body and '<li class="task">☑ done</li>' in body


def test_only_the_guide_is_inserted_unescaped():
    templates = Path(web.TEMPLATES.loader.searchpath[0]).glob("*.html")
    uses = {path.name: path.read_text(encoding="utf-8").count("|safe") for path in templates}

    assert {name: count for name, count in uses.items() if count} == {"_guide.html": 1}


# Settings and targets -----------------------------------------------------------------------------


def targets(owner) -> list[tuple]:
    with owner.cursor() as cur:
        cur.execute("SELECT skill_line, skill FROM pinkas_targets ORDER BY skill_line, skill")

        return cur.fetchall()


def character_row(owner, name: str = "Fakechar") -> tuple:
    with owner.cursor() as cur:
        cur.execute(
            "SELECT role, sort, well_rested_rank, archived_at IS NOT NULL, playing_since IS NOT NULL"
            " FROM pinkas_characters WHERE name = %s",
            (name,),
        )

        return cur.fetchone()


def settings_form(owner, **fields) -> dict:
    return {"t": TOKEN, "character": ids(owner)["Fakechar"], "role": "", "sort": "", "rank": "0", **fields}


def test_settings_save_every_owner_field(logged_in, conn, owner):
    store(conn, with_alt(professions=[{"skill_line": 197, "name": "Tailoring", "rank": 1, "max": 75}]))
    logged_in.post("/playing", {"t": TOKEN, "character": ids(owner)["Fakechar"]})
    form = settings_form(owner, role=" main ", sort="-3", rank="4", **{"target-393": "225", "target-197": "300"})
    response = logged_in.post("/settings/characters", form)

    assert response.status == 303 and response.headers["Location"] == "/settings/characters?done=saved"
    assert character_row(owner) == ("main", -3, 4, False, True)
    assert targets(owner) == [(197, 150), (197, 225), (197, 300), (393, 150), (393, 225)], "Tailoring is planned"

    logged_in.post("/settings/characters", settings_form(owner, archived="on", **{"target-393": ""}))

    assert character_row(owner) == (None, None, 0, True, False), "archiving ends playing"
    assert targets(owner) == [(197, 150), (197, 225), (197, 300)], "a skill line the form left out keeps its rows"


@pytest.mark.parametrize(
    "fields",
    [
        {"t": "wrong"},
        {"character": "999"},
        {"character": "x"},
        {"rank": "6"},
        {"rank": ""},
        {"sort": "x"},
        {"sort": "10001"},
        {"role": "x" * 41},
        {"target-393": "100"},
        {"target-999": "150"},
        {"target-x": "150"},
    ],
)
def test_settings_refuse_a_bad_value_and_write_nothing(logged_in, conn, owner, fields):
    store(conn, account())
    response = logged_in.post("/settings/characters", settings_form(owner, role="main", **{"target-393": "150"}) | fields)

    assert response.status == 400
    assert character_row(owner) == (None, None, 0, False, False) and targets(owner) == []


def test_the_app_role_cannot_rename_a_character(conn):
    store(conn, account())

    with pytest.raises(psycopg2.errors.InsufficientPrivilege), conn, conn.cursor() as cur:
        cur.execute("UPDATE pinkas_characters SET name = 'Other'")


def test_settings_list_learned_professions_first_and_archived_characters_last(logged_in, conn, owner):
    store(conn, with_alt(professions=[{"skill_line": 197, "name": "Tailoring", "rank": 1, "max": 75}]))

    with owner.cursor() as cur:
        cur.execute("UPDATE pinkas_characters SET archived_at = now() WHERE name = 'Fakechar'")

    body = logged_in.get("/settings/characters").body
    altchar, fakechar = body.split("Altchar · Testrealm")[1].split("Fakechar · Testrealm")

    assert altchar.index("Tailoring") < altchar.index("Other professions") < altchar.index("Skinning")
    assert "archived" in fakechar.split("</h2>")[0]


@pytest.mark.parametrize(
    ("upto", "shown"),
    [
        (None, '<div class="prof cap"><span class="n">Skinning</span><span class="count">75/75 · train</span><meter min="0" max="75" value="75"'),
        ("150", '<div class="prof"><span class="n">Skinning</span><span class="count">75/150</span><meter min="0" max="150" value="75"'),
        ("", '<div class="prof cap"><span class="n">Skinning</span><span class="count">75/75 · train</span><meter min="0" max="75" value="75"'),
    ],
    ids=["no target: train", "a target above the cap", "target cleared"],
)
def test_targets_show_as_progress_bars(logged_in, conn, owner, upto, shown):
    store(conn, account())

    if upto is not None:
        logged_in.post("/settings/characters", settings_form(owner, **{"target-393": upto}))

    for page in ("/", "/c/Testrealm/Fakechar"):
        assert shown in logged_in.get(page).body


def test_a_met_target_is_ticked_and_a_planned_one_listed(logged_in, conn, owner):
    store(conn, with_alt(professions=[{"skill_line": 197, "name": "Tailoring", "rank": 1, "max": 75}]))

    with owner.cursor() as cur:
        cur.execute(
            "INSERT INTO pinkas_targets (character_id, skill_line, skill) VALUES (%s, 393, 150), (%s, 197, 150)",
            (ids(owner)["Fakechar"], ids(owner)["Fakechar"]),
        )
        cur.execute("UPDATE pinkas_snapshots SET data = jsonb_set(data, '{professions,0,rank}', '150')")

    body = logged_in.get("/c/Testrealm/Fakechar").body

    assert '<div class="prof met"><span class="n">Skinning</span><span class="count">150 · met</span>' in body
    assert '<span class="n">Tailoring</span><span class="note">planned, not learned · up to 150' in body


def test_a_target_for_a_profession_no_snapshot_names_stays_visible_and_clearable(logged_in, conn, owner):
    store(conn, account())

    with owner.cursor() as cur:
        cur.execute("INSERT INTO pinkas_targets (character_id, skill_line, skill) VALUES (%s, 999, 150)",
                    (ids(owner)["Fakechar"],))

    settings = logged_in.get("/settings/characters").body
    character = logged_in.get("/c/Testrealm/Fakechar").body

    assert settings.index("Skinning") < settings.index("profession 999")
    assert '<span class="n">profession 999</span><span class="note">planned, not learned · up to 150' in character
    assert logged_in.post("/settings/characters", settings_form(owner, **{"target-999": ""})).status == 303
    assert targets(owner) == []


@pytest.mark.parametrize(
    ("level", "shown"),
    [
        (
            10,
            '<meter min="0" max="7600" value="2296" aria-label="XP of Fakechar toward level 11"></meter>'
            '<span class="val">30% to level 11</span>',
        ),
        (60, '<span class="label">Level 60</span><span class="val">max level</span>'),
    ],
)
def test_a_level_bar_until_the_cap(logged_in, conn, level, shown):
    export = account()
    export["chars"][KEY]["level"] = level
    store(conn, export)

    for page in ("/", "/c/Testrealm/Fakechar"):
        assert shown in logged_in.get(page).body


def test_secondary_professions_have_their_own_column(logged_in, conn):
    store(conn, account())
    row = logged_in.get("/").body.split('<div class="facts">')[1]
    professions, secondary = row.split('label">Professions</span>')[1].split('label">Secondary</span>')
    secondary = secondary.split('class="foot"')[0]

    assert "Skinning" in professions and "Fishing" not in professions and "Cooking" not in professions
    assert "Fishing" in secondary and "Cooking" in secondary


def account_panel(logged_in) -> str:
    return logged_in.get("/").body.split('class="panel account"')[1].split("</details>")[0]


def test_the_account_leaves_archived_characters_out(logged_in, conn, owner):
    store(conn, with_alt(money=5))

    with owner.cursor() as cur:
        cur.execute("UPDATE pinkas_characters SET archived_at = now() WHERE name = 'Altchar'")

    account = account_panel(logged_in)

    assert '<span class="gold">10g 16s 94c</span>' in account and "Altchar" not in account


def test_two_characters_with_one_profession_share_its_entry_highest_first(logged_in, conn):
    store(conn, with_alt(professions=[{"skill_line": 393, "name": "Skinning", "rank": 40, "max": 75}], money=5))
    account = account_panel(logged_in)
    skinning = account.split('<span class="label">Skinning</span>')[1].split('<span class="label">')[0]

    assert '<span class="gold">10g 16s 99c</span>' in account
    assert skinning.index("Fakechar") < skinning.index("Altchar") and account.index("Skinning") < account.index("Cooking")
    assert '<a class="n" href="/c/Testrealm/Altchar">Altchar</a>' in skinning
    assert 'aria-label="Skinning of Altchar toward 75"' in skinning


def test_the_character_page_opens_the_guide_beside_it(logged_in, conn, tmp_path):
    store(conn, account())
    without = logged_in.get("/c/Testrealm/Fakechar").body
    guide = tmp_path / "guide.md"
    guide.write_text("# Plan\n\n## Rotation\n", encoding="utf-8")
    logged_in.server.conf = CONF._replace(guide=str(guide))
    body = logged_in.get("/c/Testrealm/Fakechar").body

    assert 'class="panel side"' not in without
    assert '<details class="panel side" open><summary>Guide</summary>' in body
    assert "<h2>Plan</h2>" in body and "<h3>Rotation</h3>" in body, "one level down: the character is the h1"
    assert body.count("<h1") == 1


def card(body: str, name: str) -> str:
    return body.split(f">{name}</a></h3>")[1].split("</article>")[0]


@pytest.mark.parametrize(
    ("alt", "play", "expected"),
    [({}, None, "Altchar"), ({}, "Altchar", "Fakechar"), ({"level": 60}, None, "Fakechar")],
    ids=["most rested", "not while playing", "not at max level"],
)
def test_the_most_rested_character_is_next_up(logged_in, conn, owner, alt, play, expected):
    export = with_alt()
    export["chars"]["Testrealm/Altchar"].update(rested=14000, **alt)
    store(conn, export)

    if play:
        logged_in.post("/playing", {"t": TOKEN, "character": ids(owner)[play]})

    body = logged_in.get("/").body

    assert body.count("next up") == 1 and '<span class="pill muted">next up</span>' in card(body, expected)


def test_the_cards_carry_their_sort_keys_for_the_script(logged_in, conn, owner):
    export = with_alt()
    export["chars"]["Testrealm/Altchar"]["level"] = 60
    store(conn, export)
    logged_in.post("/playing", {"t": TOKEN, "character": ids(owner)["Altchar"]})
    body = logged_in.get("/").body
    keys = json.loads(body.split('<script type="application/json" id="cards">')[1].split("</script>")[0])
    known = ids(owner)
    fakechar = keys[f"c{known['Fakechar']}"]

    assert '<div class="tools" id="card-tools" hidden>' in body and f'id="c{known["Fakechar"]}"' in body
    assert keys[f"c{known['Altchar']}"]["playing"] is True and fakechar["playing"] is False
    assert keys[f"c{known['Altchar']}"]["rested"] is None, "none at max level"
    assert fakechar["name"] == "Fakechar" and fakechar["level"] == 10 and fakechar["role"] is None
    assert fakechar["professions"] == ["Skinning", "Fishing", "Cooking"]
    assert fakechar["seen"] == account()["chars"][KEY]["logout_at"] and 0 < fakechar["rested"] <= 1.5


def test_the_only_script_is_the_apps_own_file(logged_in, conn):
    store(conn, account())
    script = Client(logged_in.port).get(web.SCRIPT_PATH)

    assert script.status == 200 and script.headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert script.body == (web.ROOT / "static" / "pinkas.js").read_text(encoding="utf-8")
    assert script.headers["Content-Security-Policy"].startswith("default-src 'none'; script-src 'self';")

    for path in ("/", "/c/Testrealm/Fakechar", "/search?q=linen", "/settings/routes", "/settings/characters",
                 "/guide/edit", "/guide", "/stats", "/nothing"):
        tags = re.findall(r"<script[^>]*>", logged_in.get(path).body)

        assert '<script src="/static/pinkas.js" defer>' in tags
        assert set(tags) <= {
            '<script src="/static/pinkas.js" defer>', '<script type="application/json" id="cards">',
            '<script src="/static/tiny-mde.js" defer>',
            '<script type="application/json" id="charts">',
        }


def test_the_script_is_revalidated_while_pages_are_never_stored(logged_in, conn):
    store(conn, account())
    direct = http.client.HTTPConnection("127.0.0.1", logged_in.port, timeout=5)
    direct.request("GET", web.SCRIPT_PATH, headers={"If-None-Match": web.SCRIPT_ETAG})
    revalidated = direct.getresponse()
    body = revalidated.read()
    direct.close()
    script = logged_in.get(web.SCRIPT_PATH)

    assert script.headers["ETag"] == web.SCRIPT_ETAG and script.headers["Cache-Control"] == "no-cache"
    assert revalidated.status == 304 and body == b""
    assert logged_in.get("/").headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "path", ["/", "/c/Testrealm/Fakechar", "/search", "/search?q=linen", "/settings/routes", "/settings/characters",
             "/guide/edit", "/guide", "/stats", "/nothing", "/login"]
)
def test_every_page_has_one_h1(logged_in, conn, path):
    store(conn, account())

    assert logged_in.get(path).body.count("<h1") == 1


@pytest.mark.parametrize(
    ("copper", "shown"),
    [(None, "?"), (0, "0c"), (12, "12c"), (2500, "25s"), (45000, "4g 50s"), (100_149_205, "10\u202f014g 92s 5c")],
)
def test_gold_leaves_zero_units_out(copper, shown):
    assert web.gold(copper) == shown


@pytest.mark.parametrize(
    ("days", "shown"),
    [(1, '<span title="{local}">1 d ago</span>'), (4, '<span class="stale" title="{local}">4 d ago · stale</span>')],
)
def test_an_as_of_older_than_three_days_is_stale(days, shown):
    moment = datetime.now(UTC) - timedelta(days=days, minutes=1)
    template = web.TEMPLATES.from_string('{% from "macros.html" import as_of %}{{ as_of(moment) }}')

    assert template.render(moment=moment) == shown.format(local=web.local(moment))


# Item groups, slots and the mail plan -------------------------------------------------------------


def item(item_id: int, name: str, count: int, quality: int = 1, cls: int = 0, sub: int = 0) -> dict:
    return {"id": item_id, "name": name, "count": count, "quality": quality, "class": cls, "subclass": sub}


LINEN, LEATHER = item(2589, "Linen Cloth", 20, cls=7, sub=5), item(2318, "Light Leather", 10, cls=7, sub=6)


def routed_account(bank_slots: int = 24) -> dict:
    """Fakechar holds cloth (routed to Altchar), leather (routed to itself) and junk; Altchar holds leather."""
    export = account_v3()
    export["routes"] = {"cloth": "Altchar", "leather": "Fakechar-Testrealm", "herb": "Nobody"}
    char = export["chars"][KEY]
    char["bags"] = [LINEN, LINEN, LEATHER, item(1, "Broken Fang", 4, quality=0), item(2, "Hearthstone", 1),
                    item(3, "Wolf Bracers", 1, quality=2)]
    char["bag_slots"] = 6
    char["bank"] = {"as_of": 1792570000, "slots": bank_slots, "items": [LEATHER, item(4, "Tigerseye", 2, quality=3)]}
    alt = copy.deepcopy(char)
    alt.update(name="Altchar", bags=[LEATHER], bag_slots=20, bank=None, mailbox=None)
    export["chars"]["Testrealm/Altchar"] = alt

    return export


def test_bags_and_bank_are_grouped_by_where_their_items_go(logged_in, conn):
    store(conn, routed_account())
    body = logged_in.get("/c/Testrealm/Fakechar").body
    bags, bank = body.split("<span>Bags</span>")[1].split("<span>Bank, as of")

    assert 'Cloth → <a href="/c/Testrealm/Altchar">Altchar</a></span><span>2 slots</span>' in bags
    assert "Linen Cloth</span><span class=\"count\">40</span>" in bags, "two stacks as one line"
    assert "<span>Leather → bank</span><span>1 slot</span>" in bags
    assert "<span>Junk → vendor</span>" in bags and "<span>Other</span><span>2 slots</span>" in bags
    assert bags.index("Cloth →") < bags.index("Leather → bank") < bags.index("Junk") < bags.index("Other")
    assert "<span>Leather · own mats</span>" in bank and "Cloth →" not in bank


def test_a_route_to_nobody_known_is_plain_text_and_its_kind_stays_routed(logged_in, conn):
    export = routed_account()
    export["chars"][KEY]["bags"].append(item(5, "Peacebloom", 3, cls=7, sub=9))
    store(conn, export)
    bags = logged_in.get("/c/Testrealm/Fakechar").body.split("<span>Bags</span>")[1].split("<span>Bank")[0]

    assert "<span>Herbs → Nobody</span>" in bags


def test_a_full_bank_turns_own_mats_in_the_bags_into_a_warning(logged_in, conn):
    store(conn, routed_account(bank_slots=2))
    bags = logged_in.get("/c/Testrealm/Fakechar").body.split("<span>Bags</span>")[1].split("<span>Bank")[0]

    assert "<span>Leather · bank full</span>" in bags


def test_items_carry_their_quality_colour_and_word(logged_in, conn):
    store(conn, routed_account())
    body = logged_in.get("/c/Testrealm/Fakechar").body

    assert '<span class="n q0">Broken Fang<span class="sr"> (poor)</span></span>' in body
    assert '<span class="n q2">Wolf Bracers<span class="sr"> (uncommon)</span></span>' in body
    assert '<span class="n q3">Tigerseye<span class="sr"> (rare)</span></span>' in body
    assert '<span class="n">Hearthstone</span>' in body, "common is plain"


@pytest.mark.parametrize(
    ("used", "level"),
    [(4, ""), (5, ' class="warn"'), (6, ' class="neg"')],
)
def test_bag_slots_show_used_of_total_and_warn_from_70_percent(logged_in, conn, used, level):
    export = routed_account()
    export["chars"][KEY]["bag_slots"] = 7 if used < 6 else 6
    export["chars"][KEY]["bags"] = export["chars"][KEY]["bags"][:used]
    store(conn, export)
    total = export["chars"][KEY]["bag_slots"]
    page = logged_in.get("/c/Testrealm/Fakechar").body

    assert f"Bags <b{level}>{used}/{total}</b>" in logged_in.get("/").body
    assert f'<meter class="slots" min="0" max="{total}"' in page and f"{used}/{total} slots" in page


def test_a_v2_snapshot_shows_slots_used_without_a_total(logged_in, conn):
    store(conn, account())
    body = logged_in.get("/c/Testrealm/Fakechar").body

    assert '<meter class="slots"' not in body and "Bags <b>27</b>" in logged_in.get("/").body


def test_the_mail_plan_adds_up_routed_slots_per_character(logged_in, conn):
    store(conn, routed_account())
    plan = logged_in.get("/").body.split("Mail plan · slots to send")[1].split("</section>")[0]

    assert '<a href="/c/Testrealm/Fakechar">Fakechar</a></span>' in plan
    assert '<a href="/c/Testrealm/Altchar">Altchar</a> 2<span class="sr"> slots</span>' in plan
    assert '<a href="/c/Testrealm/Fakechar">Fakechar-Testrealm</a> 1' in plan, "Altchar's leather, to a Name-Realm route"
    assert "<span>cloth → Altchar</span> · <span>leather → Fakechar-Testrealm</span>" in plan


def test_without_routes_there_is_no_mail_plan(logged_in, conn):
    store(conn, account())

    assert "Mail plan" not in logged_in.get("/").body


def test_search_results_carry_the_item_quality(logged_in, conn):
    store(conn, routed_account())

    assert '<span class="n q2">Wolf Bracers<span class="sr"> (uncommon)</span></span>' in searched(logged_in, "bracers")


def test_an_item_also_in_the_mail_keeps_its_quality_in_search(logged_in, conn):
    export = routed_account()
    mailed = {**item(3, "Wolf Bracers", 1), "quality": None, "class": None, "subclass": None}
    export["chars"]["Testrealm/Altchar"]["mailbox"] = {"as_of": 1792570000, "mails": [{"from": "other", "money": 0, "items": [mailed]}]}
    store(conn, export)

    assert '<span class="n q2">Wolf Bracers' in searched(logged_in, "bracers")


# /stats ------------------------------------------------------------------------------------------


def test_stats_needs_a_session(client, conn):
    response = client.get("/stats?range=30")

    assert response.status == 303 and response.headers["Location"] == "/login?next=/stats"


def test_stats_is_computed_once_until_its_data_changes(logged_in, conn, owner, monkeypatch):
    computed = []
    collect = stats.collect

    def counted(cur, key, now, tz=None):
        computed.append(key)

        return collect(cur, key, now, tz)

    class Tomorrow(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=1)

    monkeypatch.setattr(stats, "collect", counted)
    store(conn, account())
    logged_in.get("/stats")
    logged_in.get("/stats")
    logged_in.get("/stats?range=all")
    later = account()
    later["chars"][KEY]["logout_at"] += 3600
    store(conn, later)
    logged_in.get("/stats")

    with owner.cursor() as cur:
        cur.execute("UPDATE pinkas_characters SET role = 'main'")

    logged_in.get("/stats")
    monkeypatch.setattr(web, "datetime", Tomorrow)
    logged_in.get("/stats")

    assert computed == ["90", "all", "90", "90", "90"], "a new file, a role and a new day each recompute"


@pytest.mark.parametrize(("query", "shown"), [("", "90"), ("?range=30", "30"), ("?range=all", "all"), ("?range=7", "90"),
                                              ("?range=%27%3B--", "90")])
def test_the_range_row_marks_the_range_shown(logged_in, conn, query, shown):
    store(conn, account())
    body = logged_in.get("/stats" + query).body

    assert f'<a href="/stats?range={shown}" aria-current="page">' in body and body.count('aria-current="page"') == 1


def test_an_empty_account_says_what_is_missing(logged_in, conn):
    body = logged_in.get("/stats").body

    assert "No deaths in this range." in body and "No mail between your characters in this range." in body
    assert NERD in body


def test_another_players_kill_reads_player(logged_in, conn):
    export = account()
    export["chars"][KEY]["events"] = [
        {"id": 1, "at": 1792570500, "kind": "death", "value": {"killer": "player", "zone": "Durotar", "level": 10}},
        {"id": 2, "at": 1792570501, "kind": "death", "value": {"killer": None, "zone": "Durotar", "level": 10}},
    ]
    store(conn, export)
    body = logged_in.get("/stats?range=all").body

    assert '<span class="n">player</span>' in body and '<span class="n">unknown</span>' in body


def test_the_character_page_has_its_sparklines(logged_in, conn):
    store(conn, account())
    body = logged_in.get("/c/Testrealm/Fakechar").body

    assert body.count('<div class="spark">') == 2 and '<a href="/stats">More on stats</a>' in body


def test_a_ranked_list_is_scaled_by_its_largest_row():
    rows = str(web.TEMPLATES.get_template("_charts.html").module.ranked([("Durotar", 3), ("other", 20)], "Deaths"))

    assert rows.count('max="20"') == 2, "the folded tail is larger than the first zone"


@pytest.mark.parametrize(("seconds", "text"), [(None, "—"), (89, "1 min"), (3_590, "1 h 0 min"), (4_380, "1 h 13 min")])
def test_minutes(seconds, text):
    assert web.minutes(seconds) == text


def test_search_sums_stacks_past_int(logged_in, conn):
    export = with_alt()
    export["chars"][KEY]["bags"] = [{"id": 2589, "name": "Linen Cloth", "count": 2**31 - 1}] * 2
    store(conn, export)

    assert str(2 * (2**31 - 1)) in searched(logged_in, "linen")
