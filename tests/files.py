"""Pinkas.lua files for the tests: the beta fixtures and variants built from them."""

import json
from pathlib import Path

from pinkas.parse import parse

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "Testrealm/Fakechar"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def account() -> dict:
    """The beta's export, as a dict to edit."""
    return parse(fixture("fake_account.lua"))


def account_v3() -> dict:
    """The beta's export as the current contract writes it; the fixture predates item quality and slots."""
    export = account()
    export["v"] = 3
    char = export["chars"][KEY]
    char["bag_slots"] = 80

    for item in char["bags"]:
        item.update({"quality": 1, "class": 7, "subclass": 5})

    return export


def with_export(export: dict) -> bytes:
    """A file holding `export`, written the way the client writes one."""
    text = json.dumps(export, ensure_ascii=False).replace("\\", "\\\\").replace('"', '\\"')

    return b'\r\nPinkasDB = {\r\n["export"] = "' + text.encode() + b'",\r\n["v"] = 1,\r\n}\r\n'
