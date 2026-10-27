"""The Windows app's pages: a one-time launch token instead of a password, and only its own Host."""

import http.client
import re
import threading
from datetime import UTC, datetime

import psycopg2
import pytest

from pinkas import web
from pinkas.desktop import App
from pinkas.ingest import ingest
from tests.db import DSN
from tests.files import fixture
from tests.web_client import Client


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
    opened.server.app.install()
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
    assert "Addon 0.1.0 installed. Restart the game to load it." in body
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
    (addons / "Pinkas").symlink_to(tmp_path / "store", target_is_directory=True)
    response = opened.post("/settings/addon", {"t": opened.token})
    body = opened.get(response.headers["Location"]).body

    assert response.headers["Location"] == "/settings/game?done=refused"
    assert "is a link" in body and "Close the game" not in body
