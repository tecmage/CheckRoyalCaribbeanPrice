"""Pure-helper tests for CheckRoyalCaribbeanCruiseHistory (no network, no display)."""

from CheckRoyalCaribbeanCruiseHistory import mask_username, missing_note


def test_mask_username_masks_domain():
    assert mask_username("jo@gmail.com") == "jo@g…"
    assert mask_username("family.member@aol.com") == "family.member@a…"


def test_mask_username_passthrough_and_empty():
    # No @ -> nothing to mask; empty/None-ish -> empty string for callers to default
    assert mask_username("not-an-email") == "not-an-email"
    assert mask_username("") == ""
    assert mask_username(None) == ""


def test_missing_note_combines_both_reasons():
    note = missing_note(["jo@g…"], ["bo@a…"])
    assert note == "(not included: jo@g… - login failed; bo@a… - no sailings on record)"


def test_missing_note_single_reason():
    assert missing_note([], ["bo@a…"]) == "(not included: bo@a… - no sailings on record)"
    assert missing_note(["jo@g…"], []) == "(not included: jo@g… - login failed)"


def test_missing_note_empty_is_none():
    assert missing_note([], []) is None
