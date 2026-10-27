"""pinkas for Windows: its own Postgres, a watcher on the game's files, and the pages in its own window.

    python -m pinkas.desktop      (pinkas.exe in the installed app)

Closing the window ends the app: the watcher, the web server, then Postgres.
The log holds counts and hashes only, never a path: the game's folders carry the account's name.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

import psycopg2

from pinkas import postgres, web
from pinkas.parse import MAX_BYTES
from pinkas.pull import Failure, purge, store

APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "pinkas"
WATCH_SECONDS = 5
LOG_LIMIT = 1_000_000
WINDOW_SECONDS = 20
RELAUNCHED = "--relaunched"
# TODO(launch): the live client's registry key may name its folder; these are the installer's defaults.
GAME_ROOTS = (Path(r"C:\Program Files (x86)\World of Warcraft"), Path(r"C:\Program Files\World of Warcraft"))
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
# The addon from the same commit as the parser, so both read the same export contract.
ADDON_SOURCE = web.ROOT / "addon" / "Pinkas"
VERSION_RE = re.compile(r"^## Version:\s*(\S+)", re.M)


class Stamped(io.TextIOBase):
    """The log: every line starts with the local time, and the file starts over past LOG_LIMIT.
    The HTTP threads and the watcher write at once, and print() sends a line's text and its end apart: each
    thread gathers its own line and writes it whole."""

    def __init__(self, path: Path):
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            path.replace(path.with_suffix(".log.1"))

        self.file = open(path, "a", encoding="utf-8", buffering=1)
        self.lock = threading.Lock()
        self.pending = threading.local()

    def write(self, text: str) -> int:
        *lines, rest = (getattr(self.pending, "text", "") + text).split("\n")
        self.pending.text = rest

        if lines:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S ")

            with self.lock:
                self.file.write("".join(f"{stamp}{line}\n" for line in lines))

        return len(text)

    def flush(self) -> None:
        self.file.flush()


def take_lock(path: Path):
    """The open lock file while this copy runs, or None: two copies would run two servers on one data folder."""
    handle = open(path, "a+b")

    try:
        if postgres.WINDOWS:
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()

        return None

    return handle


def is_game(folder: Path) -> bool:
    return (folder / "Interface").is_dir() or (folder / "WTF").is_dir()


def looks_like_game(folder: Path) -> bool:
    """Stricter than is_game, for a parent reached from a picked Interface or WTF folder, which is_game would always
    accept: a `_<flavor>_` folder, or one holding both."""
    named = folder.name.startswith("_") and folder.name.endswith("_") and len(folder.name) > 2

    return is_game(folder) and (named or ((folder / "Interface").is_dir() and (folder / "WTF").is_dir()))


def games(roots) -> list[Path]:
    """Each `_<flavor>_` folder of an installed game, e.g. `_classic_`."""
    found = []

    for root in roots:
        try:
            found += [f for f in sorted(root.glob("_*_")) if is_game(f)]
        except OSError:
            continue

    return found


def load_settings(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def saved_files(game: Path) -> list[Path]:
    """Only the addon's own file, in each account's SavedVariables."""
    return sorted(f for f in game.glob("WTF/Account/*/SavedVariables/Pinkas.lua") if f.is_file())


def read_capped(path: Path) -> bytes:
    """Never more than the cap plus one byte, so an oversized file is refused without being read whole."""
    try:
        with open(path, "rb") as f:
            body = f.read(MAX_BYTES + 1)
    except OSError:
        raise Failure("source-failed") from None

    if len(body) > MAX_BYTES:
        raise Failure("too-large")

    return body


class Watcher:
    """Each Pinkas.lua whose size or time changed since the last look; every one on the first look."""

    def __init__(self):
        self.seen: dict[Path, tuple[int, int]] = {}
        # What the Game tab shows per account: when its file was last read, and how that went.
        self.files: dict[Path, dict] = {}

    def check(self, conn, game: Path) -> int:
        """Returns how many files it read."""
        read = 0

        for path in saved_files(game):
            try:
                stat = path.stat()
            except OSError:
                continue

            key = (stat.st_size, stat.st_mtime_ns)

            # Marked before reading: a broken file is read again when the game rewrites it, not every 5 s.
            if self.seen.get(path) == key:
                continue

            self.seen[path] = key

            try:
                status, counts = store(conn, lambda path=path: read_capped(path), lambda status, summary: None)
            # The connection failed, not the file: read it again once watch() has reconnected. Any other database
            # error would fail again the same way, so the file waits for the game to rewrite it.
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                del self.seen[path]

                raise

            before = self.files.get(path, {}).get("characters")
            fresh = counts is not None and not counts["unchanged"]
            self.files[path] = {
                "account": path.parent.parent.name, "read_at": datetime.now(UTC), "status": status,
                "characters": counts["characters"] if fresh else before,
            }
            read += 1

        if read and (purged := purge(conn)):
            print(f"purged {purged} raw files")

        return read

    def forget(self) -> None:
        self.seen.clear()
        self.files.clear()


def toc_version(toc: Path) -> str | None:
    try:
        found = VERSION_RE.search(toc.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None

    return found.group(1) if found else None


class Refused(Exception):
    """The addon's folder, or a folder on the way to it, is a link: it could point anywhere."""


