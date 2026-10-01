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


def test_pending_ledger_sailings_detects_unposted_points(tmp_path):
    """A booking snapshotted by the price checker whose cruise has ENDED but
    which is absent from the loyalty ledger = points not posted yet."""
    from datetime import date, timedelta
    from CheckRoyalCaribbeanPrice import PriceHistory
    from CheckRoyalCaribbeanCruiseHistory import pending_ledger_sailings

    db = str(tmp_path / "h.db")
    h = PriceHistory(db)
    h.start_run()
    ended_sail = (date.today() - timedelta(days=9)).strftime("%Y%m%d")
    future_sail = (date.today() + timedelta(days=30)).strftime("%Y%m%d")
    common = dict(account_label="solo@example.com", ship_code="OV",
                  ship_name="Ovation of the Seas", guest_count=1, stateroom_type="INTERIOR")
    h.record_booking(reservation_id="1234567", sail_date=ended_sail, nights=4, **common)
    h.record_booking(reservation_id="7654321", sail_date=future_sail, nights=7, **common)

    # Ledger does NOT contain the ended cruise -> pending, with solo math (4n x2)
    pending = pending_ledger_sailings(db, "solo@example.com", ledger := [])
    assert [p["reservation_id"] for p in pending] == ["1234567"]
    assert pending[0]["est_points"] == 8

    # Cancelled, not sailed: a LATER run before the sail date no longer saw the
    # booking - it must not be flagged forever (audit finding A5)
    from datetime import datetime, timezone
    cancelled_sail = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
    h.record_booking(reservation_id="5550001", sail_date=cancelled_sail, nights=4,
                     observed_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
                     **common)
    # a run 15 days ago (still before the sail date) snapshotted OTHER bookings only
    h.record_booking(reservation_id="5550002", sail_date=future_sail, nights=7,
                     observed_at=(datetime.now(timezone.utc) - timedelta(days=15)).isoformat(),
                     **common)
    pending = pending_ledger_sailings(db, "solo@example.com", [])
    ids = [p["reservation_id"] for p in pending]
    assert "5550001" not in ids, "cancelled booking must not be flagged"
    assert "1234567" in ids      # the genuinely-sailed one still is
    # Different account -> nothing
    assert pending_ledger_sailings(db, "other@example.com", []) == []
    # Once the ledger has it (ship+sailDate), no longer pending
    posted = [{"shipCode": "OV", "sailingDate": ended_sail}]
    assert pending_ledger_sailings(db, "solo@example.com", posted) == []
    # No db configured -> quiet no-op
    assert pending_ledger_sailings(None, "solo@example.com", []) == []
    assert pending_ledger_sailings(str(tmp_path / "nope.db"), "solo@example.com", []) == []


def test_tier_progress_shows_pending_adjustment(monkeypatch):
    """--pending-points raises the working balance and is disclosed in the
    source label rather than silently blended in."""
    import CheckRoyalCaribbeanCruiseHistory as hist
    import CheckRoyalCaribbeanPrice as crccl

    logged = []
    monkeypatch.setattr(crccl, "log", lambda m, *a, **k: logged.append(str(m)))

    class _Acct:
        is_royal = True

    hist.show_tier_progress(_Acct(), 245, [], [], earns_blocks=False, pending=8)
    out = "\n".join(logged)
    assert "253 points" in out                 # 245 + 8
    assert "profile + 8 pending" in out        # disclosed, not blended


