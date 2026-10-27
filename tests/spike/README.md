# P9a spike: embedded Postgres and an app window on Windows

The gate before any desktop code: does an embedded Postgres hold up on a
gaming PC, and does pywebview open a window there? `p9a.py` measures it; nothing here ships.

The Actions workflow `spike-windows` runs the non-interactive part on Windows Server 2022 and 2025
(the report is the run's artifact). What only a real PC can show is below: sleep, the folder dialog,
Windows 10 or 11 as people have them, and your antivirus with today's signatures.

## What it checks

| Command | Answers |
|---|---|
| `pg` | Our own `initdb` (scram passwords, UTF-8, Unicode case folding) in a folder named with Polish letters; start and stop times; Postgres listens on 127.0.0.1 only and refuses a wrong password; `alembic upgrade head` with both roles, the app role still can't rewrite history; disk use; the whole pytest suite against it; a killed parent, a power cut, and a stale `postmaster.pid` whose pid now belongs to another program |
| `sleep` | Whether a held and a fresh connection both work after the PC sleeps |
| `window` | pywebview on WebView2: the window opens, the launch cookie (`HttpOnly`, `SameSite=Strict`) survives the redirect, the `Host` header is `127.0.0.1:<port>`; `--dialog` opens the folder picker; `--no-webview2` acts as if WebView2 were missing, `--fallback` then opens the browser |
| `build` | PyInstaller one-folder: its size, Postgres' share of it, the frozen exe running `pg --quick` and `window`, a Microsoft Defender scan of the folder |

Every result goes to `build\p9a\report.json`: timings, sizes and yes/no answers, no paths and no
passwords. That file is what to send back.

## On the gaming PC

Once, in an ordinary PowerShell (not "Run as administrator": Postgres drops administrator rights
and then can't read what an elevated shell created):

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
```

Close and reopen PowerShell so both are on the PATH, then:

```powershell
git clone https://github.com/Solarden/pinkas
cd pinkas
git switch p9-desktop
uv run --no-project --python 3.14 python packaging/winpthread.py build/p9a/dlls
$env:PINKAS_SPIKE_DLLS = "$PWD\build\p9a\dlls"
function spike { uv run --python 3.13 --with pixeltable-pgserver==0.6.0 --with pywebview==6.2.1 --with pyinstaller==6.22.3 --with tzdata==2026.5 python tests/spike/p9a.py @args }
```

`packaging/winpthread.py` fetches the one DLL the packaged Postgres 18 lacks (below). The last two lines last
for this PowerShell window; type them again in a new one. The first `spike`
downloads Python 3.13 and the packages (a few hundred MB, once).

1. `spike pg`: about two minutes, most of it the test suite. Ends with `saved to build/p9a/report.json`.
2. `spike window --dialog`: a window says **pinkas P9a: window OK**, then a folder picker opens. Pick
   any folder, then close the window.
3. `spike window --no-webview2 --fallback`: the window should fail, and your browser opens the same
   page instead. Press Enter in PowerShell when you've looked.
4. `spike sleep`: wait for three lines with `held ok  fresh ok`, put the PC to sleep (Start → Power →
   Sleep) for at least two minutes, wake it, wait for three more lines, then press Ctrl+C.
5. `spike build`: about five minutes. If Defender or another antivirus pops up a warning, write down
   what it named.
6. Send `build\p9a\report.json`, plus `build\p9a\suite.txt` if it exists (only when a test failed),
   and the PC's Windows version (`winver`).

Nothing stays running afterwards: every command stops its own Postgres and deletes its data folder
from `%TEMP%`. To remove the rest: delete the `pinkas` folder, then `uv cache clean`.

## Found on Windows 11 (the gaming PC, code page 1252), 2026-10-26

- **pixeltable-pgserver 0.6.0's Postgres 18 can't start on a clean PC**: `postgres.exe` links
  `libwinpthread-1.dll` (MinGW) and the wheel lacks it (`0xC0000135`, DLL not found). Its Postgres 16,
  and versions 0.3.1 and 0.5.1, don't need it. `winpthread.py` takes the DLL from MSYS2, both hashes pinned;
  the app would ship it beside `postgres.exe`. The runners have Strawberry Perl's copy on PATH, so
  `postgres_runs_without_extra_dlls` tells a real PC from a runner.
- **A path outside the ANSI code page breaks initdb**: Postgres takes its arguments in the ANSI code
  page (1252 on a Western Windows), so a folder `zażółć` reached initdb as
  `zaz?lc`: a user name like `Nguyễn` in `%LOCALAPPDATA%` breaks the same way. The 8.3 short
  path is ASCII and works with pg_ctl (`short_path_pg_ctl_start`), but pgserver resolves it back to
  the long one.
- **An elevated process can't run initdb on files it created**: the files belong to Administrators,
  and Postgres runs with that group as deny-only. Measured non-elevated (`runas /trustlevel:0x20000`),
  as a friend's app runs.
- **pgserver keeps Postgres' log inside the data folder**: after a crash, recovery syncs the folder,
  hits that log held open ("sharing violation") and retries for 30 s, past pgserver's own 10 s
  timeout. pg_ctl with the log outside the folder starts in about 1 s after a crash and after a stale
  pid (`own_start_*`).
- **pg_ctl -w can't check the pid on Windows** and takes a `postmaster.pid` younger than 2 s for the
  new server: the caller waits for a working connection, not for pg_ctl.
- **pgserver trusts a stale pid**: it takes any live process with the pid in `postmaster.pid` for
  Postgres and returns a dead address. Postgres and pg_ctl check the pid themselves.
- **WebView2 missing doesn't raise**: pywebview logs it and the window never loads; only a timeout on
  the page's `loaded` event tells.
- The suite passes (341, without the Unix-only pull tests) once `tzdata` is there: Windows has no zone
  database, and `tests/test_stats.py` names Europe/Warsaw. The app itself uses the local zone only.
- The frozen folder is 128 MB, 40 of them Postgres 16, which the app doesn't need. Defender found
  nothing.

## Go or no-go

Go for embedded Postgres when, on both your PC and the runners:

- `pg`: every `*_refused` and `app_role_cannot_rewrite_history` is `true`, `suite` shows no failures,
  `stopped_clean` is `true`, and the starts after a killed parent and after a power cut connect;
- `sleep`: `last_fresh_ok` is `true`;
- `build`: `frozen_pg_exit` is `0` and Defender finds nothing (`defender_exit` `0`).

A stale pid (`start_with_reused_pid`) or a killed parent that leaves Postgres running are expected
to need code in P9b, not a no-go. No-go means SQLite for everyone, never two databases, and is the
owner's call.
