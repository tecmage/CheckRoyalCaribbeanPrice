"""
Check what it would cost to upgrade each booked cruise to a better stateroom.

For every booking on the account this reads the real pricing ledger from the
booking's amend page (ORIGINAL_CRUISE_FARE, DISCOUNT with its itemization,
GROSS_TOTALS = the all-in amount you pay, PAYMENTS_APPLIED, BALANCE_DUE), then
prices every stateroom category currently for sale on the same sailing - with
your loyalty number and the booking's guest count - and shows two deltas per
candidate:

    dl-paid    candidate's current all-in total minus what you pay today
               (what a straight repricing would owe)
    dl-rate    candidate minus your booked category's CURRENT price
               (the category-difference math an upgrade/casino desk uses)

Casino-rate bookings (Club Royale comps/GOBO) are detected from the ledger's
discount items ("Casino ...", "ClubR ...") and flagged: their dl-paid is not
what the casino desk would charge - prior CASINO UPGRD charges on this account
billed roughly the category difference, so lean on dl-rate there.

    python3.12 CheckRoyalCaribbeanUpgrades.py -c config.yaml
    python3.12 CheckRoyalCaribbeanUpgrades.py -c config.yaml --reservation 1234567
    python3.12 CheckRoyalCaribbeanUpgrades.py -c config.yaml --alert-below 100

--alert-below N (or upgradeAlertBelow: N in config.yaml) sends one Apprise
notification per run listing every upgrade - a higher class, or a pricier
category within your class - whose category-difference cost is at or below N.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from typing import Any, Dict, List, Optional, Tuple

import yaml

import CheckRoyalCaribbeanPrice as crccl
from CheckRoyalCaribbeanPrice import RED, GREEN, YELLOW, BLUE, RESET, USER_AGENT_WEB

##################################
# Global Constants & Variables
##################################
CYAN = "\033[96m"

# Ledger discount/option descriptions that mark a Club Royale casino-rate booking
CASINO_MARKER = re.compile(r"casino|clubr|club royale", re.I)

# Functional logging hooks, bound from the main module once logging is initialized
log = None
log_warn = None
log_err = None


##################################
# Config & Authentication
##################################
def build_account(config_path: str):
    """Login + profile, mirroring the casino tracker's reuse pattern."""
    with open(config_path) as f:
        data = crccl.expand_env_vars(yaml.safe_load(f)) or {}
    crccl.setup_hybrid_logging(data.get("logFile"))
    global log, log_warn, log_err
    log, log_warn, log_err = crccl.log, crccl.log_warn, crccl.log_err

    accounts = data.get("accountInfo") or []
    if not accounts:
        print("No accountInfo in config.", file=sys.stderr)
        sys.exit(1)
    a = accounts[0]
    account = crccl.AccountInfo(username=a["username"], password=a["password"],
                                cruise_line=a.get("cruiseLine", "royalcaribbean"))
    account.access = crccl.login(account)
    state, loyalty, _points = crccl.get_profile(account)
    account.access.loyalty_number = loyalty
    return account, state, loyalty, data


def build_apprise(data: Dict[str, Any]):
    """Apprise notifier from any apprise URLs in the configuration (or None)."""
    urls = [i["url"] for i in data.get("apprise", []) if isinstance(i, dict) and "url" in i]
    if not urls:
        return None
    try:
        from apprise import Apprise
    except ImportError:
        log_warn("apprise not installed; console output only")
        return None
    apobj = Apprise()
    for url in urls:
        apobj.add(url)
    return apobj


# Class ladder used to decide what counts as an UPGRADE for alerting
TYPE_RANK = {"INTERIOR": 0, "OUTSIDE": 1, "BALCONY": 2, "CONCIERGE": 3, "AQUA": 3, "DELUXE": 4}