def test_load_accounts_skips_malformed_entry(tmp_path, monkeypatch):
    """A config entry missing its password (or any unexpected per-account
    exception) must skip that account, not kill the multi-account run."""
    import CheckRoyalCaribbeanCruiseHistory as hist
    import CheckRoyalCaribbeanPrice as crccl
    from unittest.mock import MagicMock

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "accountInfo:\n"
        "  - username: broken@example.com\n"        # no password -> KeyError
        "  - username: good@example.com\n"
        "    password: pw\n")
    monkeypatch.setattr(crccl, "setup_hybrid_logging", lambda *a, **k: None)
    logged = []
    monkeypatch.setattr(crccl, "log", lambda m, *a, **k: logged.append(str(m)))
    monkeypatch.setattr(crccl, "login", lambda acct: MagicMock())
    monkeypatch.setattr(crccl, "get_profile", lambda acct: ("FL", "123456", 42))
    monkeypatch.setattr(hist.time, "sleep", lambda s: None)

    accounts, skipped, history_db, sailed_file = hist.load_accounts(str(cfg))
    assert sailed_file == hist.SAILED_FILE_DEFAULT
    assert [a[0].username for a in accounts] == ["good@example.com"]
    assert skipped == ["broken@e…"]
    assert any("KeyError" in s for s in logged)


def test_unique_account_labels_disambiguates_collisions():
    """jim@aol.com and jim@att.net both mask to jim@a… - colliding labels merged
    the household joins, dropping one member's history (finding B10)."""
    import CheckRoyalCaribbeanCruiseHistory as hist

    class _A:
        def __init__(self, u): self.username = u

    labels = hist.unique_account_labels(
        [_A("jim@aol.com"), _A("jim@att.net"), _A("bo@example.com")])
    assert labels == ["jim@a…", "jim@a… (2)", "bo@e…"]
    assert len(set(labels)) == 3


def test_old_promo_cap_warning_excludes_new_promo_bookings(monkeypatch):
    """An id given to BOTH promo flags resolves as new-promo, so it must not
    count toward the OLD promo's 2-cruise cap warning (finding B13)."""
    import CheckRoyalCaribbeanCruiseHistory as hist

    def make_row(bid, sail):
        b = {"bookingId": bid, "shipCode": "WN", "stateroomNumber": "1234"}
        return (sail, b, 7, "7n x1 (standard)")

    rows = [make_row("1000001", "20261001"), make_row("1000002", "20261101"),
            make_row("1000003", "20261201")]
    logged = []
    monkeypatch.setattr(hist.crccl, "log", lambda m, *a, **k: logged.append(str(m)))

    # 3 ids on the old flag would warn (cap is 2) - but one resolved as new-promo
    hist.show_upcoming_earnings(
        rows, {}, "JIM EXAMPLE",
        promo_ids=frozenset({"1000001", "1000002", "1000003"}),
        new_promo_ids=frozenset({"1000003"}))
    assert not any("caps at" in s for s in logged)

    # without the exclusion the warning fires as before
    logged.clear()
    hist.show_upcoming_earnings(
        rows, {}, "JIM EXAMPLE",
        promo_ids=frozenset({"1000001", "1000002", "1000003"}))
    assert any("caps at" in s for s in logged)


def test_old_promo_window_gate():
    """The original double-points promo only covers Sep 2026 - Apr 2027 sailings:
    a flagged booking outside the window earns single points with a visible
    'ignored' note; the window edges themselves double."""
    from CheckRoyalCaribbeanCruiseHistory import upcoming_earnings

    outside = _promo_booking("1234567", guests=1, sail="20270601")
    inside = _promo_booking("2345678", guests=1, sail="20261001")
    at_end = _promo_booking("3456789", guests=1, sail="20270430")
    ids = frozenset({"1234567", "2345678", "3456789"})
    rows = {r[1]["bookingId"]: r for r in
            upcoming_earnings([outside, inside, at_end], None, promo_ids=ids)}

    assert rows["1234567"][2] == 14                        # 7n x2, NOT doubled
    assert "--double-points ignored" in rows["1234567"][3]
    assert rows["2345678"][2] == 28                        # in-window doubles
    assert rows["3456789"][2] == 28                        # end edge doubles


