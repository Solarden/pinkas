"""P9a spike: can the Windows app embed Postgres and open its own window?

Not part of the app or the test suite. Every command prints what it measured and adds it to
build/p9a/report.json: timings, sizes, yes/no answers, never a path, a password or a user name.

    pg        embedded Postgres: own initdb, start, auth, migrations, stop, killed parent, crash, the test suite
    sleep     a server kept open across Windows sleep (interactive: Ctrl+C to end)
    window    pywebview at a local page: launch cookie, Host header, optional folder dialog
    build     PyInstaller one-folder of this script, its size, the frozen exe run, a Defender scan

Run from the repo root (exact steps for Windows: tests/spike/README.md).
"""

import argparse
import http.server
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parents[2]
OUT = Path.cwd() / "build" / "p9a"
WINDOWS = platform.system() == "Windows"
REPORT: dict = {}
# A piped console on Windows is cp1252: Postgres' messages must not crash the print.
sys.stdout.reconfigure(errors="backslashreplace")
# pixeltable-pgserver 0.6.0's Windows Postgres 18 links libwinpthread-1.dll without shipping it. A folder
# holding it goes on PATH here; the build puts it beside postgres.exe, as the app would.
DLLS = Path(os.environ["PINKAS_SPIKE_DLLS"]).resolve() if os.environ.get("PINKAS_SPIKE_DLLS") else None
if DLLS:
    os.environ["PATH"] = f"{DLLS}{os.pathsep}{os.environ['PATH']}"


def note(key: str, value) -> None:
    REPORT[key] = value
    print(f"{key}: {value}", flush=True)


def save(command: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "report.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    old[command + (" (frozen)" if FROZEN else "")] = REPORT | {
        "os": f"{platform.system()} {platform.release()} {platform.version()}",
        "python": platform.python_version(),
    }
    path.write_text(json.dumps(old, indent=2))
    print(f"saved to build/p9a/report.json ({command})")


def timed(fn):
    start = time.monotonic()
    result = fn()

    return result, round(time.monotonic() - start, 2)


# --- Postgres -----------------------------------------------------------------------------------


class Pg:
    """One data folder: our own initdb with scram auth, then pgserver only starts and stops it.

    pgserver's own initdb uses `--auth=trust`, which lets any local program in as the superuser.
    """

    def __init__(self, data: Path):
        import pixeltable_pgserver

        self.lib = pixeltable_pgserver
        self.bin = pixeltable_pgserver.utils.POSTGRES_VERSIONS[pixeltable_pgserver.TARGET_POSTGRES_VERSION]
        self.data = data.resolve()
        self.password = secrets.token_hex(24)
        self.server = None

    def initdb(self) -> None:
        # This Windows initdb can't create its own folder: it reports the existing parent as "File exists".
        self.data.mkdir()
        pwfile = self.data.parent / "pw.tmp"
        pwfile.write_text(self.password)
        try:
            result = subprocess.run(
                [
                    str(self.bin / "initdb"),
                    "-D", str(self.data), "-U", "postgres", f"--pwfile={pwfile}",
                    "--auth=scram-sha-256", "--encoding=UTF8",
                    "--locale=C", "--locale-provider=builtin", "--builtin-locale=C.UTF-8",
                ],
                capture_output=True, timeout=120,
            )
            if result.returncode:
                print(result.stderr.decode("utf-8", "replace")[-2000:])
                raise RuntimeError(f"initdb failed: exit {result.returncode}")
        finally:
            pwfile.unlink()

    def start(self):
        self.server = self.lib.get_server(self.data, cleanup_mode="stop")

        return self.server

    def dsn(self, user="postgres", password=None, db="postgres") -> str:
        info = self.server.get_postmaster_info()
        auth = f"{user}:{password or self.password}"
        if info.socket_dir is not None:
            return f"postgresql://{auth}@/{db}?host={info.socket_dir}&port={info.port}"

        return f"postgresql://{auth}@{info.hostname}:{info.port}/{db}"

    def connect(self, **kw):
        import psycopg2

        return psycopg2.connect(self.dsn(**kw), connect_timeout=5)

    def info(self):
        return self.lib.utils.PostmasterInfo.read_from_pgdata(self.data)

    def pg_ctl_stop(self) -> None:
        subprocess.run([str(self.bin / "pg_ctl"), "-D", str(self.data), "-w", "-m", "fast", "stop"], capture_output=True,
                       timeout=60)

    def forget(self) -> None:
        """Drop pgserver's in-process handle, so the next start() looks at the folder afresh."""
        self.lib.PostgresServer._instances.pop(self.data.resolve(), None)
        self.server = None