def is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def install_addon(game: Path, source: Path) -> str:
    """Copies the addon into <game>/Interface/AddOns/Pinkas and writes nothing else. Returns "installed"
    or "updated"; raises Refused or OSError, having changed nothing outside AddOns."""
    addons = game / "Interface" / "AddOns"
    target = addons / "Pinkas"
    staging, old = addons / "Pinkas.new", addons / "Pinkas.old"

    for step in (game / "Interface", addons, target, staging, old):
        if is_link(step) or (step.exists() and not step.is_dir()):
            raise Refused

    addons.mkdir(parents=True, exist_ok=True)

    if not addons.resolve().is_relative_to(game.resolve()):
        raise Refused

    # Left over from an install that died halfway.
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    shutil.copytree(source, staging)
    existed = target.exists()

    if existed:
        target.rename(old)

    try:
        staging.rename(target)
    # The old addon goes back, so a failed update never leaves the game without one.
    except OSError:
        if existed:
            old.rename(target)

        raise

    shutil.rmtree(old, ignore_errors=True)

    return "updated" if existed else "installed"


class App:
    """What the settings pages ask of the Windows app: its game folder, the files it read, the addon, the log.
    Every path here comes from its own settings or the game's folders, never from a request."""

    def __init__(self, app_dir: Path, window=None):
        self.dir = app_dir
        self.settings = app_dir / "settings.json"
        self.watcher = Watcher()
        # pywebview's window, for the folder dialog; None when the page is in the browser.
        self.window = window
        # Each request and the watcher run on their own threads: two saves would share settings.json's temp file, and
        # on Windows a read holding the file open makes the save's replace fail.
        self.settings_lock = threading.Lock()

    def saved_game(self) -> str | None:
        with self.settings_lock:
            return load_settings(self.settings).get("game")

    def game(self) -> Path | None:
        saved = self.saved_game()

        return Path(saved) if saved and is_game(Path(saved)) else None

    def missing(self) -> str | None:
        """The saved folder when it is no game any more: moved or uninstalled."""
        saved = self.saved_game()

        return saved if saved and not is_game(Path(saved)) else None

    def save_game(self, folder: Path) -> None:
        # Whole or not at all: a power cut mid-write would otherwise leave no saved folder.
        temp = self.settings.with_suffix(".tmp")

        with self.settings_lock:
            temp.write_text(json.dumps({"game": str(folder)}), encoding="utf-8")
            temp.replace(self.settings)

    def candidates(self) -> list[Path]:
        return games(GAME_ROOTS)

    def detect(self) -> Path | None:
        """The saved folder, else the one installed game, saved; several or none wait for the Game tab."""
        if game := self.game():
            return game

        found = self.candidates()

        if len(found) != 1:
            return None

        self.save_game(found[0])

        return found[0]

    def use(self, index: int) -> bool:
        """A pick from the found list, by its number: the request names no path."""
        found = self.candidates()

        if not 0 <= index < len(found):
            return False

        self.save_game(found[index])

        return True

    def choose(self) -> str:
        """Windows' own folder dialog. Returns "chosen", "not-game", "cancelled" or "no-window" (the pages are in
        the browser: WebView2 is missing)."""
        if self.window is None:
            return "no-window"

        import webview

        picked = self.window.create_file_dialog(webview.FileDialog.FOLDER, directory=str(self.dialog_start()))

        if not picked:
            return "cancelled"

        folder = Path(picked[0])

        # The game's own folder; the World of Warcraft folder above it when it holds one version; or the Interface or
        # WTF folder inside it, which people know from installing addons by hand.
        if not is_game(folder) and len(inside := games([folder])) == 1:
            folder = inside[0]
        elif folder.name.lower() in ("interface", "wtf") and looks_like_game(folder.parent):
            folder = folder.parent

        if not is_game(folder):
            return "not-game"

        self.save_game(folder)

        return "chosen"

    def dialog_start(self) -> Path | str:
        """Near the game: above the folder that went missing, else the first installer folder that exists."""
        saved = self.saved_game()

        for near in ([Path(saved).parent, Path(saved).parent.parent] if saved else []) + list(GAME_ROOTS):
            if near.is_dir():
                return near

        return ""

    def addon(self) -> dict:
        game = self.game()
        installed = toc_version(game / "Interface" / "AddOns" / "Pinkas" / "Pinkas.toc") if game else None

        return {"installed": installed, "newest": toc_version(ADDON_SOURCE / "Pinkas.toc")}

    def install(self) -> str:
        """Returns "installed", "updated", "failed" (Windows refused a write), "refused" (a link on the way, which
        pinkas won't write through) or "no-game" (the folder went away since the page was shown)."""
        game = self.game()

        if game is None:
            return "no-game"

        try:
            outcome = install_addon(game, ADDON_SOURCE)
        except Refused:
            print("addon install refused (a link)")

            return "refused"
        # The class only: an OSError's message names the path.
        except OSError as exc:
            print(f"addon install failed ({type(exc).__name__})")

            return "failed"

        print(f"addon {outcome}")

        return outcome

    def files(self) -> list[dict]:
        game = self.game()

        # A copy: the watcher's thread adds to the dict, or clears it, meanwhile.
        files = list(self.watcher.files.items())

        return sorted((f for path, f in files if game and path.is_relative_to(game)), key=lambda f: f["account"])

    def open_log(self) -> None:
        if postgres.WINDOWS:
            os.startfile(self.dir)


