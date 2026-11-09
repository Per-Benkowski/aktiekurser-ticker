"""
Hardware / environment constants. Everything the user changes day to day
(instruments, views, timing, portfolio) lives in settings.json and is edited
through the web interface — see settings.py.
"""
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Display ───────────────────────────────────────────────────────────────────
# "epd4in26"   – Waveshare 4.26" (800×480)  ← the panel in use
# "epd3in52"   – Waveshare 3.52" (360×240)
# "epd2in13v3" – Waveshare 2.13" (250×122)
# "preview"    – write PNG files, no hardware required (used for tests)
DISPLAY_DRIVER = os.getenv("AKTIE_DRIVER", "epd4in26")
DISPLAY_WIDTH  = 800     # only used by the preview driver
DISPLAY_HEIGHT = 480
# Waveshare e-Paper demo library (git clone https://github.com/waveshareteam/e-Paper)
WAVESHARE_LIB  = os.getenv("AKTIE_WAVESHARE_LIB",
                           os.path.expanduser("~/e-Paper/RaspberryPi_JetsonNano/python/lib"))

# ── Network ───────────────────────────────────────────────────────────────────
HTTP_TIMEOUT = 15
WEB_PORT     = int(os.getenv("AKTIE_PORT", "80"))   # falls back to 8080 if 80 is not allowed
WEB_FALLBACK_PORT = 8080

# ── Files ─────────────────────────────────────────────────────────────────────
SETTINGS_FILE = os.getenv("AKTIE_SETTINGS", os.path.join(APP_DIR, "settings.json"))
# One-time import on first start if settings.json does not exist yet.
STOCKS_SEED   = os.path.join(APP_DIR, "stocks.txt.imported-seed")
STATIC_DIR    = os.path.join(APP_DIR, "static")
