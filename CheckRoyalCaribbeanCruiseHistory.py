"""
Report: past cruise history + who shared the stateroom.

Uses the same config.yaml as the other tools. For EVERY account listed under
accountInfo it logs in and pulls the loyalty cruise history
(/guestAccounts/loyalty/history/{accountId} - the per-sailing ledger behind
the "Cruise History" page), then:

  1. prints each person's past sailings (date, ship, nights, cabin, itinerary)
  2. if more than one account is configured, joins the histories on
     ship + sail date + cabin number to show who shared each room
  3. prints upcoming bookings with the roommates the API lists per stateroom

The loyalty ledger only records the account holder, so roommates on PAST
sailings can only be derived by cross-referencing multiple accounts' histories.
Add family members' logins to accountInfo in config.yaml to match them up.

    python3.12 CheckRoyalCaribbeanCruiseHistory.py -c path/to/config.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import yaml

import CheckRoyalCaribbeanPrice as crccl
from CheckRoyalCaribbeanPrice import GREEN, YELLOW, BLUE, RESET

# shipCode -> friendly name, populated in main() once the fleet API is queried
SHIP_NAMES: Dict[str, str] = {}


SAILED_FILE_DEFAULT = "data/sailed-bookings.json"


def load_accounts(config_path: str) -> Tuple[List[Any], List[str], Optional[str], str]:
    """Log every account in. Returns (accounts, skipped, history_db, sailed_file)
    where skipped holds the masked usernames that failed login, history_db is
    the price checker's optional SQLite path (used to spot sailed-but-unposted
    points), and sailed_file is this script's own record of bookings seen
    (`sailedBookingsFile`, default data/sailed-bookings.json)."""
    with open(config_path) as f:
        data = crccl.expand_env_vars(yaml.safe_load(f)) or {}
    crccl.setup_hybrid_logging(data.get("logFile"))
    history_db = data.get("historyDb")
    sailed_file = str(data.get("sailedBookingsFile") or SAILED_FILE_DEFAULT)

    entries = data.get("accountInfo") or []
    if not entries:
        print("No accountInfo in config.", file=sys.stderr)
        sys.exit(1)

    accounts = []
    skipped: List[str] = []
    for i, a in enumerate(entries):
        if i:
            # Same courtesy pause the main price checker uses between accounts
            crccl.log(f"Sleeping {crccl.ACCOUNT_COOLDOWN_SECONDS} seconds to allow API to cool down between accounts")
            time.sleep(crccl.ACCOUNT_COOLDOWN_SECONDS)
        # One bad account must not kill the whole multi-account run: login and
        # get_profile sys.exit on failure, a malformed config entry KeyErrors,
        # and a 200-with-garbage profile body ValueErrors - skip them all
        label = mask_username((a.get("username") if isinstance(a, dict) else "") or "") \
            or f"account {i + 1}"
        try:
            account = crccl.AccountInfo(username=a["username"], password=a["password"],
                                        cruise_line=a.get("cruiseLine", "royalcaribbean"))
            account.access = crccl.login(account)
            _state, loyalty, points = crccl.get_profile(account)
        except SystemExit:
            crccl.log(f"{YELLOW}skipping {label}: login failed{RESET}")
            skipped.append(label)
            continue
        except Exception as e:
            crccl.log(f"{YELLOW}skipping {label}: {type(e).__name__}: {e}{RESET}")
            skipped.append(label)
            continue
        accounts.append((account, loyalty, points))
    if not accounts:
        print("No accounts could log in.", file=sys.stderr)
        sys.exit(1)
    return accounts, skipped, history_db, sailed_file


def api_get(account, url: str, params: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """Single-shot GET through the logged-in session; on failure show the error body."""
    headers = {
        "Access-Token": account.access.token,
        "AppKey": crccl.APPKEY_WEB,
        "account-id": account.access.id,
        "vds-id": account.access.id,
    }
    try:
        resp = account.access.session.get(url, params=params, headers=headers, timeout=15)
    except Exception as e:
        crccl.log(f"  {YELLOW}{url.split('.com', 1)[-1]}: {e}{RESET}")
        return None
    if resp.status_code != 200:
        crccl.log(f"  {YELLOW}{url.split('.com', 1)[-1]}: HTTP {resp.status_code}  "
                  f"body: {resp.text[:600]}{RESET}")
        return None
    try:
        return resp.json()
    except ValueError:
        crccl.log(f"  {YELLOW}non-JSON response from {url}{RESET}")
        return None


def pretty_date(compact: str) -> str:
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}" if len(compact or "") == 8 else (compact or "?")


def guest_name(guest: Dict[str, Any]) -> str:
    first, last = guest.get("firstName"), guest.get("lastName")
    return " ".join(p for p in (first, last) if p) or "<unnamed>"


def mask_username(name: str) -> str:
    """Mask a login email for display: local part + first letter of the domain
    (jo@g…). Output is persisted to logFile, so full logins must not leak."""
    name = name or ""
    if "@" in name:
        local, domain = name.split("@", 1)
        name = f"{local}@{domain[:1]}…"
    return name


def account_label(account, idx: int) -> str:
    """Display label for section headers (masked login, or a positional fallback)."""
    return mask_username(account.username) or f"account {idx + 1}"


def unique_account_labels(accounts: List[Any]) -> List[str]:
    """Masked labels, disambiguated: jim@aol.com and jim@att.net both mask to
    "jim@a…", and colliding labels silently merged the household/shared-room
    joins (one member's history vanished). Collisions get " (2)", " (3)"..."""
    labels: List[str] = []
    seen: Dict[str, int] = {}
    for idx, account in enumerate(accounts):
        base = account_label(account, idx)
        seen[base] = seen.get(base, 0) + 1
        labels.append(base if seen[base] == 1 else f"{base} ({seen[base]})")
    return labels


def missing_note(skipped: List[str], no_history: List[str]) -> Optional[str]:
    """One-line disclosure of who is absent from the cross-account views, so a
    shrunken household/shared-room join is never mistaken for the full picture."""
    parts = [f"{n} - login failed" for n in skipped]
    parts += [f"{n} - no sailings on record" for n in no_history]
    return f"(not included: {'; '.join(parts)})" if parts else None


##################################
# Loyalty history (past sailings)
##################################
def fetch_history(account, loyalty: Optional[str],
                  idx: int) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Returns (lifetime summary payload, sailings list). Logs nothing but errors."""
    base = f"https://aws-prd.api.rccl.com/en/{account.api_brand}/web/v1/guestAccounts/loyalty"

    lifetime = {}
    if loyalty:
        summary = api_get(account, f"{base}/history/summary", {"loyaltyNumber": loyalty})
        lifetime = (summary or {}).get("payload") or {}

    data = api_get(account, f"{base}/history/{account.access.id}",
                   {"loyaltyNumber": loyalty} if loyalty else None)
    if not data:
        return lifetime, []
    return lifetime, ((data or {}).get("payload") or {}).get("sailings") or []


##################################
# C&A points math
##################################
# Per-night earn rates: 1 = double+ occupancy (or solo in a studio),
# 2 = suite OR solo, 3 = solo in a suite; promo "double points" sailings
# double whichever of those applied.
CA_TIERS = [("Gold", 3), ("Platinum", 30), ("Emerald", 55), ("Diamond", 80),
            ("Diamond Plus", 175), ("Pinnacle Club", 700)]

# D+ point-level perks. Amenity counts per relationship from cas-amenities.pdf
# (175+ = 1, 340+ = 2, 525+ = 3; 7+ night sailings, shorter stay at 1). 340 is
# the big one: reduced single-supplement fares on qualified sailings, plus the
# second amenity.
DP_MILESTONES = {340: "Diamond Plus 340 milestone, Single supplement cruise fare "
                      "reduced on qualified sailings, 2 amenities on 7+ night sailings",
                 525: "525 milestone: 3 amenities on 7+ night sailings"}

# Pinnacle: free milestone cruise at 700, another every 350 points after
PINNACLE_FREE_FIRST, PINNACLE_FREE_STEP = 700, 350

# Crown & Anchor double-points promo (booked Jul 21-31 2026): eligible sailing
# window, max 2 cruises per member, no transatlantic/transpacific, no casino
# rates. Booking date and casino status aren't in the API, so qualifying
# bookings are named by the user via --double-points.
PROMO_SAIL_START, PROMO_SAIL_END = "20260901", "20270430"
PROMO_MAX_CRUISES = 2

# Casino-rate markers in the ledger's discount/option items (same as upgrade checker)
CASINO_MARKER = re.compile(r"casino|clubr|club royale", re.I)
TATP_MARKER = re.compile(r"transatlantic|transpacific|trans-atlantic|trans-pacific", re.I)
# NOTE: no API we can reach exposes the booking-creation date. The amend page's
# top-level "createdDate" is a render artifact, and its payment "schedule" holds
# DUE dates (TOTAL = final-payment deadline ~90 days out), not payments made.
# So the promo's Jul 21-31 booking window can only be confirmed by the user.


def probe_promo(account, bookings: List[Dict[str, Any]],
                holder_name: Optional[str]) -> frozenset:
    """
    Screen the holder's in-window bookings for double-points eligibility by
    reading each booking's amend page: casino-rate markers in the ledger and
    transatlantic/transpacific mentions. The booking date is NOT available from
    the API, so nothing is auto-counted - this prints the bookings that pass
    every verifiable check so the user can confirm them via --double-points.
    Returns an empty set (screening is informational only).
    """
    today = date.today().strftime("%Y%m%d")
    candidates = []
    for b in sorted(bookings, key=lambda x: x.get("sailDate") or ""):
        sail = b.get("sailDate") or ""
        if not (PROMO_SAIL_START <= sail <= PROMO_SAIL_END) or sail < today:
            continue
        names = [guest_name(g).upper() for g in (b.get("passengersInStateroom") or [])]
        if holder_name and holder_name not in names:
            continue
        candidates.append(b)
    if not candidates:
        return frozenset()

    crccl.log(f"\n{BLUE}Double-points promo screening{RESET} "
              f"(in-window sailings, casino & TA/TP checked; booking date is not "
              f"in the API):")
    passed = []
    for b in candidates:
        bid = str(b.get("bookingId") or "?")
        label = f"  {bid} ({pretty_date(b.get('sailDate'))})"
        token = b.get("amendToken")
        if not token:
            crccl.log(f"{label}: no amend token - can't check, use --double-points to force")
            continue
        resp = crccl._execute_api_request(
            account, "GET",
            f"https://www.{account.url_brand}.com/usa/en/booked/overview",
            params={"token": token, "country": b.get("bookingOfficeCountryCode", "USA")},
            headers={"User-Agent": crccl.USER_AGENT_WEB, "Accept": "text/x-component", "RSC": "1"},
            on_failure="retry")
        if resp is None:
            crccl.log(f"{label}: amend page unavailable - unknown")
            continue
        text = resp.text
        casino = False
        for p in crccl._extract_json_array(text, "prices") or []:
            if p.get("priceTypeCode") in ("DISCOUNT", "OPTIONS"):
                for item in (p.get("priceItems") or []):
                    if CASINO_MARKER.search(item.get("description") or ""):
                        casino = True
        # The casino check above is scoped to the ledger's priceItems, but no
        # comparable narrow itinerary/voyage-description field is reliably
        # extractable from the amend page's RSC payload, so the TA/TP check
        # scans the WHOLE page text. Marketing copy that merely mentions
        # "Transatlantic" can trip it - treat a hit as a hint, not a verdict.
        tatp = bool(TATP_MARKER.search(text))

        facts = [f"casino rate: {'YES' if casino else 'no'}",
                 f"text mentions TA/TP: {'YES' if tatp else 'no'}"]
        if casino:
            verdict = f"{YELLOW}not eligible{RESET}"
        elif tatp:
            verdict = (f"{YELLOW}page text mentions TA/TP - verify the itinerary; "
                       f"use --double-points to force if it is not TA/TP{RESET}")
        else:
            verdict = f"{GREEN}eligible if booked Jul 21-31 2026{RESET}"
            passed.append(bid)
        crccl.log(f"{label}: {', '.join(facts)} -> {verdict}")

    if passed:
        crccl.log(f"  To count the ones you booked during the promo window (max "
                  f"{PROMO_MAX_CRUISES}), rerun with: --double-points "
                  f"{','.join(passed[:PROMO_MAX_CRUISES])}"
                  + (f"  (or pick {PROMO_MAX_CRUISES} of: {', '.join(passed)})"
                     if len(passed) > PROMO_MAX_CRUISES else ""))
    return frozenset()


def next_free_cruise(points: int) -> int:
    if points < PINNACLE_FREE_FIRST:
        return PINNACLE_FREE_FIRST
    return PINNACLE_FREE_FIRST + (
        (points - PINNACLE_FREE_FIRST) // PINNACLE_FREE_STEP + 1) * PINNACLE_FREE_STEP

# Crystal blocks: first awarded at 140 points, another every 70 after (210, 280, ...)
BLOCK_FIRST, BLOCK_STEP = 140, 70


def next_block(points: int) -> int:
    if points < BLOCK_FIRST:
        return BLOCK_FIRST
    return BLOCK_FIRST + ((points - BLOCK_FIRST) // BLOCK_STEP + 1) * BLOCK_STEP


def blocks_crossed(before: int, after: int) -> List[int]:
    out = []
    t = BLOCK_FIRST
    while t <= after:
        if t > before:
            out.append(t)
        t += BLOCK_STEP
    return out


def block_number(threshold: int) -> int:
    return (threshold - BLOCK_FIRST) // BLOCK_STEP + 1


def sail_ints(s: Dict[str, Any]) -> Tuple[int, int]:
    try:
        return int(s.get("itineraryNightsQuantity") or 0), int(s.get("points") or 0)
    except (TypeError, ValueError):
        return 0, 0


def is_suite(s: Dict[str, Any]) -> bool:
    return "suite" in (s.get("cabinClassDescription") or "").lower()


def rate_label(s: Dict[str, Any]) -> str:
    """Explain the per-night earn rate for a sailing (blank for the standard 1x)."""
    nights, pts = sail_ints(s)
    if not nights or not pts or pts == nights:
        return ""
    rate = pts / nights
    suite = is_suite(s)
    explanations = {
        2: "suite" if suite else "solo or 2x promo",
        3: "solo suite" if suite else "unexpected 3x",
        4: "suite + 2x promo" if suite else "solo + 2x promo",
        6: "solo suite + 2x promo" if suite else "unexpected 6x",
    }
    why = explanations.get(rate, "unusual rate")
    return f"  {GREEN}{rate:g}x pts{RESET} ({why})"


def sail_date(s: Dict[str, Any]) -> Optional[date]:
    try:
        return datetime.strptime(s.get("sailingDate") or "", "%Y%m%d").date()
    except ValueError:
        return None


def show_sailings(sailings: List[Dict[str, Any]], earns_blocks: bool = True) -> None:
    cum = 0  # assumes the history list is the complete points ledger
    for s in sorted(sailings, key=lambda x: x.get("sailingDate") or ""):
        ship = s.get("shipName") or s.get("shipCode") or "?"
        nights = s.get("itineraryNightsQuantity") or "?"
        cabin = s.get("cabinNumber") or "?"
        cat = s.get("cabinCategory") or "?"
        pts = s.get("points")
        pts_txt = f"  {pts} pts" if pts is not None else ""
        before, cum = cum, cum + sail_ints(s)[1]
        block_txt = ""
        if earns_blocks:
            block_txt = "".join(f"  {GREEN}[crystal block #{block_number(t)} at {t}]{RESET}"
                                for t in blocks_crossed(before, cum))
        crccl.log(f"  {pretty_date(s.get('sailingDate'))}  {ship:<26} {nights}n  "
                  f"cabin {cabin} ({cat}){pts_txt}{rate_label(s)}  "
                  f"{s.get('itineraryDescription') or ''}{block_txt}")


def show_household(histories: List[Tuple[str, List[Dict[str, Any]]]],
                   absent_note: Optional[str] = None) -> None:
    """Merged view across everyone's histories: together vs apart, per year."""
    crccl.log(f"\n{BLUE}=== Household combined view ==={RESET}")
    if absent_note:
        crccl.log(f"{YELLOW}{absent_note}{RESET}")
    sets: Dict[str, set] = {}
    key_info: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for label, sailings in histories:
        ks = set()
        for s in sailings:
            k = (s.get("sailingDate") or "?", s.get("shipCode") or "?")
            ks.add(k)
            key_info.setdefault(k, s)
        sets[label] = ks

    all_keys = set().union(*sets.values())
    together = set.intersection(*sets.values())
    solo_parts = ", ".join(f"{len(sets[label] - together)} {label} only" for label in sets)
    crccl.log(f"  {len(all_keys)} unique cruises across the household: "
              f"{len(together)} together (same ship & date), {solo_parts}")

    years: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for k in all_keys:
        year = (k[0] or "????")[:4]
        nights = sail_ints(key_info[k])[0]
        years[year][0] += 1
        years[year][2] += nights
        if k in together:
            years[year][1] += 1
            years[year][3] += nights
    crccl.log("  year   cruises  together  nights  nights-together")
    for year in sorted(years):
        c, t, n, tn = years[year]
        crccl.log(f"  {year}   {c:>7}  {t:>8}  {n:>6}  {tn:>15}")
    total = [sum(v[i] for v in years.values()) for i in range(4)]
    crccl.log(f"  total  {total[0]:>7}  {total[1]:>8}  {total[2]:>6}  {total[3]:>15}")
    crccl.log("  (cabin-level detail is in the shared-rooms section above)")


def show_yearly(sailings: List[Dict[str, Any]],
                upcoming: List[Tuple[str, Dict[str, Any], int, str]],
                pending_points: int = 0, credited: int = 0) -> None:
    """pending_points is the --pending-points adjustment (points earned but not
    posted, with no sailing of their own); credited is the profile balance's
    lead over the ledger sum (points posted to the balance whose ledger line
    has not appeared yet). Both land in the current year so this table ends on
    the same number the tier-progress line reports."""
    crccl.log(f"\n{BLUE}Per-year totals:{RESET}")
    years: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0])
    for s in sailings:
        nights, pts = sail_ints(s)
        year = (s.get("sailingDate") or "????")[:4]
        years[year][0] += 1
        years[year][1] += nights
        years[year][2] += pts

    # Booked (not yet sailed) cruises, as estimated additions per year
    est: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0])
    for sail, b, pts, _why in upcoming:
        try:
            nights = int(b.get("numberOfNights") or 0)
        except (TypeError, ValueError):
            nights = 0
        est[sail[:4]][0] += 1
        est[sail[:4]][1] += nights
        est[sail[:4]][2] += pts

    # One uniform table (same style as the projections table): history rows and
    # booked ("est") rows interleaved by year, with a running points total so
    # each year shows where the balance stood/lands.
    this_year = date.today().strftime("%Y")
    table = []
    cum = 0
    for year in sorted(set(years) | set(est) | ({this_year} if pending_points or credited else set())):
        if year in years:
            cruises, nights, pts = years[year]
            cum += pts
            table.append((year, str(cruises), str(nights), str(pts), str(cum)))
        if credited and year == this_year:
            cum += credited
            table.append((f"{year} posted", "", "", f"+{credited}", str(cum)))
        if pending_points and year == this_year:
            cum += pending_points
            table.append((f"{year} pending", "", "", f"+{pending_points}", str(cum)))
        if year in est:
            cruises, nights, pts = est[year]
            cum += pts
            table.append((f"{year} est", f"+{cruises}", f"+{nights}", f"+{pts}", str(cum)))

    total = [sum(v[i] for v in years.values()) for i in range(3)]
    est_total = [sum(v[i] for v in est.values()) for i in range(3)]
    summary = [("total", str(total[0]), str(total[1]), str(total[2]), "")]
    if est_total[0] or pending_points or credited:
        summary.append(("w/ booked", str(total[0] + est_total[0]),
                        str(total[1] + est_total[1]),
                        str(total[2] + est_total[2] + pending_points + credited), ""))

    headers = ("Year", "Cruises", "Nights", "Points", "Total")
    widths = [max(len(r[i]) for r in ([headers] + table + summary)) for i in range(5)]

    def emit(r, suffix=""):
        cells = [r[0].ljust(widths[0])] + [r[i].rjust(widths[i]) for i in range(1, 5)]
        crccl.log(("  " + "  ".join(cells)).rstrip() + suffix if suffix else
                  "  " + "  ".join(cells).rstrip())

    crccl.log("  " + "  ".join(h.ljust(widths[i]) if i == 0 else h.rjust(widths[i])
                               for i, h in enumerate(headers)))
    crccl.log("  " + "  ".join("-" * w for w in widths))
    for r in table:
        emit(r, "  (booked)" if r[0].endswith("est") else
                "  (--pending-points, not posted yet)" if r[0].endswith("pending") else
                "  (in your balance, not itemized in the ledger yet)" if r[0].endswith("posted") else "")
    crccl.log("  " + "  ".join("-" * w for w in widths))
    for r in summary:
        emit(r)
    bonus = total[2] - total[1]
    if bonus > 0:
        crccl.log(f"  ({bonus} points above 1x/night, from suite/solo/promo sailings)")