def running(pg: Pg) -> bool:
    info = pg.info()

    return bool(info and info.is_running())


def postgres_pids(data: Path) -> list[int]:
    import psutil

    # pg_ctl on Windows hands postgres the folder with forward slashes.
    folder = str(data).replace("\\", "/").lower()
    pids = []
    for proc in psutil.process_iter(["name", "cmdline"]):
        name = (proc.info["name"] or "").lower()
        if name.startswith("postgres") and folder in " ".join(proc.info["cmdline"] or []).replace("\\", "/").lower():
            pids.append(proc.pid)

    return pids


def kill_tree(data: Path) -> None:
    """A power cut: every postgres process of this folder dies at once, nothing is cleaned up."""
    import psutil

    for pid in postgres_pids(data):
        try:
            psutil.Process(pid).kill()
        except psutil.NoSuchProcess:
            pass

    time.sleep(1)


def lan_refused(port: int) -> bool | str:
    """Postgres must not answer on this PC's LAN address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the outgoing interface
            lan = s.getsockname()[0]
    except OSError:
        return "no LAN address"

    if lan.startswith("127."):
        return "no LAN address"

    try:
        socket.create_connection((lan, port), timeout=2).close()
    except OSError:
        return True

    return False


def migrate(pg: Pg) -> None:
    owner_pw, app_pw = secrets.token_hex(24), secrets.token_hex(24)
    admin = pg.connect()
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE ROLE pinkas_owner LOGIN PASSWORD '{owner_pw}'")
        cur.execute(f"CREATE ROLE pinkas LOGIN PASSWORD '{app_pw}'")
        cur.execute("CREATE DATABASE pinkas OWNER pinkas_owner")
    admin.close()
    owner = pg.connect(user="pinkas_owner", password=owner_pw, db="pinkas")
    owner.autocommit = True
    with owner.cursor() as cur:
        cur.execute("GRANT USAGE ON SCHEMA public TO pinkas")
    owner.close()

    from alembic.config import main as alembic

    os.environ["MIGRATE_DATABASE_URL"] = pg.dsn(user="pinkas_owner", password=owner_pw, db="pinkas")
    _, seconds = timed(lambda: alembic(argv=["-c", str(ROOT / "alembic.ini"), "-q", "upgrade", "head"]))
    del os.environ["MIGRATE_DATABASE_URL"]
    note("migrate_s", seconds)

    import psycopg2.errors

    app = pg.connect(user="pinkas", password=app_pw, db="pinkas")
    with app.cursor() as cur:
        cur.execute("SELECT count(*) FROM pinkas_characters")
        try:
            cur.execute("UPDATE pinkas_snapshots SET data = data")
            note("app_role_cannot_rewrite_history", False)
        except psycopg2.errors.InsufficientPrivilege:
            note("app_role_cannot_rewrite_history", True)
    app.close()


def folder_mb(path: Path) -> float:
    return round(sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 2**20, 1)


def cmd_pg(args) -> None:
    import psycopg2

    base = Path(tempfile.mkdtemp(prefix="pinkas-p9a-"))
    pg = Pg(base / "pg")
    data = pg.data
    try:
        import pixeltable_pgserver

        note("pgserver", f"pixeltable-pgserver, Postgres {pixeltable_pgserver.TARGET_POSTGRES_VERSION}")
        if WINDOWS:
            # The runners' PATH carries Strawberry Perl's own copy of the DLL; a friend's PC has none.
            root = os.environ["SystemRoot"]
            bare = subprocess.run([str(pg.bin / "postgres"), "-V"], capture_output=True,
                                  env={"SystemRoot": root, "PATH": str(Path(root) / "System32")})
            note("postgres_runs_without_extra_dlls", bare.returncode == 0)
        non_ascii_path(base)
        _, seconds = timed(pg.initdb)
        note("initdb_s", seconds)
        _, seconds = timed(pg.start)
        note("start_first_s", seconds)

        conn = pg.connect()
        with conn.cursor() as cur:
            cur.execute("SHOW server_version")
            note("server_version", cur.fetchone()[0])
            cur.execute("SHOW listen_addresses")
            note("listen_addresses", cur.fetchone()[0])
            cur.execute("SELECT upper('zażółć') = 'ZAŻÓŁĆ', 'Łódź' ILIKE '%ŁÓDŹ%'")
            note("unicode_case_folding", cur.fetchone())
        conn.close()

        info = pg.info()
        if info.port and not info.socket_dir:
            note("lan_refused", lan_refused(info.port))
        try:
            pg.connect(password="wrong").close()
            note("wrong_password_refused", False)
        except psycopg2.OperationalError:
            note("wrong_password_refused", True)

        migrate(pg)
        if not args.quick:
            run_suite(pg)
        note("data_mb", folder_mb(data))

        _, seconds = timed(pg.server.cleanup)
        note("stop_s", seconds)
        pg.forget()
        note("stopped_clean", not running(pg) and not postgres_pids(data))

        _, seconds = timed(pg.start)
        note("start_again_s", seconds)

        if not args.quick:
            killed_parent(pg)
            crash(pg)
            stale_pid(pg)
            own_launcher(base)
    finally:
        if pg.server is not None and running(pg):
            pg.pg_ctl_stop()

        kill_tree(data)
        shutil.rmtree(base, ignore_errors=True)

    save("pg")
    sys.exit(1 if REPORT.get("suite_exit") else 0)


def non_ascii_path(base: Path) -> None:
    """A user name like "Nguyễn" puts letters into %LOCALAPPDATA% that the ANSI code page may lack."""
    folder = base / "zażółć gęślą"
    folder.mkdir()
    pg = Pg(folder / "pg")
    try:
        pg.initdb()
        note("non_ascii_path_initdb", True)
    except RuntimeError:
        note("non_ascii_path_initdb", False)
    if not WINDOWS:
        return

    import ctypes

    note("ansi_code_page", ctypes.windll.kernel32.GetACP())
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(folder), buffer, 1024)
    short = Path(buffer.value)
    note("short_path_is_ascii", buffer.value.isascii())
    if not buffer.value.isascii():
        return

    # pgserver resolves its folder back to the long name, so this drives pg_ctl directly.
    shutil.rmtree(folder / "pg", ignore_errors=True)
    pg.data = short / "pg"
    try:
        pg.initdb()
        pg_ctl_connect(pg, pg_ctl_start(pg, short / "log"))
        note("short_path_pg_ctl_start", True)
    except Exception as exc:
        note("short_path_pg_ctl_start", type(exc).__name__)
    finally:
        pg.pg_ctl_stop()


def pg_ctl_start(pg: Pg, log: Path) -> int:
    port = pg.lib.utils.find_suitable_port("127.0.0.1")
    # Not a pipe: postgres inherits it and keeps it open, so reading it to the end never returns.
    subprocess.run([str(pg.bin / "pg_ctl"), "-D", str(pg.data), "-l", str(log), "-w", "-t", "90", "-o",
                    f"-h 127.0.0.1 -p {port}", "start"], check=True, timeout=120,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return port


def pg_ctl_connect(pg: Pg, port: int) -> None:
    """pg_ctl -w on Windows can't check the pid, and takes a postmaster.pid younger than 2 s for the new one."""
    import psycopg2

    deadline = time.monotonic() + 90
    while True:
        try:
            psycopg2.connect(host="127.0.0.1", port=port, user="postgres", password=pg.password, dbname="postgres",
                             connect_timeout=5).close()
            return
        except psycopg2.OperationalError:
            if time.monotonic() > deadline:
                raise

            time.sleep(0.5)


