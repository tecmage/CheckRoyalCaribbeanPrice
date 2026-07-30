# Fork-Only Tools

Extra standalone scripts that live on this fork (`tecmage/CheckRoyalCaribbeanPrice`,
branch `aes-all-fixes`) alongside the main price checker. Each reuses the main
script's login / logging / API plumbing where it needs an account, and all of them
need `curl_cffi` (same as the main script) to get past the cruise line's edge servers.

- [FindBackToBackCabins.py](#findbacktobackcabinspy) – find cabins you can keep across
  consecutive sailings (no account needed)
- [CheckRoyalCaribbeanUpgrades.py](#checkroyalcaribbeanupgradespy) – what upgrading each
  booked cruise would cost, casino-rate aware (uses your account)
- [CheckRoyalCaribbeanCasinoOffers.py](#checkroyalcaribbeancasinoofferspy) – track Club
  Royale casino offers and their reserve-by deadlines (uses your account)

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

Fare-deposit awareness: the fare's deposit type (refundable vs non-refundable "NRD")
is read from the ledger and shown per booking. NRD bookings get Royal's published
rules inline: category changes (up or down) on the same ship/sail date carry no
change fee and keep the deposit; reprices must stay on a non-refundable fare (the
quoted prices are NRD rates); ship/sail-date changes cost $100/person; cancelling
forfeits the deposit. Refundable bookings are warned that the quoted prices are NRD
rates - matching one may mean a one-way switch to NRD.

Apprise alerts: `--alert-below N` (or `upgradeAlertBelow: N` in `config.yaml`, handy
for cron) sends one notification per run listing every upgrade – a higher class, or a
pricier category within your class – whose category-difference cost is at or below N.
Uses the same `apprise:` URLs as the main script. Without a threshold set the script
is display-only.

Notes: uses the first `accountInfo` entry in your config. Sailings with no inventory
for sale (sold out / too close to departure) are reported as such rather than priced.

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