def test_show_yearly_totals_and_projections(monkeypatch):
    """Past years aggregate cruises/nights/points with a running total; booked
    cruises appear as 'est' rows and a 'w/ booked' summary line."""
    import CheckRoyalCaribbeanCruiseHistory as hist

    logged = []
    monkeypatch.setattr(hist.crccl, "log", lambda m, *a, **k: logged.append(str(m)))
    sailings = [
        {"sailingDate": "20240301", "itineraryNightsQuantity": 7, "points": 7},
        {"sailingDate": "20241101", "itineraryNightsQuantity": 3, "points": 3},
        {"sailingDate": "20250601", "itineraryNightsQuantity": 7, "points": 14},
    ]
    upcoming = [("20260901", {"numberOfNights": 7}, 14, "7n x2 (solo)")]
    hist.show_yearly(sailings, upcoming)

    out = [hist.crccl.StripAnsiFilter.ANSI_REGEX.sub("", s) for s in logged]
    def row(prefix):
        return next((l.split() for l in out if l.strip().startswith(prefix)), None)

    assert row("2024") == ["2024", "2", "10", "10", "10"]
    assert row("2025") == ["2025", "1", "7", "14", "24"]
    assert row("2026 est") == ["2026", "est", "+1", "+7", "+14", "38", "(booked)"]
    assert row("total") == ["total", "3", "17", "24"]
    assert row("w/ booked") == ["w/", "booked", "4", "24", "38"]


def test_show_yearly_includes_pending_points_in_the_current_year(monkeypatch):
    """--pending-points reached the tier-progress line but not this table, so
    the two ended on different numbers (467 vs 453 live: 14 points of a
    just-ended cruise). The adjustment now lands in the current year."""
    from datetime import date
    import CheckRoyalCaribbeanCruiseHistory as hist
    logged = []
    monkeypatch.setattr(hist.crccl, "log", lambda m, *a, **k: logged.append(str(m)))
    year = date.today().strftime("%Y")
    sailings = [{"sailingDate": f"{year}0101", "itineraryNightsQuantity": 7, "points": 7}]
    upcoming = [(f"{year}1231", {"numberOfNights": 7}, 14, "7n x2 (solo)")]
    hist.show_yearly(sailings, upcoming, pending_points=14)
    out = [hist.crccl.StripAnsiFilter.ANSI_REGEX.sub("", s) for s in logged]
    rows = [l.split() for l in out if l.strip().startswith(year) or l.strip().startswith("w/")]
    assert rows[0] == [year, "1", "7", "7", "7"]
    assert rows[1][:2] == [year, "pending"] and rows[1][2:4] == ["+14", "21"] and "not posted" in " ".join(rows[1])
    assert rows[2][:6] == [year, "est", "+1", "+7", "+14", "35"]
    assert rows[3] == ["w/", "booked", "2", "14", "35"]            # same end point as tier progress
    logged.clear()
    hist.show_yearly(sailings, upcoming)                           # no adjustment: no pending row
    assert not any("pending" in l for l in logged)


def test_show_pending_points_lists_unposted_sailings(monkeypatch):
    import CheckRoyalCaribbeanCruiseHistory as hist
    from datetime import date, timedelta

    logged = []
    monkeypatch.setattr(hist.crccl, "log", lambda m, *a, **k: logged.append(str(m)))
    hist.show_pending_points([{"sail_date": "20260810", "ship": "Wonder of the Seas",
                               "ended": date.today() - timedelta(days=5),
                               "est_points": 14}])
    out = "\n".join(logged)
    assert "Points not posted yet (1 sailed cruise(s)" in out
    assert "Wonder of the Seas" in out and "ended 5d ago" in out
    assert "~14 pts expected" in out

    logged.clear()
    hist.show_pending_points([])
    assert logged == []   # nothing pending prints nothing


def _booking_on(days_from_today, nights=7, guests=2, suite=False, ship="SR", bid="1234567"):
    from datetime import date, timedelta
    sail = (date.today() + timedelta(days=days_from_today)).strftime("%Y%m%d")
    return {"bookingId": bid, "sailDate": sail, "numberOfNights": nights, "shipCode": ship,
            "stateroomType": "D" if suite else "B",
            "passengersInStateroom": [{"firstName": f"G{i}", "lastName": "Test"} for i in range(guests)]}


