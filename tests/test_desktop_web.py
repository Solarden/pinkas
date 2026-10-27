"""The Windows app's pages: a one-time launch token instead of a password, and only its own Host."""

import copy
import http.client
import re
import sys
import threading
from datetime import UTC, datetime

import psycopg2
import pytest

from pinkas import web
from pinkas.desktop import App, app_routes_lua, self_test
from pinkas.ingest import ingest
from pinkas.parse import ROUTE_KINDS
from tests.db import DSN
from tests.files import KEY, account, fixture, link_folder, with_export
from tests.web_client import XSS, XSS_ESCAPED, Client


@pytest.fixture
def desktop(conn, monkeypatch, tmp_path):
    def as_app_role(dsn: str):
        c = psycopg2.connect(dsn)

        with c.cursor() as cur:
            cur.execute("SET ROLE pinkas")

        c.commit()

        return c

    monkeypatch.setattr(web, "connect", as_app_role)
    server, url = web.desktop_server(DSN, App(tmp_path))
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    client = Client(server.server_address[1])
    client.start = url.removeprefix(f"http://{server.conf.desktop_host}")
    client.server = server
    yield client
    server.shutdown()
    server.server_close()


def test_the_launch_token_opens_the_window_once(desktop):
    first = desktop.get(desktop.start)
    cookie = first.headers["Set-Cookie"]
    again = desktop.get(desktop.start)

    assert first.status == 303 and first.headers["Location"] == "/"
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Max-Age" not in cookie
    assert desktop.get("/", cookie=cookie.split(";")[0]).status == 200
    assert again.status == 403


@pytest.mark.parametrize("path", ["/start", "/start?token=", "/start?token=forged"])
def test_a_wrong_token_is_refused(desktop, path):
    assert desktop.get(path).status == 403


@pytest.mark.parametrize("path", ["/", "/settings", "/static/pinkas.js"])
def test_without_the_cookie_only_the_script_is_served(desktop, path):
    assert desktop.get(path).status == (200 if path == "/static/pinkas.js" else 403)


def test_a_post_without_the_cookie_is_refused(desktop):
    assert desktop.post("/settings", {"t": "x"}).status == 403


@pytest.mark.parametrize("path", ["/login", "/logout"])
def test_there_is_no_login(desktop, path):
    desktop.cookie = desktop.get(desktop.start).headers["Set-Cookie"].split(";")[0]

    assert desktop.get(path).status == 404
    assert desktop.post(path, {"password": ""}).status == 404


def test_the_page_offers_no_log_out(desktop):
    desktop.cookie = desktop.get(desktop.start).headers["Set-Cookie"].split(";")[0]

    assert 'action="/logout"' not in desktop.get("/").body


@pytest.mark.parametrize("host", ["evil.example:{port}", "localhost:{port}", "127.0.0.1", "127.0.0.1:1", ""])
@pytest.mark.parametrize("path", ["/start", "/", "/static/pinkas.js"])
def test_another_host_is_refused(desktop, host, path):
    token_path = desktop.start if path == "/start" else path
    conn = http.client.HTTPConnection("127.0.0.1", desktop.port, timeout=5)
    conn.putrequest("GET", token_path, skip_host=True)
    conn.putheader("Host", host.format(port=desktop.port))
    conn.endheaders()
    status = conn.getresponse().status
    conn.close()

    assert status == 400
    # The token survived the refused request.
    assert path != "/start" or desktop.get(desktop.start).status == 303


@pytest.fixture
def opened(desktop, tmp_path, monkeypatch):
    """The window after its launch token: a session cookie, a form token, and two installed game versions."""
    desktop.cookie = desktop.get(desktop.start).headers["Set-Cookie"].split(";")[0]
    desktop.token = web.form_token(desktop.server.conf)
    root = tmp_path / "World of Warcraft"

    for flavor in ("_classic_", "_retail_"):
        (root / flavor / "Interface").mkdir(parents=True)

    monkeypatch.setattr("pinkas.desktop.GAME_ROOTS", (root,))
    desktop.root = root

    return desktop


def test_the_self_test_reads_the_game_tab_through_the_launch_token(desktop):
    url = f"http://{desktop.server.conf.desktop_host}{desktop.start}"

    assert self_test(url)
    assert not self_test(url), "the token works once"


