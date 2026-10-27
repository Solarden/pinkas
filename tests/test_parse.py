import pytest

from pinkas.parse import MAX_BYTES, ParseError, parse, unescape_lua_string
from tests.files import KEY, account, account_v3, fixture, with_export


def test_the_beta_file_parses():
    export = account()
    char = export["chars"][KEY]

    assert (export["v"], export["build"], export["routes"]) == (2, "1.60.1.70450", {})
    assert (char["class"], char["level"], char["xp_max"]) == ("ROGUE", 10, 7600)
    assert char["bags"][0] == {"id": 6948, "name": "Hearthstone", "count": 1}
    assert char["bank"] is None and char["events"] == []


def test_a_v3_export_parses_with_quality_class_and_slots():
    char = parse(with_export(account_v3()))["chars"][KEY]

    assert char["bags"][0] == {"id": 6948, "name": "Hearthstone", "count": 1, "quality": 1, "class": 7, "subclass": 5}
    assert char["bag_slots"] == 80


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
        (lambda e, c: e.update(v=1), "contract version"),
        (lambda e, c: e.pop("build"), "export: the keys differ"),
        (lambda e, c: e.update(build=70450), r"export\.build: string_type"),
        (lambda e, c: e.update(routes={"herb": "Alt", "gems": "Alt"}), r"export\.routes\[1\]: an unknown kind"),
        (lambda e, c: e.update(routes={"herb": 1}), r"export\.routes\[0\]: expected a name"),
        (lambda e, c: c.pop("money"), r"chars\[0\]: the keys differ"),
        (lambda e, c: c.update(extra=1), r"chars\[0\]: the keys differ"),
        (lambda e, c: c.update(level="10"), r"chars\[0\]\.level: int_type"),
        (lambda e, c: c.update(resting=1), r"chars\[0\]\.resting: bool_type"),
        (lambda e, c: c["bags"][1].update(count=True), r"chars\[0\]\.bags\[1\]\.count: int_type"),
        (lambda e, c: c["bags"][1].update(id=2**31), r"chars\[0\]\.bags\[1\]\.id: less_than"),
        (lambda e, c: c.update(events=[{"id": 1, "at": 1, "kind": "kill", "value": {}}]), r"chars\[0\]\.events\[0\]: union_tag_invalid"),
        (lambda e, c: c.update(name="Other"), "not realm/name"),
        (
            lambda e, c: c.update(totals=[{"session": 1, "kind": "gold", "key": "loot", "value": {}}] * 2),
            "appears twice",
        ),
    ],
    ids=[
        "version", "no build", "numeric build", "unknown route kind", "numeric recipient", "missing key",
        "extra key", "string level", "int bool", "bool count", "item id past int", "unknown kind",
        "key mismatch", "duplicate total",
    ],
)
def test_the_contract_is_enforced(edit, message):
    with pytest.raises(ParseError, match=message):
        parse(changed(edit))


def changed_v3(edit) -> bytes:
    export = account_v3()
    edit(export, export["chars"][KEY])

    return with_export(export)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda e, c: e.update(v=4), "contract version"),
        (lambda e, c: c.pop("bag_slots"), r"chars\[0\]: the keys differ"),
        (lambda e, c: c["bags"][0].pop("quality"), r"chars\[0\]\.bags\[0\]: the keys differ"),
        (lambda e, c: c["bags"][0].update(quality="1"), r"chars\[0\]\.bags\[0\]\.quality: int_type"),
        (lambda e, c: c.update(bank={"as_of": 1, "items": []}), r"chars\[0\]\.bank: the keys differ"),
        (lambda e, c: c.update(bag_slots=80.5), r"chars\[0\]\.bag_slots: int_type"),
        (lambda e, c: c.update(bags=c["bags"][:1] * 301), r"chars\[0\]\.bags: too_long"),
    ],
    ids=["future version", "no bag slots", "v2 item", "string quality", "v2 bank", "float slots", "too many bag items"],
)
def test_the_v3_contract_is_enforced(edit, message):
    with pytest.raises(ParseError, match=message):
        parse(changed_v3(edit))


def test_a_v2_export_refuses_v3_fields():
    with pytest.raises(ParseError, match=r"chars\[0\]: the keys differ"):
        parse(changed(lambda e, c: c.update(bag_slots=80)))


def test_null_is_allowed_where_the_contract_allows_it():
    def unknown(e, c):
        c.update(level=None, x=None, played=None)

    assert parse(changed(unknown))["chars"][KEY]["level"] is None


