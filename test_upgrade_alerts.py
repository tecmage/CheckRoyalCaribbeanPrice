"""
Upgrade-alert decision tests for CheckRoyalCaribbeanUpgrades.

Pins the contract of is_upgrade_candidate, including the live regression where
a balcony 1B booking (its category sold out, so no row carried its class) was
alerted to "upgrade" to interior 4U because the unknown class fell back to -1
and every real class outranked it.

Also pins money()/delta() formatting (None handling and the pad-before-color
column-alignment invariant) and read_ledger's classification of the amend-page
prices[] ledger (NRD vs refundable deposits, casino-rate detection, and the
amount extraction per price code) against synthetic fixtures - no network.
"""
import re

import CheckRoyalCaribbeanPrice as crccl
from CheckRoyalCaribbeanUpgrades import (TYPE_RANK, delta, is_upgrade_candidate,
                                         money, read_ledger)

INTERIOR = TYPE_RANK["INTERIOR"]
BALCONY = TYPE_RANK["BALCONY"]
DELUXE = TYPE_RANK["DELUXE"]


def test_higher_class_is_upgrade():
    assert is_upgrade_candidate(BALCONY, 1500.0, DELUXE, 2200.0) is True
    assert is_upgrade_candidate(INTERIOR, 900.0, BALCONY, 1900.0) is True


def test_lower_class_is_never_upgrade():
    # The reported bug's shape: interior offered against a balcony booking
    assert is_upgrade_candidate(BALCONY, 1500.0, INTERIOR, 1600.0) is False
    assert is_upgrade_candidate(BALCONY, None, INTERIOR, 900.0) is False


def test_same_class_pricier_is_upgrade():
    # e.g. Balcony 2D -> Spacious Balcony 4B
    assert is_upgrade_candidate(BALCONY, 1500.0, BALCONY, 1900.0) is True


def test_same_class_cheaper_or_unpriced_booked_is_not():
    assert is_upgrade_candidate(BALCONY, 1500.0, BALCONY, 1200.0) is False
    # booked category not priced -> can't call a same-class sibling an upgrade
    assert is_upgrade_candidate(BALCONY, None, BALCONY, 1900.0) is False


def test_unknown_booked_class_never_alerts():
    # Regression: booked 1B (balcony) sold out, class unknown -> the old code
    # used rank -1 and flagged interior 4U (+$113.50) as an upgrade
    assert is_upgrade_candidate(None, None, INTERIOR, 966.08) is False
    assert is_upgrade_candidate(None, None, DELUXE, 5000.0) is False


def test_unknown_row_class_never_alerts():
    # A row type missing from TYPE_RANK must not be treated as any rank
    assert is_upgrade_candidate(BALCONY, 1500.0, None, 9000.0) is False


def test_same_class_niche_products_are_not_upgrades():
    # Live regression: booked interior 4V, "2W Studio Interior" priced +$161
    # above it - a smaller solo cabin is not an upgrade just because pricier
    assert is_upgrade_candidate(INTERIOR, 831.14, INTERIOR, 992.14,
                                'Studio Interior') is False
    assert is_upgrade_candidate(BALCONY, 1500.0, BALCONY, 1900.0,
                                'Ocean View Balcony (Obstructed)') is False
    assert is_upgrade_candidate(BALCONY, 1500.0, BALCONY, 1900.0,
                                'Partial View Balcony') is False
    # the docstring's canonical same-class upgrade still qualifies
    assert is_upgrade_candidate(BALCONY, 1500.0, BALCONY, 1900.0,
                                'Spacious Ocean View Balcony') is True


def test_class_jump_niche_products_still_upgrade():
    # For a solo guest, interior -> studio BALCONY is a real class upgrade
    assert is_upgrade_candidate(INTERIOR, 900.0, BALCONY, 1400.0,
                                'Studio Ocean View Balcony') is True


##################################
# money() / delta() formatting
##################################
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def visible(s: str) -> str:
    return ANSI.sub("", s)


def test_money_formatting_and_none_placeholder():
    assert money(None) == "-"
    assert money(1234.5) == "$1,234.50"
    assert money(0) == "$0.00"


def test_delta_none_is_plain_placeholder_at_full_width():
    # None -> the placeholder right-justified to the column width, no colour
    assert delta(None) == "-".rjust(12)
    assert delta(None, 8) == "-".rjust(8)


