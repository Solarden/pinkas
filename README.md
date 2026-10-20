# pinkas

פנקס, "notebook, ledger". A ledger of your WoW: Forever characters, for anyone who keeps a
household of alts that feed each other materials.

- **What each character holds:** bags, bank, mailbox and gold, as of its last logout.
- **How far its professions are**, against the targets you set.
- **How much rested XP it has built up**, estimated from the time since it logged out.

## How it works

1. **An addon** writes each character's state into its SavedVariables when the character logs out.
   The same addon fills mails with the materials another alt crafts with.
2. **A pull script** fetches that file from the gaming PC and stores each new snapshot in Postgres.
   It never runs the file, it only parses it.
3. **A small web app** shows all characters at once. It is meant for a home server behind a login.

pinkas only reads files the game already writes, the way log uploaders do. It does not write into
the game or automate play.

## Status

Early development.

## License

[AGPL-3.0-or-later](LICENSE). See [LICENSING.md](LICENSING.md).
