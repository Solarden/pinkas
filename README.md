# pinkas

פנקס, "notebook, ledger". A ledger of your WoW: Forever characters, for anyone who keeps a
household of alts that feed each other materials.

- **What each character holds:** bags, bank, mailbox and gold, as of its last logout.
- **How far its professions are**, against the targets you set.
- **How much rested XP it has built up**, estimated from the time since it logged out.
- **Who has an item**: one search across every character's bags, bank and mailbox.
- **Your own plan**, a markdown file shown as a page next to the characters.
- **How the play went**: hours a day, levels, deaths and their killers, gathering, the materials mailed
  between alts, gold in and out, dungeon runs and combat by level, as charts drawn by the server.
- **A "Route mats" button** in the game's mail window, which fills a mail with the materials another
  alt crafts with.

## How it works

1. **An addon** keeps each character's state and writes it into its SavedVariables when you log out
   or `/reload`.
2. **A pull script** reads that file and stores each new snapshot in Postgres. It never runs the
   file, it only parses it.
3. **A small web app** shows all characters at once, behind a login.

pinkas only reads files the game already writes, the way log uploaders do. It does not automate
play, and it sends nothing anywhere: your data stays on your own machines.

## Documentation

| | |
|---|---|
| [The addon](docs/addon.md) | install it, set routes, what it records |
| [Self-hosting](docs/self-hosting.md) | run the pull and the page on your own server |
| [Privacy](docs/privacy.md) | what is stored, what never is, and where it goes |
| [Windows app](docs/windows.md) | **planned:** one installer for players who don't run a server |

## Status

Built and tested; first real use at the game's launch. The addon was checked in the beta; a few
things the beta couldn't reach (gathering spell ids, some gold sources) are confirmed in the first
days of live play.

## Contributing

Issues and pull requests are welcome. Before a pull request, run:

```sh
lua tests/addon_test.lua                                   # the addon, against stubbed WoW APIs
docker compose -f docker-compose.test.yml up -d --wait     # once: a throwaway Postgres
uv run pytest -q                                           # parser, ingest, pull, pages
pre-commit run --all-files                                 # secrets, StyLua, luacheck, ruff
```

## License

[AGPL-3.0-or-later](LICENSE). See [LICENSING.md](LICENSING.md).