def own_launcher(base: Path) -> None:
    """pg_ctl driven directly, its log outside the data folder: a crash and a stale pid, as in P9b."""
    pg = Pg(base / "own")
    log = base / "own.log"
    pg.initdb()
    pg_ctl_start(pg, log)
    kill_tree(pg.data)
    try:
        _, seconds = timed(lambda: pg_ctl_connect(pg, pg_ctl_start(pg, log)))
        note("own_start_after_crash_s", seconds)
    except Exception as exc:
        note("own_start_after_crash_s", type(exc).__name__)

    pidfile = pg.data / "postmaster.pid"
    lines = pidfile.read_text().splitlines()
    kill_tree(pg.data)
    lines[0] = str(os.getpid())
    pidfile.write_text("\n".join(lines) + "\n")
    try:
        _, seconds = timed(lambda: pg_ctl_connect(pg, pg_ctl_start(pg, log)))
        note("own_start_with_reused_pid_s", seconds)
    except Exception as exc:
        note("own_start_with_reused_pid_s", type(exc).__name__)
    finally:
        pg.pg_ctl_stop()
        kill_tree(pg.data)


def run_suite(pg: Pg) -> None:
    """The whole pytest suite against this Postgres, as the design promises (section 2)."""
    admin = pg.connect()
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("CREATE DATABASE pinkas_test")
    admin.close()
    env = os.environ | {"PINKAS_TEST_DATABASE_URL": pg.dsn(db="pinkas_test")}
    started = time.monotonic()
    # The server's pull reads its source with select() on a pipe, which Windows lacks; the app never pulls.
    skip = ["--ignore=tests/test_pull.py"] if WINDOWS else []
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *skip], cwd=ROOT, env=env,
                            capture_output=True, text=True)
    lines = result.stdout.strip().splitlines()
    note("suite", lines[-1] if lines else "no output")
    note("suite_exit", result.returncode)
    note("suite_s", round(time.monotonic() - started, 1))
    if result.returncode:
        (OUT / "suite.txt").parent.mkdir(parents=True, exist_ok=True)
        (OUT / "suite.txt").write_text(result.stdout[-20000:])
        print("pytest output: build/p9a/suite.txt")


