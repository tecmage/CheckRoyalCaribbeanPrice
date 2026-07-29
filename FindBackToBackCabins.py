"""
Find available back-to-back (and back-to-back-to-back...) cabins on a ship.

Given a ship + stateroom type/category, this walks the ship's future schedule,
finds runs of consecutive sailings (one leg's end date = the next leg's start
date), checks your category's availability on every leg, and ranks the results:

    1. SAME cabin open across every leg   (true no-move back-to-back)
    2. CLOSE cabins                        (same deck, near each other per leg)
    3. Category available on each leg      (you may have to switch cabins)

You can filter to a port or starboard preference (derived from cabin-number
parity - see the note printed at run time). Everything here is the public
booking funnel: no login required.

    python FindBackToBackCabins.py                 # fully interactive
    python FindBackToBackCabins.py --ship IC --type BALCONY --sub D --side port

Requires curl_cffi (as the other scripts do) to get past the edge WAF.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    from curl_cffi import requests
    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:
    import requests
    IMPERSONATE = {}
    print("WARNING: curl_cffi not installed - the cruise line edge servers may return "
          "403. Install it (the main script uses it too).", file=sys.stderr)

APPKEY_WEB = "hyNNqIPHHzaLzVpcICPdAdbFV8yvTsAm"
USER_AGENT_WEB = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) Gecko/20100101 Firefox/149.0"
API = "https://aws-prd.api.rccl.com"

RED, GREEN, YELLOW, BLUE, CYAN, MAGENTA, RESET = (
    "\033[91m", "\033[1;32m", "\033[93m", "\033[94m", "\033[96m", "\033[95m", "\033[0m")

# Windows PowerShell 5.x / classic conhost print ANSI color escapes literally unless
# virtual-terminal processing is enabled. Harmless where already on (Windows Terminal)
# or unsupported - failures are ignored. (Same fix as the main scripts.)
if sys.platform == "win32":
    try:
        import ctypes
        _kernel32 = ctypes.windll.kernel32
        _handle = _kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        _mode = ctypes.c_uint32()
        if _kernel32.GetConsoleMode(_handle, ctypes.byref(_mode)):
            # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
            _kernel32.SetConsoleMode(_handle, _mode.value | 0x0004)
    except Exception:
        pass

session = requests.Session()

# Set by --debug-pricing: dump the raw pricing fields the API returns (stderr)
DEBUG_PRICING = False


def price_str(price: Optional[float], exact: bool) -> str:
    """'$2,453.64' for a known category price, '~$...' when approximated from the
    subtype's cheapest category, '' when unknown."""
    if price is None:
        return ""
    return f"{'' if exact else '~'}${price:,.2f}"


##################################
# Low-level fetch + RSC parsing
##################################
def _get(url: str, **kwargs) -> Optional[requests.Response]:
    try:
        return session.get(url, timeout=45, **IMPERSONATE, **kwargs)
    except Exception as e:
        print(f"{RED}Request failed: {e}{RESET}")
        return None


def _extract_json_array(text: str, key: str) -> Optional[list]:
    """Bracket-count a "key": [ ... ] array out of an RSC/text stream."""
    m = re.search(rf'"{re.escape(key)}"\s*:\s*\[', text)
    if not m:
        return None
    start = m.end() - 1
    depth, i = 0, start
    in_string = escape = False
    while i < len(text):
        c = text[i]
        if escape:
            escape = False
        elif c == "\\" and in_string:
            escape = True
        elif c == '"':
            in_string = not in_string
        elif not in_string:
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        return None
        i += 1
    return None


def _dash(sail: str) -> str:
    s = str(sail)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


##################################
# Fleet + schedule
##################################
def get_ships(brand: str) -> List[Dict[str, str]]:
    r = _get(f"{API}/en/royal/web/v2/ships", params={"sort": "name"},
             headers={"User-Agent": USER_AGENT_WEB, "Accept": "application/json", "appkey": APPKEY_WEB})
    if not r or r.status_code != 200:
        return []
    ships = r.json().get("payload", {}).get("ships", [])
    return [{"code": s.get("shipCode"), "name": s.get("name")}
            for s in ships if (s.get("brand") or "R") == brand]


def get_voyages(ship_code: str) -> List[Dict[str, Any]]:
    r = _get(f"{API}/en/royal/web/v3/ships/{ship_code}/voyages",
             headers={"User-Agent": USER_AGENT_WEB, "Accept": "application/json", "appkey": APPKEY_WEB})
    if not r or r.status_code != 200:
        return []
    today = date.today().strftime("%Y%m%d")
    out = []
    for v in r.json().get("payload", {}).get("voyages", []) or []:
        if not isinstance(v, dict) or v.get("charter") or v.get("nonRevenue") or v.get("blacklist"):
            continue
        if not v.get("sailDate") or v["sailDate"] < today:
            continue
        out.append(v)
    out.sort(key=lambda v: v["sailDate"])
    return out


