# CheckRoyalCaribbeanPrice
Checks if you have the cheapest price for your **Royal Caribbean** and **Celebrity Cruises** purchases (beverage packages, excursions, internet, etc.).  Not affiliated with Royal Caribbean Group in any way. 
- ✅ Automatically checks your purchased packages (no need to enter them manually)  
- ✅ Alerts you if a lower price is available (email, ntfy, Home Assistant, etc) 
- ✅ Finds deals specific to each passenger (loyalty or casino status, age-based or room specials) where other "royal price trackers" only find publicly available (often higher) prices
- ✅ Shows currently assigned cabin in Royal's backend system (*likely* the room you will get if purchased a GTY "We choose your room")
- ✅ Shows the payment balance Royal's backend system thinks they are owed (does not include TA's take!) and estimated final payment date
- ✅ Supports multiple Royal and Celebrity accounts or linked cruises
- ✅ Handles all currencies (checks each item based on the currency used to purchase it)
- ✅ Can automatically check **cabin prices** for any cruise you booked, using loyalty/senior/resident discounts
- ✅ Can create a "watchlist" to check prices of items you have not purchased (thanks @jhedlund)  
- ✅ Can also watchlist **cabin prices** with just a booking URL (no login required, supports discounts)  
- ✅ Can display active sitewide promotions (flash sales, percentage-off deals) for each sailing
- ✅ Runs on Windows, macOS, Linux, Docker, iOS, Android, and Home Assistant. Also a vibe-coded Android App and Greasemonkey script
- ✅ Completely open source, free to use or modify.
- ✅ Separate `BrowseRoyalCaribbeanPrice.py` script lets you look up any cruise's addon prices, cabin prices, onboard activity schedule, MDR menus, and dress codes. No setup/account required! Runs on Windows, macOS, Linux, Docker, iOS, Android. Also made a vibe-coded Android App and a Web-based version.
   

> ⚠️ This is **not a hack**. All API calls and data are publicly available. The script simply automates what you can do on the Royal Caribbean website.

If the code saved you money or correctly predicted your cabin number, please star the repo or post on your favorite cruise board.

### Installation
- [Install (Windows 10/11 Option) - Python Not Required!](docs/install-windows.md)
- [Install (MacOS Option) - Python Not Required!](docs/install-macos.md)
- [Install (Python Source Code, Recommended Option, any Operating System)](docs/install-python.md)
- [Install (Greasemonkey script, runs in your browser, Python Not Required)](docs/install-greasemonkey.md)
- [Install (iOS / iPhone)](docs/install-ios.md)
- [Install (Android)](docs/install-android.md)
- [Install (Home Assistant Addon/App Option)](docs/install-homeassistant.md)
- [Install (Docker Option - thanks @JDare)](docs/install-docker.md)

### Advanced Configuration & Usage
- [Edit Config File](docs/config.md)
- [Run](docs/run.md)
- [Output](docs/output.md)
- [Get Cruise URL for Watchlist Functionality (Optional - This is only for a cruise you have not booked!)](docs/watchlist-cruise-url.md)
- [Watch List for Beverage Packages/Excursions/etc (Optional)](docs/watchlist-addons.md)
- [Notification Emails/Pushbullet/etc via Apprise (Optional)](docs/apprise.md)
- [Automating](docs/automating.md)

### BrowseRoyalCaribbeanPrice
A separate tool in this repo that browses **any** Royal Caribbean or Celebrity sailing and displays current public prices for every excursion, drink package, and dining package, plus the cheapest cabin in each category, onboard activity schedule (trivia, game shows, theme nights), MDR menus, and dress codes. No account, config file, or setup required.
- [Browse RoyalCaribbean Prices](docs/browse.md)

### Reference
- [Other Notes - Find GTY Early and Other Tips](docs/other-notes.md)
- [Related Tools](docs/related-tools.md)
- [Issues](docs/issues.md)

### Fork-Only Tools

This fork carries extra standalone scripts – see [FORK-TOOLS.md](FORK-TOOLS.md):

- `FindBackToBackCabins.py` – find cabins you can keep across consecutive sailings (back-to-back), or list a sailing's open cabins with prices; port/starboard, deck, category, hump and quality filters. No account needed.
- `CheckRoyalCaribbeanUpgrades.py` – what upgrading each booked cruise would cost (two deltas: vs what you paid, and vs your category's current rate), with Club Royale casino-rate detection and optional Apprise alerts.
- `CheckRoyalCaribbeanCasinoOffers.py` – track Club Royale casino offers and their reserve-by deadlines.
- `CheckRoyalCaribbeanCruiseHistory.py` – past cruise history with roommate matching across household accounts, plus Crown & Anchor points/tier projections.
- `CheckRoyalCaribbeanGui.py` – a desktop GUI wrapping all of the above (and the price checker/browser): one tab per config file, live colored output, ship/sailing dropdowns, HTML reports, run-all and repeat timers. `pyinstaller CheckRoyalCaribbeanGui.spec` builds a windowed exe.
### Credits

Thanks to contributors:
- Anonymous (Retrieve AccountID programmatically)
- @cyntil8 (Celebrity support, per-day pricing, various bug fixes)  
- @tecmage (UDP, Coffee Card, Evian Water logic)  
- @iareanthony (fixed "The Key")  
- @jipis (internet pricing & passenger specials)  
- @ProxesOnBoxes (date display options, config improvements)
- @JDare (Docker support and documentation, github workflow)
- @jhedlund (Watchlist)
- @chblan (fix for iPhone script)
- @AESternberg (Formatting and updates to below Browse script)
- @RoyalCaribbeanBlog.com for featuring in an [article](https://www.royalcaribbeanblog.com/2025/04/19/cruise-price-trackers)
- Frommers.com for featuring in an [article](https://www.frommers.com/tips/cruise/how-to-save-hundreds-on-royal-caribbeans-packages-and-excursions/)
