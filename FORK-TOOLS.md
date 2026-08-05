# Fork-Only Tools

Extra standalone scripts that live on this fork (`tecmage/CheckRoyalCaribbeanPrice`,
branch `fork-tools`) alongside the main price checker. Each reuses the main
script's login / logging / API plumbing where it needs an account, and all of them
need `curl_cffi` (same as the main script) to get past the cruise line's edge servers.

- [FindBackToBackCabins.py](#findbacktobackcabinspy) – find cabins you can keep across
  consecutive sailings (no account needed)
- [CheckRoyalCaribbeanUpgrades.py](#checkroyalcaribbeanupgradespy) – what upgrading each
  booked cruise would cost, casino-rate aware (uses your account)
- [CheckRoyalCaribbeanCasinoOffers.py](#checkroyalcaribbeancasinoofferspy) – track Club
  Royale casino offers and their reserve-by deadlines (uses your account)
- [CheckRoyalCaribbeanCruiseHistory.py](#checkroyalcaribbeancruisehistorypy) – past
  cruise history, roommate matching across accounts, C&A tier progress (uses your account)
- [CheckRoyalCaribbeanGui.py](#checkroyalcaribbeanguipy) – desktop GUI that wraps all of the scripts:
  config tabs per account, live colored output, HTML reports

---

## FindBackToBackCabins.py

Finds available **back-to-back (and back-to-back-to-back...) cabins**: runs of
consecutive sailings on a ship where the same physical cabin stays open across every
leg, so you never have to move. Also works as a plain open-cabin lister for a single
sailing. Everything comes from the public booking funnel – no login required.

```
python FindBackToBackCabins.py                                    # fully interactive
python FindBackToBackCabins.py --ship ovation --category 4d --side port
python FindBackToBackCabins.py --ship EG --type veranda --saildate 2027-01-02
python FindBackToBackCabins.py --ship voyager --type interior,balcony --after 1/1/2027 --before 3/31/2027
```

What it reports:

- **Same-cabin spans** (longest first): cabins open across N consecutive sailings, with
  the summed all-in price for the whole chain
- **Switch-cabin option** when moving cabins would let you stay back-to-back longer
- **Single-sailing mode** (give one date): every open cabin grouped by deck, with the
  category price

Filters and tags:

| Option | Meaning |
| --- | --- |
| `--ship` | code (`OV`) or any part of the name (`ovation`, `celebrity edge`); brand auto-detected |
| `--type` | comma-separated classes: `interior`/`oceanview`/`balcony`/`suite` (or API codes); `all` = every type |
| `--category` | specific category code (`4D`); `all` skips the filter |
| `--side` | `port` / `starboard` / `any`. Sides are derived from measured deck-plan geometry: Royal ships split sides by room number (per-ship split points built in; Voyager/Freedom classes are numbered mirror-image and handled), Celebrity by odd/even (odd = port). `--flip-sides` inverts if a ship reads reversed |
| `--decks` | e.g. `8,9,10`; `all` skips the filter |
| `--saildate` | one date = list that sailing's open cabins instead of hunting chains |
| `--after` / `--before` | date window for the chain hunt (any common date format) |
| `--hide-avoid`, `--hump-only` | Quantum-class extras: a deck-guide quality tag (`[recommended]`/`[caution]`/`[avoid]`) and the hump cabins (bigger balconies at the elevator banks, `[hump]`) |
| `--connecting-permitted` | connecting staterooms are excluded by default; this re-includes them (tagged `[connecting]`) |
| `--adults` / `--children`, `--min-legs`, `--limit` | occupancy, minimum chain length, output cap |
| `--brand`, `--sub` | force `R`/`C` when not giving `--ship` (auto-detected otherwise); raw subtype code for power users (usually use `--category`) |

All values are case-insensitive. Prices are the tax-inclusive party total at the public
rate (current promos included, no loyalty/qualifier discounts). Guarantee categories
(line picks your room) are always excluded – you can't hold a specific cabin with one.

---

## CheckRoyalCaribbeanUpgrades.py

For every booking on your account, shows **what it would cost to move to a better
stateroom** on the same sailing. Reads the booking's real pricing ledger from the
amend page, then prices every category currently for sale – with your loyalty number
and the booking's guest count – and shows two deltas per candidate:

- **dl-paid** – candidate's current all-in total minus what you pay today
  (what a straight repricing would owe)
- **dl-rate** – candidate minus your booked category's *current* price
  (the category-difference math an upgrade/casino desk uses)

```
python CheckRoyalCaribbeanUpgrades.py -c config.yaml
python CheckRoyalCaribbeanUpgrades.py -c config.yaml --reservation 1234567
python CheckRoyalCaribbeanUpgrades.py -c config.yaml --alert-below 100
```

Casino-rate awareness: bookings made on Club Royale rates (comps / GOBO) are detected
from the ledger's discount itemization and flagged. On those, a straight web reprice
(dl-paid) would forfeit the comp – the casino desk generally bills ~the category
difference, so **dl-rate is the number to use there** (confirm with the desk).

Fare-deposit awareness: the fare's deposit type is read from the ledger and shown
per booking. NRD bookings get Royal's published rules inline: category changes (up
or down) on the same ship/sail date carry no change fee and keep the deposit;
reprices must stay on a non-refundable fare (the quoted prices are NRD rates);
ship/sail-date changes cost $100/person; cancelling forfeits the deposit.
Refundable bookings are warned that the quoted prices are NRD rates - matching one
may mean a one-way switch to NRD. Casino-rate bookings are labeled separately:
Club Royale's own terms govern their deposits and changes (changes/cancellations
can forfeit the offer; canceling within 7 days or no-showing carries a
$200/stateroom charge and can suspend future offers).

Apprise alerts: `--alert-below N` (or `upgradeAlertBelow: N` in `config.yaml`, handy
for cron) sends one notification per run listing every upgrade – a higher class, or a
pricier category within your class – whose category-difference cost is at or below N.
Uses the same `apprise:` URLs as the main script. Without a threshold set the script
is display-only.

Notes: uses the first `accountInfo` entry in your config. Sailings with no inventory
for sale (sold out / too close to departure) are reported as such rather than priced.
`--limit N` caps how many candidate categories are listed per booking (0 = all).
Each booking's header shows `Reservation #id (name)` using `reservationFriendlyNames`
from your config, same as the main price checker.

---

## CheckRoyalCaribbeanCasinoOffers.py

Tracks the account's active **Club Royale casino offers** (the bookable offer codes
like `26XXX###`) and alerts when a reserve-by deadline is approaching.

```
python CheckRoyalCaribbeanCasinoOffers.py -c config.yaml --warn-days 14
```

- Lists every active offer: code, type, reserve-by date, perks (FreePlay etc.)
- **COMP** offers (second guest discounted/comped) are highlighted – generally more
  valuable than **GOBO** ("Get One, Buy One": second guest pays the going rate).
  The API's description text doesn't reliably distinguish them; the offer type code does
- Offers within `--warn-days` of their reserve-by date are flagged and, if `apprise:`
  is configured, sent as a notification

Uses the first `accountInfo` entry in your config; stateless (no history file).

---

## CheckRoyalCaribbeanCruiseHistory.py

Reports **past cruise history + who shared each stateroom**, plus Crown & Anchor
progress. For every account in `accountInfo` it pulls the per-sailing loyalty ledger
and upcoming bookings, then:

```
python CheckRoyalCaribbeanCruiseHistory.py -c config.yaml
python CheckRoyalCaribbeanCruiseHistory.py -c config.yaml --double-points 123456,789012
```

- Each person's past sailings: date, ship, nights, cabin, itinerary, points earned
- Roommate matching on past sailings by joining multiple accounts' histories on
  ship + sail date + cabin (the ledger only records the account holder)
- Upcoming bookings with the roommates the API lists per stateroom
- C&A points projection for booked cruises (suite/solo multipliers, crystal-block and
  Diamond-Plus milestone math), with `--double-points` to mark bookings made during a
  double-points promo (the API has no booking date, so you supply the IDs)

Output is console-only (plus `logFile` if configured); nothing is written to disk.
A 5-second cooldown is applied between account logins (same as the main script), and
accounts that fail login or have no sailings are called out in the household/shared-room
sections rather than silently shrinking them.

---

## CheckRoyalCaribbeanGui.py

A **desktop GUI (Tkinter, no extra dependencies)** that wraps all of the scripts in
one dark-themed window:

```
python CheckRoyalCaribbeanGui.py
```

- **Bottom tabs, one per config file** (e.g. `config.yaml.alice`, `config.yaml.bob`):
  the `+` tab adds one, right-click removes or opens it in your editor. Each tab keeps
  its own output pane and its own saved form values for the account-based scripts;
  ship-search settings are shared. Tabs show ▶ / ✓ / ✖ while running / after a run.
- **Script picker + options form** generated per script (tooltips on every field).
  Back-to-Back and the Cruise Planner Browser get live dropdowns: the fleet list
  (brand-filtered, API placeholder ships removed) and, after picking a ship, its
  actual sailing dates.
- **Run / Refresh, Run All Tabs** (each config sequentially), **Stop**, repeat-every-N-hours
  timer, and per-run **HTML reports** (Export button, or auto-export to `reports/`).
- Output pane: ANSI colors rendered, smart autoscroll, Ctrl+F search, font zoom
  (Ctrl +/−/wheel), right-click copy/export menu.
- Scripts run as child processes, so the GUI never interferes with CLI/cron/Docker
  usage. Settings persist in `gui_settings.json` (gitignored, next to the script/exe).

**Windows exe**: `pyinstaller CheckRoyalCaribbeanGui.spec` (windowed; bundles the six scripts and
runs them via the exe's internal `--run-script` dispatch). Because the scripts are bundled at
build time, the exe keeps running its built-in copies until you rebuild it - run the `.py`
directly if you want script changes picked up immediately. Unit tests for its helpers
live in `test_gui_helpers.py`.
