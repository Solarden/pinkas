"""Reads the addon's export out of Pinkas.lua without running it.

The file comes from another machine, so it is parsed, never executed, and anything unexpected
rejects the whole file. Errors name a path in the export, never a value: the pull's output goes to
a log.
"""

import json
from typing import Annotated, Literal, TypedDict, Union

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, with_config

MAX_BYTES = 5_000_000
EXPORT_PREFIX = b'["export"] = "'

# In the order the page lists them.
ROUTE_KINDS = ("herb", "cloth", "leather", "ore", "enchanting")
# Trade-goods (class 7) subclasses per route kind, the pairs addon/Pinkas/Routes.lua matches in game.
KIND_OF = {(7, 9): "herb", (7, 5): "cloth", (7, 6): "leather", (7, 7): "ore", (7, 12): "enchanting"}
KIND_LABELS = {"herb": "Herbs", "cloth": "Cloth", "leather": "Leather", "ore": "Ore", "enchanting": "Enchanting"}


class ParseError(ValueError):
    pass


# The client runs Lua 5.1: these and \ddd are all it can write. Its other escape, a backslash before
# a line break, can't occur here: the export is read as a single line.
SIMPLE_ESCAPES = {
    ord("a"): 7, ord("b"): 8, ord("f"): 12, ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("v"): 11,
    ord("\\"): 92, ord('"'): 34, ord("'"): 39,
}
# Bytes, not str.isdigit(), which also accepts "²" (0xB2) and would misread raw UTF-8.
DIGITS = b"0123456789"


def unescape_lua_string(raw: bytes, start: int) -> tuple[bytes, int]:
    """Decodes the Lua string whose opening quote is at raw[start]; returns its bytes and the index
    after the closing quote. Escapes give bytes, so UTF-8 is decoded by the caller, once."""
    out = bytearray()
    i = start + 1

    while i < len(raw):
        c = raw[i]
        if c == ord('"'):
            return bytes(out), i + 1
        if c != ord("\\"):
            out.append(c)
            i += 1
            continue

        i += 1
        if i >= len(raw):
            break

        e = raw[i]
        if e in SIMPLE_ESCAPES:
            out.append(SIMPLE_ESCAPES[e])
            i += 1
        elif e in DIGITS:
            end = i
            while end < len(raw) and end - i < 3 and raw[end] in DIGITS:
                end += 1

            value = int(raw[i:end])
            if value > 255:
                raise ParseError("a decimal escape is above 255")

            out.append(value)
            i = end
        else:
            raise ParseError("an unknown escape in the export string")

    raise ParseError("the export string is not closed")


def export_text(body: bytes) -> str:
    if len(body) > MAX_BYTES:
        raise ParseError(f"the file is over {MAX_BYTES} bytes")
    found = [(n, line) for n, line in enumerate(body.split(b"\n"), 1) if line.startswith(EXPORT_PREFIX)]
    if len(found) != 1:
        raise ParseError(f"expected one export line, found {len(found)}")
    number, line = found[0][0], found[0][1].rstrip(b"\r")
    raw, end = unescape_lua_string(line, len(EXPORT_PREFIX) - 1)
    if line[end:] not in (b",", b""):
        raise ParseError(f"line {number}: unexpected text after the export string")

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ParseError(f"line {number}: the export is not valid UTF-8") from None


# The export contract, written by addon/Pinkas/Export.lua. Every key is required and no other is allowed,
# except inside an event's or total's value (below): a new field or kind bumps `v`.
# Lua 5.1 numbers are doubles, exact up to 2^53; JSON's 1e400 reads as inf and is refused here.
LARGEST = 2**53
# Every text the addon writes is a name or a label from the game.
TEXT_LIMIT = 256
# Lists are bounded by what the game and the addon hold, so pydantic builds a bounded number of objects.
CHAR_LIMIT = 200
PROFESSION_LIMIT = 20
BAG_LIMIT = 300
BANK_LIMIT = 500
MAIL_LIMIT = 100
# ATTACHMENTS_MAX_RECEIVE.
ATTACHMENT_LIMIT = 16
# ns.EVENT_CAP in addon/Pinkas/Core.lua.
EVENT_LIMIT = 500
TOTAL_LIMIT = 2000