def show_b2b(sailings: List[Dict[str, Any]]) -> None:
    """Flag consecutive sailings where one ends the day the next begins."""
    dated = sorted((s for s in sailings if sail_date(s)), key=sail_date)
    found = False
    for prev, nxt in zip(dated, dated[1:]):
        nights, _ = sail_ints(prev)
        if not nights:
            continue
        gap = (sail_date(nxt) - sail_date(prev)).days - nights
        if gap == 0:
            if not found:
                crccl.log(f"\n{BLUE}Back-to-back sailings:{RESET}")
                found = True
            same = prev.get("shipCode") == nxt.get("shipCode")
            kind = "B2B" if same else "side-to-side (ship change)"
            crccl.log(f"  {pretty_date(prev.get('sailingDate'))} "
                      f"{prev.get('shipName')} -> {pretty_date(nxt.get('sailingDate'))} "
                      f"{nxt.get('shipName')}  [{kind}]")


def sailing_status(booking: Dict[str, Any], today: Optional[date] = None) -> str:
    """'upcoming', 'in_progress' (sailed, not yet debarked), 'ended', or 'unknown'.

    The loyalty ledger only gains a cruise a few days after debarkation, so a
    sailing in progress is in neither the history nor the future: without this
    it read as [past] with no points at all."""
    today = today or date.today()
    try:
        sailed = datetime.strptime(str(booking.get("sailDate") or ""), "%Y%m%d").date()
        nights = int(booking.get("numberOfNights") or 0)
    except (TypeError, ValueError):
        return "unknown"
    if sailed > today:
        return "upcoming"
    if nights and today < sailed + timedelta(days=nights):
        return "in_progress"
    return "ended"


