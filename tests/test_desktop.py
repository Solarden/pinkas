"""The Windows app's watcher, game folder, lock, log and migrations; the window itself is the spike's."""

import contextlib
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import types
from datetime import UTC, datetime
from pathlib import Path

import psycopg2
import pytest

from pinkas import desktop, postgres, web
from pinkas.ingest import ingest
from pinkas.parse import MAX_BYTES
from tests.db import DSN
from tests.files import fixture, link_file, link_folder, server_folder


def saved(game, account="ACCOUNT", name="Pinkas.lua"):
    path = game / "WTF" / "Account" / account / "SavedVariables" / name
    path.parent.mkdir(parents=True, exist_ok=True)

    return path


def stored(owner) -> list:
    with owner.cursor() as cur:
        cur.execute("SELECT count(*) FROM pinkas_files")
        files = cur.fetchone()[0]
        cur.execute("SELECT status FROM pinkas_pull_state")
        row = cur.fetchone()

    return [files, row and row[0]]


@pytest.fixture
def reads(monkeypatch):
    calls = []
    real = desktop.read_capped

    def counted(path):
        calls.append(path.name)

        return real(path)

    monkeypatch.setattr(desktop, "read_capped", counted)

    return calls


def test_a_changed_file_is_read_once_and_an_unchanged_one_never(conn, owner, tmp_path, reads):
    path = saved(tmp_path)
    path.write_bytes(fixture("fake_account.lua"))
    watcher = desktop.Watcher()

    assert [watcher.check(conn, tmp_path), watcher.check(conn, tmp_path)] == [1, 0]
    assert stored(owner) == [1, "ok"]

    path.write_bytes(fixture("fake_account.lua") + b"\n")

    assert watcher.check(conn, tmp_path) == 1
    assert len(reads) == 2


def test_each_account_is_read_and_nothing_else(conn, tmp_path, reads):
    saved(tmp_path, "ONE").write_bytes(fixture("fake_account.lua"))
    saved(tmp_path, "TWO").write_bytes(fixture("fake_nonascii.lua"))
    saved(tmp_path, "ONE", "Other.lua").write_bytes(b"x")
    (tmp_path / "WTF" / "Account" / "ONE" / "Pinkas.lua").write_bytes(b"x")

    assert desktop.Watcher().check(conn, tmp_path) == 2
    assert reads == ["Pinkas.lua", "Pinkas.lua"]


def test_a_broken_file_is_recorded_and_not_retried_until_it_changes(conn, owner, tmp_path, reads):
    saved(tmp_path).write_bytes(b"PinkasDB = {")
    watcher = desktop.Watcher()
    watcher.check(conn, tmp_path)
    watcher.check(conn, tmp_path)

    assert stored(owner) == [0, "parse-failed"]
    assert len(reads) == 1


def test_an_oversized_file_is_refused_without_reading_it_whole(conn, owner, tmp_path, monkeypatch):
    path = saved(tmp_path)

    with open(path, "wb") as f:
        f.truncate(3 * MAX_BYTES)

    sizes = []
    real_open = open

    def spy(*args, **kw):
        f = real_open(*args, **kw)
        read = f.read
        f.read = lambda n=-1: sizes.append(n) or read(n)

        return f

    monkeypatch.setattr(desktop, "open", spy, raising=False)
    desktop.Watcher().check(conn, tmp_path)

    assert stored(owner) == [0, "too-large"]
    assert sizes == [MAX_BYTES + 1]


def game(root, flavor, *parts):
    folder = root / "World of Warcraft" / flavor
    folder.mkdir(parents=True)

    for part in parts:
        (folder / part).mkdir(parents=True)

    return folder


def test_a_game_is_a_flavor_folder_with_interface_or_wtf(tmp_path):
    classic = game(tmp_path, "_classic_", "Interface")
    retail = game(tmp_path, "_retail_", "WTF")
    game(tmp_path, "_ptr_")
    game(tmp_path, "Cache", "WTF")

    assert desktop.games([tmp_path / "World of Warcraft", tmp_path / "missing"]) == [classic, retail]


