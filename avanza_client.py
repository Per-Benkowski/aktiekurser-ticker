"""
Avanza public web API — no login required.

Everything the display and the web interface need from Avanza goes through here:
quotes, charts (with a small TTL cache), instrument search and FX rates.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from functools import lru_cache

import pandas as pd
import pytz

import config
import views

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

STOCKHOLM_TZ = pytz.timezone("Europe/Stockholm")

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    "Referer": "https://www.avanza.se/",
}

_STOCK_QUOTE = "https://www.avanza.se/_api/market-guide/stock/{id}"
# /price-chart/stock/{id} serves any valid orderbook id (funds, ETFs, indices too).
_STOCK_CHART = "https://www.avanza.se/_api/price-chart/stock/{id}?timePeriod={p}"
_FUND_QUOTE  = "https://www.avanza.se/_api/fund-guide/guide/{id}"
_SEARCH_URL  = "https://www.avanza.se/_api/search/filtered-search"

# Orderbook ids of Avanza's "XXX/SEK" spot-rate instruments (price = SEK per 1 unit).
_FX_IDS = {"USD": "19000", "EUR": "18998", "NOK": "53293", "DKK": "53292",
           "GBP": "108703", "CHF": "108704", "JPY": "108702", "CAD": "108701"}

EXCHANGE_CURRENCY = {
    "SE": "SEK", "NO": "NOK", "DK": "DKK", "FI": "EUR",
    "DE": "EUR", "FR": "EUR", "NL": "EUR", "IT": "EUR", "ES": "EUR",
    "GB": "GBP", "UK": "GBP", "US": "USD", "CA": "CAD",
    "JP": "JPY", "CH": "CHF", "AU": "AUD", "PL": "PLN",
}

EXCHANGE_HOURS = {
    "SE": ("Europe/Stockholm",  dtime(9,  0), dtime(17, 30)),
    "NO": ("Europe/Oslo",       dtime(9,  0), dtime(16, 30)),
    "DK": ("Europe/Copenhagen", dtime(9,  0), dtime(17,  0)),
    "FI": ("Europe/Helsinki",   dtime(10, 0), dtime(18, 30)),
    "DE": ("Europe/Berlin",     dtime(9,  0), dtime(17, 30)),
    "FR": ("Europe/Paris",      dtime(9,  0), dtime(17, 30)),
    "GB": ("Europe/London",     dtime(8,  0), dtime(16, 30)),
    "UK": ("Europe/London",     dtime(8,  0), dtime(16, 30)),
    "US": ("America/New_York",  dtime(9, 30), dtime(16,  0)),
    "CA": ("America/Toronto",   dtime(9, 30), dtime(16,  0)),
    "JP": ("Asia/Tokyo",        dtime(9,  0), dtime(15,  0)),
    "PL": ("Europe/Warsaw",     dtime(9,  0), dtime(17,  0)),
}

CURRENCY_SYMBOL = {
    "USD": "$", "EUR": "€", "SEK": "kr", "NOK": "kr", "DKK": "kr",
    "GBP": "£", "CAD": "$", "JPY": "¥", "CHF": "Fr", "AUD": "$", "PLN": "zł",
}

_EMPTY = pd.Series(dtype="float64")     # shared, never mutated


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Quote:
    kind: str               # "stock" | "fund" | "index"
    oid: str                # Avanza orderbook id
    name: str
    price: float
    change_pct: float       # day change %
    currency: str
    country: str
    charts: dict = field(default_factory=dict)      # Avanza timePeriod -> pd.Series of closes

    # Extra fields — populated for stocks, left at defaults for funds.
    ticker_symbol:  str | None = None
    market_place:   str | None = None   # e.g. "Stockholmsbörsen"
    market_status:  str | None = None   # "OPEN" | "CLOSED" (from Avanza directly)
    change_abs:     float | None = None
    volume:         float | None = None
    vwap:           float | None = None
    day_high:       float | None = None
    day_low:        float | None = None
    prev_close:     float | None = None
    historical:     dict = field(default_factory=dict)   # Avanza key -> close at that point in time
    pe_ratio:       float | None = None
    direct_yield:   float | None = None
    market_cap:     float | None = None

    @property
    def currency_symbol(self) -> str:
        return CURRENCY_SYMBOL.get(self.currency, self.currency)

    @property
    def display_symbol(self) -> str:
        return self.ticker_symbol or self.name

    # ── chart helpers (by Avanza timePeriod) ─────────────────────────────────
    def series(self, period: str) -> pd.Series:
        return self.charts.get(period, _EMPTY)

    def change_pct_over(self, period: str) -> float | None:
        s = self.series(period)
        if len(s) < 2:
            return None
        first = float(s.iloc[0])
        return None if first == 0 else (float(s.iloc[-1]) - first) / first * 100.0

    def range_over(self, period: str) -> tuple[float, float] | None:
        s = self.series(period)
        return None if s.empty else (float(s.min()), float(s.max()))

    def reference(self, view: str) -> tuple[str, float] | None:
        """(label, price) of the dashed reference line: yesterday's close, or the price one period ago."""
        spec = views.VIEWS[view]
        if view == "day":
            return (spec["ago"], float(self.prev_close)) if self.prev_close else None
        val = self.historical.get(spec["hist"])
        if val is None:
            s = self.series(spec["period"])
            val = float(s.iloc[0]) if not s.empty else None
        return None if val is None else (spec["ago"], float(val))

    def day_change_abs(self) -> float | None:
        """Today's change per unit, derived from the percentage when Avanza gives no absolute value."""
        if self.change_abs is not None:
            return float(self.change_abs)
        if self.change_pct is None:
            return None
        prev = self.price / (1 + self.change_pct / 100.0)
        return self.price - prev

    # ── market hours ─────────────────────────────────────────────────────────
    def open_close_times(self) -> tuple[dtime, dtime]:
        tz_name, lo, lc = EXCHANGE_HOURS.get(self.country, EXCHANGE_HOURS["SE"])
        tz = pytz.timezone(tz_name)
        today = datetime.now(STOCKHOLM_TZ).date()
        open_se  = tz.localize(datetime.combine(today, lo)).astimezone(STOCKHOLM_TZ)
        close_se = tz.localize(datetime.combine(today, lc)).astimezone(STOCKHOLM_TZ)
        return open_se.time(), close_se.time()

    def market_closed(self) -> bool:
        # Prefer Avanza's own live market status when we have it.
        if self.market_status is not None:
            return self.market_status != "OPEN"
        now = datetime.now(STOCKHOLM_TZ)
        if now.weekday() >= 5 or _is_holiday(now.date(), self.country):
            return True
        lo, lc = self.open_close_times()
        return not (lo <= now.time() <= lc)

    def time_until_open(self) -> str:
        now = datetime.now(STOCKHOLM_TZ)
        lo, _ = self.open_close_times()
        nxt = now.replace(hour=lo.hour, minute=lo.minute, second=0, microsecond=0)
        if now >= nxt:
            nxt += timedelta(days=1)
        while nxt.weekday() >= 5 or _is_holiday(nxt.date(), self.country):
            nxt += timedelta(days=1)
        delta = nxt - now
        h, rem = divmod(delta.seconds, 3600)
        m = rem // 60
        if delta.days > 0:
            return f"Opens in {delta.days}d {h}h {m}m"
        return f"Opens in {h}h {m}m"