def watch(app: App, dsn: str, stop: threading.Event) -> None:
    conn = None
    current = None

    while not stop.is_set():
        game = app.game()

        # A folder chosen on the Game tab: start over, reading every file of the new one once.
        if game != current:
            app.watcher.forget()
            current = game

        try:
            if game is not None:
                if conn is None:
                    conn = psycopg2.connect(dsn, connect_timeout=5)

                app.watcher.check(conn, game)
        # The class only: a driver message can quote the DSN.
        except psycopg2.Error as exc:
            print(f"database error ({type(exc).__name__})")

            if conn is not None:
                conn.close()
                conn = None

        stop.wait(WATCH_SECONDS)

    if conn is not None:
        conn.close()


def migrate(owner_dsn: str) -> None:
    from alembic import command
    from alembic.config import Config

    # migrations/env.py reads it from the environment; Postgres, started earlier, never sees it.
    os.environ["MIGRATE_DATABASE_URL"] = owner_dsn

    try:
        command.upgrade(Config(str(web.ROOT / "alembic.ini")), "head")
    finally:
        del os.environ["MIGRATE_DATABASE_URL"]


def tell(text: str) -> None:
    """A message box: the app has no console."""
    print(text)

    if postgres.WINDOWS:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "pinkas", 0x40)


