"""Unit tests for the pure helpers in FindBackToBackCabins.py.

No network calls: every function under test is pure. Run with
    python3.12 -m pytest test_b2b.py -q
"""
import pytest

import FindBackToBackCabins as m


##################################
# side_of / filter_side
##################################
def _side_params(ship: str, brand: str, user_flip: bool = False):
    """Mirror main()'s derivation of (by_number, flip_eff, split) for a ship."""
    by_number = brand == "R"
    flip = user_flip
    if by_number and ship in m.SIDE_PORT_HIGH:
        flip = not flip
    return by_number, flip, m.SIDE_SPLIT.get(ship, m.SIDE_SPLIT_DEFAULT)


def test_side_constants_sanity():
    # the tests below derive expectations from these facts about the module's data
    assert "OV" in m.SIDE_SPLIT and "OV" not in m.SIDE_PORT_HIGH   # split ship, low = port
    assert "VY" in m.SIDE_SPLIT and "VY" in m.SIDE_PORT_HIGH       # mirrored: high = port


def _royal_cabin(split: int, delta: int) -> str:
    """5-digit cabin whose last-3-digits value is split+delta."""
    return f"10{split + delta:03d}"


@pytest.mark.parametrize("ship,brand,delta_or_last,flip,expect", [
    # Royal split-point ship (OV): lower room numbers = port
    ("OV", "R", -1, False, "port"),
    ("OV", "R",  0, False, "starboard"),
    ("OV", "R", -1, True,  "starboard"),   # user flip inverts
    ("OV", "R",  0, True,  "port"),
    # Mirrored Voyager-class ship (VY): HIGHER room numbers = port
    ("VY", "R", -1, False, "starboard"),
    ("VY", "R",  0, False, "port"),
    ("VY", "R", -1, True,  "port"),
    ("VY", "R",  0, True,  "starboard"),
])
def test_side_of_royal(ship, brand, delta_or_last, flip, expect):
    by_number, flip_eff, split = _side_params(ship, brand, flip)
    cabin = _royal_cabin(split, delta_or_last)
    assert m.side_of(cabin, flip_eff, by_number, split) == expect


@pytest.mark.parametrize("cabin,flip,expect", [
    ("9123", False, "port"),        # odd = port
    ("9124", False, "starboard"),   # even = starboard
    ("9123", True,  "starboard"),   # flip inverts
    ("9124", True,  "port"),
])
def test_side_of_celebrity_parity(cabin, flip, expect):
    by_number, flip_eff, split = _side_params("EG", "C", flip)
    assert by_number is False
    assert m.side_of(cabin, flip_eff, by_number, split) == expect


def test_filter_side():
    by_number, flip_eff, split = _side_params("OV", "R")
    cabins = [{"cabin": _royal_cabin(split, -1)}, {"cabin": _royal_cabin(split, 0)}]
    assert m.filter_side(cabins, None, flip_eff, by_number, split) == cabins
    port = m.filter_side(cabins, "port", flip_eff, by_number, split)
    assert [c["cabin"] for c in port] == [_royal_cabin(split, -1)]
    star = m.filter_side(cabins, "starboard", flip_eff, by_number, split)
    assert [c["cabin"] for c in star] == [_royal_cabin(split, 0)]


##################################
# build_chains
##################################
V1 = {"sailDate": "20270101", "sailEndDate": "20270108"}
V2 = {"sailDate": "20270108", "sailEndDate": "20270115"}   # adjacent to V1
V3 = {"sailDate": "20270120", "sailEndDate": "20270127"}   # gap - not adjacent


def test_build_chains_adjacency():
    assert m.build_chains([V1, V2, V3], 2) == [[V1, V2]]


def test_build_chains_non_adjacent_break():
    assert m.build_chains([V1, V2, V3], 1) == [[V1, V2], [V3]]
    assert m.build_chains([V1, V3], 2) == []


@pytest.mark.parametrize("min_len", [0, -5])
def test_build_chains_min_len_clamped(min_len):
    chains = m.build_chains([V1, V2, V3], min_len)
    assert chains == [[V1, V2], [V3]]          # behaves like min_len=1
    assert all(chains), "no empty chains may be emitted"


def test_build_chains_empty_input():
    assert m.build_chains([], 0) == []


##################################
# span helpers
##################################
def test_maximal_spans():
    assert m._maximal_spans([], 1) == []
    assert m._maximal_spans([True, True, True], 2) == [(0, 2)]
    assert m._maximal_spans([True, False, True, True], 2) == [(2, 3)]
    assert m._maximal_spans([True, False, True, True], 1) == [(0, 0), (2, 3)]


def _leg(*cabins, deck="07"):
    return [{"cabin": c, "deck": deck} for c in cabins]


def test_same_cabin_spans():
    legs = [_leg("1234", "1236"), _leg("1234"), _leg("1234", "1236")]
    assert m.same_cabin_spans(legs, 2) == [("1234", 0, 2)]
    spans = m.same_cabin_spans(legs, 1)
    assert spans[0] == ("1234", 0, 2)                       # longest first
    assert set(spans[1:]) == {("1236", 0, 0), ("1236", 2, 2)}
    assert m.same_cabin_spans([], 1) == []


def test_same_cabin_spans_letter_suffix_sorts():
    # sort key goes through _cabin_int, so a suffixed cabin must not crash
    legs = [_leg("1234A", "1230"), _leg("1234A", "1230")]
    spans = m.same_cabin_spans(legs, 2)
    assert spans == [("1230", 0, 1), ("1234A", 0, 1)]


def test_closest_on_deck_optimal_counterexample():
    # anchor-on-leg-0 picks 0/-10/9 (spread 19); the true optimum is 0/10/9 (spread 10)
    spread, pick = m.closest_on_deck([[0], [-10, 10], [9]])
    assert spread == 10
    assert pick == [0, 10, 9]