def killed_parent(pg: Pg) -> None:
    """The exe killed from Task Manager: does Postgres outlive it, and does the next start cope?"""
    pg.server.cleanup()
    pg.forget()

    child = subprocess.Popen([*self_command(), "_hold", str(pg.data)], stdout=subprocess.PIPE, text=True)
    child.stdout.readline()
    child.kill()
    child.wait()
    note("orphan_outlives_killed_parent", running(pg))
    orphan = pg.info().pid

    _, seconds = timed(pg.start)
    note("start_after_killed_parent_s", seconds)
    note("reused_orphan", pg.info().pid == orphan)
    try:
        pg.connect().close()
        note("connect_after_killed_parent", True)
    except Exception as exc:
        note("connect_after_killed_parent", type(exc).__name__)

    # pgserver counts handles by pid in a file; a killed process never removes its own.
    pg.server.cleanup()
    pg.forget()
    note("pgserver_stops_after_killed_parent", not running(pg))
    if running(pg):
        pg.pg_ctl_stop()
        note("pg_ctl_stops_after_killed_parent", not running(pg))

    (pg.data / ".handle_pids.json").unlink(missing_ok=True)


def log_tail(pg: Pg) -> None:
    """Postgres' own log, to the console only: it names paths."""
    log = pg.data / "log"
    if log.exists():
        print("--- postgres log ---", *log.read_text(errors="replace").splitlines()[-15:], "---", sep="\n")


def crash(pg: Pg) -> None:
    """A power cut or a blue screen: every postgres process gone, postmaster.pid left behind."""
    pg.start()
    kill_tree(pg.data)
    pg.forget()
    note("postmaster_pid_left_after_crash", (pg.data / "postmaster.pid").exists())
    try:
        _, seconds = timed(pg.start)
        pg.connect().close()
        note("start_after_crash_s", seconds)
        if seconds > 5:
            log_tail(pg)
    except Exception as exc:
        note("start_after_crash_s", type(exc).__name__)
        log_tail(pg)

    (pg.data / ".handle_pids.json").unlink(missing_ok=True)


def stale_pid(pg: Pg) -> None:
    """After a reboot, the pid in a stale postmaster.pid can belong to another program."""
    if not running(pg):
        pg.start()

    pidfile = pg.data / "postmaster.pid"
    lines = pidfile.read_text().splitlines()
    kill_tree(pg.data)
    pg.forget()
    lines[0] = str(os.getpid())
    pidfile.write_text("\n".join(lines) + "\n")
    try:
        pg.start()
        pg.connect().close()
        note("start_with_reused_pid", True)
    except Exception as exc:
        note("start_with_reused_pid", type(exc).__name__)
        pidfile.unlink(missing_ok=True)
        pg.forget()
        try:
            pg.start()
            pg.connect().close()
            note("start_after_removing_stale_pid", True)
        except Exception as exc2:
            note("start_after_removing_stale_pid", type(exc2).__name__)
            log_tail(pg)

    (pg.data / ".handle_pids.json").unlink(missing_ok=True)