def test_one_installed_game_is_saved_and_several_wait_for_a_choice(tmp_path, monkeypatch):
    app = desktop.App(tmp_path)
    classic = game(tmp_path, "_classic_", "WTF")
    monkeypatch.setattr(desktop, "GAME_ROOTS", (tmp_path / "World of Warcraft",))

    assert app.detect() == classic
    assert json.loads(app.settings.read_text()) == {"game": str(classic)}

    app.settings.unlink()
    retail = game(tmp_path, "_retail_", "WTF")

    assert app.detect() is None
    assert not app.settings.exists()
    assert [app.use(5), app.use(-1), app.game()] == [False, False, None]
    assert [app.use(1), app.game()] == [True, retail]


def test_a_game_outside_the_defaults_is_found_through_the_registry(tmp_path, monkeypatch):
    retail = game(tmp_path, "_retail_", "WTF")

    def open_key(hive, key, reserved, access):
        if access & winreg.KEY_WOW64_64KEY:
            raise FileNotFoundError(key)

        return contextlib.nullcontext(key)

    winreg = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=0, KEY_READ=1, KEY_WOW64_32KEY=2, KEY_WOW64_64KEY=4, OpenKey=open_key,
        QueryValueEx=lambda key, name: (str(retail) + "/", 1),
    )
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    monkeypatch.setattr(desktop, "GAME_ROOTS", ())
    app = desktop.App(tmp_path)

    assert app.candidates() == [retail]

    monkeypatch.setattr(desktop, "GAME_ROOTS", (retail.parent,))

    assert app.candidates() == [retail]


def test_a_saved_folder_that_is_no_game_is_not_used(tmp_path, monkeypatch):
    app = desktop.App(tmp_path)
    app.settings.write_text(json.dumps({"game": str(tmp_path)}))
    monkeypatch.setattr(desktop, "GAME_ROOTS", ())

    assert app.detect() is None


class Window:
    """pywebview's window, as far as the folder dialog goes."""

    def __init__(self, picked):
        self.picked = picked
        self.directory = None

    def create_file_dialog(self, kind, directory=""):
        self.directory = directory

        return self.picked


@pytest.mark.parametrize(("picked", "outcome", "saved"), [
    (None, "cancelled", False),
    ((), "cancelled", False),
    (("{tmp}/elsewhere",), "not-game", False),
    (("{tmp}/World of Warcraft/_classic_",), "chosen", True),
])
def test_a_folder_from_the_dialog_is_saved_only_if_it_is_a_game(tmp_path, picked, outcome, saved):
    classic = game(tmp_path, "_classic_", "Interface")
    (tmp_path / "elsewhere").mkdir()
    picked = tuple(p.format(tmp=tmp_path) for p in picked) if picked is not None else None
    app = desktop.App(tmp_path, Window(picked))

    assert app.choose() == outcome
    assert app.game() == (classic if saved else None)


def test_without_a_window_the_dialog_says_why(tmp_path):
    assert desktop.App(tmp_path).choose() == "no-window"


def test_a_saved_folder_that_is_gone_is_reported_missing(tmp_path):
    classic = game(tmp_path, "_classic_", "WTF")
    app = desktop.App(tmp_path)
    app.save_game(classic)

    assert [app.game(), app.missing()] == [classic, None]

    shutil.rmtree(classic)

    assert [app.game(), app.missing()] == [None, str(classic)]


class Calls(list):
    """Each command run, answered with `code` as its exit code."""

    code = 0


@pytest.fixture
def relaunch(monkeypatch):
    """An elevated start of the installed exe; runas is recorded, not run, and so is a message box."""
    calls = Calls()
    calls.told = []
    monkeypatch.setattr(desktop, "tell", calls.told.append)
    monkeypatch.setattr(desktop.postgres, "is_elevated", lambda: True)
    monkeypatch.setattr(desktop.sys, "frozen", True, raising=False)
    monkeypatch.setattr(desktop.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or type("Done", (), {"returncode": calls.code})())

    return calls