def test_delta_visible_width_is_constant():
    # Pad-before-colour invariant: whatever the sign (or None), the VISIBLE
    # width (ANSI stripped) is the column width, so table columns stay aligned
    for v in (123.45, -123.45, 0.0, 0.001, -0.004, 1500.0, None):
        assert len(visible(delta(v))) == 12, f"width broken for {v!r}"
    for v in (5.0, -5.0, None):
        assert len(visible(delta(v, 15))) == 15


def test_delta_sign_and_colour():
    # negatives (savings) and ~zero are green; positives are uncoloured
    assert visible(delta(-50.0)).strip() == "-$50.00"
    assert delta(-50.0) != visible(delta(-50.0))          # colour applied
    assert visible(delta(0.001)).strip() == "$0.00"
    assert delta(0.001) != visible(delta(0.001))          # colour applied
    assert delta(50.0) == "+$50.00".rjust(12)             # no colour codes


##################################
# read_ledger classification
##################################
def _patch_prices(monkeypatch, prices):
    """read_ledger reaches the network only through crccl.get_dining_and_prices."""
    monkeypatch.setattr(crccl, "get_dining_and_prices",
                        lambda account, booking: {"prices": prices})


def test_read_ledger_nrd_fare_and_amounts(monkeypatch):
    _patch_prices(monkeypatch, [
        {"priceTypeCode": "ORIGINAL_CRUISE_FARE", "amount": 1500.0},
        {"priceTypeCode": "DISCOUNTED_CRUISE_FARE", "amount": 1100.0},
        {"priceTypeCode": "DISCOUNT", "amount": -400.0, "priceItems": [
            {"code": "PROMO1", "description": "Savings Promotion",
             "amount": -400.0, "promoCd": "SAV400",
             "refundability": "DEPOSIT_NOT_REFUNDABLE"},
        ]},
        {"priceTypeCode": "TAXES_AND_FEES", "amount": 134.56},
        {"priceTypeCode": "GROSS_TOTALS", "amount": 1234.56},
        {"priceTypeCode": "PAYMENTS_APPLIED", "amount": 984.56},
        {"priceTypeCode": "BALANCE_DUE", "amount": 250.0},
    ])
    ledger = read_ledger(None, {})
    assert ledger["deposit_type"] == "NRD"
    assert ledger["is_casino"] is False
    assert ledger["casino_items"] == []
    assert ledger["gross"] == 1234.56
    assert ledger["original_fare"] == 1500.0
    assert ledger["discount"] == -400.0
    assert ledger["taxes"] == 134.56
    assert ledger["balance_due"] == 250.0
    assert [i["promo"] for i in ledger["promo_items"]] == ["SAV400"]


def test_read_ledger_refundable_deposit(monkeypatch):
    _patch_prices(monkeypatch, [
        {"priceTypeCode": "GROSS_TOTALS", "amount": 2000.0},
        {"priceTypeCode": "DISCOUNT", "amount": -100.0, "priceItems": [
            {"code": "PROMO2", "description": "Loyalty Savings",
             "amount": -100.0, "promoCd": "LOY100",
             "refundability": "REFUNDABLE"},
        ]},
    ])
    ledger = read_ledger(None, {})
    assert ledger["deposit_type"] == "REFUNDABLE"
    assert ledger["is_casino"] is False
    # unlisted codes come back None, not 0
    assert ledger["balance_due"] is None
    assert ledger["taxes"] is None


def test_read_ledger_detects_casino_booking(monkeypatch):
    # Casino markers live in DISCOUNT and OPTIONS priceItems descriptions
    _patch_prices(monkeypatch, [
        {"priceTypeCode": "GROSS_TOTALS", "amount": 350.0},
        {"priceTypeCode": "DISCOUNT", "amount": -900.0, "priceItems": [
            {"code": "CAS1", "description": "Casino Discount - GOBO",
             "amount": -900.0, "promoCd": "GOBO"},
        ]},
        {"priceTypeCode": "OPTIONS", "amount": 0.0, "priceItems": [
            {"code": "CAS2", "description": "ClubR Instant Reward",
             "amount": 0.0, "promoCd": None},
        ]},
    ])
    ledger = read_ledger(None, {})
    assert ledger["is_casino"] is True
    assert [i["desc"] for i in ledger["casino_items"]] == \
        ["Casino Discount - GOBO", "ClubR Instant Reward"]
    assert ledger["casino_items"][0]["promo"] == "GOBO"
    # casino items are not double-counted as ordinary promos
    assert ledger["promo_items"] == []
    # no refundability on the casino items -> deposit type stays unknown
    assert ledger["deposit_type"] is None