def test_sailing_status_three_way():
    """A cruise that has departed but not debarked is neither history nor future."""
    from CheckRoyalCaribbeanCruiseHistory import sailing_status
    assert sailing_status(_booking_on(1)) == "upcoming"
    assert sailing_status(_booking_on(0)) == "in_progress"          # embarkation day
    assert sailing_status(_booking_on(-3)) == "in_progress"         # mid-cruise (7 nights)
    assert sailing_status(_booking_on(-6)) == "in_progress"         # last night aboard
    assert sailing_status(_booking_on(-7)) == "ended"               # debarkation morning
    assert sailing_status(_booking_on(-30)) == "ended"
    assert sailing_status(_booking_on(-3, nights=0)) == "ended"     # no length known: can't be aboard
    assert sailing_status({"sailDate": "soon", "numberOfNights": 7}) == "unknown"
    assert sailing_status({}) == "unknown"


def test_in_progress_sailing_is_projected_with_the_same_rate_math():
    """The complaint: a current sailing showed as [past] with no points. It is
    estimated exactly like a future one - nights x (base + suite + solo)."""
    from CheckRoyalCaribbeanCruiseHistory import upcoming_earnings
    rows = upcoming_earnings([_booking_on(-2, nights=7, guests=1, suite=True)], None)
    assert len(rows) == 1
    sail, b, pts, why = rows[0]
    assert pts == 21                                                # 7n x3 (suite, solo)
    assert "suite, solo" in why and "sailing now" in why and "estimate" in why
    # promos still apply to a sailing in progress
    rows = upcoming_earnings([_booking_on(-2, nights=7, guests=1)], None,
                             new_promo_ids=frozenset({"1234567"}))
    assert rows[0][2] == 21 and "sailing now" in rows[0][3]          # 7n x3 (new promo, solo)


def test_ended_sailing_is_estimated_only_until_the_ledger_has_it():
    from CheckRoyalCaribbeanCruiseHistory import upcoming_earnings
    ended = _booking_on(-9, nights=7, guests=2, ship="SR")
    rows = upcoming_earnings([ended], None)
    assert rows and rows[0][2] == 7 and "ended" in rows[0][3] and "not in the loyalty ledger" in rows[0][3]
    # once C&A has posted it (ledger key = shipCode + sailingDate) it is history, not a projection
    posted = frozenset({("SR", ended["sailDate"])})
    assert upcoming_earnings([ended], None, posted=posted) == []
    # a different ship on the same date does not count as posted
    assert upcoming_earnings([ended], None, posted=frozenset({("AN", ended["sailDate"])}))


def test_projection_orders_current_before_future_and_flags_estimates(monkeypatch):
    import CheckRoyalCaribbeanCruiseHistory as hist
    logged = []
    monkeypatch.setattr(hist.crccl, "log", lambda m, *a, **k: logged.append(str(m)))
    rows = hist.upcoming_earnings([_booking_on(30, bid="2222222"), _booking_on(-2, bid="1111111")], None)
    assert [r[1]["bookingId"] for r in rows] == ["1111111", "2222222"]
    hist.show_upcoming_earnings(rows, {"SR": "Serenade of the Seas"}, None)
    out = "\n".join(logged)
    assert "total: +14 pts" in out
    assert "estimates until Crown & Anchor posts them" in out
    logged.clear()
    hist.show_upcoming_earnings(hist.upcoming_earnings([_booking_on(30)], None), {}, None)
    assert "estimates until" not in "\n".join(logged)                # future-only: no caveat