def test_the_window_opens_where_the_app_says(desktop):
    desktop.server.first_page = "/settings/game"

    assert desktop.get(desktop.start).headers["Location"] == "/settings/game"


def test_settings_open_on_the_game_tab(opened):
    assert opened.get("/settings").headers["Location"] == "/settings/game"


def test_the_first_run_lists_its_steps_and_reloads_until_a_character_is_read(opened, conn):
    body = opened.get("/settings/game").body

    assert "Getting started" in body and "refresh" not in body
    assert "Found 2 game versions." in body and body.count('type="radio" name="game"') == 2

    opened.server.app.use(0)
    opened.server.app.install(dict)
    body = opened.get("/settings/game?done=installed").body

    assert '<meta http-equiv="refresh" content="5; url=/settings/game">' in body
    ingest(conn, fixture("fake_account.lua"), datetime.now(UTC))
    body = opened.get("/settings/game").body

    assert "refresh" not in body and "Getting started" not in body


def test_a_found_version_is_picked_by_its_number(opened):
    response = opened.post("/settings/game", {"t": opened.token, "game": "1"})

    assert response.headers["Location"] == "/settings/game?done=chosen"
    assert opened.server.app.game() == opened.root / "_retail_"


@pytest.mark.parametrize("game", ["2", "-1", "1e0", "../x", ""])
def test_a_number_outside_the_list_picks_nothing(opened, game):
    assert opened.post("/settings/game", {"t": opened.token, "game": game}).status == 400
    assert opened.server.app.game() is None


def test_the_folder_dialog_says_when_a_folder_is_not_the_game(opened, tmp_path):
    class Window:
        def create_file_dialog(self, kind, directory=""):
            return (str(tmp_path),)

    opened.server.app.window = Window()
    response = opened.post("/settings/game", {"t": opened.token, "choose": ""})
    body = opened.get(response.headers["Location"]).body

    assert response.headers["Location"] == "/settings/game?done=not-game"
    assert 'class="flash error"' in body and "Not saved: that is not a game folder." in body


def test_the_addon_install_says_which_version_to_load(opened):
    opened.server.app.use(0)
    response = opened.post("/settings/addon", {"t": opened.token})
    body = opened.get(response.headers["Location"]).body

    assert response.headers["Location"] == "/settings/game?done=installed"
    assert "Addon 0.2.0 installed. Restart the game to load it." in body
    assert re.search(r'<span class="pill pos">up to date</span>', body)
    assert (opened.root / "_classic_" / "Interface" / "AddOns" / "Pinkas" / "Pinkas.toc").is_file()


@pytest.mark.parametrize(("path", "form"), [("/settings/game", {"game": "0"}), ("/settings/addon", {})])
def test_the_game_tabs_posts_need_the_form_token(opened, path, form):
    assert opened.post(path, {"t": "stale", **form}).status == 400
    assert opened.server.app.game() is None


def test_a_lost_folder_says_so_instead_of_starting_over(opened, conn):
    opened.server.app.use(0)
    ingest(conn, fixture("fake_account.lua"), datetime.now(UTC))
    (opened.root / "_classic_" / "Interface").rmdir()
    (opened.root / "_classic_").rmdir()
    body = opened.get("/settings/game").body

    assert "Getting started" not in body
    assert '<span class="pill neg">missing</span>' in body and "Your characters are kept." in body
    assert "Found 1 game version." in body


def test_without_a_window_choosing_a_folder_says_why(opened):
    response = opened.post("/settings/game", {"t": opened.token, "choose": ""})
    body = opened.get(response.headers["Location"]).body

    assert response.headers["Location"] == "/settings/game?done=no-window"
    assert "needs Microsoft Edge WebView2" in body


@pytest.mark.skipif(sys.platform == "win32", reason="a Windows folder name can't hold < or >")
def test_account_and_folder_names_stay_text(opened, conn):
    hostile = "<img src=x onerror=alert(1)>"
    folder = opened.root / hostile
    (folder / "WTF" / "Account" / hostile / "SavedVariables").mkdir(parents=True)
    (folder / "WTF" / "Account" / hostile / "SavedVariables" / "Pinkas.lua").write_bytes(fixture("fake_account.lua"))
    opened.server.app.save_game(folder)
    opened.server.app.watcher.check(conn, folder)
    body = opened.get("/settings/game").body

    assert hostile not in body and body.count("&lt;img src=x onerror=alert(1)&gt;") >= 2


