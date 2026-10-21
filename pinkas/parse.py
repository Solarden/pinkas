"""Reads the addon's export out of Pinkas.lua without running it.

The file comes from another machine, so it is parsed, never executed, and anything unexpected
rejects the whole file. Errors name a path in the export, never a value: the pull's output goes to
a log.
"""

import json

MAX_BYTES = 5_000_000
EXPORT_PREFIX = b'["export"] = "'

EVENT_KINDS = {"level", "death", "quest", "mail", "dungeon"}
TOTAL_KINDS = {"gold", "combat", "gather", "rested"}
# In the order the page lists them.
ROUTE_KINDS = ("herb", "cloth", "leather", "ore", "enchanting")


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


# The export contract, written by addon/Pinkas/Export.lua. Every key is required and no other is allowed:
# a new field or kind is a contract change, and the addon bumps `v` for it.
INT, NUM, STR, BOOL, OBJ = "int", "num", "str", "bool", "obj"


def nullable(spec):
    return ("null", spec)


ITEM = {"id": INT, "name": nullable(STR), "count": INT}
CHARACTER = {
    "name": STR, "realm": STR, "class": nullable(STR), "race": nullable(STR),
    "level": nullable(INT), "xp": nullable(INT), "xp_max": nullable(INT), "rested": nullable(INT),
    "resting": nullable(BOOL), "money": nullable(INT), "zone": nullable(STR), "map": nullable(INT),
    "x": nullable(NUM), "y": nullable(NUM), "session_started_at": nullable(INT),
    "logout_at": nullable(INT), "played": nullable({"total": INT, "level": INT, "at": INT}),
    "professions": [{"skill_line": nullable(INT), "name": nullable(STR), "rank": nullable(INT), "max": nullable(INT)}],
    "bags": [ITEM],
    "bank": nullable({"as_of": INT, "items": [ITEM]}),
    "mailbox": nullable({"as_of": INT, "mails": [{"from": STR, "money": INT, "items": [ITEM]}]}),
    "events": [{"id": INT, "at": INT, "kind": ("enum", EVENT_KINDS), "value": OBJ}],
    "totals": [{"session": INT, "kind": ("enum", TOTAL_KINDS), "key": STR, "value": OBJ}],
}


def check(value, spec, path: str) -> None:
    if isinstance(spec, tuple) and spec[0] == "null":
        if value is not None:
            check(value, spec[1], path)

        return

    if isinstance(spec, tuple) and spec[0] == "enum":
        if value not in spec[1]:
            raise ParseError(f"{path}: an unknown kind")

        return

    if isinstance(spec, list):
        if not isinstance(value, list):
            raise ParseError(f"{path}: expected a list")

        for i, item in enumerate(value):
            check(item, spec[0], f"{path}[{i}]")

        return

    if isinstance(spec, dict):
        if not isinstance(value, dict):
            raise ParseError(f"{path}: expected an object")
        if value.keys() != spec.keys():
            raise ParseError(f"{path}: the keys differ from the contract")

        for key, sub in spec.items():
            check(value[key], sub, f"{path}.{key}")

        return

    # bool is an int in Python, so it is excluded from the numbers explicitly.
    ok = {
        INT: isinstance(value, int) and not isinstance(value, bool),
        NUM: isinstance(value, int | float) and not isinstance(value, bool),
        STR: isinstance(value, str),
        BOOL: isinstance(value, bool),
        OBJ: isinstance(value, dict),
    }[spec]

    if not ok:
        raise ParseError(f"{path}: expected {spec}")


def not_json(constant: str):
    raise ParseError("the export holds NaN or Infinity, which JSON has no value for")


def parse(body: bytes) -> dict:
    try:
        export = json.loads(export_text(body), parse_constant=not_json)
    except json.JSONDecodeError as exc:
        raise ParseError(f"the export is not JSON (column {exc.colno})") from None
    except RecursionError:
        raise ParseError("the export is nested too deeply") from None

    check(export, {"v": INT, "exported_at": INT, "build": STR, "routes": OBJ, "chars": OBJ}, "export")
    if export["v"] != 2:
        raise ParseError("export.v: an unknown contract version")

    for i, (kind, recipient) in enumerate(export["routes"].items()):
        if kind not in ROUTE_KINDS:
            raise ParseError(f"export.routes[{i}]: an unknown kind")
        if not isinstance(recipient, str):
            raise ParseError(f"export.routes[{i}]: expected str")

    # Characters are addressed by position: their keys are names.
    for i, (key, char) in enumerate(export["chars"].items()):
        check(char, CHARACTER, f"chars[{i}]")
        if key != f"{char['realm']}/{char['name']}":
            raise ParseError(f"chars[{i}]: the key is not realm/name")

        # Session totals are upserted, and one statement can't update a row twice; the addon keys them uniquely.
        keys = [(t["session"], t["kind"], t["key"]) for t in char["totals"]]
        if len(keys) != len(set(keys)):
            raise ParseError(f"chars[{i}].totals: a session total appears twice")

    return export