def fetch_bookings(account) -> List[Dict[str, Any]]:
    brand_code = "R" if account.is_royal else "C"
    url = f"https://aws-prd.api.rccl.com/v1/profileBookings/enriched/{account.access.id}"
    resp = crccl._execute_api_request(account, "GET", url,
                                      params={"brand": brand_code, "includeCheckin": "true"})
    if resp is None:
        return []
    return resp.json().get("payload", {}).get("profileBookings", [])


##################################
# Booking ledger (what you paid)
##################################
def read_ledger(account, booking: Dict[str, Any]) -> Dict[str, Any]:
    """Digest the amend-page prices[] into the fields the upgrade math needs."""
    result = crccl.get_dining_and_prices(account, booking)
    by_code: Dict[str, Dict[str, Any]] = {}
    for p in result.get("prices", []) or []:
        code = p.get("priceTypeCode")
        if code and code not in by_code:
            by_code[code] = p

    def amt(code):
        rec = by_code.get(code)
        return rec.get("amount") if rec else None

    casino_items, promo_items = [], []
    for code in ("DISCOUNT", "OPTIONS"):
        for item in (by_code.get(code, {}).get("priceItems") or []):
            desc = item.get("description") or ""
            entry = {"code": item.get("code"), "desc": desc,
                     "amount": item.get("amount"), "promo": item.get("promoCd")}
            (casino_items if CASINO_MARKER.search(desc) else promo_items).append(entry)

    return {
        "gross": amt("GROSS_TOTALS"),
        "original_fare": amt("ORIGINAL_CRUISE_FARE"),
        "discounted_fare": amt("DISCOUNTED_CRUISE_FARE"),
        "discount": amt("DISCOUNT"),
        "taxes": amt("TAXES_AND_FEES"),
        "payments_applied": amt("PAYMENTS_APPLIED"),
        "balance_due": amt("BALANCE_DUE"),
        "casino_items": casino_items,
        "promo_items": [i for i in promo_items if i.get("amount")],
        "is_casino": bool(casino_items),
    }


##################################
# Current prices for the sailing
##################################
def _rsc_get(account, url: str, params: Dict[str, Any]):
    headers = {"user-agent": USER_AGENT_WEB, "accept": "text/x-component", "RSC": "1"}
    try:
        return account.access.session.get(url, params=params, headers=headers)
    except Exception as e:
        log(f"{RED}Request failed: {e}{RESET}")
        return None


def get_sailing_inventory(account, booking: Dict[str, Any], loyalty: Optional[str]
                          ) -> List[Dict[str, Any]]:
    """Every stateroom subtype currently for sale on the booking's sailing, with the
    subtype-level all-in total (taxes included) priced for this booking's guest count
    and the account's loyalty number. One RSC call."""
    sd = str(booking.get("sailDate") or "")
    sail = f"{sd[0:4]}-{sd[4:6]}-{sd[6:8]}" if len(sd) == 8 else sd
    guests = booking.get("passengersInStateroom") or []
    params = {
        "packageCode": booking.get("packageCode"), "sailDate": sail,
        "country": booking.get("bookingOfficeCountryCode") or "USA",
        "selectedCurrencyCode": booking.get("bookingCurrency") or "USD",
        "shipCode": (booking.get("packageCode") or "")[0:2],
        "cabinClassType": "INTERIOR", "roomIndex": "0",
        "r0a": str(max(1, len(guests))), "r0c": "0",
        "r0b": "n", "r0r": "n", "r0s": "n", "r0q": "n", "r0t": "n",
        "r0d": "INTERIOR", "r0D": "y", "rgVisited": "true", "r0C": "y",
    }
    if loyalty:
        params["r0l"] = str(loyalty)
    r = _rsc_get(account, f"https://www.{account.url_brand}.com/room-selection/type-and-subtype",
                 params)
    if not r or r.status_code != 200:
        return []
    rooms = crccl._extract_json_array(r.text, "rooms")
    if not rooms:
        return []
    out = []
    try:
        stateroom_types = rooms[0]["options"]["stateroomTypes"]
    except (KeyError, IndexError, TypeError):
        return []
    for t in stateroom_types:
        for s in t.get("stateroomSubtypes", []) or []:
            total = ((s.get("pricing") or {}).get("invoice") or {}).get("total")
            out.append({
                "type": t.get("code"), "subtype": s.get("code"),
                "category": s.get("categoryCode"), "name": s.get("name") or "",
                "guarantee": bool(s.get("guarantee")),
                "connecting": "connect" in (s.get("name") or "").lower(),
                "total": float(total) if isinstance(total, (int, float)) else None,
            })
    return out


