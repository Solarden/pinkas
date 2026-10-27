# The addon

The Pinkas addon does two things in WoW: Forever:
- it keeps a ledger of each character (bags, bank, mailbox, professions, rested XP, what happened in
  play) and writes it to the game's SavedVariables, where the [pull](self-hosting.md) reads it;
- it adds a **Route mats** button to the mail window, which fills a mail with the materials one of
  your alts crafts with.

## Install

There is no release zip yet. Copy the `addon/Pinkas` folder of this repository into the game:

```
World of Warcraft\_<flavor>_\Interface\AddOns\Pinkas\
```

`_<flavor>_` is the client's own folder (the beta's was `_classic_beta_`). Restart the game, and check
that **Pinkas** is enabled in the AddOns list on the character screen.

## When it saves

The game writes SavedVariables only when you **log out, `/reload`, or quit cleanly**. Until then,
nothing reaches the file. A crash loses that session's state; the next clean logout replaces it.

The file is per game account, so it holds every character of that account:

```
World of Warcraft\_<flavor>_\WTF\Account\<ACCOUNT>\SavedVariables\Pinkas.lua
```

A character appears in it after its first logout with the addon installed.

## Routes and the Route mats button

A route says which alt gets one kind of material:

| Kind | What counts |
|---|---|
| `herb` | herbs |
| `cloth` | cloth |
| `leather` | leather and hides |
| `ore` | metal and stone |
| `enchanting` | enchanting materials |

Set them once, on any character of the account (routes are shared by the whole account):

```
/pinkas route herb Alchemistname
/pinkas route cloth Tailorname
/pinkas route herb              (no name: turns the herb route off)
/pinkas routes                  (lists them)
```

No route is set when you install the addon. A route with no recipient is skipped. Once a route is set in the
[Windows app](windows.md), the app's routes replace these, and `/pinkas route` says so.

At a mailbox, open the **Send Mail** tab. The **Route mats** button sits just outside the right edge of
the mail window. Each click fills one mail:
- the first kind, in the order above, that has a recipient and items in your bags;
- up to the mail's attachment limit;
- with the recipient and the kind as the subject.

**It never sends.** Check the mail and press Send yourself. A character never mails itself, and an item
already attached or being moved is skipped. Click again for the next kind.

## What it records

| What | When |
|---|---|
| Name, realm, class, race, level, XP, rested XP, money, zone | as they change |
| Professions and their skill | as they change |
| Bags | as they change |
| Bank | when you open it (with the time it was seen) |
| Mailbox: items and money per mail | when you open it (with the time it was seen) |
| Level ups, with `/played` for the level | at each level |
| Deaths: where, and what killed you | at each death |
| Quests turned in: XP and money | at each turn-in |
| Mail to your own alts: items and counts | when it is sent |
| Dungeon runs: which, how long, deaths inside | at the exit |
| Per session: your own damage, healing and damage taken by level; gold in and out by source; gathering by zone; rested XP used | summed while you play |

It keeps up to 500 events per character, the newest ones.

**What it never records** is in [privacy.md](privacy.md): other players' names, chat, mail subjects
and bodies.

## The `/played` line

To know how long each level took, the addon asks the game for `/played` at login and at each level.
It hides the game's answer to its own request, so your chat stays clean. A `/played` you type still
shows.

## If something goes wrong

The addon never shows Lua errors from its own handlers: a failing handler is recorded in
`PinkasDB.faults` in the same SavedVariables file, and the rest keeps working. To check from the game:

```
/dump PinkasDB.faults
```

An empty or missing table means no faults.