def test_read_ledger_empty_prices(monkeypatch):
    _patch_prices(monkeypatch, [])
    ledger = read_ledger(None, {})
    assert ledger["gross"] is None
    assert ledger["is_casino"] is False
    assert ledger["deposit_type"] is None
    assert ledger["casino_items"] == [] and ledger["promo_items"] == []


def test_reservation_header_uses_friendly_names(monkeypatch):
    import CheckRoyalCaribbeanUpgrades as up
    monkeypatch.setattr(up, "friendly_names", {"1234567": "Summer Cruise"})
    assert up.reservation_header("1234567") == "Reservation #1234567 (Summer Cruise)"
    # unnamed and non-string ids fall back to the bare number
    assert up.reservation_header(7654321) == "Reservation #7654321"


##################################
# DP340 detection and application
##################################
def test_dp340_eligibility_math():
    import CheckRoyalCaribbeanUpgrades as up

    class _Acct:
        def __init__(self, royal): self.is_royal = royal

    assert up.dp340_eligible(_Acct(True), 340) is True
    assert up.dp340_eligible(_Acct(True), 339) is False
    assert up.dp340_eligible(_Acct(True), None) is False
    assert up.dp340_eligible(_Acct(False), 500) is False  # Royal-only benefit


def test_read_ledger_records_dp340_promo(monkeypatch):
    _patch_prices(monkeypatch, [
        {"priceTypeCode": "DISCOUNT", "amount": -800.0, "priceItems": [
            {"code": "DPLUS", "description": "Diamond Plus Single Supplement",
             "amount": -800.0, "promoCd": "DP340", "refundability": "REFUNDABLE"},
        ]},
        {"priceTypeCode": "GROSS_TOTALS", "amount": 1200.0},
    ])
    ledger = read_ledger(None, {})
    assert any(i.get("promo") == "DP340" for i in ledger["promo_items"])


def test_sailing_inventory_sends_dp340_param_only_when_asked(monkeypatch):
    import CheckRoyalCaribbeanUpgrades as up
    captured = []

    def fake_rsc_get(account, url, params):
        captured.append(dict(params))
        return None  # short-circuits after the request - params are what we test

    monkeypatch.setattr(up, "_rsc_get", fake_rsc_get)
    monkeypatch.setattr(up, "log", lambda *a, **k: None)

    class _Acct:
        url_brand = "royalcaribbean"

    booking = {"sailDate": "20270815", "packageCode": "WN07X123",
               "passengersInStateroom": [{"firstName": "Solo"}]}
    up.get_sailing_inventory(_Acct(), booking, "123456", dp340=True)
    assert captured[0].get("r0i") == "DP340"
    # empty result triggers the retry-without-code fallback
    assert len(captured) == 2 and "r0i" not in captured[1]

    captured.clear()
    up.get_sailing_inventory(_Acct(), booking, "123456", dp340=False)
    assert len(captured) == 1 and "r0i" not in captured[0]


def test_category_prices_sends_coupon_code_only_when_asked(monkeypatch):
    import json as _json
    import CheckRoyalCaribbeanUpgrades as up
    captured = []

    class _Sess:
        def get(self, url, params=None, headers=None):
            captured.append(_json.loads(params["filter"]))
            return None  # short-circuits after the request

    class _Access:
        session = _Sess()

    class _Acct:
        url_brand = "royalcaribbean"
        is_royal = True
        access = _Access()

    booking = {"sailDate": "20270815", "packageCode": "WN07X123",
               "passengersInStateroom": [{"firstName": "Solo"}]}
    up.get_category_prices(_Acct(), booking, "D", "BALCONY", "123456", dp340=True)
    assert captured[0]["rooms"][0].get("couponCode") == "DP340"

    up.get_category_prices(_Acct(), booking, "D", "BALCONY", "123456", dp340=False)
    assert "couponCode" not in captured[1]["rooms"][0]


def test_should_apply_dp340_gate():
    import CheckRoyalCaribbeanUpgrades as up
    # account qualifies, solo booking
    assert up.should_apply_dp340(True, False, 1) is True
    # booking already carries the code - keep quoting with it even if the
    # account doesn't qualify on points
    assert up.should_apply_dp340(False, True, 1) is True
    # never on multi-guest bookings, regardless of source
    assert up.should_apply_dp340(True, True, 2) is False
    # neither qualified nor already applied
    assert up.should_apply_dp340(False, False, 1) is False
