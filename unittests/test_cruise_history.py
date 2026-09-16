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

    accounts, skipped, history_db = hist.load_accounts(str(cfg))
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
