"""The settings tabs and the guide written in the app, on the owner's server (the Windows app's in test_desktop_web)."""

import re
from datetime import UTC, datetime
from html import unescape
from urllib.parse import urlencode

import pytest

from pinkas import web
from pinkas.ingest import ingest
from tests.files import account, with_export
from tests.web_client import CONF, TOKEN, XSS, XSS_ESCAPED


def textarea(body: str) -> str:
    return unescape(re.search(r'<textarea id="guide-text"[^>]*>(.*?)</textarea>', body, re.S).group(1))


def test_settings_open_on_routes_and_the_game_tab_is_the_apps_only(logged_in):
    assert logged_in.get("/settings").headers["Location"] == "/settings/routes"
    assert logged_in.get("/settings/game").status == 404
    assert logged_in.post("/settings/game", {"t": TOKEN, "game": "0"}).status == 404
    assert logged_in.post("/settings/addon", {"t": TOKEN}).status == 404
    assert "/settings/game" not in logged_in.get("/settings/routes").body


def test_the_routes_tab_shows_the_games_routes_read_only(logged_in, conn):
    export = account()
    export["routes"] = {"herb": "Herbchar", "cloth": XSS}
    ingest(conn, with_export(export), datetime.now(UTC))
    body = logged_in.get("/settings/routes").body

    assert "<dt>Herbs</dt><dd>Herbchar</dd>" in body and XSS_ESCAPED in body
    assert body.count('<span class="muted">nobody</span>') == 3
    assert "<select" not in body


def test_a_save_says_so_at_the_top_and_in_the_title(logged_in):
    body = logged_in.get("/settings/characters?done=saved").body

    assert "<title>Saved. settings: characters" in body
    assert '<p class="flash ok" id="flash" tabindex="-1" role="status">Saved.</p>' in body


@pytest.mark.parametrize(("path", "done"), [
    ("/settings/characters", ""), ("/settings/characters", "nope"), ("/settings/characters", XSS),
    ("/settings/characters", "Saved."),
    # A known outcome on a page its POST doesn't redirect to.
    ("/", "failed"), ("/stats", "installed"), ("/settings/characters", "guide"), ("/guide", "saved"),
])
def test_only_a_known_outcome_on_its_own_page_shows_and_never_text_from_the_url(logged_in, path, done):
    body = logged_in.get(path + "?" + urlencode({"done": done})).body

    assert 'class="flash' not in body and XSS not in body


def test_the_guide_written_in_the_app_keeps_each_line(logged_in):
    response = logged_in.post("/guide/edit", {"t": TOKEN, "text": "## Plan\r\nHerbchar gathers\r\nAltchar sews"})
    body = logged_in.get("/guide").body

    assert response.status == 303 and response.headers["Location"] == "/guide?done=guide"
    assert "<h3>Plan</h3>" in body and "Herbchar gathers<br />\nAltchar sews" in body
    assert 'href="/guide/edit">Edit guide</a>' in body
    assert textarea(logged_in.get("/guide/edit").body) == "## Plan\nHerbchar gathers\nAltchar sews"


def test_a_guide_with_html_stays_text_on_the_page_and_in_the_editor(logged_in):
    hostile = f"{XSS}\n\n<div>{XSS}</div>\n</textarea><script>alert(1)</script>"
    logged_in.post("/guide/edit", {"t": TOKEN, "text": hostile})
    body = logged_in.get("/guide").body
    editor = logged_in.get("/guide/edit").body

    assert XSS not in body and body.count(XSS_ESCAPED) == 2 and "<script>alert" not in body
    assert XSS not in editor and "</textarea><script>" not in editor and textarea(editor) == hostile


def test_a_guide_over_the_cap_comes_back_in_the_editor_unsaved(logged_in):
    text = "ą" * (web.GUIDE_LIMIT // 2 + 1)
    response = logged_in.post("/guide/edit", {"t": TOKEN, "text": text})

    assert response.status == 413 and "over 256 KB" in response.body
    assert textarea(response.body) == text
    assert "No guide yet" in logged_in.get("/guide").body


def test_a_stale_form_brings_the_text_back(logged_in):
    response = logged_in.post("/guide/edit", {"t": "stale", "text": "my notes"})

    assert response.status == 400 and textarea(response.body) == "my notes"
    assert f'name="t" value="{TOKEN}"' in response.body


def test_the_guides_form_may_be_larger_than_the_others(logged_in):
    text = "x" * (2 * web.FORM_LIMIT)

    assert logged_in.post("/guide/edit", {"t": TOKEN, "text": text}).status == 303
    assert logged_in.post("/settings/characters", {"t": TOKEN, "role": text}).status == 413


def test_a_guide_file_is_edited_where_it_lives(logged_in, tmp_path):
    guide = tmp_path / "guide.md"
    guide.write_text("# From the file\n", encoding="utf-8")
    logged_in.server.conf = CONF._replace(guide=str(guide))

    assert logged_in.get("/guide/edit").status == 404
    assert logged_in.post("/guide/edit", {"t": TOKEN, "text": "x"}).status == 404
    assert "/guide/edit" not in logged_in.get("/guide").body


def test_a_nul_in_the_guide_is_dropped_rather_than_failing_the_save(logged_in):
    response = logged_in.post("/guide/edit", {"t": TOKEN, "text": "one\x00two"})

    assert response.status == 303
    assert textarea(logged_in.get("/guide/edit").body) == "onetwo"
