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


def _promo_booking(bid, guests=1, suite=False, nights=7, sail="20261115"):
    return {"bookingId": bid, "sailDate": sail, "numberOfNights": nights,
            "stateroomType": "D" if suite else "B",
            "passengersInStateroom": [{"firstName": f"G{i}", "lastName": "Test"}
                                      for i in range(guests)]}


def test_upcoming_earnings_old_vs_new_promo_math():
    """Old promo doubles the whole rate ((base+suite+solo) x2); the new promo
    doubles only base+suite and pays solo single ((base+suite) x2 + solo)."""
    from CheckRoyalCaribbeanCruiseHistory import upcoming_earnings

    solo = _promo_booking("1234567", guests=1)
    suite_solo = _promo_booking("2345678", guests=1, suite=True)
    couple_suite = _promo_booking("3456789", guests=2, suite=True)

    def pts(bookings, **kw):
        return {str(b["bookingId"]): row[2]
                for b in bookings for row in upcoming_earnings([b], None, **kw)}

    base = pts([solo, suite_solo, couple_suite])
    assert base == {"1234567": 14, "2345678": 21, "3456789": 14}  # 7n x2 / x3 / x2

    old = pts([solo, suite_solo, couple_suite],
              promo_ids=frozenset({"1234567", "2345678", "3456789"}))
    assert old == {"1234567": 28, "2345678": 42, "3456789": 28}   # whole rate x2

    new = pts([solo, suite_solo, couple_suite],
              new_promo_ids=frozenset({"1234567", "2345678", "3456789"}))
    # solo: (1)x2+1=3/n -> 21; suite solo: (2)x2+1=5/n -> 35; couple suite: (2)x2=4/n -> 28
    assert new == {"1234567": 21, "2345678": 35, "3456789": 28}


def test_new_promo_wins_when_booking_given_to_both_flags():
    from CheckRoyalCaribbeanCruiseHistory import upcoming_earnings
    b = _promo_booking("1234567", guests=1)
    rows = upcoming_earnings([b], None,
                             promo_ids=frozenset({"1234567"}),
                             new_promo_ids=frozenset({"1234567"}))
    assert rows[0][2] == 21          # new math (3/night), not old (4/night)
    assert "new double-points" in rows[0][3]
