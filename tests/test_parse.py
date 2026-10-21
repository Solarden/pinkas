import pytest

from pinkas.parse import MAX_BYTES, ParseError, parse, unescape_lua_string
from tests.files import KEY, account, fixture, with_export


def test_the_beta_file_parses():
    export = account()
    char = export["chars"][KEY]

    assert export["v"] == 1
    assert (char["class"], char["level"], char["xp_max"]) == ("SHAMAN", 10, 7600)
    assert char["bags"][0] == {"id": 6948, "name": "Hearthstone", "count": 1}
    assert char["bank"] is None and char["events"] == []


@pytest.mark.parametrize("name", ["fake_nonascii.lua", "fake_nonascii_ddd.lua"], ids=["raw UTF-8", "ddd escapes"])
def test_a_non_ascii_name_comes_back_whole(name):
    assert "Testrealm/Zoë" in parse(fixture(name))["chars"]


def test_escapes_are_bytes():
    raw, end = unescape_lua_string(b'"a\\"b\\\\c\\104\\n",', 0)

    assert (raw, end) == (b'a"b\\ch\n', 15)
    # \ddd takes at most three digits: \0491 is "1" then "1".
    assert unescape_lua_string(b'"\\0491"', 0)[0] == b"11"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"x" * (MAX_BYTES + 1), "over"),
        (b"PinkasDB = {\r\n}\r\n", "found 0"),
        (b'["export"] = "{}",\r\n["export"] = "{}",\r\n', "found 2"),
        (b'["export"] = "{\\q}",\r\n', "unknown escape"),
        (b'["export"] = "{\\300}",\r\n', "above 255"),
        (b'["export"] = "{",\r\n', "not JSON"),
        (b'["export"] = "{}" .. x,\r\n', "unexpected text"),
        (b'["export"] = "\xff",\r\n', "UTF-8"),
        (b'["export"] = "{}\r\n', "not closed"),
        (b'["export"] = "{\\"v\\":NaN}",\r\n', "NaN or Infinity"),
        (b'["export"] = "' + b"[" * 100000 + b'",\r\n', "nested too deeply"),
    ],
    ids=[
        "too big", "no export", "two exports", "bad escape", "ddd over 255", "bad JSON", "trailing text",
        "bad UTF-8", "unclosed", "NaN", "deep nesting",
    ],
)
def test_a_broken_file_is_refused(body, message):
    with pytest.raises(ParseError, match=message):
        parse(body)


def changed(edit) -> bytes:
    export = account()
    edit(export, export["chars"][KEY])

    return with_export(export)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda e, c: e.update(v=2), "contract version"),
        (lambda e, c: c.pop("money"), r"chars\[0\]: the keys differ"),
        (lambda e, c: c.update(extra=1), r"chars\[0\]: the keys differ"),
        (lambda e, c: c.update(level="10"), r"chars\[0\]\.level: expected int"),
        (lambda e, c: c.update(resting=1), r"chars\[0\]\.resting: expected bool"),
        (lambda e, c: c["bags"][1].update(count=True), r"chars\[0\]\.bags\[1\]\.count: expected int"),
        (lambda e, c: c.update(events=[{"id": 1, "at": 1, "kind": "kill", "value": {}}]), "unknown kind"),
        (lambda e, c: c.update(name="Other"), "not realm/name"),
        (
            lambda e, c: c.update(totals=[{"session": 1, "kind": "gold", "key": "loot", "value": {}}] * 2),
            "appears twice",
        ),
    ],
    ids=[
        "version", "missing key", "extra key", "string level", "int bool", "bool count", "unknown kind",
        "key mismatch", "duplicate total",
    ],
)
def test_the_contract_is_enforced(edit, message):
    with pytest.raises(ParseError, match=message):
        parse(changed(edit))


def test_null_is_allowed_where_the_contract_allows_it():
    def unknown(e, c):
        c.update(level=None, x=None, played=None)

    assert parse(changed(unknown))["chars"][KEY]["level"] is None


def test_an_error_never_carries_a_value():
    with pytest.raises(ParseError) as caught:
        parse(changed(lambda e, c: c.update(zone=12345678)))

    assert "12345678" not in str(caught.value) and "Fakechar" not in str(caught.value)
