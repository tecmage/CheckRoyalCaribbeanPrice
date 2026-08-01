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
import re
import sys

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import yaml

import CheckRoyalCaribbeanPrice as crccl
from CheckRoyalCaribbeanPrice import GREEN, YELLOW, BLUE, RESET

# shipCode -> friendly name, populated in main() once the fleet API is queried
SHIP_NAMES: Dict[str, str] = {}


def load_accounts(config_path: str) -> List[Any]:
    with open(config_path) as f:
        data = crccl.expand_env_vars(yaml.safe_load(f)) or {}
    crccl.setup_hybrid_logging(data.get("logFile"))

    entries = data.get("accountInfo") or []
    if not entries:
        print("No accountInfo in config.", file=sys.stderr)
        sys.exit(1)

    accounts = []
    for a in entries:
        account = crccl.AccountInfo(username=a["username"], password=a["password"],
                                    cruise_line=a.get("cruiseLine", "royalcaribbean"))
        # login/get_profile call sys.exit on failure; one bad account must not
        # kill the whole multi-account run
        try:
            account.access = crccl.login(account)
            _state, loyalty, points = crccl.get_profile(account)
        except SystemExit:
            crccl.log(f"{YELLOW}skipping {a.get('username')}: login failed{RESET}")
            continue
        accounts.append((account, loyalty, points))
    if not accounts:
        print("No accounts could log in.", file=sys.stderr)
        sys.exit(1)
    return accounts


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


def account_label(account, idx: int) -> str:
    """Display label for section headers. Headers are persisted to logFile, so
    mask the login email: local part + first letter of the domain (jo@g…)."""
    name = account.username or ""
    if "@" in name:
        local, domain = name.split("@", 1)
        name = f"{local}@{domain[:1]}…"
    return name or f"account {idx + 1}"


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


def show_household(histories: List[Tuple[str, List[Dict[str, Any]]]]) -> None:
    """Merged view across everyone's histories: together vs apart, per year."""
    crccl.log(f"\n{BLUE}=== Household combined view ==={RESET}")
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
                upcoming: List[Tuple[str, Dict[str, Any], int, str]]) -> None:
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

    crccl.log("  year       cruises  nights  points")
    for year in sorted(years):
        cruises, nights, pts = years[year]
        crccl.log(f"  {year}       {cruises:>7}  {nights:>6}  {pts:>6}")
    for year in sorted(est):
        cruises, nights, pts = est[year]
        crccl.log(f"  {year} est  {'+' + str(cruises):>7}  {'+' + str(nights):>6}  "
                  f"{'+' + str(pts):>6}  (booked)")

    total = [sum(v[i] for v in years.values()) for i in range(3)]
    crccl.log(f"  total      {total[0]:>7}  {total[1]:>6}  {total[2]:>6}")
    est_total = [sum(v[i] for v in est.values()) for i in range(3)]
    if est_total[0]:
        crccl.log(f"  w/ booked  {total[0] + est_total[0]:>7}  {total[1] + est_total[1]:>6}  "
                  f"{total[2] + est_total[2]:>6}")
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
                      promo_ids: frozenset = frozenset()
                      ) -> List[Tuple[str, Dict[str, Any], int, str]]:
    """Project C&A points from booked future cruises: (sailDate, booking, pts, why)."""
    today = date.today().strftime("%Y%m%d")
    rows = []
    for b in bookings:
        sail = b.get("sailDate") or ""
        if not sail or sail < today:
            continue
        guests = b.get("passengersInStateroom") or []
        names = [guest_name(g).upper() for g in guests]
        if holder_name and holder_name not in names:
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
        if str(b.get("bookingId") or "") in promo_ids:
            if PROMO_SAIL_START <= sail <= PROMO_SAIL_END:
                pts *= 2
                desc = f"{nights}n x{rate}x2 ({why} + double-points promo)"
            else:
                desc += (f"  {YELLOW}[--double-points ignored: sails outside the "
                         f"Sep 2026 - Apr 2027 promo window]{RESET}")
        rows.append((sail, b, pts, desc))
    return sorted(rows, key=lambda r: r[0])