# ── Holiday calendar (from original script) ───────────────────────────────────

def _country_code(c: str) -> str:
    c = (c or "SE").upper()
    return "UK" if c == "GB" else c

def _easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19*a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    L = (32 + 2*e + 2*i - h - k) % 7
    m = (a + 11*h + 22*L) // 451
    month = (h + L - 7*m + 114) // 31
    day   = ((h + L - 7*m + 114) % 31) + 1
    return date(year, month, day)

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    if n > 0:
        d = date(year, month, 1)
        return d + timedelta(days=(weekday - d.weekday()) % 7 + 7*(n-1))
    last = (date(year+1, 1, 1) if month == 12 else date(year, month+1, 1)) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)

def _us_obs(d: date) -> date:
    if d.weekday() == 5: return d - timedelta(days=1)
    if d.weekday() == 6: return d + timedelta(days=1)
    return d

def _midsommarafton(year: int) -> date:
    for day in range(19, 26):
        d = date(year, 6, day)
        if d.weekday() == 4: return d
    return date(year, 6, 21)

@lru_cache(maxsize=64)
def _holidays(country: str, year: int) -> frozenset:
    e = _easter(year)
    gf = e - timedelta(days=2)
    em = e + timedelta(days=1)
    asc = e + timedelta(days=39)
    wm  = e + timedelta(days=50)
    mt  = e - timedelta(days=3)
    h: set = set()
    if country in ("US", "CA"):
        h |= {_us_obs(date(year,1,1)), _nth_weekday(year,1,0,3),
              _nth_weekday(year,2,0,3), gf, _nth_weekday(year,5,0,-1),
              _us_obs(date(year,6,19)), _us_obs(date(year,7,4)),
              _nth_weekday(year,9,0,1), _nth_weekday(year,11,3,4),
              _us_obs(date(year,12,25))}
    elif country == "SE":
        h |= {date(year,1,1), date(year,1,6), gf, em, date(year,5,1),
              asc, date(year,6,6), _midsommarafton(year),
              date(year,12,24), date(year,12,25), date(year,12,26), date(year,12,31)}
    elif country == "NO":
        h |= {date(year,1,1), mt, gf, em, date(year,5,1), date(year,5,17),
              asc, wm, date(year,12,24), date(year,12,25), date(year,12,26), date(year,12,31)}
    elif country in ("UK", "GB"):
        h |= {_us_obs(date(year,1,1)), gf, em, _nth_weekday(year,5,0,1),
              _nth_weekday(year,5,0,-1), _nth_weekday(year,8,0,-1),
              date(year,12,25), date(year,12,26)}
    elif country == "DE":
        h |= {date(year,1,1), gf, em, date(year,5,1),
              date(year,12,24), date(year,12,25), date(year,12,26), date(year,12,31)}
    elif country == "US":
        pass  # handled above
    return frozenset(h)