def test_closest_on_deck_basics():
    assert m.closest_on_deck([[5], [], [7]]) is None        # a leg with no cabins
    assert m.closest_on_deck([]) is None
    assert m.closest_on_deck([[3, 9]]) == (0, [3])          # single leg: spread 0
    spread, pick = m.closest_on_deck([[1, 100], [2, 99]])
    assert spread == 1 and sorted(pick) in ([1, 2], [99, 100])


def test_deck_close_spans_uses_optimal_window():
    legs = [_leg("7100"), _leg("7090", "7110"), _leg("7109")]
    assert m.deck_close_spans(legs, 3) == [("07", 0, 2, [7100, 7110, 7109], 10)]


def test_deck_close_spans_broken_deck():
    legs = [_leg("7100"), [], _leg("7102")]                 # deck absent on leg 1
    assert m.deck_close_spans(legs, 2) == []
    assert m.deck_close_spans(legs, 1) == [("07", 0, 0, [7100], 0),
                                           ("07", 2, 2, [7102], 0)]


##################################
# small pure utilities
##################################
@pytest.mark.parametrize("raw,expect", [
    ("1234", 1234),
    ("1234A", 1234),        # letter suffix stripped
    ("A123", 123),          # letter prefix stripped
    ("abc", 0),             # no digits at all
    ("", 0),
    (7100, 7100),           # non-string input
])
def test_cabin_int(raw, expect):
    assert m._cabin_int(raw) == expect


@pytest.mark.parametrize("raw,expect", [
    ("7,8,10", {"07", "08", "10"}),
    ("7, 8 ,10", {"07", "08", "10"}),   # spaces stripped
    ("all", set()),
    ("ANY", set()),
    (None, None),
    ("", None),
    ("abc", None),                      # no digits -> no filter parsed
])
def test_parse_decks(raw, expect):
    assert m.parse_decks(raw) == expect


@pytest.mark.parametrize("raw,expect", [
    ("20270108", "2027-01-08"),
    (20270108, "2027-01-08"),           # int accepted via str()
    ("2027-01-08", "2027-01-08"),       # already dashed: passthrough
    ("abc", "abc"),
])
def test_dash(raw, expect):
    assert m._dash(raw) == expect


def test_price_str():
    assert m.price_str(None, True) == ""
    assert m.price_str(2453.639, True) == "$2,453.64"
    assert m.price_str(1000, False) == "~$1,000.00"


def test_extract_json_array_nested():
    text = '{"rooms": [[1, 2], [3, [4]]], "z": 0}'
    assert m._extract_json_array(text, "rooms") == [[1, 2], [3, [4]]]


def test_extract_json_array_escaped_quotes_and_brackets():
    text = '{"decks": [{"name": "say \\"hi\\" ] ["}, {"code": "07"}], "x": 1}'
    assert m._extract_json_array(text, "decks") == [
        {"name": 'say "hi" ] ['}, {"code": "07"}]


def test_extract_json_array_missing_or_unclosed():
    assert m._extract_json_array('{"a": 1}', "rooms") is None
    assert m._extract_json_array('"rooms": [1, 2', "rooms") is None


@pytest.mark.parametrize("raw,expect", [
    ("2027-01-02", "20270102"),
    ("01/02/2027", "20270102"),
    ("1/2/2027", "20270102"),
    ("20270102", "20270102"),
    (" 2027-01-02 ", "20270102"),       # whitespace tolerated
    ("garbage", None),
    ("13/40/2027", None),               # impossible date
    (None, None),
    ("", None),
])
def test_norm(raw, expect):
    assert m._norm(raw) == expect


##################################
# Chain pricing (--price-chains)
##################################
def _subtype(code, category, total, gty=False):
    return {"code": code, "categoryCode": category, "guarantee": gty,
            "pricing": {"invoice": {"total": total}}}


def test_class_minimums_picks_cheapest_including_guarantees():
    types = [
        {"code": "INTERIOR", "stateroomSubtypes": [
            _subtype("V", "4V", 1500.0),
            _subtype("XN", "XN", 1299.0, gty=True),   # guarantee undercuts - must win
        ]},
        {"code": "BALCONY", "stateroomSubtypes": [
            _subtype("D", "4D", 2100.0),
            _subtype("B", "2B", 2350.0),
        ]},
    ]
    mins = m.class_minimums(types)
    assert mins["INTERIOR"] == {"total": 1299.0, "category": "XN", "gty": True}
    assert mins["BALCONY"] == {"total": 2100.0, "category": "4D", "gty": False}


def test_class_minimums_skips_unpriced_subtypes():
    types = [{"code": "DELUXE", "stateroomSubtypes": [
        {"code": "GS", "categoryCode": "GS", "guarantee": False,
         "pricing": {"invoice": {"total": None}}},
    ]}]
    assert m.class_minimums(types) == {}
    assert m.class_minimums([]) == {}


def test_chain_class_totals_requires_every_leg():
    leg1 = {"INTERIOR": {"total": 1000.0, "category": "4V", "gty": False},
            "BALCONY": {"total": 2000.0, "category": "4D", "gty": False}}
    leg2 = {"INTERIOR": {"total": 1100.0, "category": "4V", "gty": False}}
    totals = m.chain_class_totals([leg1, leg2])
    assert totals["INTERIOR"] == 2100.0
    assert totals["BALCONY"] is None       # missing on leg 2 -> no fake full-chain price
    assert m.chain_class_totals([]) == {}


def test_class_display_order_known_first_then_alpha():
    assert m._class_display_order({"DELUXE", "INTERIOR", "ZZTOP"}) == \
        ["INTERIOR", "DELUXE", "ZZTOP"]