def test_an_elevated_start_starts_the_exe_again_without_admin_rights(relaunch):
    exe = desktop.postgres.short(Path(sys.executable))

    assert desktop.leave_elevation([desktop.SELF_TEST, "a b", '"']) == 0
    assert relaunch == [["runas", "/trustlevel:0x20000",
                         subprocess.list2cmdline([exe, desktop.RELAUNCHED, desktop.SELF_TEST])]], "with its own flags"


@pytest.mark.parametrize("case", ["relaunched and still elevated", "runas failed", "not the installed exe"])
def test_an_elevated_start_gives_up_rather_than_loop(relaunch, monkeypatch, case):
    argv = [desktop.RELAUNCHED] if case == "relaunched and still elevated" else []
    relaunch.code = 1 if case == "runas failed" else 0

    if case == "not the installed exe":
        monkeypatch.delattr(desktop.sys, "frozen")

    assert desktop.leave_elevation(argv) == 1
    assert len(relaunch) == (1 if case == "runas failed" else 0)


def test_a_start_without_admin_rights_goes_on(monkeypatch):
    monkeypatch.setattr(desktop.postgres, "is_elevated", lambda: False)

    assert desktop.leave_elevation([]) is None


def test_an_elevated_start_creates_nothing_before_it_starts_again(relaunch, monkeypatch, tmp_path):
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path / "pinkas")
    monkeypatch.setattr(desktop.sys, "argv", ["pinkas.exe"])

    assert desktop.main() == 0
    assert not (tmp_path / "pinkas").exists()


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A game, the app's addon from this repo, and an app pointed at both."""
    folder = game(tmp_path, "_classic_", "Interface/AddOns/Other", "WTF")
    app = desktop.App(tmp_path)
    app.save_game(folder)

    return folder, app


def files_under(folder):
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))


def test_a_folder_windows_wont_look_into_is_no_folder(tmp_path, monkeypatch):
    folder = game(tmp_path, "_classic_", "WTF")
    is_dir = Path.is_dir

    def untrusted(self):
        if self.name == "Interface":
            raise OSError(448, "The path cannot be traversed because it contains an untrusted mount point")

        return is_dir(self)

    monkeypatch.setattr(Path, "is_dir", untrusted)

    assert desktop.is_game(folder)


def test_the_addon_is_copied_into_its_own_folder_and_nowhere_else(installed):
    folder, app = installed
    (folder / "Interface" / "AddOns" / "Other" / "Other.toc").write_text("## Title: Other\n")
    before = files_under(folder)

    assert app.addon() == {"installed": None, "newest": "0.2.0"}
    assert app.install(dict) == "installed"

    added = sorted(set(files_under(folder)) - set(before))
    shipped = files_under(desktop.ADDON_SOURCE.parent)

    assert added == [f"Interface/AddOns/{p}" for p in shipped]
    assert app.addon()["installed"] == "0.2.0"
    assert app.install(dict) == "updated"
    assert files_under(folder) == sorted(set(before) | set(added))


def test_a_failed_swap_puts_the_old_addon_back(installed, monkeypatch):
    folder, app = installed
    app.install(dict)
    toc = folder / "Interface" / "AddOns" / "Pinkas" / "Pinkas.toc"
    toc.write_text("## Version: 0.0.9\n")
    rename = Path.rename

    def refuse_the_new_copy(self, target):
        if self.name == "Pinkas.new":
            raise PermissionError("locked")

        return rename(self, target)

    monkeypatch.setattr(Path, "rename", refuse_the_new_copy)

    assert app.install(dict) == "failed"
    assert toc.read_text() == "## Version: 0.0.9\n"


@pytest.mark.parametrize("link", ["Interface/AddOns/Pinkas", "Interface/AddOns", "Interface"])
def test_a_link_on_the_way_to_the_addon_is_refused(installed, tmp_path, link):
    folder, app = installed
    outside = tmp_path / "outside"
    outside.mkdir()
    target = folder / link
    shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    link_folder(target, outside)

    assert app.install(dict) == "refused"
    assert list(outside.iterdir()) == []


def test_an_install_without_a_game_folder_says_so(tmp_path):
    assert desktop.App(tmp_path).install(dict) == "no-game"