def cmd_hold(args) -> None:
    """The child that killed_parent kills: starts the server, says so, then waits."""
    Pg(Path(args.data)).start()
    print("ready", flush=True)
    time.sleep(600)


def cmd_sleep(args) -> None:
    base = Path(tempfile.mkdtemp(prefix="pinkas-p9a-"))
    pg = Pg(base / "pg")
    pg.initdb()
    pg.start()
    held = pg.connect()
    held.autocommit = True
    print("Wait for three 'ok' lines, put the PC to sleep for 2+ minutes, wake it, wait for three more, then Ctrl+C.")
    gaps, held_ok, fresh_ok, last = [], [], [], time.monotonic()
    try:
        while True:
            time.sleep(5)
            now = time.monotonic()
            gaps.append(round(now - last, 1))
            last = now
            row = [time.strftime("%H:%M:%S")]
            for name, results, fn in (("held", held_ok, lambda: held.cursor().execute("SELECT 1")),
                                      ("fresh", fresh_ok, lambda: pg.connect().close())):
                try:
                    fn()
                    results.append(True)
                    row.append(f"{name} ok")
                except Exception as exc:
                    results.append(False)
                    row.append(f"{name} {type(exc).__name__}")
            print("  ".join(row), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        note("longest_gap_s", max(gaps, default=0))
        note("held_connection_failures", held_ok.count(False))
        note("fresh_connection_failures", fresh_ok.count(False))
        note("last_fresh_ok", bool(fresh_ok and fresh_ok[-1]))
        pg.pg_ctl_stop()
        kill_tree(pg.data)
        shutil.rmtree(base, ignore_errors=True)
        save("sleep")


# --- Window -------------------------------------------------------------------------------------


class Page(http.server.BaseHTTPRequestHandler):
    token = secrets.token_urlsafe(32)
    seen: dict = {}

    def do_GET(self):  # noqa: N802
        self.seen.setdefault("host_headers", set()).add(self.headers.get("Host", ""))
        if self.path == f"/start?token={self.token}":
            self.send_response(303)
            self.send_header("Set-Cookie", "s=1; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Location", "/")
            self.end_headers()
            return

        cookie = "s=1" in (self.headers.get("Cookie") or "")
        self.seen["cookie_after_redirect"] = cookie
        body = f'<!doctype html><title>pinkas spike</title><body data-cookie="{cookie}"><h1>pinkas P9a: window OK</h1>'
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


def cmd_window(args) -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/start?token={Page.token}"
    if args.no_webview2:
        # The WebView2 loader takes the runtime from this folder instead of the installed one: an empty
        # folder behaves like a PC without WebView2.
        os.environ["WEBVIEW2_BROWSER_EXECUTABLE_FOLDER"] = tempfile.mkdtemp(prefix="pinkas-no-webview2-")
        note("simulated_missing_webview2", True)

    import webview

    window = webview.create_window("pinkas P9a", url, width=900, height=600)
    loaded = threading.Event()

    def watchdog():
        if loaded.wait(args.timeout + 30):
            return

        note("webview_start", "hung")
        save("window" + (" no-webview2" if args.no_webview2 else ""))
        if args.fallback:
            fallback(url)
        os._exit(3)

    threading.Thread(target=watchdog, daemon=True).start()
    started = time.monotonic()
    try:
        webview.start(probe, (window, loaded, args), gui="edgechromium" if WINDOWS else None)
        note("webview_start", "returned")
    except Exception as exc:
        note("webview_start", f"raised {type(exc).__name__}")
    note("window_open_s", round(time.monotonic() - started, 1))
    note("renderer", getattr(webview, "renderer", None))
    note("page_loaded", loaded.is_set())
    note("cookie_after_redirect", Page.seen.get("cookie_after_redirect"))
    note("host_header_is_loopback", Page.seen.get("host_headers", set()) <= {f"127.0.0.1:{port}"} or
         sorted(Page.seen.get("host_headers", ())))
    save("window" + (" no-webview2" if args.no_webview2 else ""))
    if not loaded.is_set() and args.fallback:
        fallback(url)
    server.shutdown()


def probe(window, loaded: threading.Event, args) -> None:
    import webview

    started = time.monotonic()
    if window.events.loaded.wait(args.timeout):
        loaded.set()
        note("page_loaded_s", round(time.monotonic() - started, 1))
    if loaded.is_set() and args.dialog:
        picked = window.create_file_dialog(webview.FileDialog.FOLDER)
        note("folder_dialog_returned_a_folder", bool(picked))
    if args.close_after is not None or not loaded.is_set():
        time.sleep(args.close_after or 0)
        window.destroy()


def fallback(url: str) -> None:
    import webbrowser

    print("browser opened:", webbrowser.open(url))
    input("Check the browser shows 'pinkas P9a: window OK', then press Enter.")


# --- Build --------------------------------------------------------------------------------------


def self_command() -> list[str]:
    return [sys.executable] if FROZEN else [sys.executable, str(Path(__file__).resolve())]


def cmd_build(args) -> None:
    work = OUT / "pyinstaller"
    sep = ";" if WINDOWS else ":"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--name", "p9a",
        "--distpath", str(work / "dist"), "--workpath", str(work / "work"), "--specpath", str(work),
        "--collect-all", "pixeltable_pgserver",
        # The migrations are data files to PyInstaller, so it never sees what they import.
        "--hidden-import", "sqlalchemy.dialects.postgresql.psycopg2", "--hidden-import", "logging.config",
        "--add-data", f"{ROOT / 'migrations'}{sep}migrations", "--add-data", f"{ROOT / 'alembic.ini'}{sep}.",
        str(Path(__file__).resolve()),
    ]
    if DLLS:
        bin_dir = "pixeltable_pgserver/pginstall18/bin"
        command[-1:-1] = [arg for dll in DLLS.glob("*.dll") for arg in ("--add-binary", f"{dll}{sep}{bin_dir}")]
    note("extra_dlls", sorted(dll.name for dll in DLLS.glob("*.dll")) if DLLS else [])
    result, seconds = timed(lambda: subprocess.run(command, capture_output=True, text=True))
    note("pyinstaller_ok", result.returncode == 0)
    note("pyinstaller_s", seconds)
    if result.returncode:
        print(result.stderr[-5000:])
        save("build")
        sys.exit(1)

    folder = work / "dist" / "p9a"
    files = [f for f in folder.rglob("*") if f.is_file()]
    note("folder_mb", folder_mb(folder))
    note("folder_files", len(files))
    for part in ("pginstall", "pginstall18"):
        hit = next(folder.rglob(part), None)
        note(f"{part}_mb", folder_mb(hit) if hit else None)

    exe = folder / ("p9a.exe" if WINDOWS else "p9a")
    frozen_env = {k: v for k, v in os.environ.items() if k != "PINKAS_SPIKE_DLLS"}
    frozen_env["PATH"] = os.environ["PATH"].removeprefix(f"{DLLS}{os.pathsep}") if DLLS else os.environ["PATH"]
    for sub in (["pg", "--quick"], ["window", "--close-after", "3", "--timeout", "30"]):
        run = subprocess.run([str(exe), *sub], capture_output=True, text=True, timeout=180, env=frozen_env)
        note(f"frozen_{sub[0]}_exit", run.returncode)
        print(run.stdout[-3000:], run.stderr[-3000:])

    scanner = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Defender" / "MpCmdRun.exe"
    if WINDOWS and scanner.exists():
        scan = subprocess.run([str(scanner), "-Scan", "-ScanType", "3", "-File", str(folder), "-DisableRemediation"],
                              capture_output=True, text=True)
        # 0: nothing found, 2: a threat found; anything else is the scanner's own failure.
        note("defender_exit", scan.returncode)
        note("defender_summary", [line for line in scan.stdout.splitlines() if "threat" in line.lower()][-3:])
    else:
        note("defender_exit", "no scanner")

    save("build")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    pg = sub.add_parser("pg")
    pg.add_argument("--quick", action="store_true", help="start, auth, migrate and stop only")
    sub.add_parser("sleep")
    window = sub.add_parser("window")
    window.add_argument("--no-webview2", action="store_true")
    window.add_argument("--dialog", action="store_true", help="also open the folder dialog")
    window.add_argument("--fallback", action="store_true", help="open the browser when the window fails")
    window.add_argument("--close-after", type=float, help="close the window after this many seconds")
    window.add_argument("--timeout", type=float, default=20)
    sub.add_parser("build")
    hold = sub.add_parser("_hold")
    hold.add_argument("data")
    args = parser.parse_args()
    {"pg": cmd_pg, "sleep": cmd_sleep, "window": cmd_window, "build": cmd_build, "_hold": cmd_hold}[args.command](args)


if __name__ == "__main__":
    main()