@pytest.mark.parametrize(
    ("edit", "secret"),
    [(lambda e, c: c.update(zone=12345678), "12345678"), (lambda e, c: c.update(Secretname=1), "Secretname")],
    ids=["value", "unknown key"],
)
def test_an_error_never_carries_a_value(edit, secret):
    with pytest.raises(ParseError) as caught:
        parse(changed(edit))

    assert secret not in str(caught.value) and "Fakechar" not in str(caught.value)


def with_value(part: str, kind: str, value: dict):
    if part == "events":
        row = {"id": 1, "at": 1, "kind": kind, "value": value}
    else:
        row = {"session": 1, "kind": kind, "key": "k", "value": value}

    return lambda e, c: c.update({part: [row]})


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (with_value("events", "level", {"level": "5"}), r"events\[0\]\.level\.value\.level: int_type"),
        (with_value("events", "death", {"zone": ["x"]}), r"events\[0\]\.death\.value\.zone: string_type"),
        (with_value("events", "mail", {"to": "Testrealm/Alt", "items": [5]}), r"mail\.value\.items\[0\]: dict_type"),
        (with_value("events", "mail", {"items": [{"id": 1, "count": "3"}]}), r"mail\.value\.items\[0\]\.count: int_type"),
        (with_value("events", "dungeon", {"deaths": "1"}), r"events\[0\]\.dungeon\.value\.deaths: int_type"),
        (with_value("totals", "gold", {"in": "5"}), r"totals\[0\]\.gold\.value\.in: int_type"),
    ],
    ids=["string level", "list zone", "item not an object", "string count", "string deaths", "string gold"],
)
def test_a_value_field_stats_reads_is_checked(edit, message):
    with pytest.raises(ParseError, match=message):
        parse(changed(edit))


def test_a_value_field_may_be_missing_null_or_unknown():
    death = {"level": 9, "zone": None, "recap_id": 3}

    assert parse(changed(with_value("events", "death", death)))["chars"][KEY]["events"][0]["value"] == death


@pytest.mark.parametrize(
    "edit",
    [lambda e, c: c.update(level=2**60), with_value("events", "level", {"level": 10**400}),
     with_value("totals", "combat", {"damage": 1e308})],
    ids=["huge level", "huge level event", "huge damage"],
)
def test_a_number_past_what_lua_writes_is_refused(edit):
    with pytest.raises(ParseError, match="less_than_equal"):
        parse(changed(edit))


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (with_value("events", "mail", {"items": [{"id": 1, "count": 5e-324}]}), r"items\[0\]\.count: int_type"),
        (with_value("totals", "gather", {"count": -9}), r"gather\.value\.count: greater_than_equal"),
        (with_value("totals", "rested", {"xp": 0.5}), r"rested\.value\.xp: int_type"),
        (lambda e, c: c.update(zone="x" * 257), r"chars\[0\]\.zone: string_too_long"),
        (lambda e, c: c.update(professions=c["professions"][:1] * 21), r"chars\[0\]\.professions: too_long"),
        (with_value("events", "mail", {"items": [{}] * 17}), r"mail\.value\.items: too_long"),
        (lambda e, c: c.update(bags=c["bags"][:1] * 301), r"chars\[0\]\.bags: too_long"),
        (lambda e, c: c.update(events=[{"id": i, "at": 1, "kind": "quest", "value": {}} for i in range(501)]),
         r"chars\[0\]\.events: too_long"),
    ],
    ids=["tiny count", "negative count", "fractional xp", "long text", "too many professions", "too many attachments",
         "too many bag items", "too many events"],
)
def test_counts_texts_and_lists_are_bounded(edit, message):
    with pytest.raises(ParseError, match=message):
        parse(changed(edit))


def test_too_many_characters_are_refused():
    export = account()
    char = export["chars"][KEY]
    export["chars"] = {f"Testrealm/C{i}": {**char, "name": f"C{i}"} for i in range(201)}

    with pytest.raises(ParseError, match=r"export\.chars: too_long"):
        parse(with_export(export))


def test_a_negative_run_duration_still_parses():
    run = {"instance": "Ragefire Chasm", "deaths": 0, "duration": -30, "ended_by_logout": True}

    assert parse(changed(with_value("events", "dungeon", run)))["chars"][KEY]["events"][0]["value"] == run


def test_a_negative_time_on_a_level_still_parses():
    level = {"level": 2, "played_level": -499400}

    assert parse(changed(with_value("events", "level", level)))["chars"][KEY]["events"][0]["value"] == level