def test_bookings_list_tags_a_sailing_in_progress(monkeypatch):
    import CheckRoyalCaribbeanCruiseHistory as hist
    logged = []
    monkeypatch.setattr(hist.crccl, "log", lambda m, *a, **k: logged.append(str(m)))
    hist.show_bookings([_booking_on(-2, bid="1111111"), _booking_on(-20, bid="2222222"),
                        _booking_on(20, bid="3333333")], {})
    out = "\n".join(logged)
    # listed by sail date: the ended one, then the one in progress, then the future one
    assert out.index("[past]") < out.index("[sailing now]") < out.index("[upcoming]")
    assert out.count("[past]") == 1 and out.count("[sailing now]") == 1


def test_tier_progress_names_the_sailing_in_progress(monkeypatch):
    import CheckRoyalCaribbeanCruiseHistory as hist
    logged = []
    monkeypatch.setattr(hist.crccl, "log", lambda m, *a, **k: logged.append(str(m)))
    account = type("A", (), {"is_royal": True})()
    rows = hist.upcoming_earnings([_booking_on(-2, nights=7, guests=2)], None)   # +7
    hist.show_tier_progress(account, 78, [], rows)                                # 80 = next block
    out = "\n".join(logged)
    assert "sailing (sailing now)" in out and "(booked)" not in out


def _store():
    return {"version": 1, "accounts": {}}


def test_remember_bookings_records_mine_and_forgets_cancellations():
    """Bookings the holder is in are recorded (counts and room type, no names);
    one that vanishes BEFORE its sail date was cancelled and is forgotten; one
    that vanishes after sailing is kept - that is the point of the record."""
    from datetime import date, timedelta
    import CheckRoyalCaribbeanCruiseHistory as hist
    store = _store()
    mine = _booking_on(10, bid="1111111", guests=1, suite=True)
    mine["passengersInStateroom"][0].update(firstName="Jane", lastName="Holder")
    linked = _booking_on(20, bid="2222222")                      # someone else's room
    hist.remember_bookings(store, "jane@example.com", [mine, linked], "JANE HOLDER")
    acct = store["accounts"]["jane@example.com"]
    assert set(acct) == {"1111111"}
    assert acct["1111111"]["guests"] == 1 and acct["1111111"]["stateroomType"] == "D"
    assert "Jane" not in str(acct) and "Holder" not in str(acct)      # no names stored
    # next run: 1111111 is gone and its sail date is still ahead -> cancelled
    hist.remember_bookings(store, "jane@example.com", [], "JANE HOLDER")
    assert acct == {}
    # a booking that sailed and then dropped off the profile is kept
    sailed = _booking_on(-3, bid="3333333")
    sailed["passengersInStateroom"][0].update(firstName="Jane", lastName="Holder")
    hist.remember_bookings(store, "jane@example.com", [sailed], "JANE HOLDER",
                           today=date.today() - timedelta(days=5))         # seen before it sailed
    hist.remember_bookings(store, "jane@example.com", [], "JANE HOLDER")   # gone now, after sailing
    assert "3333333" in acct
    # unknown holder name: record everything rather than nothing
    hist.remember_bookings(_s := _store(), "x@example.com", [linked], None)
    assert "2222222" in _s["accounts"]["x@example.com"]


def test_parse_sailed_spec_forms_and_errors():
    import pytest
    from CheckRoyalCaribbeanCruiseHistory import parse_sailed_spec
    e = parse_sailed_spec("an:20260920:7:solo")
    assert (e["shipCode"], e["sailDate"], e["nights"], e["guests"], e["stateroomType"]) == ("AN", "20260920", 7, 1, "B")
    e = parse_sailed_spec("SR:20260913:7:2:suite")
    assert e["guests"] == 2 and e["stateroomType"] == "D" and e["manual"] is True
    assert parse_sailed_spec("SR:20260913:7")["guests"] == 2                 # default: not solo
    for bad, why in [("SR:20260913", "expected"), ("Serenade:20260913:7", "two letters"),
                     ("SR:2026-09-13:7", "YYYYMMDD"), ("SR:20260913:0", "positive"),
                     ("SR:20260913:7:balcony", "unrecognized"), ("SR:20260913:7:0", "at least 1")]:
        with pytest.raises(ValueError, match=why):
            parse_sailed_spec(bad)