HOSTILE = {"herb": '"]] os.exit() --', "cloth": 'Line\nbreak "quoted" \\', "leather": "Altname-Testrealm",
           "ore": "Zoë-OtherRealm", "nope": "x"}


def test_the_routes_file_escapes_every_name_as_the_addons_test_loads_it():
    assert desktop.app_routes_lua(HOSTILE) == fixture("fake_app_routes.lua")


def routes_file(folder):
    return folder / "Interface" / "AddOns" / "Pinkas" / "AppRoutes.lua"


HERB = {"herb": "Herbchar-Testrealm"}


def test_a_settings_file_that_is_no_object_is_written_whole_again(tmp_path):
    (tmp_path / "settings.json").write_text("[]")
    app = desktop.App(tmp_path)

    assert app.saved_game() is None

    app.save_game(tmp_path)

    assert app.saved_game() == str(tmp_path)


def test_routes_reach_the_game_when_the_mark_cant_be_saved(installed, monkeypatch):
    folder, app = installed
    app.install(dict)
    monkeypatch.setattr(app, "save_setting", lambda *a: (_ for _ in ()).throw(PermissionError("in use")))

    assert app.write_routes(lambda: HERB) == "routes"
    assert routes_file(folder).read_bytes() == desktop.app_routes_lua(HERB)


def test_the_game_folder_and_the_routes_mark_keep_each_other(installed, tmp_path):
    folder, app = installed
    app.write_routes(dict)
    app.save_game(folder)

    assert app.routes_set_here() and app.game() == folder
    assert desktop.App(tmp_path).routes_set_here(), "kept for the next start"


def test_the_install_carries_the_saved_routes_and_a_save_replaces_them(installed):
    folder, app = installed
    app.install(lambda: HERB)

    assert routes_file(folder).read_bytes() == desktop.app_routes_lua(HERB)
    assert app.write_routes(lambda: {"cloth": "Altname-Testrealm"}) == "routes"
    assert routes_file(folder).read_bytes() == desktop.app_routes_lua({"cloth": "Altname-Testrealm"})
    assert "AppRoutes.lua.tmp" not in files_under(folder)


def test_without_routes_set_in_the_app_the_games_own_apply(installed):
    folder, app = installed
    app.install(dict)

    assert b"appRoutes =" not in routes_file(folder).read_bytes()


@pytest.mark.parametrize("toc", [None, "## Version: 0.1.0\nCore.lua\nRoutes.lua\n"])
def test_routes_without_an_addon_that_loads_them_wait_for_its_install(installed, tmp_path, toc):
    folder, app = installed

    if toc:
        (folder / "Interface" / "AddOns" / "Pinkas").mkdir()
        (folder / "Interface" / "AddOns" / "Pinkas" / "Pinkas.toc").write_text(toc)

    before = files_under(folder)

    assert app.write_routes(lambda: HERB) == "routes-later"
    assert files_under(folder) == before
    (tmp_path / "other").mkdir()

    assert desktop.App(tmp_path / "other").write_routes(dict) == "routes-later"


def test_routes_are_never_written_through_a_link(installed, tmp_path):
    folder, app = installed
    app.install(dict)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Pinkas.toc").write_text("AppRoutes.lua\n")
    (outside / "AppRoutes.lua").write_text("-- another addon's\n")
    addon = folder / "Interface" / "AddOns" / "Pinkas"
    shutil.rmtree(addon)
    link_folder(addon, outside)

    assert app.write_routes(lambda: HERB) == "routes-refused"
    assert (outside / "AppRoutes.lua").read_text() == "-- another addon's\n"

    addon.unlink()
    app.install(dict)
    routes_file(folder).unlink()
    link_file(routes_file(folder), outside / "AppRoutes.lua")

    assert app.write_routes(lambda: HERB) == "routes"
    assert (outside / "AppRoutes.lua").read_text() == "-- another addon's\n"