def open_window(app: App, url: str, fresh_url) -> None:
    """Blocks until the window closes. Without WebView2 the page never loads: the browser takes it then, with a
    fresh token, as a window that was only slow may have spent the first one."""
    import webview

    window = webview.create_window("pinkas", url, width=1280, height=860, min_size=(375, 500))
    app.window = window
    loaded = threading.Event()

    def wait_for_page():
        if window.events.loaded.wait(WINDOW_SECONDS):
            loaded.set()
        else:
            window.destroy()

    webview.start(wait_for_page, gui="edgechromium" if postgres.WINDOWS else None)
    app.window = None

    if not loaded.is_set():
        print("window: no page in time, opening the browser")
        webbrowser.open(fresh_url())
        tell(
            "pinkas opened in your browser: this PC lacks Microsoft Edge WebView2, which gives pinkas its own "
            f"window. It is free from Microsoft: {WEBVIEW2_URL}\n\nClick OK to close pinkas."
        )


def first_page(app: App, dsn: str) -> str:
    """The Game tab until a character has been read; the characters after that."""
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM pinkas_characters)")
        (any_character,) = cur.fetchone()

    conn.close()

    return "/" if any_character and app.game() else "/settings/game"


def leave_elevation(argv: list[str]) -> int | None:
    """An elevated run starts pinkas again without administrator rights, once, as Postgres needs; returns its exit
    code, or None to go on."""
    if not postgres.is_elevated():
        return None

    # Only the installed exe: `python -m` would start in runas's own folder, where pinkas isn't importable.
    if RELAUNCHED not in argv and getattr(sys, "frozen", False):
        # The 8.3 name has no spaces, so runas gets no quotes nested in its own.
        program = subprocess.list2cmdline([postgres.short(Path(sys.executable)), RELAUNCHED])

        if subprocess.run(["runas", "/trustlevel:0x20000", program], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            return 0

    tell("pinkas can't run with administrator rights. Start it the usual way, from the Start menu.")

    return 1


def main() -> int:
    # First: an elevated run must create nothing, or the files would belong to Administrators, out of reach of the
    # run it starts again.
    if (code := leave_elevation(sys.argv[1:])) is not None:
        return code

    APP_DIR.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = Stamped(APP_DIR / "pinkas.log")
    print("starting")

    lock = take_lock(APP_DIR / "pinkas.lock")

    if lock is None:
        tell("pinkas is already open.")

        return 1

    started = time.monotonic()

    try:
        server = postgres.start(APP_DIR)
    except postgres.LostSecrets:
        print("postgres: secrets.json is gone, the database is kept")
        tell("pinkas's database is there, but its passwords file (secrets.json) is gone, so pinkas can't open it."
             " Nothing was deleted. Put the file back, or ask whoever helps you with pinkas.")

        return 1
    except Exception as exc:
        print(f"postgres failed to start ({type(exc).__name__})")
        # A server that started but never answered would otherwise outlive the app.
        postgres.stop(APP_DIR / "pg")
        tell("pinkas couldn't start its database. The log is in %LOCALAPPDATA%\\pinkas\\pinkas.log.")

        return 1

    try:
        migrate(server.owner_dsn)
        print(f"database ready in {time.monotonic() - started:.1f} s")
        app = App(APP_DIR)
        print("game folder: " + ("found" if app.detect() else "not set"))
        httpd, url = web.desktop_server(server.app_dsn, app, first_page(app, server.app_dsn))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        stop = threading.Event()
        watcher = threading.Thread(target=watch, args=(app, server.app_dsn, stop), daemon=True)
        watcher.start()
        open_window(app, url, lambda: web.launch_url(httpd))
        stop.set()
        watcher.join(WATCH_SECONDS * 2)
        httpd.shutdown()
    # Without a message box the app would just not appear.
    except Exception as exc:
        print(f"start failed ({type(exc).__name__})")
        tell("pinkas couldn't start. The log is in %LOCALAPPDATA%\\pinkas\\pinkas.log.")

        return 1
    finally:
        postgres.stop(APP_DIR / "pg")
        print("stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
