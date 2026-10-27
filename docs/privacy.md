# Privacy

pinkas keeps a ledger of **your own** characters, on **your own** machines. Nothing is sent anywhere:
the addon makes no network calls, and neither does the server. There is no telemetry.

## Where the data goes

1. The addon keeps it in the game's SavedVariables file on your PC, one file per game account
   (`WTF\Account\<ACCOUNT>\SavedVariables\Pinkas.lua`), holding every character of that account.
2. The pull copies that file to your server and stores what it holds in your Postgres database. The
   raw copy of each file is deleted after 30 days; what was read from it stays.
3. The page shows it to whoever knows its password.

## What is recorded

Your characters' state and what they did in play: bags, bank, mailbox, gold, professions, XP and
rested XP, levels, deaths, quests, dungeon runs, gathering, your own damage and healing, and mail
between your own alts. The full list, with when each is read, is in [addon.md](addon.md#what-it-records).

## What is never recorded

- **Other players' names.** The game's damage meter and death recap list everyone around you; the
  addon reads only your own row. A killer who is another player is stored as `player`, and one the
  game doesn't flag clearly is not named at all.
- **Mail with other players.** A mail you send to someone outside your account leaves no trace. A
  mail you receive from one shows its sender as `other`.
- **Mail subjects and bodies**, of any mail.
- **Chat.** The addon only hides the game's answer to its own `/played` request; it reads nothing
  else from the chat.
- **Your login, your Battle.net account or anything outside the game.**

**One exception you control:** a route's recipient is stored as you typed it
(`/pinkas route herb <Name>`). It is meant to be your own alt, but the addon can't check that, because
an alt that hasn't logged in with the addon yet is unknown to it.

## Things to keep out of public places

- **The SavedVariables path contains your account name.** Share the file or its path only with
  people you would tell your account name to.
- **The file itself** lists your characters, realms, gold and items. The test files in this repository
  are made up.
- The page's "stats for nerds" panel shows the time of your last logout. It appears only after login.

## Removing it

- In the game: remove the `Pinkas` folder from `Interface\AddOns`, and delete `Pinkas.lua` (and
  `Pinkas.lua.bak`) from each account's `SavedVariables` folder.
- On the server: drop the database, or every table whose name starts with `pinkas_` (Alembic's
  `pinkas_alembic_version` among them).