def test_a_routes_file_windows_wont_replace_says_so(installed, monkeypatch):
    folder, app = installed
    app.install(dict)
    monkeypatch.setattr(Path, "replace", lambda *a: (_ for _ in ()).throw(PermissionError("in use")))

    assert app.write_routes(lambda: HERB) == "routes-failed"
    assert routes_file(folder).read_bytes() == desktop.app_routes_lua({})


def test_the_routes_are_read_while_the_addon_is_locked(installed):
    folder, app = installed

    def read():
        assert app.addon_lock.locked()

        return HERB

    assert app.install(read) == "installed"
    assert app.write_routes(read) == "routes"


@pytest.mark.parametrize(("flavors", "outcome"), [(["_classic_"], "chosen"), (["_classic_", "_retail_"], "not-game")])
def test_the_world_of_warcraft_folder_with_one_version_counts_as_that_version(tmp_path, flavors, outcome):
    for flavor in flavors:
        game(tmp_path, flavor, "WTF")

    root = tmp_path / "World of Warcraft"
    app = desktop.App(tmp_path, Window((str(root),)))

    assert app.choose() == outcome
    assert app.game() == (root / "_classic_" if outcome == "chosen" else None)


@pytest.mark.parametrize("inside", ["Interface", "WTF", "wtf"])
def test_a_folder_picked_inside_the_game_counts_as_the_game(tmp_path, inside):
    classic = game(tmp_path, "_classic_", "Interface", "WTF")
    picked = classic / inside
    app = desktop.App(tmp_path, Window((str(picked),)))

    assert app.choose() == "chosen"
    assert app.game() == classic


def test_an_old_layout_without_a_version_folder_counts_too(tmp_path):
    root = tmp_path / "World of Warcraft"

    for part in ("Interface", "WTF"):
        (root / part).mkdir(parents=True)

    app = desktop.App(tmp_path, Window((str(root / "Interface"),)))

    assert app.choose() == "chosen"
    assert app.game() == root


def test_a_folder_named_interface_outside_a_game_is_still_refused(tmp_path):
    (tmp_path / "Downloads" / "Interface").mkdir(parents=True)
    app = desktop.App(tmp_path, Window((str(tmp_path / "Downloads" / "Interface"),)))

    assert app.choose() == "not-game"


def test_saving_the_folder_replaces_the_file_whole(tmp_path, monkeypatch):
    classic = game(tmp_path, "_classic_", "WTF")
    app = desktop.App(tmp_path)
    app.save_game(classic)
    written = []
    write_text = Path.write_text

    def crash_after_writing(self, text, **kw):
        written.append(self.name)
        write_text(self, text[: len(text) // 2], **kw)

        raise OSError("power cut")

    monkeypatch.setattr(Path, "write_text", crash_after_writing)

    with pytest.raises(OSError):
        app.save_game(tmp_path / "elsewhere")

    monkeypatch.undo()

    assert written == ["settings.tmp"]
    assert app.game() == classic


def test_two_saves_at_once_take_turns(tmp_path, monkeypatch):
    app = desktop.App(tmp_path)
    inside, most, errors = [0], [0], []
    write_text, replace = Path.write_text, Path.replace

    def counted_write(self, *a, **kw):
        if self.name == "settings.tmp":
            inside[0] += 1
            most[0] = max(most[0], inside[0])

        return write_text(self, *a, **kw)

    def slow_replace(self, target):
        time.sleep(0.05)
        inside[0] -= 1

        return replace(self, target)

    monkeypatch.setattr(Path, "write_text", counted_write)
    monkeypatch.setattr(Path, "replace", slow_replace)

    def save(name):
        try:
            app.save_game(tmp_path / name)
        except OSError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(name,)) for name in ("one", "two")]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    assert errors == [] and most[0] == 1