def test_the_log_folder_is_one_press_away_in_every_state(opened, conn):
    several = opened.get("/settings/game").body

    for flavor in ("_classic_", "_retail_"):
        (opened.root / flavor / "Interface").rmdir()
        (opened.root / flavor).rmdir()

    none = opened.get("/settings/game").body

    for body in (several, none):
        assert '<button class="quiet" name="log">Open the log folder</button>' in body


def test_an_install_into_a_folder_gone_meanwhile_shows_no_message(opened):
    response = opened.post("/settings/addon", {"t": opened.token})

    assert response.headers["Location"] == "/settings/game"


def test_a_link_in_the_way_says_to_remove_it(opened, tmp_path):
    opened.server.app.use(0)
    addons = opened.root / "_classic_" / "Interface" / "AddOns"
    addons.mkdir()
    (tmp_path / "store").mkdir()
    link_folder(addons / "Pinkas", tmp_path / "store")
    response = opened.post("/settings/addon", {"t": opened.token})
    body = opened.get(response.headers["Location"]).body

    assert response.headers["Location"] == "/settings/game?done=refused"
    assert "is a link" in body and "Close the game" not in body


def ingest_characters(conn, routes: dict, more: dict | None = None) -> dict[str, int]:
    """Fakechar, Herbchar with Alchemy, a hostile name, Farmer on a realm with a space, and any `more`; ids by name."""
    export = account()
    export["routes"] = routes
    fake = export["chars"][KEY]
    herb = {**copy.deepcopy(fake), "name": "Herbchar", "professions": [{**fake["professions"][0], "name": "Alchemy",
                                                                         "skill_line": 171, "rank": 230}]}
    farmer = {**copy.deepcopy(fake), "name": "Farmer", "realm": "Other Realm"}
    export["chars"].update({"Testrealm/Herbchar": herb, f"Testrealm/{XSS}": {**copy.deepcopy(fake), "name": XSS},
                            "Other Realm/Farmer": farmer})
    export["chars"].update({key: {**copy.deepcopy(fake), **char} for key, char in (more or {}).items()})
    ingest(conn, with_export(export), datetime.now(UTC))

    with conn, conn.cursor() as cur:
        cur.execute("SELECT name, id FROM pinkas_characters")

        return dict(cur.fetchall())


@pytest.fixture
def characters(conn):
    return ingest_characters(conn, {})


def routes_form(token, **picks):
    return {"t": token, **{f"route-{kind}": str(picks.get(kind, "")) for kind in ROUTE_KINDS}}


def test_the_routes_tab_offers_each_realms_characters_with_the_crafter_as_a_hint(opened, characters):
    body = opened.get("/settings/routes").body

    assert body.count('<select id="route-') == 5 and '<optgroup label="Other Realm">' in body
    assert XSS not in body and XSS_ESCAPED in body
    assert '<p class="hint" id="hint-herb">Alchemy: Herbchar 230</p>' in body
    assert '<p class="hint" id="hint-enchanting">Enchanting: nobody has it yet</p>' in body


def test_saved_routes_reach_the_installed_addon_and_the_page(opened, characters):
    opened.server.app.use(0)
    opened.server.app.install(dict)
    written = opened.root / "_classic_" / "Interface" / "AddOns" / "Pinkas" / "AppRoutes.lua"
    response = opened.post("/settings/routes", routes_form(opened.token, herb=characters["Herbchar"],
                                                           ore=characters["Farmer"]))
    body = opened.get(response.headers["Location"]).body

    assert response.headers["Location"] == "/settings/routes?done=routes"
    assert "Routes saved. Type /reload in the game to use them." in body
    assert written.read_bytes() == app_routes_lua({"herb": "Herbchar-Testrealm", "ore": "Farmer-OtherRealm"})
    assert f'<option value="{characters["Herbchar"]}" selected>Herbchar</option>' in body

    opened.post("/settings/routes", routes_form(opened.token, ore=characters["Farmer"]))

    assert written.read_bytes() == app_routes_lua({"ore": "Farmer-OtherRealm"})