def get_category_prices(account, booking: Dict[str, Any], subtype: str, stype: str,
                        loyalty: Optional[str]) -> Dict[str, float]:
    """Per-CATEGORY all-in totals inside one subtype (e.g. 2D vs 4D), via the
    room-selection JSON API with the loyalty qualifier."""
    sd = str(booking.get("sailDate") or "")
    sail = f"{sd[0:4]}-{sd[4:6]}-{sd[6:8]}" if len(sd) == 8 else sd
    guests = booking.get("passengersInStateroom") or []
    room: Dict[str, Any] = {
        "adultCount": max(1, len(guests)), "childCount": 0,
        "stateroomTypeCode": stype, "stateroomSubtypeCode": subtype,
        "accessible": False, "selectionFallbackStrategy": "RECOMMENDATION",
        "editMode": True, "reset": False, "taxesAndFeesBundled": True,
    }
    if loyalty:
        room["qualifiers"] = {"loyaltyNumber": str(loyalty)}
    flt = {"countryCode": booking.get("bookingOfficeCountryCode") or "USA",
           "packageId": booking.get("packageCode"), "sailDate": sail,
           "currencyCode": booking.get("bookingCurrency") or "USD",
           "language": "en", "options": True, "roomNumbers": True,
           "rooms": [room], "platform": "web"}
    headers = {"user-agent": USER_AGENT_WEB, "accept": "*/*",
               "content-type": "application/json",
               "brand": "R" if account.is_royal else "C", "country": "USA"}
    try:
        r = account.access.session.get(
            f"https://www.{account.url_brand}.com/room-selection/api/v1/rooms",
            params={"filter": json.dumps(flt)}, headers=headers)
    except Exception:
        return {}
    if not r or r.status_code != 200:
        return {}
    try:
        data = r.json()
    except Exception:
        return {}
    out: Dict[str, float] = {}
    for rm in data.get("rooms", []) or []:
        for cat in (rm.get("roomNumbers", {}) or {}).get("categories", []) or []:
            code = cat.get("categoryCode") or cat.get("code")
            total = ((cat.get("pricing") or {}).get("invoice") or {}).get("total")
            if code and isinstance(total, (int, float)):
                out[code] = float(total)
    return out


##################################
# Reporting
##################################
def money(v: Optional[float]) -> str:
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "-"


def delta(v: Optional[float], width: int = 12) -> str:
    """Signed money, right-padded to a fixed VISIBLE width, green when <= 0 (a saving).
    Colour codes are applied after padding so columns stay aligned."""
    if not isinstance(v, (int, float)):
        return "-".rjust(width)
    if abs(v) < 0.005:
        return f"{GREEN}{'$0.00'.rjust(width)}{RESET}"
    text = f"{'+' if v > 0 else '-'}${abs(v):,.2f}".rjust(width)
    return f"{GREEN}{text}{RESET}" if v <= 0 else text