def get_holder_name(account) -> Optional[str]:
    """Account holder's name from the v3 profile, for matching them in bookings."""
    url = f"https://aws-prd.api.rccl.com/en/{account.api_brand}/web/v3/guestAccounts/{account.access.id}"
    pay = (api_get(account, url) or {}).get("payload") or {}
    for node in (pay, pay.get("personalInformation") or {}, pay.get("userProfile") or {}):
        first, last = node.get("firstName"), node.get("lastName")
        if first and last:
            return f"{first} {last}".upper()
    return None


def upcoming_earnings(bookings: List[Dict[str, Any]], holder_name: Optional[str],
                      promo_ids: frozenset = frozenset(),
                      new_promo_ids: frozenset = frozenset(),
                      posted: frozenset = frozenset()
                      ) -> List[Tuple[str, Dict[str, Any], int, str]]:
    """Project C&A points from booked cruises: (sailDate, booking, pts, why).

    Covers future sailings, a sailing in progress, and one that has ended but
    is not yet in the loyalty ledger (`posted` = the ledger's (shipCode,
    sailingDate) keys) - the same estimate either way, marked as such, since
    C&A posts points a few days after debarkation. A sailing already in the
    ledger is history, not a projection.

    Two promo shapes exist: the original double-points promo doubled the whole
    per-night rate ((base + suite + solo) x2); the newer one doubles only the
    base+suite part and pays the solo supplement single ((base + suite) x2 + solo),
    so solo earns 3/night (not 4) and suite-solo 5/night (not 6). Non-solo
    bookings earn the same either way."""
    rows = []
    for b in bookings:
        sail = b.get("sailDate") or ""
        status = sailing_status(b)
        if not sail or status == "unknown":
            continue
        if status == "ended" and (b.get("shipCode"), sail) in posted:
            continue
        guests = b.get("passengersInStateroom") or []
        names = [guest_name(g).upper() for g in guests]
        if holder_name and holder_name not in names and not b.get("_remembered"):
            continue  # a linked booking (someone else's room)
        try:
            nights = int(b.get("numberOfNights") or 0)
        except (TypeError, ValueError):
            nights = 0
        if not nights:
            continue
        suite = b.get("stateroomType") == "D"
        solo = len(guests) == 1
        rate = 1 + (1 if suite else 0) + (1 if solo else 0)
        why = ", ".join(w for w, on in (("suite", suite), ("solo", solo)) if on) or "standard"
        pts = nights * rate
        desc = f"{nights}n x{rate} ({why})"
        bid = str(b.get("bookingId") or "")
        if bid in new_promo_ids:
            # New promo shape: only base + suite doubles; the solo point stays single.
            # No sail-window gate - Royal has not published one for this promo, and
            # the ids are user-supplied explicitly.
            new_rate = (1 + (1 if suite else 0)) * 2 + (1 if solo else 0)
            pts = nights * new_rate
            desc = f"{nights}n x{new_rate} ({why} + new double-points promo: base x2 + solo x1)"
        elif bid in promo_ids:
            if PROMO_SAIL_START <= sail <= PROMO_SAIL_END:
                pts *= 2
                desc = f"{nights}n x{rate}x2 ({why} + double-points promo)"
            else:
                desc += (f"  {YELLOW}[--double-points ignored: sails outside the "
                         f"Sep 2026 - Apr 2027 promo window]{RESET}")
        if status == "in_progress":
            desc += f"  {YELLOW}[sailing now - estimate; posts after debarkation]{RESET}"
        elif status == "ended":
            desc += f"  {YELLOW}[ended - estimate; not in the loyalty ledger yet]{RESET}"
        rows.append((sail, b, pts, desc))
    return sorted(rows, key=lambda r: r[0])