def _is_holiday(d: date, country: str) -> bool:
    return d in _holidays(_country_code(country), d.year)


# ── HTTP + cache ──────────────────────────────────────────────────────────────

def _get_json(url: str, body: dict | None = None) -> dict:
    headers = dict(_HEADERS)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _find_first(obj, keys, depth=0):
    if depth > 6: return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, (str, int, float)) and v not in ("", 0):
                return v
        for v in obj.values():
            r = _find_first(v, keys, depth+1)
            if r is not None: return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_first(v, keys, depth+1)
            if r is not None: return r
    return None


_cache: dict = {}
_cache_lock = threading.Lock()


def _cached(key, max_age: float, producer, ok=lambda v: True):
    """Return a cached value younger than `max_age` seconds, else call `producer` (raises on failure).

    Results that fail `ok` (e.g. empty charts) are returned but not stored.
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and now - hit[0] <= max_age:
        return hit[1]
    val = producer()
    if ok(val):
        with _cache_lock:
            _cache[key] = (time.monotonic(), val)
            if len(_cache) > 600:                       # keep memory bounded on the Pi
                cutoff = time.monotonic() - 3600
                for k in [k for k, (t, _) in _cache.items() if t < cutoff]:
                    del _cache[k]
    return val


# ── Quote fetching ────────────────────────────────────────────────────────────

def _fetch_stock_quote(oid: str) -> dict:
    data       = _get_json(_STOCK_QUOTE.format(id=oid))
    quote      = data.get("quote") or {}
    listing    = data.get("listing") or {}
    hist       = data.get("historicalClosingPrices") or {}
    market     = data.get("marketPlace") or {}
    key_ind    = data.get("keyIndicators") or {}

    price = (quote.get("last") or quote.get("lastPrice")
             or quote.get("buy") or quote.get("sell"))
    if price is None:
        raise RuntimeError(f"No price for stock {oid}")

    chg = quote.get("changePercent")
    prev_close = hist.get("oneDay")
    if chg is None:
        chg = ((float(price) - float(prev_close)) / float(prev_close) * 100.0) if prev_close else 0.0

    country  = _country_code(listing.get("countryCode") or data.get("countryCode") or "SE")
    currency = listing.get("currency") or data.get("currency") or EXCHANGE_CURRENCY.get(country, "SEK")

    mcap = key_ind.get("marketCapital") or {}

    return {
        "kind": "index" if data.get("type") == "INDEX" else "stock",
        "name": data.get("name") or oid,
        "price": float(price),
        "change_pct": float(chg),
        "currency": currency,
        "country": country,
        "ticker_symbol": listing.get("tickerSymbol"),
        "market_place":  listing.get("marketPlaceName"),
        "market_status": market.get("currentStatus"),
        "change_abs":    quote.get("change"),
        "volume":        quote.get("totalVolumeTraded"),
        "vwap":          quote.get("volumeWeightedAveragePrice"),
        "day_high":      quote.get("highest"),
        "day_low":       quote.get("lowest"),
        "prev_close":    prev_close,
        "historical":    hist,
        "pe_ratio":      key_ind.get("priceEarningsRatio"),
        "direct_yield":  key_ind.get("directYield"),
        "market_cap":    mcap.get("value"),
    }


def _fetch_fund_quote(oid: str) -> dict:
    data = _get_json(_FUND_QUOTE.format(id=oid))
    price = data.get("nav") or _find_first(data, ["nav", "lastNav", "navPerUnit", "last", "lastPrice", "price"])
    if price is None:
        raise RuntimeError(f"No NAV for fund {oid}")
    chg = data.get("developmentOneDay")
    if chg is None:
        chg = _find_first(data, ["changeOneDay", "changePercent"]) or 0.0
    return {
        "kind": "fund",
        "name": data.get("name") or _find_first(data, ["name"]) or oid,
        "price": float(price),
        "change_pct": float(chg),
        "currency": data.get("currency") or _find_first(data, ["currency"]) or "SEK",
        "country": "SE",
    }


def _fetch_chart_uncached(oid: str, period: str) -> pd.Series:
    data = _get_json(_STOCK_CHART.format(id=oid, p=period))
    ohlc = data.get("ohlc") or data.get("dataSerie") or data.get("data") or []
    times, closes = [], []
    for e in ohlc:
        ts = e.get("timestamp") or e.get("x")
        if ts is None: continue
        dt = datetime.fromtimestamp(ts / 1000.0, tz=pytz.UTC).astimezone(STOCKHOLM_TZ)
        close = e.get("close") if e.get("close") is not None else e.get("y")
        if close is None: continue
        times.append(dt)
        closes.append(float(close))
    if not times:
        return _EMPTY
    return pd.Series(closes, index=pd.DatetimeIndex(times)).sort_index()


_SHORT_FALLBACK = {"one_week": ("three_months", 7), "one_month": ("three_months", 30)}


def fetch_chart(oid: str, period: str, max_age: float = 900) -> pd.Series:
    """Close prices for one Avanza timePeriod. Never raises: failures give an empty series."""
    age = min(max_age, 60) if period == "today" else max_age
    try:
        return _cached(("chart", oid, period), age,
                       lambda: _fetch_chart_uncached(oid, period), ok=lambda s: len(s) > 0)
    except Exception as exc:
        if period in _SHORT_FALLBACK:
            # Funds have no 1-week / 1-month chart (HTTP 400): cut it out of the 3-month one.
            src, days = _SHORT_FALLBACK[period]
            s = fetch_chart(oid, src, max_age)
            if not s.empty:
                return s[s.index >= s.index[-1] - pd.Timedelta(days=days)]
        log.warning("Chart fetch failed (%s %s): %s", oid, period, exc)
        return _EMPTY


def fetch_quote(kind: str, oid: str, override_name: str | None = None,
                view_keys: list[str] | tuple = ("year",), max_age: float = 45) -> Quote:
    """Quote plus the charts needed for `view_keys`. Raises if the quote itself cannot be fetched."""
    fetcher = _fetch_fund_quote if kind == "fund" else _fetch_stock_quote
    q = dict(_cached(("quote", kind, oid), max_age, lambda: fetcher(oid)))
    _names[oid] = {"name": q["name"], "ticker": q.get("ticker_symbol") or ""}   # Avanza's own name
    if override_name:
        q["name"] = override_name

    charts = {}
    for period in views.periods_needed(list(view_keys)):
        if period == "today" and kind == "fund":
            continue
        charts[period] = fetch_chart(oid, period, max(max_age, 900) if period != "today" else max_age)

    return Quote(
        kind=q["kind"], oid=oid, name=q["name"],
        price=q["price"], change_pct=q["change_pct"],
        currency=q["currency"], country=q["country"], charts=charts,
        ticker_symbol=q.get("ticker_symbol"),
        market_place=q.get("market_place"),
        market_status=q.get("market_status"),
        change_abs=q.get("change_abs"),
        volume=q.get("volume"),
        vwap=q.get("vwap"),
        day_high=q.get("day_high"),
        day_low=q.get("day_low"),
        prev_close=q.get("prev_close"),
        historical=q.get("historical") or {},
        pe_ratio=q.get("pe_ratio"),
        direct_yield=q.get("direct_yield"),
        market_cap=q.get("market_cap"),
    )


# ── Names (so the web interface can show names instead of ids) ────────────────

_names: dict[str, dict] = {}


def known_names() -> dict:
    return dict(_names)


def resolve_names(items: list[tuple[str, str]]) -> dict:
    """Look up names for (kind, id) pairs not seen yet, in parallel (quotes are cached)."""
    from concurrent.futures import ThreadPoolExecutor
    todo = [(k, i) for k, i in items if i not in _names]

    def one(pair):
        kind, oid = pair
        try:
            fetch_quote(kind, oid, None, [], max_age=3600)
        except Exception as exc:
            log.warning("Name lookup failed for %s: %s", oid, exc)

    if todo:
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(one, todo))
    return known_names()


# ── FX ────────────────────────────────────────────────────────────────────────

def fx_rate(currency: str, max_age: float = 900) -> float | None:
    """SEK per 1 unit of `currency`; None when no rate can be found."""
    cur = (currency or "SEK").upper()
    if cur == "SEK":
        return 1.0
    oid = _FX_IDS.get(cur)
    try:
        if oid is None:
            hits = _search_raw(f"{cur}/SEK", types=["INDEX"])
            for h in hits:
                if str(h.get("title", "")).upper().startswith(f"{cur}/SEK"):
                    oid = str(h["orderBookId"])
                    break
        if oid is None:
            return None
        d = _cached(("fx", oid), max_age, lambda: _get_json(_STOCK_QUOTE.format(id=oid)))
        last = (d.get("quote") or {}).get("last")
        return float(last) if last else None
    except Exception as exc:
        log.warning("FX lookup failed for %s: %s", cur, exc)
        return None


# ── Search ────────────────────────────────────────────────────────────────────

def _search_raw(query: str, types: list[str] | None = None) -> list[dict]:
    body = {"query": query, "searchFilter": {"types": types or []},
            "screenSize": "DESKTOP", "originPath": "/"}
    return _get_json(_SEARCH_URL, body).get("hits") or []


def search(query: str, limit: int = 12) -> list[dict]:
    """Instruments matching `query`, ready for the web interface."""
    out = []
    for h in _search_raw(query):
        oid = str(h.get("orderBookId") or "")
        if not oid.isdigit():
            continue
        title = str(h.get("title") or oid)
        m = re.match(r"^(.*?)\s*\(([^()]*)\)$", title)
        name, ticker = (m.group(1), m.group(2)) if m else (title, "")
        price = h.get("price") or {}
        out.append({
            "id": oid,
            "kind": "fund" if h.get("type") == "FUND" else "stock",
            "type": h.get("type") or "",
            "name": name,
            "ticker": ticker,
            "price": price.get("last"),
            "currency": price.get("currency"),
        })
        if len(out) >= limit:
            break
    return out