def test_no_read_of_the_settings_overlaps_a_save(tmp_path, monkeypatch):
    app = desktop.App(tmp_path)
    app.save_game(game(tmp_path, "_classic_", "WTF"))
    saving, overlaps, done = [False], [], threading.Event()
    write_text, replace, load = Path.write_text, Path.replace, desktop.load_settings

    def start_saving(self, *a, **kw):
        saving[0] = self.name == "settings.tmp"

        return write_text(self, *a, **kw)

    def slow_replace(self, target):
        time.sleep(0.01)
        saving[0] = False

        return replace(self, target)

    def watched_load(path):
        overlaps.append(saving[0])

        return load(path)

    monkeypatch.setattr(Path, "write_text", start_saving)
    monkeypatch.setattr(Path, "replace", slow_replace)
    monkeypatch.setattr(desktop, "load_settings", watched_load)

    def read():
        while not done.is_set():
            app.game()

    reader = threading.Thread(target=read)
    reader.start()

    for _ in range(20):
        app.save_game(tmp_path / "World of Warcraft" / "_classic_")

    done.set()
    reader.join()

    assert overlaps and not any(overlaps)


def test_the_dialog_opens_near_the_game(tmp_path, monkeypatch):
    classic = game(tmp_path, "_classic_", "WTF")
    window = Window(None)
    app = desktop.App(tmp_path, window)
    monkeypatch.setattr(desktop, "GAME_ROOTS", (tmp_path / "missing", classic.parent))

    app.choose()

    assert window.directory == str(classic.parent)

    app.save_game(classic)
    shutil.rmtree(classic)
    app.choose()

    assert window.directory == str(classic.parent)

    monkeypatch.setattr(desktop, "GAME_ROOTS", ())
    shutil.rmtree(classic.parent)
    app.choose()

    assert window.directory == str(tmp_path)


@pytest.mark.parametrize(("error", "reads"), [(psycopg2.OperationalError, 1), (psycopg2.ProgrammingError, 0)])
def test_a_file_the_database_failed_on_is_read_again_only_after_a_lost_connection(
    conn, owner, tmp_path, monkeypatch, error, reads,
):
    saved(tmp_path).write_bytes(fixture("fake_account.lua"))
    watcher = desktop.Watcher()
    real = desktop.store
    monkeypatch.setattr(desktop, "store", lambda *a: (_ for _ in ()).throw(error("down")))

    with pytest.raises(error):
        watcher.check(conn, tmp_path)

    monkeypatch.setattr(desktop, "store", real)

    assert watcher.check(conn, tmp_path) == reads
    assert stored(owner) == ([1, "ok"] if reads else [0, None])


def test_the_files_table_shows_each_account_of_the_current_game(conn, tmp_path):
    folder = game(tmp_path, "_classic_", "Interface")
    saved(folder, "ONE").write_bytes(fixture("fake_account.lua"))
    saved(folder, "TWO").write_bytes(b"PinkasDB = {")
    app = desktop.App(tmp_path)
    app.save_game(folder)
    app.watcher.check(conn, folder)

    assert [(f["account"], f["status"], f["characters"]) for f in app.files()] == [
        ("ONE", "ok", 1), ("TWO", "parse-failed", None)]

    saved(folder, "ONE").write_bytes(fixture("fake_account.lua") + b"\n")
    app.watcher.check(conn, folder)

    assert app.files()[0]["characters"] == 1


def test_a_second_copy_does_not_get_the_lock(tmp_path):
    first = desktop.take_lock(tmp_path / "pinkas.lock")

    assert first is not None
    assert desktop.take_lock(tmp_path / "pinkas.lock") is None

    first.close()

    assert desktop.take_lock(tmp_path / "pinkas.lock") is not None


@pytest.mark.skipif(sys.platform != "win32", reason="the mutex is the installer's, on Windows")
def test_the_lock_holds_the_mutex_the_installer_waits_for(tmp_path):
    import ctypes

    assert desktop.take_lock(tmp_path / "pinkas.lock") is not None
    # SYNCHRONIZE: enough to see that it exists, as Inno Setup's AppMutex check does.
    assert ctypes.windll.kernel32.OpenMutexW(0x00100000, False, desktop.APP_MUTEX)


def test_the_log_stamps_each_line_and_starts_over_past_its_limit(tmp_path, monkeypatch):
    path = tmp_path / "pinkas.log"
    path.write_text("x" * (desktop.LOG_LIMIT + 1))
    log = desktop.Stamped(path)
    log.write("one\ntw")
    log.write("o\n")
    log.flush()
    lines = path.read_text().splitlines()

    assert (tmp_path / "pinkas.log.1").stat().st_size == desktop.LOG_LIMIT + 1
    assert [line[20:] for line in lines] == ["one", "two"]
    assert all(line[4] == "-" and line[19] == " " for line in lines)