def show_upcoming_earnings(rows: List[Tuple[str, Dict[str, Any], int, str]],
                           ships: Dict[str, str], holder_name: Optional[str],
                           start_points: int = 0, earns_blocks: bool = True,
                           promo_ids: frozenset = frozenset(),
                           new_promo_ids: frozenset = frozenset()) -> int:
    who = f" for {holder_name}" if holder_name else ""
    start_txt = f" (starting from {start_points} pts)" if start_points else ""
    crccl.log(f"\n{BLUE}Projected points from booked cruises{who}{start_txt}:{RESET}")
    if not holder_name:
        crccl.log(f"  {YELLOW}(couldn't read the account holder's name - linked bookings "
                  f"for other people's rooms may be counted below){RESET}")
    if not rows:
        crccl.log("  (no upcoming bookings found)")
        return 0

    # Build ANSI-free cells first so the column-width math is never skewed by
    # color codes (same pattern as the main script's check-in/payment table);
    # milestone notes are colored at print time only.
    strip_ansi = crccl.StripAnsiFilter.ANSI_REGEX.sub
    total = 0
    cum = start_points
    table = []
    for sail, b, pts, why in rows:
        ship = ships.get(b.get("shipCode"), b.get("shipCode") or "?")
        before, cum = cum, cum + pts
        notes = []
        if earns_blocks:
            notes += [f"crystal block #{block_number(t)} at {t}" for t in blocks_crossed(before, cum)]
        notes += [f"reaches {name}" for name, needed in CA_TIERS if before < needed <= cum]
        notes += [perk for m, perk in DP_MILESTONES.items() if before < m <= cum]
        table.append((pretty_date(sail), ship, str(b.get("stateroomNumber") or "GTY"),
                      strip_ansi("", why), f"+{pts}", str(cum), "; ".join(notes)))
        total += pts

    headers = ("Sail Date", "Ship", "Room", "Earn", "Pts", "Total", "")
    widths = [max(len(str(r[i])) for r in ([headers] + table)) for i in range(6)]
    crccl.log("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers[:6])))
    crccl.log("  " + "  ".join("-" * w for w in widths))
    for r in table:
        line = "  " + "  ".join(str(r[i]).ljust(widths[i]) for i in range(6))
        if r[6]:
            line += f"  {GREEN}[{r[6]}]{RESET}"
        crccl.log(line.rstrip())
    crccl.log(f"  total: +{total} pts -> {cum}")
    if any(sailing_status(b) != "upcoming" for _s, b, _p, _w in rows):
        crccl.log(f"  {YELLOW}(rows marked 'sailing now' / 'ended' are estimates until Crown & "
                  f"Anchor posts them - usually a few days after debarkation){RESET}")

    # ids given to BOTH flags resolve as new-promo in projected_rows, so they
    # must not count toward the OLD promo's per-member cruise cap
    doubled = [r for r in rows
               if str(r[1].get("bookingId") or "") in (promo_ids - new_promo_ids)
               and PROMO_SAIL_START <= r[0] <= PROMO_SAIL_END]
    if len(doubled) > PROMO_MAX_CRUISES:
        crccl.log(f"  {YELLOW}Warning: {len(doubled)} bookings flagged --double-points, but "
                  f"the promo caps at {PROMO_MAX_CRUISES} cruises per member{RESET}")
    crccl.log("  (solo-studio rules and unregistered promos can't be known in advance)")
    return total


def pending_ledger_sailings(db_path: Optional[str], username: str,
                            ledger: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cruises the price checker's historyDb snapshotted for this account that
    have already ENDED but are absent from the loyalty ledger - i.e. points that
    have not posted yet. Returns [] when no historyDb is configured/present.

    The API alone cannot reveal an unposted cruise (profile points and ledger
    both lack it); the bookings snapshot is the only record it happened."""
    if not db_path or not os.path.exists(db_path):
        return []
    posted = {(s.get("shipCode"), s.get("sailingDate")) for s in ledger}
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT observed_at, reservation_id, ship_code, ship_name, sail_date, "
                "nights, guest_count, stateroom_type FROM bookings WHERE account_label = ? "
                "ORDER BY observed_at", (username,)).fetchall()
    except sqlite3.Error:
        return []
    latest: Dict[str, tuple] = {r[1]: r for r in rows}   # last snapshot per reservation
    # Every snapshot time for this account, to tell "sailed" from "cancelled":
    # snapshots stop at the sail date either way, but a CANCELLED booking also
    # vanishes from runs that happen BEFORE its sail date
    observations = sorted({r[0] for r in rows})
    out = []
    today = date.today()
    for last_seen, rid, ship_code, ship_name, sail, nights, guest_count, stype in latest.values():
        try:
            nights = int(nights or 0)
            sailed = datetime.strptime(sail or "", "%Y%m%d").date()
            ended = sailed + timedelta(days=nights)
        except ValueError:
            continue
        if not nights or ended >= today or (ship_code, sail) in posted:
            continue
        # Cancelled, not sailed: the price checker ran again before the sail
        # date and this booking no longer appeared - do not nag about it forever
        if any(o > last_seen and datetime.strptime(o[:10], "%Y-%m-%d").date() < sailed
               for o in observations):
            continue
        suite = (stype or "").upper() in ("D", "DELUXE", "SUITE")
        solo = guest_count == 1
        est = nights * (1 + (1 if suite else 0) + (1 if solo else 0))
        out.append({"reservation_id": rid, "ship": ship_name or ship_code or "?",
                    "ship_code": ship_code, "sail_date": sail, "ended": ended, "est_points": est})
    return sorted(out, key=lambda p: p["sail_date"])


##################################
# Sailed-bookings record: bridging debarkation -> points posted
##################################
# No API Royal exposes returns a cruise between debarkation and the day Crown &
# Anchor posts it (usually about a week): the profile drops the booking on
# debarkation day and the loyalty ledger only gains it once posted - checked
# against the profile endpoint's parameters, the v3 profile, the web and
# mobile GraphQL schemas (both are the onboard commerce API). So this script
# remembers the bookings it sees, and a remembered booking that has sailed but
# not posted is listed as waiting, with the usual estimate.
SAILED_KEEP_DAYS = 120          # give up on an entry that never posts


def load_sailed_file(path: str) -> Dict[str, Any]:
    """Missing file starts fresh; an unreadable one is reported and NOT overwritten."""
    empty = {"version": 1, "accounts": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty
    except (OSError, ValueError) as e:
        crccl.log(f"{YELLOW}Could not read {path} ({type(e).__name__}); sailed-booking "
                  f"memory is off for this run{RESET}")
        return {"version": 1, "accounts": {}, "_readonly": True}
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
        crccl.log(f"{YELLOW}{path} is not a sailed-bookings file; ignoring it{RESET}")
        return {"version": 1, "accounts": {}, "_readonly": True}
    return data


def save_sailed_file(path: str, store: Dict[str, Any]) -> None:
    if store.get("_readonly"):
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "accounts": store["accounts"]}, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:
        crccl.log(f"{YELLOW}Could not write {path} ({e}); sailed bookings not remembered{RESET}")


def remember_bookings(store: Dict[str, Any], username: str, bookings: List[Dict[str, Any]],
                      holder_name: Optional[str], today: Optional[date] = None) -> None:
    """Record every booking on the profile the account holder is in (guest count
    and room type only - no names), and forget a remembered booking that
    vanished BEFORE its sail date: that is a cancellation, not a sailing."""
    today = today or date.today()
    mine = store["accounts"].setdefault(username, {})
    seen = set()
    for b in bookings:
        bid = str(b.get("bookingId") or "")
        if not bid or sailing_status(b, today) == "unknown":
            continue
        guests = b.get("passengersInStateroom") or []
        names = [guest_name(g).upper() for g in guests]
        if holder_name and holder_name not in names:
            continue
        seen.add(bid)
        entry = mine.get(bid, {"firstSeen": today.isoformat()})
        entry.update({"shipCode": b.get("shipCode"), "sailDate": str(b.get("sailDate")),
                      "nights": int(b.get("numberOfNights") or 0), "guests": len(guests),
                      "stateroomType": b.get("stateroomType"), "lastSeen": today.isoformat()})
        mine[bid] = entry
    for bid in list(mine):
        e = mine[bid]
        if bid in seen or bid.startswith("manual:"):
            continue
        try:
            sailed = datetime.strptime(str(e.get("sailDate") or ""), "%Y%m%d").date()
        except ValueError:
            del mine[bid]
            continue
        if sailed > today:
            del mine[bid]                      # gone before it sailed: cancelled


def parse_sailed_spec(spec: str) -> Dict[str, Any]:
    """SHIP:YYYYMMDD:NIGHTS[:GUESTS][:suite] -> a remembered-booking entry.
    Raises ValueError with the reason for anything else."""
    parts = [x.strip() for x in spec.split(":")]
    if len(parts) < 3:
        raise ValueError("expected SHIP:YYYYMMDD:NIGHTS[:GUESTS][:suite]")
    ship, sail, nights = parts[0].upper(), parts[1], parts[2]
    if not re.fullmatch(r"[A-Z]{2}", ship):
        raise ValueError(f"ship code must be two letters (got {parts[0]!r})")
    try:
        datetime.strptime(sail, "%Y%m%d")
    except ValueError:
        raise ValueError(f"sail date must be YYYYMMDD (got {sail!r})") from None
    if not nights.isdigit() or int(nights) < 1:
        raise ValueError(f"nights must be a positive number (got {nights!r})")
    guests = 2
    suite = False
    for extra in parts[3:]:
        if extra.isdigit():
            guests = int(extra)
        elif extra.lower() in ("suite", "solo"):
            suite = suite or extra.lower() == "suite"
            guests = 1 if extra.lower() == "solo" else guests
        elif extra:
            raise ValueError(f"unrecognized part {extra!r} (use a guest count, 'solo' or 'suite')")
    if guests < 1:
        raise ValueError("guest count must be at least 1")
    return {"shipCode": ship, "sailDate": sail, "nights": int(nights), "guests": guests,
            "stateroomType": "D" if suite else "B", "manual": True,
            "firstSeen": date.today().isoformat(), "lastSeen": date.today().isoformat()}


def add_manual_sailed(store: Dict[str, Any], username: str, specs: List[str]) -> List[str]:
    """Seed sailings nothing recorded (they ended before this script ran).
    Returns the specs that could not be parsed, with the reason."""
    errors = []
    mine = store["accounts"].setdefault(username, {})
    for spec in specs:
        try:
            entry = parse_sailed_spec(spec)
        except ValueError as e:
            errors.append(f"{spec}: {e}")
            continue
        mine[f"manual:{entry['shipCode']}:{entry['sailDate']}"] = entry
    return errors


def sailed_unposted(store: Dict[str, Any], username: str, posted: frozenset,
                    on_profile: set, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Remembered bookings that have ended, are not in the loyalty ledger and are
    no longer on the profile, as booking-shaped dicts the projection can price.
    Posted and long-stale entries are dropped from the record."""
    today = today or date.today()
    mine = store["accounts"].get(username) or {}
    out = []
    for bid in list(mine):
        e = mine[bid]
        try:
            sailed = datetime.strptime(str(e.get("sailDate") or ""), "%Y%m%d").date()
            nights = int(e.get("nights") or 0)
        except (TypeError, ValueError):
            del mine[bid]
            continue
        ended = sailed + timedelta(days=nights)
        if (e.get("shipCode"), e.get("sailDate")) in posted or (today - ended).days > SAILED_KEEP_DAYS:
            del mine[bid]                      # posted at last, or never will be
            continue
        if bid in on_profile or ended > today or not nights:
            continue
        out.append({"bookingId": bid, "shipCode": e.get("shipCode"), "sailDate": e.get("sailDate"),
                    "numberOfNights": nights, "stateroomType": e.get("stateroomType"),
                    "passengersInStateroom": [{} for _ in range(int(e.get("guests") or 2))],
                    "_remembered": "manual" if e.get("manual") else "seen"})
    return sorted(out, key=lambda b: b["sailDate"])


def reconcile_credited(upcoming: List[Tuple[str, Dict[str, Any], int, str]],
                       surplus: int) -> Tuple[List[Tuple[str, Dict[str, Any], int, str]], int]:
    """Crown & Anchor credits a cruise's points to the balance before its line
    appears in the ledger (seen live: balance 285, ledger 271). `surplus` is
    balance minus ledger sum. An ended-but-unlisted sailing whose estimate fits
    inside that surplus is already paid: its points are zeroed in the
    projection (the balance has them) and its row says so. Returns the
    adjusted rows and the surplus left unexplained."""
    if surplus <= 0:
        return upcoming, 0
    out = []
    for sail, b, pts, why in upcoming:
        if sailing_status(b) == "ended" and pts and pts <= surplus:
            surplus -= pts
            why = re.sub(r"  \x1b\[[0-9;]*m\[ended[^\]]*\]\x1b\[[0-9;]*m$", "", why)
            out.append((sail, dict(b, _credited=pts), 0,
                        why + f"  {GREEN}[ended - {pts} pts already in your balance; ledger line pending]{RESET}"))
        else:
            out.append((sail, b, pts, why))
    return out, surplus


def show_pending_points(pending: List[Dict[str, Any]]) -> None:
    if not pending:
        return
    crccl.log(f"\n{YELLOW}Points not posted yet ({len(pending)} sailed cruise(s) "
              f"missing from the loyalty ledger):{RESET}")
    for p in pending:
        days = (date.today() - p["ended"]).days
        src = {"manual": " (entered with --sailed)", "seen": " (remembered from an earlier run)"
               }.get(p.get("source"), "")
        if p.get("credited"):
            crccl.log(f"  {pretty_date(p['sail_date'])}  {p['ship']:<26} ended {days}d ago  "
                      f"{p['credited']} pts already in your balance; ledger line pending{src}")
            continue
        crccl.log(f"  {pretty_date(p['sail_date'])}  {p['ship']:<26} ended {days}d ago  "
                  f"~{p['est_points']} pts expected{src}")
    crccl.log("  (estimated in the projection below until Crown & Anchor posts them - usually "
              "within a week of debarkation; contact C&A if a cruise is still missing after 2 weeks)")


def show_tier_progress(account, profile_points: int, sailings: List[Dict[str, Any]],
                       upcoming: List[Tuple[str, Dict[str, Any], int, str]],
                       earns_blocks: bool = True, block_holder: str = "",
                       pending: int = 0) -> None:
    if not account.is_royal:
        return
    points = (profile_points or sum(sail_ints(s)[1] for s in sailings)) + pending
    source = "profile" if profile_points else "sum of history"
    if pending:
        source += f" + {pending} pending"
    crccl.log(f"\n{BLUE}Crown & Anchor progress:{RESET} {points} points ({source})")

    current = None
    next_tier = None
    for name, needed in CA_TIERS:
        if points >= needed:
            current = name
        elif next_tier is None:
            next_tier = (name, needed)
    crccl.log(f"  Current tier: {current or 'Pre-Gold'}")

    # Historical pace (trailing 24 months) and where the booked cruises leave us
    cutoff = date.today() - timedelta(days=730)
    recent = sum(sail_ints(s)[1] for s in sailings
                 if sail_date(s) and cutoff <= sail_date(s) <= date.today())
    pace = recent / 2  # points per year
    booked_pts = sum(r[2] for r in upcoming)
    end_pts = points + booked_pts
    if upcoming:
        last_sail, last_b = upcoming[-1][0], upcoming[-1][1]
        try:
            last_nights = int(last_b.get("numberOfNights") or 0)
        except (TypeError, ValueError):
            last_nights = 0
        try:
            booked_end = (datetime.strptime(last_sail, "%Y%m%d").date()
                          + timedelta(days=last_nights))
        except (TypeError, ValueError):
            booked_end = date.today()
    else:
        booked_end = date.today()

    def when(target: int) -> str:
        cum = points
        for sail, b, pts_, _why in upcoming:
            cum += pts_
            if cum >= target:
                ship = SHIP_NAMES.get(b.get("shipCode"), b.get("shipCode") or "?")
                how = {"in_progress": "sailing now", "ended": "ended, points pending"
                       }.get(sailing_status(b), "booked")
                return f"{GREEN}on the {pretty_date(sail)} {ship} sailing ({how}){RESET}"
        if pace:
            remaining = target - end_pts
            eta = booked_end + timedelta(days=365 * remaining / pace)
            return f"~{eta.strftime('%b %Y')} (after booked cruises, at {pace:.0f} pts/yr)"
        return "no recent sailings to estimate a pace"

    targets: Dict[int, List[str]] = defaultdict(list)
    if next_tier:
        targets[next_tier[1]].append(f"{next_tier[0]} tier")
    if earns_blocks:
        nb = next_block(points)
        targets[nb].append(f"crystal block #{block_number(nb)}")
    elif block_holder:
        crccl.log(f"  (crystal blocks go to {block_holder}, the household's highest member)")
    for m, perk in DP_MILESTONES.items():
        if points < m:
            targets[m].append(perk)
            break
    nfc = next_free_cruise(points)
    targets[nfc].append("free milestone cruise")
    for target in sorted(targets):
        crccl.log(f"  {target - points:>4} pts to {' + '.join(targets[target])} "
                  f"({target}): {when(target)}")

    if upcoming:
        crccl.log(f"  After all booked cruises (through {booked_end.strftime('%b %Y')}): "
                  f"~{end_pts} pts")


def room_key(sailing: Dict[str, Any]) -> Tuple[str, str, str]:
    return (sailing.get("sailingDate") or "?",
            sailing.get("shipCode") or "?",
            sailing.get("cabinNumber") or "?")


def show_shared_rooms(histories: List[Tuple[str, List[Dict[str, Any]]]],
                      absent_note: Optional[str] = None) -> None:
    """Join everyone's history on ship+date+cabin to show who shared each room."""
    crccl.log(f"\n{BLUE}=== Who shared the room (matched across accounts) ==={RESET}")
    if absent_note:
        crccl.log(f"{YELLOW}{absent_note}{RESET}")
    rooms: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    occupants: Dict[Tuple[str, str, str], List[str]] = defaultdict(list)
    for label, sailings in histories:
        for s in sailings:
            key = room_key(s)
            rooms.setdefault(key, s)
            occupants[key].append(label)

    for key in sorted(rooms):
        s = rooms[key]
        ship = s.get("shipName") or key[1]
        who = ", ".join(occupants[key])
        alone = len(occupants[key]) == 1
        color = "" if alone else GREEN
        crccl.log(f"  {pretty_date(key[0])}  {ship:<26} cabin {key[2]}: {color}{who}{RESET}")
    crccl.log("\n(Only people whose logins are in accountInfo can be matched; the loyalty")
    crccl.log(" ledger itself does not record other guests in the cabin.)")


##################################
# Upcoming bookings (roommates come straight from the API)
##################################
def fetch_bookings(account, idx: int) -> List[Dict[str, Any]]:
    brand_code = "R" if account.is_royal else "C"
    url = f"https://aws-prd.api.rccl.com/v1/profileBookings/enriched/{account.access.id}"
    data = api_get(account, url, {"brand": brand_code, "includeCheckin": "true"})
    if not data:
        return []
    return ((data or {}).get("payload") or {}).get("profileBookings") or []


def show_bookings(bookings: List[Dict[str, Any]], ships: Dict[str, str]) -> None:
    crccl.log(f"\n{BLUE}=== Bookings on profile (roommates per stateroom) ==={RESET}")
    if not bookings:
        crccl.log("No bookings returned (this endpoint only exposes upcoming sailings).")
        return

    for b in sorted(bookings, key=lambda x: x.get("sailDate") or ""):
        sail = b.get("sailDate") or "?"
        tag = {"in_progress": f"{BLUE}[sailing now]{RESET}", "ended": f"{YELLOW}[past]{RESET}",
               "upcoming": f"{GREEN}[upcoming]{RESET}"}.get(sailing_status(b), f"{YELLOW}[?]{RESET}")
        ship = ships.get(b.get("shipCode"), b.get("shipCode") or "?")
        room = b.get("stateroomNumber") or "GTY"
        nights = b.get("numberOfNights")
        nights_txt = f"  {nights} nights" if nights else ""
        crccl.log(f"\n{tag} {pretty_date(sail)}  {ship}{nights_txt}  "
                  f"reservation {b.get('bookingId') or '?'}  room {room}")
        guests = b.get("passengersInStateroom") or []
        if not guests:
            crccl.log("    (no guest list in this booking record)")
        for g in guests:
            born = str(g.get("birthdate") or "")
            born_txt = f"  (b. {born[:4]})" if len(born) >= 4 else ""
            crccl.log(f"    {guest_name(g)}{born_txt}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cruise history + roommates report")
    parser.add_argument("-c", "--config", default="config.yaml",
                        help="Path to configuration YAML file (default: config.yaml)")
    parser.add_argument("--double-points", default="", metavar="ID,ID",
                        help="Comma-separated booking IDs that qualify for the original C&A "
                             "double-points promo: (base + suite + solo) x2 "
                             "(booked Jul 21-31 2026, sailing Sep 2026 - Apr 2027, "
                             "non-casino, non-TA/TP, max 2/member)")
    parser.add_argument("--pending-points", type=int, default=0, metavar="N",
                        help="Points earned but not yet posted to Crown & Anchor (e.g. a "
                             "cruise that just ended). Added to the current balance for "
                             "tier progress and projections, shown as an explicit "
                             "'+N pending' adjustment. Applies to every account in the "
                             "config, so best used with a single-account config file")
    parser.add_argument("--new-double-points", default="", metavar="ID,ID",
                        help="Comma-separated booking IDs on the NEWER double-points promo, "
                             "which doubles only base + suite and pays the solo point single: "
                             "(base + suite) x2 + solo. Solo earns 3/night, suite solo 5/night")
    parser.add_argument("--sailed", action="append", default=[], metavar="SHIP:YYYYMMDD:NIGHTS[:GUESTS][:suite]",
                        help="A cruise that already ended but is not in the loyalty ledger yet and "
                             "was never seen by this script (e.g. AN:20260920:7:solo or "
                             "SR:20260913:7:2:suite). Remembered for the first account in the "
                             "config and estimated until Crown & Anchor posts it. Repeatable, or "
                             "comma-separated")
    args = parser.parse_args()
    sailed_specs = [s.strip() for chunk in args.sailed for s in chunk.split(",") if s.strip()]
    promo_ids = frozenset(i.strip() for i in args.double_points.split(",") if i.strip())
    new_promo_ids = frozenset(i.strip() for i in args.new_double_points.split(",") if i.strip())
    both = promo_ids & new_promo_ids
    if both:
        print(f"Booking(s) {', '.join(sorted(both))} given to BOTH promo flags; "
              f"using the new-promo math for them.", file=sys.stderr)

    accounts, skipped, history_db, sailed_file = load_accounts(args.config)
    try:
        _run_report(accounts, skipped, promo_ids, new_promo_ids, history_db,
                    pending_points=args.pending_points, sailed_file=sailed_file,
                    sailed_specs=sailed_specs)
    finally:
        for account, _loyalty, _points in accounts:
            account.access.session.close()


def _run_report(accounts: List[Any], skipped: List[str], promo_ids: frozenset,
                new_promo_ids: frozenset = frozenset(),
                history_db: Optional[str] = None,
                pending_points: int = 0,
                sailed_file: str = SAILED_FILE_DEFAULT,
                sailed_specs: Optional[List[str]] = None) -> None:
    store = load_sailed_file(sailed_file)
    for err in add_manual_sailed(store, accounts[0][0].username, sailed_specs or []):
        crccl.log(f"{YELLOW}--sailed ignored - {err}{RESET}")
    registry = crccl.ShipRegistry()
    try:
        crccl.get_ship_dictionary_web(registry)
    except SystemExit:
        pass
    SHIP_NAMES.update({code: ship.name for code, ship in registry.ships.items()})

    # Fetch every account's history first, so the crystal-block holder (the
    # household's highest-POINT member by their own earned history, not the
    # shared relationship points the profile reports) is known before display.
    fetched = []
    for idx, (account, loyalty, points) in enumerate(accounts):
        lifetime, sailings = fetch_history(account, loyalty, idx)
        fetched.append((account, points, lifetime, sailings))

    hist_pts = [sum(sail_ints(s)[1] for s in f[3]) for f in fetched]
    block_idx = max(range(len(fetched)),
                    key=lambda i: (hist_pts[i], fetched[i][1] or 0))
    labels = unique_account_labels([f[0] for f in fetched])
    block_holder = labels[block_idx]

    histories: List[Tuple[str, List[Dict[str, Any]]]] = []
    per_account: List[Tuple[Any, int, List[Dict[str, Any]]]] = []
    for idx, (account, points, lifetime, sailings) in enumerate(fetched):
        label = labels[idx]
        crccl.log(f"\n{BLUE}=== Cruise history: {label} ==={RESET}")
        if lifetime:
            crccl.log(f"Lifetime: {lifetime.get('totalTrips', '?')} cruises, "
                      f"{lifetime.get('totalNights', '?')} nights")
        if sailings:
            crccl.log(f"\n{len(sailings)} sailings on record:")
            show_sailings(sailings, earns_blocks=(idx == block_idx))
            show_b2b(sailings)
            histories.append((label, sailings))
        else:
            crccl.log("No past sailings returned.")
        per_account.append((account, points, sailings))

    # Disclose anyone missing from the joins; with fewer than two usable
    # histories, say why the household sections are absent instead of silence.
    no_history = [labels[i]
                  for i, (acc, _pts, sails) in enumerate(per_account) if not sails]
    absent = missing_note(skipped, no_history)
    if len(histories) > 1:
        show_shared_rooms(histories, absent)
        show_household(histories, absent)
    elif len(per_account) + len(skipped) > 1:
        crccl.log(f"\n{YELLOW}Household views skipped: only {len(histories)} account(s) "
                  f"with history. {absent}{RESET}")

    # Upcoming bookings + roommates (per-booking data, first account's view)
    bookings = fetch_bookings(accounts[0][0], 0)
    show_bookings(bookings, SHIP_NAMES)

    # Final summary: yearly table, projected earnings from booked cruises, tier progress
    for idx, (account, points, sailings) in enumerate(per_account):
        crccl.log(f"\n{BLUE}=== Summary: {labels[idx]} ==={RESET}")
        own_bookings = bookings if idx == 0 else fetch_bookings(account, idx)
        holder = get_holder_name(account)
        # Manual --double-points wins; otherwise auto-detect from the amend pages
        acct_promo = promo_ids or probe_promo(account, own_bookings, holder)
        posted = frozenset((s.get("shipCode"), s.get("sailingDate")) for s in sailings)
        remember_bookings(store, account.username, own_bookings, holder)
        remembered = sailed_unposted(store, account.username, posted,
                                     {str(b.get("bookingId") or "") for b in own_bookings})
        upcoming = upcoming_earnings(own_bookings + remembered, holder, acct_promo, new_promo_ids, posted)
        ledger_sum = sum(sail_ints(s)[1] for s in sailings)
        # points C&A has credited to the balance ahead of the ledger line
        credited = max(0, points - ledger_sum) if points else 0
        upcoming, unexplained = reconcile_credited(upcoming, credited)
        eff_points = (points or ledger_sum) + pending_points
        earns_blocks = idx == block_idx
        # Every sailed-but-unposted cruise the projection estimates (still on the
        # profile, remembered, or entered with --sailed), plus the price
        # checker's historyDb view for any it never saw
        pending = [{"sail_date": sail, "ship": SHIP_NAMES.get(b.get("shipCode"), b.get("shipCode") or "?"),
                    "ended": datetime.strptime(sail, "%Y%m%d").date() + timedelta(days=int(b.get("numberOfNights") or 0)),
                    "est_points": pts, "source": b.get("_remembered"), "credited": b.get("_credited")}
                   for sail, b, pts, _w in upcoming if sailing_status(b) == "ended"]
        projected = {(b.get("shipCode"), sail) for sail, b, _p, _w in upcoming}
        pending += [p for p in pending_ledger_sailings(history_db, account.username, sailings)
                    if (p.get("ship_code"), p["sail_date"]) not in projected]
        show_pending_points(sorted(pending, key=lambda p: p["sail_date"]))
        show_upcoming_earnings(upcoming, SHIP_NAMES, holder,
                               start_points=eff_points, earns_blocks=earns_blocks,
                               promo_ids=acct_promo, new_promo_ids=new_promo_ids)
        show_tier_progress(account, points, sailings, upcoming, pending=pending_points,
                           earns_blocks=earns_blocks, block_holder=block_holder)
        if credited:
            crccl.log(f"  {YELLOW}{credited} pts are in your balance but not itemized in the loyalty "
                      f"ledger yet{' - the sailing(s) above' if credited > unexplained else ''}; the "
                      f"ledger usually catches up within days{RESET}")
        if sailings or upcoming or pending_points or credited:
            show_yearly(sailings, upcoming, pending_points, credited)
    save_sailed_file(sailed_file, store)
    crccl.log("")


if __name__ == "__main__":
    main()
