"""
Walrus Flip Finder v1.2 - desktop app (pywebview)
Made by Walrus Dev.

Shows the most profitable Hypixel SkyBlock Bazaar flips: buy with a buy order,
sell with a sell offer, keep the difference (minus the Bazaar tax).
Prices come from Hypixel's public Bazaar API (no API key needed).

Watchlist + price alerts: star an item, set a target, and you get a Windows
notification when its sell price goes above (or below) that target.

v1.2 adds:
  - Price history saved to SQLite every 5 minutes (kept for 7 days) + price charts
  - System tray: closing the window can hide it to the tray so alerts keep working
  - Settings tab with your own Bazaar tax

Run:
    pip install -r requirements.txt
    python app.py
    (winotify = Windows notifications, pystray + pillow = tray icon. All optional.)

Build the exe:
    pip install pyinstaller
    pyinstaller "Walrus Flip Finder.spec"
"""

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.request
import webview

try:   # optional - real Windows notifications
    from winotify import Notification, audio
    TOAST_OK = True
except ImportError:
    TOAST_OK = False

try:   # optional - system tray icon
    import pystray
    from PIL import Image, ImageDraw
    TRAY_OK = True
except ImportError:
    TRAY_OK = False

API_URL = "https://api.hypixel.net/v2/skyblock/bazaar"
DEFAULT_TAX = 1.25        # Bazaar tax on sell offers in % (lower with Community Shop upgrades)
REFRESH_SECONDS = 60      # Hypixel only updates the Bazaar about once a minute anyway
HISTORY_EVERY = 5 * 60    # save prices to the database every 5 minutes
CLEANUP_EVERY = 60 * 60   # delete old history once an hour
KEEP_DAYS = 7
BUCKET_MS = 30 * 60 * 1000   # the 7-day chart averages prices into 30-minute buckets

# When PyInstaller builds a one-file exe, bundled files are unpacked to a temp
# folder (sys._MEIPASS), so we look there first, otherwise next to this script.
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