def build_chains(voyages: List[Dict[str, Any]], min_len: int) -> List[List[Dict[str, Any]]]:
    """Group voyages into maximal runs where each leg's end date = the next leg's start date."""
    chains, run = [], []
    for v in voyages:
        if run and run[-1].get("sailEndDate") == v.get("sailDate"):
            run.append(v)
        else:
            if len(run) >= min_len:
                chains.append(run)
            run = [v]
    if len(run) >= min_len:
        chains.append(run)
    return chains


##################################
# Availability (category + cabins)
##################################
def _occ(adults: int, children: int) -> Dict[str, str]:
    return {"r0a": str(adults), "r0c": str(children), "r0b": "n", "r0r": "n",
            "r0s": "n", "r0q": "n", "r0t": "n", "r0D": "y", "rgVisited": "true", "r0C": "y"}


def get_stateroom_types(pkg: str, sail: str, ship: str, brand: str,
                        adults: int, children: int) -> List[Dict[str, Any]]:
    """Return the ship's stateroom types + subtypes (code/name) from a sample sailing."""
    host = "royalcaribbean" if brand == "R" else "celebritycruises"
    params = {"packageCode": pkg, "sailDate": _dash(sail), "country": "USA",
              "selectedCurrencyCode": "USD", "shipCode": ship, "cabinClassType": "INTERIOR",
              "roomIndex": "0", "r0d": "INTERIOR", **_occ(adults, children)}
    r = _get(f"https://www.{host}.com/room-selection/type-and-subtype", params=params,
             headers={"user-agent": USER_AGENT_WEB, "accept": "text/x-component", "RSC": "1"})
    if not r or r.status_code != 200:
        return []
    rooms = _extract_json_array(r.text, "rooms")
    if not rooms:
        return []
    try:
        return rooms[0]["options"]["stateroomTypes"]
    except (KeyError, IndexError, TypeError):
        return []


