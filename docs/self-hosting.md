# Self-hosting

pinkas runs as two programs from a clone of this repository, next to a Postgres database:
- **the pull** (`python3 -m pinkas.pull`), run every few minutes: it reads the addon's file, stores what
  is new and exits;
- **the page** (`python3 -m pinkas.web`), a long-running web server behind a TLS proxy.

The game runs on Windows; the server runs on **Linux or macOS** (the pull uses Unix-only calls) and
reaches the file over ssh or a mounted share. To run everything on the game PC itself, wait for the
[Windows app](windows.md).

## Requirements

- Python 3.13 or newer.
- PostgreSQL. The test suite runs on 18.
- The Python packages listed in [pyproject.toml](../pyproject.toml): Jinja2, psycopg2, Alembic,
  SQLAlchemy, markdown-it-py and pydantic, at the versions given there. Either `uv sync --no-dev` in the
  clone (then run everything with `uv run`), or your distribution's packages at the same minor versions.
- The [addon](addon.md), installed in the game.

## 1. The database

pinkas uses two roles: an **owner** that runs the migrations, and the **app role**, named `pinkas`, that
the pull and the page use. The migrations grant the app role only what it needs: it can add snapshots
and events but never change or delete them. The name `pinkas` is fixed, because the migrations grant
to it.

As a Postgres superuser:

```sql
CREATE DATABASE pinkas;
CREATE ROLE pinkas_owner LOGIN;
CREATE ROLE pinkas LOGIN;
GRANT CONNECT ON DATABASE pinkas TO pinkas_owner, pinkas;
\c pinkas
GRANT CREATE ON SCHEMA public TO pinkas_owner;
\password pinkas_owner
\password pinkas
```

A database shared with other applications works too: every table starts with `pinkas_`, and Alembic
keeps its own version table.

## 2. The settings

Copy [.env.example](../.env.example) to a file outside the clone, readable only by the user that runs
pinkas (`chmod 600`), and fill it in. Each line is explained in the example. The ones you must set:

| Setting | What |
|---|---|
| `DATABASE_URL` | the app role's connection, used by the pull and the page |
| `MIGRATE_DATABASE_URL` | the owner's connection, used only by the migrations |
| `SESSION_SECRET` | `python3 -c 'import secrets; print(secrets.token_hex(32))'` |
| `PINKAS_PASSWORD_HASH` | `python3 -m pinkas.web --hash-password`, which asks for the password twice |
| `PINKAS_SOURCE` | a command that prints the addon's file (step 4) |

None of these values may contain `$`, a backquote or an unquoted space, so the same file can be read
by systemd's `EnvironmentFile` and by a shell's `set -a; . ./file`.

## 3. The tables

From the clone:

```sh
PINKAS_ENV=/path/to/pinkas.env python3 -m alembic upgrade head
```

Run it again after every update; when nothing changed it does nothing.

## 4. Getting the file: `PINKAS_SOURCE`

The pull runs `PINKAS_SOURCE` and reads its standard output as the file. The command is split like a
shell would split it, but no shell runs it, so pipes and `$VARIABLES` don't work. A file over 5 MB,
a command that takes more than 60 seconds, or one that exits with an error is a failed run, and
nothing is stored.

The file lives on the game PC at:

```
World of Warcraft\_<flavor>_\WTF\Account\<ACCOUNT>\SavedVariables\Pinkas.lua
```

**With the game's folder mounted** on the server (an SMB share, for example):

```sh
PINKAS_SOURCE="cat /mnt/game/WTF/Account/ACCOUNT/SavedVariables/Pinkas.lua"
```

**Over ssh**, from a home server to the game PC (Windows has an OpenSSH server built in). Use a key
made only for this, and pin it to one command on the PC, so the key can print that file and nothing
else. In the PC's authorized keys file (for an administrator account,
`C:\ProgramData\ssh\administrators_authorized_keys`), one line:

```
from="<server address>",command="<folder>\print.cmd",no-pty,no-port-forwarding,no-x11-forwarding,no-agent-forwarding ssh-ed25519 AAAA... pinkas
```

with `<folder>\print.cmd` printing the file:

```bat
@type "C:\World of Warcraft\_<flavor>_\WTF\Account\<ACCOUNT>\SavedVariables\Pinkas.lua"
```

Keep `<folder>` writable by administrators only, or anyone who can edit `print.cmd` decides what the
key runs. Then, on the server, an entry in the pull user's `~/.ssh/config`:

```
Host game-pc
    HostName <game PC>
    User <account>
    IdentityFile ~/.ssh/<key made for pinkas>
    IdentitiesOnly yes
    BatchMode yes
    ConnectTimeout 5
```

and `PINKAS_SOURCE="ssh game-pc"`. Whatever command the client asks for, the PC runs `print.cmd`.

**A PC that sleeps.** Set `PINKAS_PROBE=<game PC>:22`. The pull first checks that port for 3 seconds;
with no answer it says `asleep` and ends without an error. Checking every 5 minutes does not keep a
Windows PC awake.

## 5. Running the pull

Every 5 minutes, from the clone, with the settings in the environment. A cron line:

```
*/5 * * * * cd /path/to/pinkas && set -a && . /path/to/pinkas.env && set +a && python3 -m pinkas.pull >> /path/to/pinkas-pull.log 2>&1
```

Each run prints one line: `asleep`, `unchanged <hash>`, `ingested <hash>` with the counts of what it
stored, or the failure (`source-failed`, `too-large`, `parse-failed`). It never prints the file's
content or the connection string. A file that is broken or doesn't match the
contract stores nothing at all, and the page's "stats for nerds" panel shows the last state.

Raw files older than 30 days are deleted at the start of a run; the snapshots stay.

## 6. Running the page

```sh
python3 -m pinkas.web
```

It listens on `PINKAS_LISTEN`, by default `127.0.0.1:8094`, and speaks plain HTTP. **Put a TLS proxy
in front of it** and bind it to loopback, or to the address only the proxy reaches. It refuses to
listen on every interface (`0.0.0.0`). With Caddy:

```
pinkas.example.com {
    reverse_proxy 127.0.0.1:8094
}
```

A systemd unit keeps it running:

```ini
[Unit]
Description=pinkas
After=network-online.target

[Service]
User=pinkas
WorkingDirectory=/path/to/pinkas
EnvironmentFile=/path/to/pinkas.env
ExecStart=/usr/bin/python3 -m pinkas.web
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

The page has one password, the one you hashed in step 2. A new hash or a new `SESSION_SECRET` logs
every device out.

## 7. Your guide (optional)

`PINKAS_GUIDE=/path/to/guide.md` shows that markdown file at `/guide`, beside the characters. It is
read on every visit, so editing the file needs no restart. Raw HTML in it shows as text.

## 8. Updating

```sh
git pull --ff-only
PINKAS_ENV=/path/to/pinkas.env python3 -m alembic upgrade head
systemctl restart pinkas
```

Update the addon in the game at the same time: the pull accepts the export versions this code knows,
and refuses a file from an addon that is newer than the server.