def show_upcoming_earnings(rows: List[Tuple[str, Dict[str, Any], int, str]],
                           ships: Dict[str, str], holder_name: Optional[str],
                           start_points: int = 0, earns_blocks: bool = True,
                           promo_ids: frozenset = frozenset()) -> int:
    who = f" for {holder_name}" if holder_name else ""
    crccl.log(f"\n{BLUE}Projected points from booked cruises{who}:{RESET}")
    if not holder_name:
        crccl.log(f"  {YELLOW}(couldn't read the account holder's name - linked bookings "
                  f"for other people's rooms may be counted below){RESET}")
    if not rows:
        crccl.log("  (no upcoming bookings found)")
        return 0
    total = 0
    cum = start_points
    for sail, b, pts, why in rows:
        ship = ships.get(b.get("shipCode"), b.get("shipCode") or "?")
        before, cum = cum, cum + pts
        notes = []
        if earns_blocks:
            notes += [f"crystal block #{block_number(t)} at {t}" for t in blocks_crossed(before, cum)]
        notes += [f"reaches {name}" for name, needed in CA_TIERS if before < needed <= cum]
        notes += [perk for m, perk in DP_MILESTONES.items() if before < m <= cum]
        note_txt = f"  {GREEN}[{'; '.join(notes)}]{RESET}" if notes else ""
        crccl.log(f"  {pretty_date(sail)}  {ship:<26} room {b.get('stateroomNumber') or 'GTY'}  "
                  f"{why} = {pts} pts{note_txt}")
        total += pts
    crccl.log(f"  total: +{total} pts")

    doubled = [r for r in rows if str(r[1].get("bookingId") or "") in promo_ids
               and PROMO_SAIL_START <= r[0] <= PROMO_SAIL_END]
    if len(doubled) > PROMO_MAX_CRUISES:
        crccl.log(f"  {YELLOW}Warning: {len(doubled)} bookings flagged --double-points, but "
                  f"the promo caps at {PROMO_MAX_CRUISES} cruises per member{RESET}")
    crccl.log("  (solo-studio rules and unregistered promos can't be known in advance)")
    return total


def show_tier_progress(account, profile_points: int, sailings: List[Dict[str, Any]],
                       upcoming: List[Tuple[str, Dict[str, Any], int, str]],
                       earns_blocks: bool = True, block_holder: str = "") -> None:
    if not account.is_royal:
        return
    points = profile_points or sum(sail_ints(s)[1] for s in sailings)
    source = "profile" if profile_points else "sum of history"
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
                return f"{GREEN}on the {pretty_date(sail)} {ship} sailing (booked){RESET}"
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


def show_shared_rooms(histories: List[Tuple[str, List[Dict[str, Any]]]]) -> None:
    """Join everyone's history on ship+date+cabin to show who shared each room."""
    crccl.log(f"\n{BLUE}=== Who shared the room (matched across accounts) ==={RESET}")
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

    today = date.today().strftime("%Y%m%d")
    for b in sorted(bookings, key=lambda x: x.get("sailDate") or ""):
        sail = b.get("sailDate") or "?"
        tag = f"{YELLOW}[past]{RESET}" if sail < today else f"{GREEN}[upcoming]{RESET}"
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
                        help="Comma-separated booking IDs that qualify for the C&A "
                             "double-points promo (booked Jul 21-31 2026, sailing "
                             "Sep 2026 - Apr 2027, non-casino, non-TA/TP, max 2/member)")
    args = parser.parse_args()
    promo_ids = frozenset(i.strip() for i in args.double_points.split(",") if i.strip())

    accounts = load_accounts(args.config)
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
    block_holder = account_label(fetched[block_idx][0], block_idx)

    histories: List[Tuple[str, List[Dict[str, Any]]]] = []
    per_account: List[Tuple[Any, int, List[Dict[str, Any]]]] = []
    for idx, (account, points, lifetime, sailings) in enumerate(fetched):
        label = account_label(account, idx)
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

    if len(histories) > 1:
        show_shared_rooms(histories)
        show_household(histories)

    # Upcoming bookings + roommates (per-booking data, first account's view)
    bookings = fetch_bookings(accounts[0][0], 0)
    show_bookings(bookings, SHIP_NAMES)

    # Final summary: yearly table, projected earnings from booked cruises, tier progress
    for idx, (account, points, sailings) in enumerate(per_account):
        crccl.log(f"\n{BLUE}=== Summary: {account_label(account, idx)} ==={RESET}")
        own_bookings = bookings if idx == 0 else fetch_bookings(account, idx)
        holder = get_holder_name(account)
        # Manual --double-points wins; otherwise auto-detect from the amend pages
        acct_promo = promo_ids or probe_promo(account, own_bookings, holder)
        upcoming = upcoming_earnings(own_bookings, holder, acct_promo)
        eff_points = points or sum(sail_ints(s)[1] for s in sailings)
        earns_blocks = idx == block_idx
        show_upcoming_earnings(upcoming, SHIP_NAMES, holder,
                               start_points=eff_points, earns_blocks=earns_blocks,
                               promo_ids=acct_promo)
        show_tier_progress(account, points, sailings, upcoming,
                           earns_blocks=earns_blocks, block_holder=block_holder)
        if sailings or upcoming:
            show_yearly(sailings, upcoming)
    crccl.log("")


if __name__ == "__main__":
    main()