def discover_types(ship: str, brand: str, voyages: List[Dict[str, Any]],
                   adults: int, children: int) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Find a sailing whose type/subtype list loads (near-term ones can be sold out/empty)."""
    n = len(voyages)
    seen, order = set(), []
    for frac in (0.5, 0.33, 0.66, 0.25, 0.75, 0.1, 0.0, 0.9):
        idx = min(n - 1, int(n * frac))
        if idx not in seen:
            seen.add(idx)
            order.append(idx)
    for idx in order:
        v = voyages[idx]
        types = get_stateroom_types(ship + v["voyageCode"], v["sailDate"], ship, brand, adults, children)
        if types:
            return types, v
    return [], None


def category_rooms_left(pkg: str, sail: str, ship: str, brand: str, subtype: str,
                        adults: int, children: int) -> Optional[int]:
    """roomsLeft for one subtype on a sailing (None = not returned / unknown, 0 = sold out)."""
    types = get_stateroom_types(pkg, sail, ship, brand, adults, children)
    for t in types:
        for s in t.get("stateroomSubtypes", []):
            if s.get("code") == subtype:
                return s.get("roomsLeft")
    return None


def get_subtype_decks(pkg: str, sail: str, ship: str, brand: str, stype: str, subtype: str,
                      adults: int, children: int) -> List[str]:
    """Deck codes (e.g. '07') that have availability for a subtype on a sailing."""
    host = "royalcaribbean" if brand == "R" else "celebritycruises"
    rl_params = {"packageCode": pkg, "sailDate": _dash(sail), "country": "USA",
                 "selectedCurrencyCode": "USD", "shipCode": ship, "roomIndex": "0",
                 "r0d": stype, "r0e": subtype, "r0f": subtype, **_occ(adults, children)}
    rl = _get(f"https://www.{host}.com/room-selection/room-location", params=rl_params,
              headers={"user-agent": USER_AGENT_WEB, "accept": "text/x-component", "RSC": "1"})
    if not rl or rl.status_code != 200:
        return []
    decks = _extract_json_array(rl.text, "decks") or []
    # NOTE: do NOT filter on roomsLeft - it is often None ("available, count unknown"),
    # not 0. The per-deck /api/v1/rooms call is the real source of truth for cabins.
    return [d.get("code") for d in decks if d.get("code")]


def get_open_cabins(pkg: str, sail: str, ship: str, brand: str, stype: str, subtype: str,
                    adults: int, children: int,
                    only_decks: Optional[set] = None) -> List[Dict[str, Any]]:
    """Full open-cabin list for a subtype: loop decks via /room-selection/api/v1/rooms."""
    host = "royalcaribbean" if brand == "R" else "celebritycruises"
    deck_codes = get_subtype_decks(pkg, sail, ship, brand, stype, subtype, adults, children)
    if only_decks:
        deck_codes = [d for d in deck_codes if d in only_decks]

    # per-deck cabin enumeration (JSON API)
    hdr = {"user-agent": USER_AGENT_WEB, "accept": "*/*", "content-type": "application/json",
           "brand": brand, "country": "USA"}
    cabins = []
    for dc in deck_codes:
        flt = {"countryCode": "USA", "packageId": pkg, "sailDate": _dash(sail),
               "currencyCode": "USD", "language": "en", "options": True, "roomNumbers": True,
               "rooms": [{"adultCount": adults, "childCount": children,
                          "stateroomTypeCode": stype, "stateroomSubtypeCode": subtype,
                          "accessible": False, "selectionFallbackStrategy": "RECOMMENDATION",
                          "editMode": True, "reset": False, "taxesAndFeesBundled": True,
                          "room": {"deckCode": dc}}],
               "platform": "web"}
        r = _get(f"https://www.{host}.com/room-selection/api/v1/rooms",
                 params={"filter": json.dumps(flt)}, headers=hdr)
        if not r or r.status_code != 200:
            continue
        try:
            data = r.json()
        except Exception:
            continue
        # Prices are set per CATEGORY (every cabin in a category costs the same). Each
        # subtype entry in options.stateroomTypes carries its lead-in category's
        # tax-inclusive party total at pricing.invoice.total - collect those as a
        # category->price map, then look for a price directly on each roomNumbers
        # category record as well (field name probed defensively).
        lead_prices: Dict[str, float] = {}
        sub_lead_price: Optional[float] = None
        for st in (((data.get("rooms") or [{}])[0].get("options") or {}).get("stateroomTypes") or []):
            for s in st.get("stateroomSubtypes", []) or []:
                total = ((s.get("pricing") or {}).get("invoice") or {}).get("total")
                if isinstance(total, (int, float)):
                    if s.get("categoryCode"):
                        lead_prices[s["categoryCode"]] = float(total)
                    if s.get("code") == subtype:
                        sub_lead_price = float(total)

        def _cat_price(cat_record: Dict[str, Any]) -> Optional[float]:
            for path in (("pricing", "invoice", "total"), ("pricing", "total"),
                         ("invoice", "total"), ("price",), ("total",), ("startingPrice",)):
                o: Any = cat_record
                for k in path:
                    o = o.get(k) if isinstance(o, dict) else None
                if isinstance(o, (int, float)):
                    return float(o)
            return None

        for room in data.get("rooms", []) or []:
            rn = room.get("roomNumbers", {}) or {}
            for cat in rn.get("categories", []) or []:
                catcode = cat.get("categoryCode") or cat.get("code")
                price = _cat_price(cat)
                exact = price is not None
                if price is None and catcode in lead_prices:
                    price, exact = lead_prices[catcode], True   # lead-in category's own price
                if price is None:
                    price, exact = sub_lead_price, False        # approx: subtype's cheapest
                if DEBUG_PRICING and cat.get("cabins"):
                    slim = {k: v for k, v in cat.items() if k != "cabins"}
                    print(f"[debug-pricing] deck {dc} category record: {json.dumps(slim)[:400]}",
                          file=sys.stderr)
                    print(f"[debug-pricing] lead_prices={lead_prices} sub_lead={sub_lead_price}",
                          file=sys.stderr)
                for cab in cat.get("cabins", []) or []:
                    num = cab.get("cabinNumber")
                    if num:
                        cabins.append({"cabin": str(num), "deck": dc,
                                       "position": cab.get("positionCode"), "category": catcode,
                                       "price": price, "price_exact": exact})
    return cabins


##################################
# Port / starboard + ranking
##################################
# Room number below this (within a deck) is port on ships that number both sides even
# (Quantum-class): lower rooms = port, higher = starboard.
PORT_STARBOARD_SPLIT = 500


def side_of(cabin: str, flip: bool, by_number: bool = False) -> str:
    """Port/starboard from the room number: parity (odd=port) on mixed-parity ships, or
    low/high room number (lower=port) on ships numbered even on both sides (Quantum-class)."""
    if by_number:
        port = int(str(cabin)[-3:]) < PORT_STARBOARD_SPLIT
    else:
        port = int(str(cabin)[-1]) % 2 == 1
    if flip:
        port = not port
    return "port" if port else "starboard"


def filter_side(cabins: List[Dict[str, Any]], side: Optional[str], flip: bool,
                by_number: bool = False) -> List[Dict[str, Any]]:
    if not side:
        return cabins
    return [c for c in cabins if side_of(c["cabin"], flip, by_number) == side]


# Quantum-class ship codes (Quantum, Anthem, Ovation, Odyssey, Spectrum).
QUANTUM_CLASS = {"QN", "AN", "OV", "OY", "SC"}

# Balcony-cabin quality by deck and fore/aft zone, transcribed from the cruiseadmiral
# Quantum/Anthem balcony guide (approximate - edit to taste). Ratings: good/ok/avoid.
# Zones map to the API's positionCode: FW=forward, MS=mid-ship, AF=aft.
QUANTUM_QUALITY: Dict[int, Dict[str, str]] = {
    13: {"AF": "avoid", "MS": "avoid", "FW": "ok"},     # under pool deck / SeaPlex / running track
    12: {"AF": "good",  "MS": "ok",    "FW": "good"},
    11: {"AF": "good",  "MS": "good",  "FW": "good"},    # best
    10: {"AF": "good",  "MS": "good",  "FW": "good"},    # best
    9:  {"AF": "good",  "MS": "good",  "FW": "good"},
    8:  {"AF": "good",  "MS": "ok",    "FW": "good"},
    7:  {"AF": "avoid", "MS": "avoid", "FW": "good"},    # mostly red; forward ok
    6:  {"AF": "ok",    "MS": "avoid", "FW": "ok"},      # lifeboat obstructions
}
QUALITY_TAG = {"good": f"{GREEN}[recommended]{RESET}",
               "ok": f"{YELLOW}[caution]{RESET}",
               "avoid": f"{RED}[avoid]{RESET}"}


def cabin_quality(ship: str, deck: str, position: Optional[str]) -> Optional[str]:
    """Deck-guide quality (good/ok/avoid) for a Quantum-class cabin, or None if not covered."""
    if ship.upper() not in QUANTUM_CLASS:
        return None
    try:
        return QUANTUM_QUALITY.get(int(deck), {}).get((position or "").upper())
    except (ValueError, TypeError):
        return None


# "Hump" cabins jut out at the fore & aft elevator banks and have larger balconies. The
# room-number ranges are consistent across every deck (derived from the Ovation deck-plan
# PDF by finding cabins clustered at the ELEV markers); port and starboard each have a
# forward and a mid hump. Same numbering scheme should hold across Quantum-class.
QUANTUM_HUMP_RANGES = [(143, 185), (224, 262),     # port: forward hump, mid hump
                       (543, 585), (625, 661)]     # starboard: forward hump, mid hump


def is_hump(ship: str, cabin: str) -> bool:
    if ship.upper() not in QUANTUM_CLASS:
        return False
    try:
        n = int(str(cabin)[-3:])
    except ValueError:
        return False
    return any(lo <= n <= hi for lo, hi in QUANTUM_HUMP_RANGES)


def closest_on_deck(leg_lists: List[List[int]]) -> Optional[Tuple[int, List[int]]]:
    """Given one leg's cabins-on-a-deck per leg, find the pick with the smallest spread."""
    if not all(leg_lists):
        return None
    best = None
    for anchor in leg_lists[0]:
        pick = [anchor] + [min(lst, key=lambda x: abs(x - anchor)) for lst in leg_lists[1:]]
        spread = max(pick) - min(pick)
        if best is None or spread < best[0]:
            best = (spread, pick)
    return best