def test_sailed_unposted_prices_remembered_and_manual_cruises_until_posted():
    from datetime import date, timedelta
    import CheckRoyalCaribbeanCruiseHistory as hist
    store = _store()
    errors = hist.add_manual_sailed(store, "u", ["AN:%s:7:solo" % (date.today() - timedelta(days=12)).strftime("%Y%m%d"),
                                                "junk"])
    assert errors and errors[0].startswith("junk:")
    # a remembered (seen) booking that sailed 10 days ago for 7 nights: ended 3 days ago
    seen = _booking_on(-10, bid="5555555", guests=2, suite=True)
    hist.remember_bookings(store, "u", [seen], None, today=date.today() - timedelta(days=11))
    rows = hist.sailed_unposted(store, "u", posted=frozenset(), on_profile=set())
    assert [r["_remembered"] for r in rows] == ["manual", "seen"]
    # the projection prices them with the normal math and no holder-name match
    proj = hist.upcoming_earnings(rows, "SOMEONE ELSE", posted=frozenset())
    assert [(r[1]["bookingId"][:6], r[2]) for r in proj] == [("manual", 14), ("555555", 14)]   # 7n x2 solo / 7n x2 suite
    assert all("ended" in r[3] and "not in the loyalty ledger" in r[3] for r in proj)
    # still on the profile -> the profile copy is used, not the remembered one
    assert hist.sailed_unposted(store, "u", frozenset(), on_profile={"5555555"}) [0]["_remembered"] == "manual"
    # posted at last -> dropped from the record for good
    posted = frozenset({("SR", seen["sailDate"])})
    hist.sailed_unposted(store, "u", posted, set())
    assert "5555555" not in store["accounts"]["u"]
    # not ended yet -> not listed, still remembered
    current = _booking_on(-2, bid="6666666")
    hist.remember_bookings(store, "u", [current], None)
    assert not any(r["bookingId"] == "6666666" for r in hist.sailed_unposted(store, "u", frozenset(), set()))
    assert "6666666" in store["accounts"]["u"]


def test_sailed_file_round_trip_and_bad_file_is_left_alone(tmp_path, monkeypatch):
    import CheckRoyalCaribbeanCruiseHistory as hist
    logged = []
    monkeypatch.setattr(hist.crccl, "log", lambda m, *a, **k: logged.append(str(m)))
    path = str(tmp_path / "data" / "sailed.json")
    store = hist.load_sailed_file(path)                       # missing -> fresh
    hist.add_manual_sailed(store, "u", ["AN:20260920:7:solo"])
    hist.save_sailed_file(path, store)
    again = hist.load_sailed_file(path)
    assert again["accounts"]["u"]["manual:AN:20260920"]["nights"] == 7
    (tmp_path / "data" / "sailed.json").write_text("{not json")
    broken = hist.load_sailed_file(path)
    assert broken["accounts"] == {} and broken.get("_readonly")
    hist.save_sailed_file(path, broken)                        # must NOT overwrite the bad file
    assert (tmp_path / "data" / "sailed.json").read_text() == "{not json"
    assert any("sailed-booking memory is off" in m for m in logged)


def test_pending_display_names_the_source(monkeypatch):
    from datetime import date, timedelta
    import CheckRoyalCaribbeanCruiseHistory as hist
    logged = []
    monkeypatch.setattr(hist.crccl, "log", lambda m, *a, **k: logged.append(str(m)))
    hist.show_pending_points([{"sail_date": "20260920", "ship": "Anthem of the Seas",
                               "ended": date.today() - timedelta(days=4), "est_points": 14, "source": "manual"}])
    out = "\n".join(logged)
    assert "entered with --sailed" in out and "estimated in the projection below" in out