def test_routes_saved_before_the_addon_say_where_they_go(opened, characters):
    response = opened.post("/settings/routes", routes_form(opened.token, herb=characters["Herbchar"]))

    assert response.headers["Location"] == "/settings/routes?done=routes-later"
    assert "install or update it on the Game tab" in opened.get(response.headers["Location"]).body


def test_a_link_in_the_way_of_the_routes_says_to_remove_it(opened, characters, tmp_path):
    opened.server.app.use(0)
    addons = opened.root / "_classic_" / "Interface" / "AddOns"
    addons.mkdir()
    (tmp_path / "store").mkdir()
    link_folder(addons / "Pinkas", tmp_path / "store")
    response = opened.post("/settings/routes", routes_form(opened.token, herb=characters["Herbchar"]))
    body = opened.get(response.headers["Location"]).body

    assert response.headers["Location"] == "/settings/routes?done=routes-refused"
    assert "is a link" in body and "Close the game" not in body


def test_until_routes_are_set_here_the_form_starts_from_the_games(opened, conn):
    # A stranger matches nobody, Farmer alone two characters, and a realm typed in lower case still counts.
    characters = ingest_characters(
        conn, {"herb": "herbchar", "cloth": "Stranger", "leather": "Farmer", "ore": "farmer-otherrealm"},
        {"Testrealm/Farmer": {"name": "Farmer", "realm": "Testrealm"}},
    )

    with conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM pinkas_characters WHERE name = 'Farmer' AND realm = 'Other Realm'")
        (other_farmer,) = cur.fetchone()

    body = opened.get("/settings/routes").body

    assert "These are the routes set in the game." in body
    assert f'<option value="{characters["Herbchar"]}" selected>Herbchar</option>' in body
    assert f'<option value="{other_farmer}" selected>Farmer</option>' in body
    assert body.count(" selected>") == 2

    opened.post("/settings/routes", routes_form(opened.token, cloth=characters["Fakechar"]))
    body = opened.get("/settings/routes").body

    assert "These are the routes set in the game." not in body and body.count(" selected>") == 1


def test_routes_cleared_in_the_app_stay_cleared_while_the_game_still_has_them(opened, conn):
    # The last export, read before the game reloaded, still carries the app's own route.
    characters = ingest_characters(conn, {"herb": "Herbchar-Testrealm"})
    opened.post("/settings/routes", routes_form(opened.token, herb=characters["Herbchar"]))
    opened.post("/settings/routes", routes_form(opened.token))
    body = opened.get("/settings/routes").body

    assert " selected>" not in body and "These are the routes set in the game." not in body


@pytest.mark.parametrize("form", [
    {"route-herb": "999999"}, {"route-herb": "x"}, {"route-herb": "1e0"}, {"route-herb": "²"}, {"route-gems": ""}, {"route-herb": None},
])
def test_a_routes_save_with_an_unknown_kind_or_character_writes_nothing(opened, characters, conn, form):
    sent = {k: v for k, v in {**routes_form(opened.token, cloth=characters["Fakechar"]), **form}.items() if v is not None}

    assert opened.post("/settings/routes", sent).status == 400

    with conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pinkas_routes")

        assert cur.fetchone()[0] == 0


def test_a_character_whose_name_and_realm_outgrow_a_route_is_not_offered(opened, conn):
    long_name, long_realm = "N" * 200, "R" * 200
    ingest_characters(conn, {}, {f"{long_realm}/{long_name}": {"name": long_name, "realm": long_realm}})

    with conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM pinkas_characters WHERE name = %s", (long_name,))
        (long_id,) = cur.fetchone()

    assert long_name not in opened.get("/settings/routes").body
    assert opened.post("/settings/routes", routes_form(opened.token, herb=long_id)).status == 400


def test_an_archived_character_is_offered_only_while_a_route_names_it(opened, characters, conn):
    opened.post("/settings/routes", routes_form(opened.token, herb=characters["Herbchar"]))

    with conn, conn.cursor() as cur:
        cur.execute("UPDATE pinkas_characters SET archived_at = now() WHERE name IN ('Herbchar', 'Farmer')")

    body = opened.get("/settings/routes").body

    assert ">Herbchar</option>" in body and ">Farmer</option>" not in body
    assert opened.post("/settings/routes", routes_form(opened.token, ore=characters["Farmer"])).status == 400