def _maximal_spans(present: List[bool], min_legs: int) -> List[Tuple[int, int]]:
    """Index ranges (inclusive) where `present` is True for >= min_legs in a row."""
    spans, i, n = [], 0, len(present)
    while i < n:
        if present[i]:
            j = i
            while j + 1 < n and present[j + 1]:
                j += 1
            if j - i + 1 >= min_legs:
                spans.append((i, j))
            i = j + 1
        else:
            i += 1
    return spans


def same_cabin_spans(leg_cabins: List[List[Dict[str, Any]]], min_legs: int
                     ) -> List[Tuple[str, int, int]]:
    """(cabin, start_leg, end_leg) runs where ONE cabin stays open across consecutive legs."""
    leg_sets = [set(c["cabin"] for c in leg) for leg in leg_cabins]
    out = []
    for cabin in set().union(*leg_sets) if leg_sets else set():
        for i, j in _maximal_spans([cabin in s for s in leg_sets], min_legs):
            out.append((cabin, i, j))
    out.sort(key=lambda s: (-(s[2] - s[1] + 1), int(s[0])))
    return out


def deck_close_spans(leg_cabins: List[List[Dict[str, Any]]], min_legs: int
                     ) -> List[Tuple[str, int, int, List[int], int]]:
    """(deck, start, end, nearest-cabin-per-leg, spread) runs where a deck stays open (move cabins)."""
    decks = sorted(set(c["deck"] for leg in leg_cabins for c in leg))
    out = []
    for dc in decks:
        present = [any(c["deck"] == dc for c in leg) for leg in leg_cabins]
        for i, j in _maximal_spans(present, min_legs):
            leg_lists = [sorted(int(c["cabin"]) for c in leg_cabins[k] if c["deck"] == dc)
                         for k in range(i, j + 1)]
            res = closest_on_deck(leg_lists)
            if res:
                out.append((dc, i, j, res[1], res[0]))
    out.sort(key=lambda s: (-(s[2] - s[1] + 1), s[4]))
    return out