def test_the_apps_database_has_the_servers_grants():
    base = server_folder()
    server = postgres.start(base)

    try:
        desktop.migrate(server.owner_dsn)

        with psycopg2.connect(server.app_dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pinkas_characters")

            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("UPDATE pinkas_snapshots SET data = data")
    finally:
        postgres.stop(base / "pg")
        shutil.rmtree(base, ignore_errors=True)


def test_log_lines_from_two_threads_stay_whole(tmp_path):
    log = desktop.Stamped(tmp_path / "pinkas.log")
    interval = sys.getswitchinterval()
    # Threads switch as often as possible, so unguarded writes would mix.
    sys.setswitchinterval(1e-6)

    def say(word):
        for _ in range(300):
            log.write(word * 30)
            log.write("\n")

    threads = [threading.Thread(target=say, args=(w,)) for w in "ab"]

    try:
        for t in threads:
            t.start()

        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(interval)

    lines = (tmp_path / "pinkas.log").read_text().splitlines()

    assert len(lines) == 600
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (a{30}|b{30})", line) for line in lines)


@pytest.mark.parametrize(("game_set", "character", "page"), [
    (False, False, "/settings/game"), (True, False, "/settings/game"), (False, True, "/settings/game"), (True, True, "/"),
])
def test_the_window_opens_on_the_game_tab_until_both_are_there(conn, tmp_path, game_set, character, page):
    app = desktop.App(tmp_path)

    if game_set:
        app.save_game(game(tmp_path, "_classic_", "WTF"))

    if character:
        ingest(conn, fixture("fake_account.lua"), datetime.now(UTC))

    assert desktop.first_page(app, DSN) == page


def stub_webview(loads: bool):
    """pywebview as far as open_window goes: the page loads in time, or never."""
    window = types.SimpleNamespace(events=types.SimpleNamespace(loaded=types.SimpleNamespace(wait=lambda s: loads)),
                                   destroy=lambda: None)

    return types.SimpleNamespace(create_window=lambda *a, **kw: window, start=lambda func, gui=None: func())


@pytest.mark.parametrize("loads", [True, False])
def test_without_a_page_in_time_the_browser_gets_a_fresh_token(tmp_path, monkeypatch, loads):
    server, url = web.desktop_server(DSN, desktop.App(tmp_path))
    opened, told = [], []
    monkeypatch.setitem(sys.modules, "webview", stub_webview(loads))
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    monkeypatch.setattr(desktop, "tell", told.append)

    try:
        desktop.open_window(desktop.App(tmp_path), url, lambda: web.launch_url(server))
    finally:
        server.server_close()

    if loads:
        assert opened == [] and told == []
    else:
        assert len(opened) == 1 and opened[0] != url and opened[0].endswith(server.launch_token)
        assert "WebView2" in told[0]


def test_a_failure_after_the_database_started_says_so_and_stops_it(tmp_path, monkeypatch):
    told, stopped = [], []
    monkeypatch.setattr(desktop, "APP_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["pinkas.exe"])
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    # GitHub's Windows runners are administrators.
    monkeypatch.setattr(desktop.postgres, "is_elevated", lambda: False)
    monkeypatch.setattr(desktop.postgres, "start", lambda base: postgres.Server(1, "app", "owner"))
    monkeypatch.setattr(desktop.postgres, "stop", stopped.append)
    monkeypatch.setattr(desktop, "migrate", lambda dsn: (_ for _ in ()).throw(RuntimeError("no migration")))
    monkeypatch.setattr(desktop, "tell", told.append)

    assert desktop.main() == 1
    assert told == ["pinkas couldn't start. The log is in %LOCALAPPDATA%\\pinkas\\pinkas.log."]
    assert stopped == [tmp_path / "pg"]
    assert "start failed (RuntimeError)" in (tmp_path / "pinkas.log").read_text()
