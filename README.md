# Walrus Flip Finder

Desktop app that finds the most profitable Hypixel SkyBlock Bazaar flips. It runs on its own, with no Discord bot needed.

- **Folder:** `C:\Python\Python\Programs\Flip_Finder\`
- **Run:** `cd C:\Python\Python\Programs\Flip_Finder` then `python app.py`
- **Needs:** `pip install -r requirements.txt` (pywebview, plus optional winotify for Windows notifications and pystray + pillow for the tray icon; the app still works without the optional ones)
- **Build exe:** `pyinstaller "Walrus Flip Finder.spec"` (or the long form: `pyinstaller --onefile --windowed --name "Walrus Flip Finder" --add-data "web;web" --hidden-import pystray._win32 app.py`)
- **Status:** v1.2 written (history, charts, tray, custom tax). v1.1 backup in `backup_v1.1\`
- **⚠ Found 2026-11-11:** `app.py` on disk is the **v1.1** backend. It has no `get_history`, `get_settings`, `set_settings`, SQLite or tray. The `web/` files are v1.2 and call those functions, so `python app.py` gives broken charts and settings. The working v1.2 only exists in `dist\Walrus Flip Finder.exe`. Restore v1.2 `app.py` (try VS Code's Timeline panel) before pushing to GitHub.
- **GitHub prep (2026-11-11):** `README.md`, `requirements.txt`, `.gitignore` (ignores build/, dist/, backup_v1.1/) and `screenshots\flip-finder.png` added to the folder. Repo not created yet.

## Files
| File | What it does |
|---|---|
| `app.py` | Python backend. Fetches prices, works out profit, runs the watchlist and alerts, saves price history to SQLite, runs the tray icon, and opens the pywebview window |
| `web/index.html` | Page layout with the "All flips", "Watchlist" and "Settings" tabs, plus the price chart pop-up |
| `web/style.css` | Dark Walrus Dev theme |
| `web/app.js` | Filters, sorts and draws the tables; watchlist controls, in-app alert pop-ups, price charts (Chart.js from a CDN) and settings |

## How it works
- **Prices** come from Hypixel's public Bazaar API (`https://api.hypixel.net/v2/skyblock/bazaar`). No API key, every item in one request.
- **Buy/sell naming:** the lower price is taken as the buy order and the higher as the sell offer.
- **Profit** = sell × (1 − tax) − buy. Tax defaults to 1.25% and can be changed in Settings. **Weekly volume** = the smaller of buy and sell weekly volume.
- **"Best flip" banner** = highest profit × weekly volume × 1%.
- **Background thread:** fetches every 60 s; JS asks Python for its copy every 10 s. A `rev` counter tells the page when rows changed (new prices or new tax).
- **Thread safety:** a `threading.Lock` protects shared data. Disk writes happen after the lock is released.
- **Python ↔ JS:** public `Api` methods are callable as `window.pywebview.api.<name>()`; `_` names are hidden.

## Price history + charts (v1.2)
- `%APPDATA%\WalrusFlipFinder\history.db`, table `prices(product_id, ts, buy, sell)`, primary key `(product_id, ts)`, `WITHOUT ROWID`, WAL mode.
- Saved every 5 minutes (one `executemany` transaction), rows older than 7 days deleted hourly.
- The background thread has its own connection; `get_history()` opens a short-lived one (sqlite connections can't cross threads).
- `get_history(id, hours)`: 24 h = 5-minute points; 7 days = averaged into 30-minute buckets.
- Click any item name (either tab) to open the chart. Shows sell + buy lines and now/low/high/change.

## System tray (v1.2)
- pystray icon (drawn with Pillow). Closing the window hides it to the tray if "Keep running in the tray" is on in Settings. Tray menu: Open / Quit.
- `window.events.closing` handler returns False to cancel the close.

## Watchlist and alerts
- ★ to add, set above/below + target (`250k`, `1.5m`) in the Watchlist tab. Compares against the **sell offer**. Fires once per crossing.
- Saved in `%APPDATA%\WalrusFlipFinder\watchlist.json`. Settings in `settings.json` in the same folder.

## Version history
- **v1:** flip table, sorting, search, filters, favourites, best flip banner, refresh every minute
- **v1.1:** watchlist, price alerts (Windows notifications + in-app), background price thread
- **v1.2:** SQLite price history, Chart.js price charts (24 h / 7 d), system tray, Settings tab with custom Bazaar tax

## Ideas for next steps
- Bundle Chart.js locally in `web/` so charts work without the CDN
- Show a small sparkline of the last 24 h in the watchlist table