##################################
# Prompt helpers
##################################
def parse_decks(raw: Optional[str]) -> Optional[set]:
    """'7,8,10' -> {'07','08','10'} matching the API's zero-padded deck codes. Blank -> None."""
    if not raw:
        return None
    out = {tok.zfill(2) for tok in raw.replace(" ", "").split(",") if tok.isdigit()}
    return out or None


def choose(prompt: str, options: List[Tuple[str, str]]) -> Optional[str]:
    """options: list of (value, label). Returns chosen value, or None to quit."""
    for i, (_, label) in enumerate(options):
        print(f"  {BLUE}{i}{RESET}) {label}")
    print(f"  {BLUE}q{RESET}) Quit")
    try:
        raw = input(f"{prompt}: ").strip().lower()
    except EOFError:
        return None
    if raw == "q" or not raw.isdigit() or not (0 <= int(raw) < len(options)):
        return None
    return options[int(raw)][0]


##################################
# Main
##################################
def main() -> None:
    ap = argparse.ArgumentParser(description="Find back-to-back cabin availability")
    ap.add_argument("--brand", default="R", help="R=Royal, C=Celebrity")
    ap.add_argument("--ship", help="Two-letter ship code (skip the ship prompt)")
    ap.add_argument("--type", dest="stype", help="Stateroom type code, e.g. BALCONY")
    ap.add_argument("--sub", dest="subtype", help="Subtype code, e.g. D (power users; usually use --category)")
    ap.add_argument("--category", help="Category code to match, e.g. 4D")
    ap.add_argument("--side", choices=["port", "starboard"], help="Side preference")
    ap.add_argument("--decks", help="Comma-separated preferred deck numbers, e.g. 7,8,9 (blank = any)")
    ap.add_argument("--flip-sides", action="store_true",
                    help="Flip the odd/even -> port/starboard mapping for this ship")
    ap.add_argument("--adults", type=int, default=2)
    ap.add_argument("--children", type=int, default=0)
    ap.add_argument("--min-legs", type=int, default=2, help="Minimum consecutive sailings (default 2)")
    ap.add_argument("--limit", type=int, default=0, help="Max cabins to list per result (0 = all)")
    ap.add_argument("--hide-avoid", action="store_true",
                    help="Quantum-class: drop cabins the deck guide rates 'avoid'")
    ap.add_argument("--hump-only", action="store_true",
                    help="Quantum-class: only the hump cabins (bigger balconies, by the elevators)")
    ap.add_argument("--connecting-permitted", action="store_true",
                    help="Include connecting staterooms (excluded by default; tagged [connecting])")
    ap.add_argument("--after", help="Only sailings on/after this date (YYYY-MM-DD)")
    ap.add_argument("--before", help="Only sailings on/before this date (YYYY-MM-DD)")
    ap.add_argument("--saildate", help="A single sailing date (YYYY-MM-DD): list its open cabins "
                                       "instead of hunting back-to-backs")
    ap.add_argument("--debug-pricing", action="store_true",
                    help="Dump the raw category pricing fields the API returns (stderr)")
    args = ap.parse_args()

    global DEBUG_PRICING
    DEBUG_PRICING = args.debug_pricing

    def _norm(d: Optional[str]) -> Optional[str]:
        if not d:
            return None
        s = d.replace("-", "").replace("/", "")
        return s if len(s) == 8 and s.isdigit() else None

    after, before = _norm(args.after), _norm(args.before)

    # --- Ship ---
    ship = args.ship
    if not ship:
        ships = get_ships(args.brand)
        if not ships:
            print(f"{RED}Could not load ships.{RESET}")
            return
        print(f"\n{CYAN}Select a ship:{RESET}")
        ship = choose("Ship", [(s["code"], f"{s['name']} ({s['code']})") for s in ships])
        if not ship:
            return

    voyages = get_voyages(ship)
    if not voyages:
        print(f"{RED}No future voyages found for {ship}.{RESET}")
        return

    # Optional date window (interactive prompt if not given as args). A single date -
    # either --saildate or one date with no comma at the prompt - selects that one
    # sailing and switches to a plain open-cabin listing instead of the B2B hunt.
    saildate = _norm(args.saildate)
    if saildate is None and after is None and before is None and sys.stdin.isatty():
        raw = input(f"\n{CYAN}Limit to a date range?{RESET} start,end as YYYY-MM-DD, or ONE "
                    f"date to list a single sailing's open cabins "
                    f"(blank = all {len(voyages)} sailings): ").strip()
        if raw:
            parts = [p.strip() for p in raw.split(",")]
            if len(parts) == 1:
                saildate = _norm(parts[0])
            else:
                after = _norm(parts[0]) or None
                before = _norm(parts[1]) if len(parts) > 1 else None
    if saildate:
        after = before = saildate
    if after:
        voyages = [v for v in voyages if v["sailDate"] >= after]
    if before:
        voyages = [v for v in voyages if v["sailDate"] <= before]

    if saildate and not voyages:
        print(f"{RED}No {ship} sailing departs on {_dash(saildate)}.{RESET}")
        return
    if not voyages:
        print(f"{RED}No sailings in that date range.{RESET}")
        return

    single_mode = len(voyages) == 1
    if single_mode:
        v = voyages[0]
        chains = [[v]]
        print(f"\n{ship}: single sailing {_dash(v['sailDate'])} ({v['duration']}n) "
              f"{v.get('voyageDescription','')} - listing open cabins.")
    else:
        chains = build_chains(voyages, args.min_legs)
        print(f"\n{ship}: {len(voyages)} sailings in range, "
              f"{len(chains)} back-to-back chain(s) of >= {args.min_legs} legs "
              f"(longest {max((len(c) for c in chains), default=0)} legs).")
        if not chains:
            return

    # --- Stateroom type / subtype (probe a sailing that has inventory for this ship's codes) ---
    types, sample = discover_types(ship, args.brand, voyages, args.adults, args.children)
    if not types or sample is None:
        print(f"{RED}Could not read stateroom categories for {ship}.{RESET}")
        return

    stype = args.stype
    if not stype:
        print(f"\n{CYAN}Stateroom type:{RESET}")
        stype = choose("Type", [(t["code"], f"{t['name']} ({t['code']})") for t in types])
        if not stype:
            return

    # --- Preferred category code (e.g. 4D) - matches how cabins are usually referenced ---
    # Split the type's subtypes into pickable vs guarantee. Guarantee categories (X-prefixed,
    # guarantee=true) let the line assign your room, so you can't hold a specific cabin across
    # legs - exclude them from a back-to-back cabin search.
    type_subs = [s for t in types if t["code"] == stype for s in t["stateroomSubtypes"]]
    pickable = [(s.get("code"), s.get("categoryCode")) for s in type_subs if not s.get("guarantee")]
    guarantees = sorted({s.get("categoryCode") or s.get("code") for s in type_subs if s.get("guarantee")})

    category = (args.category or "").upper() or None
    if args.subtype is None and category is None and sys.stdin.isatty():
        leads = sorted({c for _, c in pickable if c})
        hint = f" (pickable: {', '.join(leads)}" if leads else " ("
        if guarantees:
            hint += f"; guarantees {', '.join(guarantees)} excluded - line picks the room"
        hint += ")"
        raw = input(f"\n{CYAN}Preferred category code{RESET} e.g. 4D{hint}, blank=all: ").strip().upper()
        category = raw or None

    # Which subtypes to query: an explicit --sub wins; else narrow to pickable subtypes whose
    # lead-in categoryCode shares the requested category's letters (4D -> the '...D' subtype).
    def _letters(s: str) -> str:
        return re.sub(r"[^A-Za-z]", "", s or "").upper()

    if args.subtype:
        subtypes = [args.subtype]
    elif category:
        subtypes = [code for code, cat in pickable if _letters(cat) == _letters(category)]
        if not subtypes:
            subtypes = [code for code, _ in pickable]
    else:
        subtypes = [code for code, _ in pickable]

    # Connecting staterooms are their own subtype ("Connecting ..." in the name) - works on
    # any ship. They aren't generally preferred (noise/privacy), so exclude them by default;
    # --connecting-permitted keeps them (still tagged [connecting]). An explicit category or
    # subtype request is always honored.
    connecting_codes = {s.get("code") for t in types if t["code"] == stype
                        for s in t["stateroomSubtypes"]
                        if "connect" in (s.get("name") or "").lower()}
    if not args.connecting_permitted and not (args.subtype or category):
        subtypes = [c for c in subtypes if c not in connecting_codes] or subtypes

    # --- Preferred decks ---
    deck_pref = parse_decks(args.decks)
    if deck_pref is None and sys.stdin.isatty():
        raw = input(f"\n{CYAN}Preferred decks{RESET}, comma-separated e.g. 8,9,10, blank=any: ").strip()
        deck_pref = parse_decks(raw)

    # --- Side preference ---
    side = args.side
    if side is None and sys.stdin.isatty():  # only prompt on a real terminal
        print(f"\n{CYAN}Side preference?{RESET}")
        side = choose("Side", [("", "No preference"), ("port", "Port"), ("starboard", "Starboard")]) or None

    show_side = False   # only label sides when a side filter is active
    by_number = False   # False = odd/even parity; True = low/high room number (Quantum-class)
    if side:
        # Decide which side rule fits this ship: parity works only when both parities are
        # present. Quantum-class (Ovation, Anthem, Odyssey, Quantum, Spectrum) numbers cabins
        # even on BOTH sides, so we fall back to the room-number rule (lower = port).
        probe_leg = chains[0][len(chains[0]) // 2]
        probe = []
        for sub in subtypes[:2]:
            probe += get_open_cabins(ship + probe_leg["voyageCode"], probe_leg["sailDate"], ship,
                                     args.brand, stype, sub, args.adults, args.children,
                                     only_decks=deck_pref)
        probe = [c for c in probe if category is None or (c.get("category") or "").upper() == category]
        odd = sum(1 for c in probe if int(c["cabin"][-1]) % 2 == 1)
        show_side = True
        by_number = bool(probe) and odd in (0, len(probe))
        if by_number:
            print(f"\n{YELLOW}Note:{RESET} this ship numbers cabins even on both sides, so "
                  f"port/starboard comes from the room number: "
                  f"{'lower=port, higher=starboard' if not args.flip_sides else 'lower=starboard, higher=port'} "
                  f"(split at {PORT_STARBOARD_SPLIT}). Re-run with --flip-sides if reversed.")
        else:
            print(f"\n{YELLOW}Note:{RESET} side is derived from cabin-number parity "
                  f"(odd={'port' if not args.flip_sides else 'starboard'}, "
                  f"even={'starboard' if not args.flip_sides else 'port'}). Usual Royal "
                  f"convention but can vary by ship - re-run with --flip-sides if reversed.")

    def keep_cabin(c: Dict[str, Any]) -> bool:
        if category is not None and (c.get("category") or "").upper() != category:
            return False
        if args.hide_avoid and cabin_quality(ship, c["deck"], c["position"]) == "avoid":
            return False
        if args.hump_only and not is_hump(ship, c["cabin"]):
            return False
        return True

    # --- Single-sailing mode: just list the open cabins, grouped by deck ---
    if single_mode:
        v = chains[0][0]
        found_any = False
        for sub in subtypes:
            cabs = [c for c in filter_side(
                        get_open_cabins(ship + v["voyageCode"], v["sailDate"], ship, args.brand,
                                        stype, sub, args.adults, args.children,
                                        only_decks=deck_pref),
                        side, args.flip_sides, by_number) if keep_cabin(c)]
            if not cabs:
                continue
            found_any = True
            print(f"{BLUE}{'='*70}{RESET}")
            print(f"{BLUE}{ship} {stype}/{sub}{RESET}  {_dash(v['sailDate'])}  "
                  f"{len(cabs)} open cabin(s):")
            limit = args.limit or len(cabs)
            shown = 0
            for deck in sorted({c["deck"] for c in cabs}):
                if shown >= limit:
                    break
                deck_cabs = sorted((c for c in cabs if c["deck"] == deck),
                                   key=lambda c: int(c["cabin"]))
                print(f"  Deck {deck}:")
                for c in deck_cabs:
                    if shown >= limit:
                        break
                    side_tag = f", {side_of(c['cabin'], args.flip_sides, by_number)}" if show_side else ""
                    q = cabin_quality(ship, c["deck"], c["position"])
                    tags = (f" {QUALITY_TAG[q]}" if q else "")
                    tags += f" {CYAN}[hump]{RESET}" if is_hump(ship, c["cabin"]) else ""
                    tags += f" {MAGENTA}[connecting]{RESET}" if sub in connecting_codes else ""
                    p = price_str(c.get("price"), c.get("price_exact", False))
                    p_tag = f"  {p}" if p else ""
                    print(f"    {GREEN}{c['cabin']}{RESET} (cat {c.get('category','?')}{side_tag}){p_tag}{tags}")
                    shown += 1
            if shown < len(cabs):
                print(f"  ... and {len(cabs) - shown} more (use --limit 0 to show all)")
        if not found_any:
            print(f"{YELLOW}No open cabins found for that selection.{RESET}")
        print(f"\n{GREEN}Done.{RESET}")
        return

    print(f"\nSweeping {len(chains)} consecutive chain(s) x {len(subtypes)} sub-categorie(s). "
          f"This makes many public API calls; please be patient.\n")

    def span_desc(chn: List[Dict[str, Any]], i: int, j: int) -> str:
        legs = chn[i:j + 1]
        start = _dash(legs[0]["sailDate"])
        end = _dash(legs[-1].get("sailEndDate") or legs[-1]["sailDate"])
        nights = sum(int(v.get("duration", 0)) for v in legs)
        return f"{len(legs)} sailings  {start} -> {end}  ({nights} nights)"

    found_any = False
    for chain in chains:
        for sub in subtypes:
            print(f"  ...checking {len(chain)} sailings from {_dash(chain[0]['sailDate'])} "
                  f"[{stype}/{sub}{'/'+category if category else ''}]", file=sys.stderr)
            # enumerate open cabins per leg (empty list where sold out), filtered by category
            leg_cabins = [
                [c for c in filter_side(get_open_cabins(ship + v["voyageCode"], v["sailDate"], ship,
                                                        args.brand, stype, sub, args.adults,
                                                        args.children, only_decks=deck_pref),
                                        side, args.flip_sides, by_number)
                 if keep_cabin(c)]
                for v in chain]
            cab_cat = {c["cabin"]: c.get("category") for leg in leg_cabins for c in leg}
            cab_q = {c["cabin"]: cabin_quality(ship, c["deck"], c["position"])
                     for leg in leg_cabins for c in leg}
            leg_price = [{c["cabin"]: (c.get("price"), c.get("price_exact", False)) for c in leg}
                         for leg in leg_cabins]

            def span_total(cabin: str, i: int, j: int) -> str:
                """Summed category price across the span's legs, '' if any leg unknown."""
                prices = [leg_price[k].get(cabin, (None, False)) for k in range(i, j + 1)]
                if any(p[0] is None for p in prices):
                    return ""
                total = sum(p[0] for p in prices)
                return price_str(total, all(p[1] for p in prices))

            s_spans = same_cabin_spans(leg_cabins, args.min_legs)
            c_spans = deck_close_spans(leg_cabins, args.min_legs)
            if not s_spans and not c_spans:
                continue
            found_any = True
            best_same = (s_spans[0][2] - s_spans[0][1] + 1) if s_spans else 0

            print(f"{BLUE}{'='*70}{RESET}")
            print(f"{BLUE}{ship} {stype}/{sub}{RESET}  {chain[0].get('voyageDescription','')}")
            limit = args.limit or len(s_spans)
            more = len(s_spans) - limit
            print(f"  {len(s_spans)} same-cabin option(s)"
                  + (f" (showing first {limit})" if more > 0 else "") + ":")
            for cabin, i, j in s_spans[:limit]:
                side_tag = f", {side_of(cabin, args.flip_sides, by_number)}" if show_side else ""
                q = cab_q.get(cabin)
                q_tag = f" {QUALITY_TAG[q]}" if q else ""
                hump_tag = f" {CYAN}[hump]{RESET}" if is_hump(ship, cabin) else ""
                conn_tag = f" {MAGENTA}[connecting]{RESET}" if sub in connecting_codes else ""
                total = span_total(cabin, i, j)
                total_tag = f"  {total} total" if total else ""
                print(f"  {GREEN}Same cabin {cabin}{RESET} (cat {cab_cat.get(cabin,'?')}"
                      f"{side_tag}): {span_desc(chain, i, j)}{total_tag}{q_tag}{hump_tag}{conn_tag}")
            if more > 0:
                print(f"  ... and {more} more (use --limit 0 to show all)")
            # show a switch-cabins option only if it runs LONGER than the best same-cabin span
            for dc, i, j, pick, spread in c_spans[:6]:
                if (j - i + 1) > best_same:
                    seq = " -> ".join(str(p) for p in pick)
                    print(f"  {YELLOW}Deck {dc}, switch cabins (spread {spread}){RESET}: "
                          f"{span_desc(chain, i, j)}  [{seq}]")

    if not found_any:
        print(f"{YELLOW}No back-to-back availability found for that selection.{RESET}")
    print(f"\n{GREEN}Done.{RESET}")


if __name__ == "__main__":
    main()