def report_booking(account, booking: Dict[str, Any], loyalty: Optional[str],
                   state: Optional[str], limit: int,
                   alert_below: Optional[float] = None) -> List[str]:
    bid = booking.get("bookingId")
    guests = booking.get("passengersInStateroom") or []
    booked_cat = next((g.get("stateroomCategoryCode") for g in guests
                       if g.get("stateroomCategoryCode")), None)
    booked_sub = booking.get("stateroomSubtype")
    sd = str(booking.get("sailDate") or "")
    sail_disp = f"{sd[0:4]}-{sd[4:6]}-{sd[6:8]}" if len(sd) == 8 else sd

    log(f"\n{BLUE}{'=' * 72}{RESET}")
    log(f"{BLUE}Reservation {bid}{RESET}  {booking.get('shipCode')} {sail_disp}  "
        f"room {booking.get('stateroomNumber')}  cat {booked_cat}  {len(guests)} guest(s)")

    ledger = read_ledger(account, booking)
    paid = ledger["gross"]
    log(f"  You pay (gross): {money(paid)}   original fare {money(ledger['original_fare'])} "
        f"- discounts {money(abs(ledger['discount'] or 0))} + taxes/fees {money(ledger['taxes'])}")
    if ledger["balance_due"]:
        log(f"  {YELLOW}Payments applied {money(ledger['payments_applied'])}; "
            f"balance due {money(ledger['balance_due'])}{RESET}")
    if ledger["is_casino"]:
        items = "; ".join(f"{i['desc']} {money(i['amount'])}" +
                          (f" ({i['promo']})" if i.get("promo") else "")
                          for i in ledger["casino_items"])
        log(f"  {YELLOW}CASINO RATE booking:{RESET} {items}")

    inventory = get_sailing_inventory(account, booking, loyalty)
    if not inventory:
        log(f"  {YELLOW}No categories currently for sale on this sailing "
            f"(sold out or too close to departure) - cannot price upgrades.{RESET}")
        return []

    # Per-category prices for the booked subtype (booked category may not be its lead-in)
    booked_now: Optional[float] = None
    if booked_sub:
        booked_type = next((r["type"] for r in inventory if r["subtype"] == booked_sub), None)
        if booked_type:
            cat_prices = get_category_prices(account, booking, booked_sub, booked_type, loyalty)
            booked_now = cat_prices.get(booked_cat)
            if booked_now is None and cat_prices:
                # category itself sold out; note the subtype's cheapest sibling instead
                booked_now = None

    if booked_now is not None:
        log(f"  Booked category {booked_cat} at today's rate: {money(booked_now)}  "
            f"(vs paid: {delta(booked_now - paid, 0).strip() if isinstance(paid, (int, float)) else '-'})")
    else:
        log(f"  Booked category {booked_cat} is not currently for sale "
            f"(dl-rate column unavailable).")

    rows = [r for r in inventory
            if r["total"] is not None and not r["guarantee"] and not r["connecting"]]
    # The table rows are each subtype's lead-in category; if the booked category is a
    # different tier of its subtype (e.g. booked 2D when 4D is the lead-in), add it as
    # its own row so it appears starred in the list.
    if booked_now is not None and not any(r["category"] == booked_cat for r in rows):
        base = next((r for r in inventory if r["subtype"] == booked_sub), None)
        rows.append({"type": base["type"] if base else "?", "subtype": booked_sub,
                     "category": booked_cat, "name": (base["name"] if base else "") + " (booked)",
                     "guarantee": False, "connecting": False, "total": booked_now})
    rows.sort(key=lambda r: r["total"])
    if not rows:
        log(f"  {YELLOW}No priced categories returned for this sailing.{RESET}")
        return []

    log(f"\n  Current prices for {max(1, len(guests))} guest(s), loyalty applied "
        f"(all-in, taxes included):")
    header = f"    {'':1} {'cat':5} {'type':8} {'now':>12} {'dl-paid':>12} {'dl-rate':>12}  description"
    log(header)
    log("    " + "-" * (len(header) - 4))
    shown = 0
    booked_rank = TYPE_RANK.get(next((x["type"] for x in rows if x["category"] == booked_cat),
                                     ""), -1)
    hits: List[str] = []
    for r in rows:
        d_paid = (r["total"] - paid) if isinstance(paid, (int, float)) else None
        d_rate = (r["total"] - booked_now) if isinstance(booked_now, (int, float)) else None

        if not limit or shown < limit:
            mark = "*" if (r["category"] == booked_cat or
                           (booked_now is None and r["subtype"] == booked_sub)) else " "
            log(f"    {mark} {str(r['category'] or r['subtype']):5} {str(r['type']):8} "
                f"{money(r['total']):>12} {delta(d_paid)} {delta(d_rate)}  {r['name']}")
            shown += 1

        # Alerting scans EVERY row regardless of the display --limit: a HIGHER-class
        # category whose category-difference cost (dl-rate, falling back to dl-paid
        # when the booked category isn't priced) is at or below the threshold
        if alert_below is not None and r["category"] != booked_cat:
            basis = d_rate if d_rate is not None else d_paid
            # An upgrade is a higher CLASS, or a pricier category within the same class
            # (e.g. Balcony 2D -> Spacious Balcony 4B)
            rank = TYPE_RANK.get(r["type"], -1)
            is_upgrade = (rank > booked_rank
                          or (rank == booked_rank and isinstance(booked_now, (int, float))
                              and r["total"] > booked_now))
            if basis is not None and basis <= alert_below and is_upgrade:
                hits.append(f"{booking.get('shipCode')} {sail_disp} #{bid}: "
                            f"{booked_cat} -> {r['category']} {r['name']} "
                            f"for {'+' if basis > 0 else ''}${basis:,.2f} "
                            f"(now ${r['total']:,.2f})")
    if limit and len(rows) > limit:
        log(f"    ... and {len(rows) - limit} more (use --limit 0 to show all)")

    if ledger["is_casino"]:
        log(f"  {YELLOW}Note: casino-rate booking - a straight repricing (dl-paid) would forfeit "
            f"the comp. Prior CASINO UPGRD charges on this account billed ~the category "
            f"difference, so dl-rate is the better estimate; confirm with the casino desk.{RESET}")
    return hits


