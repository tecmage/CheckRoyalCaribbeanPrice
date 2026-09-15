"""
Unit tests for CheckRoyalCaribbeanCasinoOffers (Club Royale offer tracker).

Pure/mocked - no network. The tracker calls account_info.access.session.get
directly (it needs the accessToken cookie the shared _execute_api_request
helper does not carry), so pagination tests stub the access session itself.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import CheckRoyalCaribbeanCasinoOffers as casino
from CheckRoyalCaribbeanCasinoOffers import CasinoOffer, fetch_casino_offers


##################################
# Shared fixtures & fakes
##################################
@pytest.fixture(autouse=True)
def capture_logs(monkeypatch):
    """Binds the module's functional logging hooks (normally set in main) to
    in-memory capture lists so fetch/report paths never touch a real logger."""
    captured = SimpleNamespace(log=[], warn=[], err=[])
    monkeypatch.setattr(casino, "log", captured.log.append)
    monkeypatch.setattr(casino, "log_warn", captured.warn.append)
    monkeypatch.setattr(casino, "log_err", captured.err.append)
    return captured


class FakeResponse:
    """Minimal stand-in for a session response: status code plus JSON payload.
    A payload of None simulates a non-JSON body (json() raises ValueError)."""

    def __init__(self, status_code: int = 200, payload: Optional[Dict[str, Any]] = None):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeSession:
    """Replays a scripted list of responses; an Exception entry is raised."""

    def __init__(self, responses: List[Any]):
        self._responses = list(responses)
        self.calls: List[Dict[str, str]] = []

    def get(self, url, params=None, headers=None, cookies=None):
        self.calls.append(dict(params or {}))
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_account(responses: List[Any]) -> SimpleNamespace:
    """Builds a fake logged-in AccountInfo whose session replays 'responses'."""
    return SimpleNamespace(
        access=SimpleNamespace(
            token="test-token",
            id="test-account-id",
            loyalty_number=123456789,
            session=FakeSession(responses),
        )
    )


def raw_offer(code: str, type_code: str = "GOBO") -> Dict[str, Any]:
    """A minimal-but-valid API offer record for pagination payloads."""
    return {
        "campaignName": f"Campaign {code}",
        "campaignOffer": {
            "offerCode": code,
            "name": f"Offer {code}",
            "offerType": {"code": type_code, "name": type_code.title()},
            "reserveByDate": "2026-12-31T23:59:59Z",
            "status": "OPEN",
            "perkCodes": [],
        },
    }


##################################
# CasinoOffer.from_api
##################################
class TestCasinoOfferFromApi:
    def test_realistic_record_extracts_all_fields(self):
        raw = {
            "campaignName": "Summer Slots Event",
            "status": "ACTIVE",
            "campaignOffer": {
                "offerCode": "26AB123",
                "name": "Ovation Interior Comp",
                "offerType": {"code": "COMP", "name": "Complimentary Cruise"},
                "reserveByDate": "2026-09-30T23:59:59Z",
                "status": "OPEN",
                "perkCodes": [
                    {"perkCode": "FP100", "perkName": "$100 FreePlay"},
                    {"perkCode": "NOPERKNAME"},          # missing perkName -> skipped
                    {"perkCode": "EMPTY", "perkName": ""},  # empty perkName -> skipped
                ],
            },
        }
        offer = CasinoOffer.from_api(raw)
        assert offer.offer_code == "26AB123"
        assert offer.name == "Ovation Interior Comp"
        assert offer.offer_type_code == "COMP"
        assert offer.offer_type_name == "Complimentary Cruise"
        assert offer.reserve_by_date == "2026-09-30T23:59:59Z"
        assert offer.campaign_name == "Summer Slots Event"
        # The nested campaignOffer status wins over the outer record's status
        assert offer.status == "OPEN"
        assert offer.perks == ["$100 FreePlay"]

    def test_null_campaign_offer_does_not_crash(self):
        raw = {"campaignName": "Orphan Campaign", "status": "EXPIRED", "campaignOffer": None}
        offer = CasinoOffer.from_api(raw)
        assert offer.offer_code == "?"
        assert offer.name == "Orphan Campaign"      # falls back to campaignName
        assert offer.offer_type_code == ""
        assert offer.offer_type_name == ""
        assert offer.reserve_by_date is None
        assert offer.status == "EXPIRED"            # falls back to the outer status
        assert offer.perks == []
        assert offer.is_complimentary is False
        assert offer.days_until_reserve_by() is None

    def test_null_inner_nesting_does_not_crash(self):
        raw = {"campaignOffer": {"offerCode": "26XY9", "offerType": None, "perkCodes": None}}
        offer = CasinoOffer.from_api(raw)
        assert offer.offer_code == "26XY9"
        assert offer.offer_type_code == ""
        assert offer.perks == []

    def test_empty_record_does_not_crash(self):
        offer = CasinoOffer.from_api({})
        assert offer.offer_code == "?"
        assert offer.name == ""
        assert offer.status == ""

    def test_comp_vs_gobo_keys_on_offer_type_code_not_description(self):
        # The API's templated description/name text is NOT reliable: a GOBO may
        # say "Complimentary" in its name. The code keys on offerType.code only.
        gobo = CasinoOffer.from_api({
            "campaignOffer": {
                "offerCode": "26GB1",
                "name": "Complimentary cruise for two",  # lying description
                "offerType": {"code": "GOBO", "name": "Get One Buy One"},
            },
        })
        assert gobo.is_complimentary is False

        comp = CasinoOffer.from_api({
            "campaignOffer": {
                "offerCode": "26CP1",
                "name": "Buy one get one",               # lying the other way
                "offerType": {"code": "COMP", "name": "Complimentary"},
            },
        })
        assert comp.is_complimentary is True


##################################
# CasinoOffer.days_until_reserve_by
##################################
class TestDaysUntilReserveBy:
    @staticmethod
    def offer_with(reserve_by: Optional[str]) -> CasinoOffer:
        return CasinoOffer(
            offer_code="26ZZ1", name="Test", offer_type_code="COMP",
            offer_type_name="Complimentary", reserve_by_date=reserve_by,
            campaign_name="Test", status="OPEN",
        )

    def test_future_date_counts_whole_days(self):
        deadline = datetime.now(timezone.utc) + timedelta(days=10, minutes=5)
        assert self.offer_with(deadline.isoformat()).days_until_reserve_by() == 10

    def test_zulu_suffix_is_accepted(self):
        deadline = datetime.now(timezone.utc) + timedelta(days=5, minutes=5)
        zulu = deadline.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert self.offer_with(zulu).days_until_reserve_by() == 5

    def test_later_today_is_zero_days(self):
        deadline = datetime.now(timezone.utc) + timedelta(minutes=30)
        assert self.offer_with(deadline.isoformat()).days_until_reserve_by() == 0

    def test_past_date_is_negative(self):
        deadline = datetime.now(timezone.utc) - timedelta(days=3, minutes=5)
        days = self.offer_with(deadline.isoformat()).days_until_reserve_by()
        assert days is not None and days < 0

    def test_none_date_returns_none(self):
        assert self.offer_with(None).days_until_reserve_by() is None

    def test_empty_string_returns_none(self):
        assert self.offer_with("").days_until_reserve_by() is None

    def test_malformed_date_returns_none(self):
        assert self.offer_with("not-a-date").days_until_reserve_by() is None


##################################
# fetch_casino_offers pagination
##################################
class TestFetchCasinoOffersPagination:
    def test_all_pages_fetched_is_complete(self):
        account = make_account([
            FakeResponse(payload={"offers": [raw_offer("26AA1")], "totalPages": 2}),
            FakeResponse(payload={"offers": [raw_offer("26AA2")], "totalPages": 2}),
        ])
        offers, complete = fetch_casino_offers(account)
        assert [o.offer_code for o in offers] == ["26AA1", "26AA2"]
        assert complete is True
        assert [c["page"] for c in account.access.session.calls] == ["1", "2"]

    def test_http_failure_mid_pagination_returns_partial(self, capture_logs):
        account = make_account([
            FakeResponse(payload={"offers": [raw_offer("26AA1")], "totalPages": 3}),
            FakeResponse(payload={"offers": [raw_offer("26AA2")], "totalPages": 3}),
            FakeResponse(status_code=500),
        ])
        offers, complete = fetch_casino_offers(account)
        # Both successful pages are kept, and the truncation is flagged
        assert [o.offer_code for o in offers] == ["26AA1", "26AA2"]
        assert complete is False
        assert any("page 3" in line for line in capture_logs.log)

    def test_network_exception_mid_pagination_returns_partial(self):
        account = make_account([
            FakeResponse(payload={"offers": [raw_offer("26AA1")], "totalPages": 2}),
            ConnectionError("boom"),
        ])
        offers, complete = fetch_casino_offers(account)
        assert [o.offer_code for o in offers] == ["26AA1"]
        assert complete is False

    def test_string_total_pages_is_coerced_not_crashed(self):
        account = make_account([
            FakeResponse(payload={"offers": [raw_offer("26AA1")], "totalPages": "3"}),
            FakeResponse(payload={"offers": [raw_offer("26AA2")], "totalPages": "3"}),
            FakeResponse(payload={"offers": [raw_offer("26AA3")], "totalPages": "3"}),
        ])
        offers, complete = fetch_casino_offers(account)
        assert [o.offer_code for o in offers] == ["26AA1", "26AA2", "26AA3"]
        assert complete is True
        assert len(account.access.session.calls) == 3

    def test_junk_total_pages_falls_back_to_one_page(self):
        account = make_account([
            FakeResponse(payload={"offers": [raw_offer("26AA1")], "totalPages": "lots"}),
        ])
        offers, complete = fetch_casino_offers(account)
        assert [o.offer_code for o in offers] == ["26AA1"]
        assert complete is True
        assert len(account.access.session.calls) == 1

    def test_missing_total_pages_defaults_to_one_page(self):
        account = make_account([
            FakeResponse(payload={"offers": [raw_offer("26AA1")], "totalPages": None}),
        ])
        offers, complete = fetch_casino_offers(account)
        assert [o.offer_code for o in offers] == ["26AA1"]
        assert complete is True

    def test_non_json_body_is_partial_not_traceback(self, capture_logs):
        account = make_account([
            FakeResponse(payload={"offers": [raw_offer("26AA1")], "totalPages": 2}),
            FakeResponse(payload=None),  # 200 OK but body is not JSON
        ])
        offers, complete = fetch_casino_offers(account)
        assert [o.offer_code for o in offers] == ["26AA1"]
        assert complete is False
        assert any("non-JSON" in line for line in capture_logs.warn)

    def test_empty_offers_page_yields_empty_complete_list(self):
        account = make_account([
            FakeResponse(payload={"offers": [], "totalPages": 1}),
        ])
        offers, complete = fetch_casino_offers(account)
        assert offers == []
        assert complete is True