# Saved data lives in AppData so it survives rebuilding the exe.
DATA_DIR = os.path.join(os.getenv("APPDATA") or os.path.expanduser("~"), "WalrusFlipFinder")
WATCH_FILE = os.path.join(DATA_DIR, "watchlist.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
DB_FILE = os.path.join(DATA_DIR, "history.db")
OLD_FAVS_FILE = os.path.join(DATA_DIR, "favourites.json")   # from v1, moved into the watchlist


def nice_name(item_id):
    """'ENCHANTED_DIAMOND' -> 'Enchanted Diamond', 'ENCHANTMENT_SHARPNESS_5' -> 'Sharpness 5 (book)'."""
    book = item_id.startswith("ENCHANTMENT_")
    if book:
        item_id = item_id[len("ENCHANTMENT_"):]
    name = item_id.replace("_", " ").title()
    return f"{name} (book)" if book else name


def fmt(n):
    """1234567 -> '1.23M' (same as fmt() in app.js, used for notification text)."""
    for size, letter in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= size:
            return f"{n / size:.2f}{letter}"
    return f"{n:.1f}"


def fetch_bazaar():
    """Download every Bazaar product in one request. Returns (products, last_updated_ms)."""
    req = urllib.request.Request(API_URL, headers={"User-Agent": "WalrusFlipFinder/1.2"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    if not data.get("success"):
        raise RuntimeError(data.get("cause", "Hypixel said the request failed"))
    return data["products"], data.get("lastUpdated")


def buy_sell(product):
    """Returns (buy, sell) for one product, or None if nobody is trading it.
    Hypixel's buy/sell naming is easy to mix up, so don't trust the names:
    the LOWER price is where buy orders sit, the HIGHER price is where sell offers sit."""
    q = product.get("quick_status") or {}
    a, b = q.get("buyPrice", 0), q.get("sellPrice", 0)
    if a <= 0 or b <= 0:
        return None
    return min(a, b), max(a, b)


def build_flips(products, tax_pct):
    """Turn Hypixel's product data into one row per item with profit worked out."""
    tax = tax_pct / 100
    rows = []
    for item_id, product in products.items():
        prices = buy_sell(product)
        if not prices:
            continue
        buy, sell = prices
        q = product["quick_status"]
        profit = sell * (1 - tax) - buy
        # Weekly volume = the smaller side, because a flip needs both
        # your buy order AND your sell offer to fill.
        weekly = min(q.get("buyMovingWeek", 0), q.get("sellMovingWeek", 0))
        rows.append({
            "id": item_id,
            "name": nice_name(item_id),
            "buy": round(buy, 1),
            "sell": round(sell, 1),
            "profit": round(profit, 1),
            "margin": round(profit / buy * 100, 2),
            "weekly": int(weekly),
        })
    return rows


def show_toast(title, message):
    """Pop up a Windows notification. Returns False if winotify isn't installed or it fails."""
    if not TOAST_OK:
        return False
    try:
        toast = Notification(app_id="Walrus Flip Finder", title=title, msg=message, duration="short")
        toast.set_audio(audio.Default, loop=False)
        toast.show()
        return True
    except Exception as e:
        print("Notification failed:", e)
        return False


# ---------- price history database ----------

def open_db():
    """Open (and if needed create) the history database.
    Each thread must open its OWN connection - sqlite3 connections can't be shared between threads."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    # WAL mode lets the chart read the database while the background thread is writing to it.
    conn.execute("PRAGMA journal_mode=WAL")
    # (product_id, ts) as the primary key keeps each item's prices sorted by time on disk,
    # and WITHOUT ROWID stops SQLite storing a second hidden copy of that key.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            product_id TEXT    NOT NULL,
            ts         INTEGER NOT NULL,   -- time in milliseconds (what JavaScript uses)
            buy        REAL    NOT NULL,
            sell       REAL    NOT NULL,
            PRIMARY KEY (product_id, ts)
        ) WITHOUT ROWID
    """)
    return conn


def db_size_mb():
    """History file size, including the WAL file SQLite keeps next to it."""
    total = 0
    for path in (DB_FILE, DB_FILE + "-wal"):
        try:
            total += os.path.getsize(path)
        except OSError:
            pass
    return round(total / (1024 * 1024), 1)


class Api:
    """Every public method here can be called from JavaScript as window.pywebview.api.<name>().
    Anything starting with _ stays hidden from JavaScript."""

    def __init__(self):
        # The background thread and the page both touch the data below at the
        # same time, so a lock stops them reading half-updated data.
        self._lock = threading.Lock()
        self._products = {}        # raw Hypixel data, kept so rows can be rebuilt when the tax changes
        self._rows = []
        self._by_id = {}
        self._updated = None
        self._error = None
        self._rev = 0              # goes up whenever rows change, so the page knows to redraw
        self._alerts = []          # recent alerts, shown inside the app too
        self._watch = self._load_watch()
        self._settings = self._load_settings()

        self.window = None         # set in main, used by the tray
        self.tray_running = False  # True once the tray icon has started
        self.quitting = False      # True when Quit is picked from the tray menu

        # Make sure the database and table exist before the page can ask for a chart.
        try:
            open_db().close()
            self._history_ok = True
        except Exception as e:
            print("Price history disabled:", e)
            self._history_ok = False

        # Fetch prices + check alerts + save history in the background,
        # so it all keeps working when the window is minimised or in the tray.
        threading.Thread(target=self._poll_loop, daemon=True).start()

    # ---------- prices ----------
    def _poll_loop(self):
        db = None
        if self._history_ok:
            try:
                db = open_db()        # this thread's own connection
                self._cleanup_history(db)
            except Exception as e:
                print("Couldn't open price history:", e)
                db = None
        last_save = 0
        last_cleanup = time.time()

        while True:
            ok = self._update()
            now = time.time()
            if db and ok and now - last_save >= HISTORY_EVERY:
                self._save_history(db)
                last_save = now
            if db and now - last_cleanup >= CLEANUP_EVERY:
                self._cleanup_history(db)
                last_cleanup = now
            time.sleep(REFRESH_SECONDS)

    def _update(self):
        """Download new prices. Returns True if it worked."""
        try:
            products, updated = fetch_bazaar()
        except Exception as e:   # no internet, Hypixel down, etc. - keep the old prices
            with self._lock:
                self._error = str(e)
            return False
        with self._lock:
            self._products = products
            self._updated = updated
            self._error = None
            self._rebuild_rows()
        self._check_alerts()
        return True

    def _rebuild_rows(self):
        # Called while already holding the lock.
        self._rows = build_flips(self._products, self._settings["tax"])
        self._by_id = {r["id"]: r for r in self._rows}
        self._rev += 1

    def get_flips(self):
        """Latest prices (the page asks for these every few seconds - it's just a copy, no download)."""
        with self._lock:
            return {
                "rows": self._rows,
                "rev": self._rev,
                "tax": self._settings["tax"],
                "updated": self._updated,
                "error": self._error,
                "alerts": self._alerts[-20:],
                "toasts": TOAST_OK,
            }

    def refresh_now(self):
        self._update()
        return self.get_flips()

    # ---------- price history ----------
    def _save_history(self, db):
        """Save the current buy/sell price of every item in one transaction."""
        ts = int(time.time() * 1000)
        with self._lock:   # only copy the data inside the lock - the disk write happens after
            rows = []
            for item_id, product in self._products.items():
                prices = buy_sell(product)
                if prices:
                    rows.append((item_id, ts, round(prices[0], 1), round(prices[1], 1)))
        try:
            with db:   # "with" makes this one transaction: commits at the end, or rolls back on error
                db.executemany("INSERT OR REPLACE INTO prices VALUES (?, ?, ?, ?)", rows)
        except Exception as e:
            print("Couldn't save price history:", e)

    def _cleanup_history(self, db):
        cutoff = int((time.time() - KEEP_DAYS * 86400) * 1000)
        try:
            with db:
                db.execute("DELETE FROM prices WHERE ts < ?", (cutoff,))
        except Exception as e:
            print("Couldn't clean up price history:", e)

    def get_history(self, item_id, hours):
        """Price history for one item: {"points": [[time_ms, buy, sell], ...]}.
        24 hours = every 5-minute save. Longer = averaged into 30-minute buckets so the chart isn't too busy."""
        if not self._history_ok:
            return {"error": "the history database couldn't be opened"}
        try:
            hours = max(1, min(int(hours), KEEP_DAYS * 24))
            since = int((time.time() - hours * 3600) * 1000)
            # Short-lived connection: this runs on pywebview's thread, not the background thread.
            conn = open_db()
            try:
                if hours <= 24:
                    cur = conn.execute(
                        "SELECT ts, buy, sell FROM prices WHERE product_id = ? AND ts >= ? ORDER BY ts",
                        (item_id, since))
                else:
                    # ts / BUCKET_MS * BUCKET_MS rounds each time down to the start of its
                    # 30-minute bucket (integer division), then we average each bucket.
                    cur = conn.execute(
                        """SELECT (ts / ?) * ? AS bucket, AVG(buy), AVG(sell)
                           FROM prices WHERE product_id = ? AND ts >= ?
                           GROUP BY bucket ORDER BY bucket""",
                        (BUCKET_MS, BUCKET_MS, item_id, since))
                points = [[int(t), round(b, 1), round(s, 1)] for t, b, s in cur.fetchall()]
            finally:
                conn.close()
            return {"points": points}
        except Exception as e:
            return {"error": str(e)}

    # ---------- settings ----------
    def get_settings(self):
        with self._lock:
            s = dict(self._settings)
        return {
            "tax": s["tax"],
            "default_tax": DEFAULT_TAX,
            "close_to_tray": s["close_to_tray"],
            "tray_available": self.tray_running,
            "history_ok": self._history_ok,
            "history_every_min": HISTORY_EVERY // 60,
            "keep_days": KEEP_DAYS,
            "history_mb": db_size_mb(),
        }

    def set_settings(self, tax, close_to_tray):
        try:
            tax = float(tax)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Tax must be a number"}
        # tax != tax is only True for NaN (Not a Number), which float() can return
        if tax != tax or not 0 <= tax <= 10:
            return {"ok": False, "error": "Tax must be between 0 and 10%"}
        with self._lock:
            tax_changed = tax != self._settings["tax"]
            self._settings = {"tax": round(tax, 3), "close_to_tray": bool(close_to_tray)}
            if tax_changed:
                self._rebuild_rows()   # profits change straight away, no new download needed
            settings = dict(self._settings)
        self._save_settings(settings)   # disk write after the lock is released
        return {"ok": True}

    def _load_settings(self):
        settings = {"tax": DEFAULT_TAX, "close_to_tray": False}
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            tax = float(saved.get("tax", DEFAULT_TAX))
            if 0 <= tax <= 10:
                settings["tax"] = tax
            settings["close_to_tray"] = bool(saved.get("close_to_tray", False))
        except (OSError, ValueError, TypeError, AttributeError):
            pass   # no settings yet (or a broken file) - use the defaults
        return settings

    def _save_settings(self, settings):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
        except OSError as e:
            print("Couldn't save settings:", e)

    def _wants_tray(self):
        with self._lock:
            return self._settings["close_to_tray"]

    # ---------- watchlist ----------
    def get_watchlist(self):
        with self._lock:
            return dict(self._watch)

    def toggle_watch(self, item_id):
        """Add or remove an item. Returns True if it's now on the watchlist."""
        with self._lock:
            if item_id in self._watch:
                del self._watch[item_id]
            else:
                self._watch[item_id] = {"target": None, "direction": "above", "triggered": False}
            self._save_watch()
            return item_id in self._watch

    def set_alert(self, item_id, target, direction):
        """target = coins (or None to turn the alert off), direction = 'above' or 'below'."""
        with self._lock:
            if item_id not in self._watch:
                return False
            self._watch[item_id] = {
                "target": float(target) if target else None,
                "direction": "below" if direction == "below" else "above",
                "triggered": False,   # new target, so it can fire again
            }
            self._save_watch()
        self._check_alerts()   # fire straight away if the price is already past it
        return True

    def test_notification(self):
        return show_toast("Walrus Flip Finder", "Notifications are working! 🐋")

    def _check_alerts(self):
        to_send = []
        with self._lock:
            changed = False
            for item_id, w in self._watch.items():
                row = self._by_id.get(item_id)
                if not row or not w.get("target"):
                    continue
                price = row["sell"]
                if w["direction"] == "above":
                    hit = price >= w["target"]
                else:
                    hit = price <= w["target"]
                # Only notify when the price CROSSES the target, not every minute
                # it stays there. When it moves back, the alert re-arms itself.
                if hit and not w["triggered"]:
                    w["triggered"] = True
                    changed = True
                    word = "above" if w["direction"] == "above" else "below"
                    msg = f"{row['name']} is {fmt(price)} ({word} your {fmt(w['target'])} target)"
                    to_send.append(msg)
                    self._alerts.append({"time": time.time() * 1000, "text": msg})
                elif not hit and w["triggered"]:
                    w["triggered"] = False
                    changed = True
            if changed:
                self._save_watch()
        # Notifications are sent outside the lock because showing one takes a moment.
        for msg in to_send:
            show_toast("Price alert 🔔", msg)

    def _load_watch(self):
        try:
            with open(WATCH_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
        # First run after v1: move v1 favourites into the watchlist.
        try:
            with open(OLD_FAVS_FILE, encoding="utf-8") as f:
                favs = json.load(f)
            return {i: {"target": None, "direction": "above", "triggered": False} for i in favs}
        except (OSError, ValueError):
            return {}

    def _save_watch(self):
        # Called while already holding the lock.
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(WATCH_FILE, "w", encoding="utf-8") as f:
            json.dump(self._watch, f, indent=2)


# ---------- system tray ----------

def make_tray_image():
    """Draw a simple 64x64 tray icon with Pillow (a teal circle with a W)."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(56, 189, 248, 255))
    # A "W" drawn as one zig-zag line
    d.line([(14, 20), (23, 46), (32, 28), (41, 46), (50, 20)], fill=(15, 23, 42, 255), width=6, joint="curve")
    return img


def start_tray(api):
    """Start the tray icon on its own thread. Returns the icon, or None if the tray isn't available."""
    if not TRAY_OK:
        return None

    def on_open(icon, item):
        api.window.show()
        api.window.restore()

    def on_quit(icon, item):
        api.quitting = True    # so the closing handler lets the window really close
        icon.stop()
        api.window.destroy()

    try:
        icon = pystray.Icon(
            "WalrusFlipFinder",
            make_tray_image(),
            "Walrus Flip Finder",
            # default=True = this is what happens when you double-click the icon
            menu=pystray.Menu(pystray.MenuItem("Open", on_open, default=True),
                              pystray.MenuItem("Quit", on_quit)),
        )
        icon.run_detached()   # runs alongside the pywebview window instead of blocking
        api.tray_running = True
        return icon
    except Exception as e:
        print("Tray icon failed:", e)
        return None


if __name__ == "__main__":
    api = Api()
    window = webview.create_window(
        "Walrus Flip Finder",
        url=os.path.join(BASE, "web", "index.html"),
        js_api=api,
        width=1150,
        height=760,
        min_size=(820, 520),
    )
    api.window = window
    tray = start_tray(api)

    def on_closing():
        # Returning False cancels the close. If the tray setting is on,
        # hide the window instead so prices and alerts keep running.
        if api.tray_running and api._wants_tray() and not api.quitting:
            window.hide()
            return False
        return True

    window.events.closing += on_closing
    webview.start()

    # The window is gone - remove the tray icon too (if Quit didn't already).
    if tray and not api.quitting:
        tray.stop()