Int = Annotated[int, Field(ge=-LARGEST, le=LARGEST)]
# The pages read these as Postgres int.
Int32 = Annotated[int, Field(ge=-(2**31), lt=2**31)]
# The addon's counters: whole and never negative.
Count = Annotated[int, Field(ge=0, le=LARGEST)]
Num = Annotated[float, Field(ge=-LARGEST, le=LARGEST)]
Text = Annotated[str, Field(max_length=TEXT_LIMIT)]


def contract(name: str, fields: dict):
    """An object with every key required and no other: "5" is no int and true is no 1. A TypedDict, not a
    model: a list of a million `{}` costs pydantic a tenth of the memory."""
    return with_config(ConfigDict(strict=True, extra="forbid"))(TypedDict(name, fields))


def value(name: str, fields: dict):
    """An event's or total's value: the fields /stats reads, typed. The addon leaves a field out when it has
    no value and writes fields /stats doesn't read, so any may be missing and the others pass."""
    return with_config(ConfigDict(strict=True, extra="allow"))(TypedDict(name, fields, total=False))


# An item cached by an older addon, or one read from mail, has no quality or class: those are null.
ITEM_V2 = {"id": Int32, "name": Text | None, "count": Int32}
ItemV2 = contract("ItemV2", ITEM_V2)
Item = contract("Item", ITEM_V2 | {"quality": Int32 | None, "class": Int32 | None, "subclass": Int32 | None})
Played = contract("Played", {"total": Int, "level": Int, "at": Int})
Profession = contract("Profession", {"skill_line": Int32 | None, "name": Text | None, "rank": Int | None, "max": Int | None})


def mailbox(item) -> type:
    mail = contract("Mail", {"from": Text, "money": Int, "items": Annotated[list[item], Field(max_length=ATTACHMENT_LIMIT)]})

    return contract("Mailbox", {"as_of": Int, "mails": Annotated[list[mail], Field(max_length=MAIL_LIMIT)]})


MailedItem = value("MailedItem", {"id": Int | None, "count": Count | None})
EVENT_VALUES = {
    # A negative time on a level: a deleted character's name came back, and the addon kept the old start.
    "level": value("LevelValue", {"level": Int | None, "played_level": Int | None}),
    "death": value("DeathValue", {"level": Int | None, "zone": Text | None, "killer": Text | None}),
    "quest": value("QuestValue", {}),
    "mail": value("MailValue", {
        "to": Text | None, "items": Annotated[list[MailedItem], Field(max_length=ATTACHMENT_LIMIT)] | None,
    }),
    # A negative duration: a crash in an instance, the run closed at the logout before it.
    "dungeon": value("DungeonValue", {
        "instance": Text | None, "deaths": Count | None, "duration": Num | None, "ended_by_logout": bool | None,
    }),
}
TOTAL_VALUES = {
    "gold": value("GoldValue", {"in": Count | None, "out": Count | None}),
    "combat": value("CombatValue", {"damage": Num | None, "healing": Num | None, "seconds": Num | None, "fights": Count | None}),
    "gather": value("GatherValue", {"count": Count | None}),
    "rested": value("RestedValue", {"xp": Count | None, "rested_used": Count | None}),
}
EVENT_KINDS, TOTAL_KINDS = set(EVENT_VALUES), set(TOTAL_VALUES)


def by_kind(name: str, fields: dict, values: dict, limit: int):
    """A list of rows told apart by `kind`, each holding its own value."""
    rows = [contract(f"{name}_{kind}", fields | {"kind": Literal[kind], "value": model}) for kind, model in values.items()]

    return Annotated[list[Annotated[Union[*rows], Field(discriminator="kind")]], Field(max_length=limit)]