##################################
# Main execution path
##################################
def main() -> None:
    parser = argparse.ArgumentParser(description="Check Royal Caribbean room upgrade costs")
    parser.add_argument("-c", "--config", type=str, default="config.yaml",
                        help="Path to configuration YAML file (default: config.yaml)")
    parser.add_argument("--reservation", type=str,
                        help="Only this reservation/booking id")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max categories listed per booking (0 = all)")
    parser.add_argument("--alert-below", type=float, default=None,
                        help="Send an Apprise alert when a higher-class category's "
                             "dl-rate is at or below this amount (also settable as "
                             "upgradeAlertBelow in config.yaml)")
    args = parser.parse_args()

    account, state, loyalty, data = build_account(args.config)
    alert_below = args.alert_below
    if alert_below is None and data.get("upgradeAlertBelow") is not None:
        alert_below = float(data["upgradeAlertBelow"])
    apobj = build_apprise(data) if alert_below is not None else None
    bookings = fetch_bookings(account)
    if args.reservation:
        bookings = [b for b in bookings if str(b.get("bookingId")) == str(args.reservation)]
        if not bookings:
            log(f"{RED}No booking {args.reservation} on this account.{RESET}")
            return
    log(f"\n{len(bookings)} booking(s) to check.")

    all_hits: List[str] = []
    for booking in bookings:
        if not booking.get("sailDate") or not booking.get("amendToken"):
            log(f"\n{YELLOW}Skipping booking {booking.get('bookingId')} "
                f"(no sail date or amend token).{RESET}")
            continue
        all_hits += report_booking(account, booking, loyalty, state, args.limit,
                                   alert_below=alert_below)

    if alert_below is not None:
        if all_hits:
            body = (f"{len(all_hits)} upgrade(s) at or below ${alert_below:,.2f} "
                    f"(category-difference basis):\n" + "\n".join(f"- {h}" for h in all_hits))
            log(f"\n{GREEN}{body}{RESET}")
            if apobj is not None:
                apobj.notify(body=body, title="Cruise Upgrade Opportunity")
        else:
            log(f"\n No upgrades at or below ${alert_below:,.2f}.")

    log(f"\n{GREEN}Done.{RESET} dl-paid = category total minus what you pay today; "
        f"dl-rate = minus your booked category at today's rate.")


if __name__ == "__main__":
    main()