CHARACTER_V2 = {
    "name": Text, "realm": Text, "class": Text | None, "race": Text | None,
    "level": Int | None, "xp": Int | None, "xp_max": Int | None, "rested": Int | None, "resting": bool | None,
    "money": Int | None, "zone": Text | None, "map": Int | None, "x": Num | None, "y": Num | None,
    "session_started_at": Int | None, "logout_at": Int | None, "played": Played | None,
    "professions": Annotated[list[Profession], Field(max_length=PROFESSION_LIMIT)],
    "bags": Annotated[list[ItemV2], Field(max_length=BAG_LIMIT)],
    "bank": contract("BankV2", {"as_of": Int, "items": Annotated[list[ItemV2], Field(max_length=BANK_LIMIT)]}) | None,
    "mailbox": mailbox(ItemV2) | None,
    "events": by_kind("Event", {"id": Int, "at": Int}, EVENT_VALUES, EVENT_LIMIT),
    "totals": by_kind("Total", {"session": Int, "key": Text}, TOTAL_VALUES, TOTAL_LIMIT),
}
CHARACTER = CHARACTER_V2 | {
    "bags": Annotated[list[Item], Field(max_length=BAG_LIMIT)],
    "bag_slots": Int | None,
    "bank": contract("Bank", {
        "as_of": Int, "slots": Int | None, "items": Annotated[list[Item], Field(max_length=BANK_LIMIT)],
    }) | None,
    "mailbox": mailbox(Item) | None,
}
HEAD = TypeAdapter(contract("Head", {
    "v": Int, "exported_at": Int, "build": Text, "routes": dict, "chars": Annotated[dict, Field(max_length=CHAR_LIMIT)],
}))
# Contract version -> the characters' type. Files written before an addon update still arrive.
CHARACTERS = {
    2: TypeAdapter(list[contract("CharacterV2", CHARACTER_V2)]),
    3: TypeAdapter(list[contract("Character", CHARACTER)]),
}


def where(loc: tuple, root: str) -> str:
    return root + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in loc)


def refused(error: ValidationError, root: str) -> ParseError:
    """The first problem as its path and pydantic's error type: no value from the file reaches the log."""
    first = error.errors(include_url=False, include_input=False)[0]

    # The last step of their path is a key, and an unknown key comes from the file.
    if first["type"] in ("extra_forbidden", "missing"):
        return ParseError(f"{where(first['loc'][:-1], root)}: the keys differ from the contract")

    return ParseError(f"{where(first['loc'], root)}: {first['type']}")


def not_json(constant: str):
    raise ParseError("the export holds NaN or Infinity, which JSON has no value for")


def parse(body: bytes) -> dict:
    """The export as JSON gave it, once it matches the contract."""
    try:
        export = json.loads(export_text(body), parse_constant=not_json)
    except json.JSONDecodeError as exc:
        raise ParseError(f"the export is not JSON (column {exc.colno})") from None
    except RecursionError:
        raise ParseError("the export is nested too deeply") from None

    try:
        HEAD.validate_python(export)
    except ValidationError as exc:
        raise refused(exc, "export") from None

    if export["v"] not in CHARACTERS:
        raise ParseError("export.v: an unknown contract version")

    check_routes(export["routes"])

    # Characters are checked by position: their keys are names.
    try:
        CHARACTERS[export["v"]].validate_python(list(export["chars"].values()))
    except ValidationError as exc:
        raise refused(exc, "chars") from None

    check_keys(export["chars"])

    return export


def check_routes(routes: dict) -> None:
    for i, (kind, recipient) in enumerate(routes.items()):
        if kind not in ROUTE_KINDS:
            raise ParseError(f"export.routes[{i}]: an unknown kind")

        if not isinstance(recipient, str) or len(recipient) > TEXT_LIMIT:
            raise ParseError(f"export.routes[{i}]: expected a name")


def check_keys(chars: dict) -> None:
    for i, (key, char) in enumerate(chars.items()):
        if key != f"{char['realm']}/{char['name']}":
            raise ParseError(f"chars[{i}]: the key is not realm/name")

        # Session totals are upserted, and one statement can't update a row twice; the addon keys them uniquely.
        keys = [(t["session"], t["kind"], t["key"]) for t in char["totals"]]
        if len(keys) != len(set(keys)):
            raise ParseError(f"chars[{i}].totals: a session total appears twice")
