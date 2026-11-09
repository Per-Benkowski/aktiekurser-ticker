"""
Renders the e-ink screens as PIL images (display text is English, as designed):

  render_focus            one instrument: price, change, chart, range bar, holding panel
  render_portfolio_summary  total value, value curve, return, movers
  render_portfolio_table    holdings with allocation bars (paged)
  render_info / render_splash  messages and the start-up screen with the web address

The panel is 1-bit: grey is only used for large chart fills (dithered); text and lines are black.
"""
from __future__ import annotations

import io
import logging
import math
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from PIL import Image, ImageDraw, ImageFont

import views
from avanza_client import Quote, STOCKHOLM_TZ
from portfolio import Portfolio, Position, period_gain

log = logging.getLogger(__name__)

# Display size — set by display.get_driver() after the driver is created
W = 800
H = 480

BLACK = 0
WHITE = 255

TABLE_ROWS_PER_PAGE = 8


# ── Fonts ─────────────────────────────────────────────────────────────────────

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            pass
    return ImageFont.load_default()


_F: dict = {}


def _f(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Font scaled to the current display width (design baseline: 360 px wide)."""
    scaled = max(6, round(size * W / 360))
    key = (scaled, bold)
    if key not in _F:
        _F[key] = _font(scaled, bold)
    return _F[key]


def _s(px: int) -> int:
    """Scale a horizontal pixel value from the 360-wide baseline to the current W."""
    return max(1, round(px * W / 360))


def _sy(px: int) -> int:
    """Scale a vertical pixel value from the 240-tall baseline to the current H."""
    return max(1, round(px * H / 240))


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_int(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def fmt_signed(x: float) -> str:
    return f"{x:+,.0f}".replace(",", " ").replace("-", "−")


def fmt_pct_signed(x: float) -> str:
    return f"{x:+.1f}%".replace("-", "−")


def fmt_qty(q: float) -> str:
    return fmt_int(q) if abs(q - round(q)) < 1e-9 else f"{q:,.2f}".replace(",", " ")


def _arrow(x: float) -> str:
    return "▲" if x >= 0 else "▼"


def _pct_str(pct: float) -> str:
    return f"{_arrow(pct)}{abs(pct):.2f}%"


# ── Drawing helpers ───────────────────────────────────────────────────────────

def _new() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("L", (W, H), WHITE)
    return img, ImageDraw.Draw(img)


def _text_r(draw, right_x: int, y: int, text: str, font) -> None:
    """Right-aligned text."""
    draw.text((right_x - int(draw.textlength(text, font=font)), y), text, font=font, fill=BLACK)


def _mpl_to_pil(fig) -> Image.Image:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    buf.seek(0)
    return Image.open(buf).convert("L")


def _embed_chart(img: Image.Image, chart: Image.Image | None, x: int, y: int, w: int, h: int) -> None:
    if chart is None:
        return
    img.paste(chart.resize((w, h), Image.LANCZOS).convert("L"), (x, y))


def _header(draw, title: str, subtitle: str, ip_text: str | None) -> int:
    """Title (left), clock + web address (right), subtitle. Returns the y below the subtitle."""
    pad_x, pad_y = _s(14), _sy(8)
    right = W - pad_x
    y = pad_y
    draw.text((pad_x, y), title, font=_f(22, bold=True), fill=BLACK)
    _text_r(draw, right, y, datetime.now(STOCKHOLM_TZ).strftime("%H:%M:%S"), _f(10))
    if ip_text:
        _text_r(draw, right, y + _sy(13), ip_text, _f(8))
    y += _sy(24)
    draw.text((pad_x, y), subtitle, font=_f(8), fill=BLACK)
    return y + _sy(15)


def _border(draw) -> None:
    draw.rectangle([(0, 0), (W - 1, H - 1)], outline=BLACK, width=1)


# ── Charts ────────────────────────────────────────────────────────────────────

_AXES = {
    "one_week":      (lambda: mdates.DayLocator(interval=1),                       "%a"),
    "one_month":     (lambda: mdates.WeekdayLocator(byweekday=mdates.MO),          "%-d %b"),
    "three_months":  (lambda: mdates.MonthLocator(interval=1),                     "%b"),
    "one_year":      (lambda: mdates.MonthLocator(interval=2),                     "%b"),
    "three_years":   (lambda: mdates.MonthLocator(interval=6),                     "%b %y"),
    "five_years":    (lambda: mdates.YearLocator(),                                "%Y"),
}


def _style_axes(ax, period: str) -> None:
    loc, fmt = _AXES.get(period, _AXES["one_year"])
    ax.xaxis.set_major_locator(loc())
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt))
    ax.tick_params(axis="x", labelsize=7)
    ax.tick_params(axis="y", labelleft=False)
    ax.grid(True, axis="x", linestyle="--", linewidth=0.3)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)


def _chart_intraday(quote: Quote, w: int, h: int) -> Image.Image:
    prices = quote.series("today")
    open_t, close_t = quote.open_close_times()
    today = datetime.now(STOCKHOLM_TZ).date()
    tz = STOCKHOLM_TZ
    x_start = tz.localize(datetime.combine(today, open_t))
    x_end   = tz.localize(datetime.combine(today, close_t))

    dpi = 100
    fig, ax = plt.subplots(figsize=(w / dpi, h / dpi), dpi=dpi)
    ax.set_xlim(x_start, x_end)

    now = datetime.now(tz)
    if not prices.empty:
        visible = prices[prices.index <= now]
        if not visible.empty:
            ax.fill_between(visible.index, visible.values, color="#999999", step="pre")
            ax.plot(visible.index, visible.values, color="black", linewidth=1.2)
            ax.set_ylim(visible.min() * 0.997, visible.max() * 1.003)

    if x_start <= now <= x_end:
        ax.axvline(now, color="black", linestyle="--", linewidth=0.8)

    if quote.prev_close:
        ax.axhline(quote.prev_close, color="black", linestyle="--", linewidth=0.7)
        ax.annotate(f"PREV {quote.prev_close:,.2f}", xy=(x_end, quote.prev_close),
                    xytext=(-2, 2), textcoords="offset points",
                    ha="right", va="bottom", fontsize=7, color="#333333")

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=tz))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
    ax.tick_params(axis="x", labelsize=7)
    ax.tick_params(axis="y", labelleft=False)
    ax.grid(True, axis="x", linestyle="--", linewidth=0.3)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout(pad=0.3)
    return _mpl_to_pil(fig)


def _chart_period(quote: Quote, view: str, w: int, h: int) -> Image.Image:
    period = views.period_of(view)
    prices = quote.series(period)
    dpi = 100
    fig, ax = plt.subplots(figsize=(w / dpi, h / dpi), dpi=dpi)

    if not prices.empty:
        ax.fill_between(prices.index, prices.values, color="#999999", step="pre")
        ax.plot(prices.index, prices.values, color="black", linewidth=1.2)
        ax.set_xlim(prices.index[0], prices.index[-1])
        ax.set_ylim(prices.min() * 0.97, prices.max() * 1.03)

        ref = quote.reference(view)
        if ref is not None:
            label, value = ref
            ax.axhline(value, color="black", linestyle="--", linewidth=0.7)
            ax.annotate(f"{label} {value:,.2f}", xy=(prices.index[-1], value),
                        xytext=(-2, 2), textcoords="offset points",
                        ha="right", va="bottom", fontsize=7, color="#333333")

    _style_axes(ax, period)
    fig.tight_layout(pad=0.3)
    return _mpl_to_pil(fig)


def _chart_value(series, baseline, baseline_label, view: str, w: int, h: int) -> Image.Image:
    """Portfolio value over time with a dashed baseline (cost, or the value at the start)."""
    dpi = 100
    fig, ax = plt.subplots(figsize=(w / dpi, h / dpi), dpi=dpi)
    ax.fill_between(series.index, series.values, color="#999999", step="pre")
    ax.plot(series.index, series.values, color="black", linewidth=1.2)
    ax.set_xlim(series.index[0], series.index[-1])
    lo = min(float(series.min()), baseline) if baseline is not None else float(series.min())
    hi = float(series.max())
    pad = (hi - lo) * 0.08 or 1.0
    ax.set_ylim(lo - pad, hi + pad)
    if baseline is not None:
        ax.axhline(baseline, color="black", linestyle="--", linewidth=0.7)
        ax.annotate(f"{baseline_label} {fmt_int(baseline)}", xy=(series.index[-1], baseline),
                    xytext=(-2, 2), textcoords="offset points",
                    ha="right", va="bottom", fontsize=7, color="#333333")
    _style_axes(ax, views.period_of(view))
    fig.tight_layout(pad=0.3)
    return _mpl_to_pil(fig)


def _range_bar(draw, x: int, y: int, w: int, low: float, high: float, current: float) -> None:
    """Horizontal range bar with a filled marker at the current price."""
    draw.line([(x, y), (x + w, y)], fill=BLACK, width=2)
    span = (high - low) or 1.0
    frac = min(1.0, max(0.0, (current - low) / span))
    mx = x + int(frac * w)
    r = max(2, _s(3))
    draw.ellipse([(mx - r, y - r), (mx + r, y + r)], fill=BLACK)


def _format_compact(n: float) -> str:
    """1_234_567 -> '1.2M'."""
    n = float(n)
    for suffix, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{suffix}"
    return f"{n:.0f}"


# ── Screen: one instrument ────────────────────────────────────────────────────

def _holding_panel(draw, x: int, y: int, pos: Position, view: str) -> None:
    amount = lambda v: f"{fmt_signed(v)} {pos.cur}"       # noqa: E731
    draw.text((x, y), f"HOLDING {fmt_qty(pos.qty)} PCS · {fmt_int(pos.value)} {pos.cur}",
              font=_f(8), fill=BLACK)
    stat_y = y + _sy(13)
    gain = period_gain(pos, view)
    if gain is not None:
        draw.text((x, stat_y), f"{_arrow(gain)} {amount(gain)}", font=_f(13, bold=True), fill=BLACK)
    if pos.total is not None and pos.total_pct is not None:
        draw.text((x, stat_y + _sy(16)), f"SINCE BUY {amount(pos.total)} ({fmt_pct_signed(pos.total_pct)})",
                  font=_f(8), fill=BLACK)


def render_focus(quote: Quote, view: str, ip_text: str | None = None,
                 position: Position | None = None) -> Image.Image:
    spec = views.VIEWS[view]
    is_day = view == "day"
    img, draw = _new()
    pad_x = _s(14)
    pad_y = _sy(8)
    right = W - pad_x

    # ── Row 1: ticker symbol (left) + clock and web address (right) ──────────
    y = pad_y
    draw.text((pad_x, y), quote.display_symbol.upper(), font=_f(22, bold=True), fill=BLACK)
    _text_r(draw, right, y, datetime.now(STOCKHOLM_TZ).strftime("%H:%M:%S"), _f(10))
    if ip_text:
        _text_r(draw, right, y + _sy(13), ip_text, _f(8))
    y += _sy(24)

    # ── Row 2: subtitle (full name · marketplace) ────────────────────────────
    subtitle = f"{quote.name[:32]} · {(quote.market_place or quote.currency)}"
    draw.text((pad_x, y), subtitle.upper(), font=_f(8), fill=BLACK)
    y += _sy(15)

    # ── Row 3: big price (left) + change (right) ─────────────────────────────
    row3_y = y
    price_str = f"{quote.price:,.2f}"
    draw.text((pad_x, row3_y), price_str, font=_f(26, bold=True), fill=BLACK)
    if quote.currency and quote.kind != "index":
        pw = draw.textlength(price_str, font=_f(26, bold=True))
        draw.text((pad_x + int(pw) + _s(10), row3_y), quote.currency, font=_f(26, bold=True), fill=BLACK)

    pct = quote.change_pct if is_day else (quote.change_pct_over(spec["period"]) or quote.change_pct)
    _text_r(draw, right, row3_y, _pct_str(pct), _f(13, bold=True))

    if is_day and quote.change_abs is not None:
        abs_str = f"{quote.change_abs:+.2f} today"
    else:
        ref = quote.reference(view)
        abs_str = f"vs {ref[1]:,.2f}" if ref else ""
    if abs_str:
        _text_r(draw, right, row3_y + _sy(17), abs_str, _f(8))
    y += _sy(32)

    # ── Divider ───────────────────────────────────────────────────────────────
    draw.line([(pad_x, y), (right, y)], fill=BLACK, width=1)
    y += _sy(5)

    # ── Row 4: view label (left) + stats (right) ─────────────────────────────
    draw.text((pad_x, y), spec["label"], font=_f(8), fill=BLACK)
    if is_day:
        parts = []
        if quote.volume:
            parts.append(f"VOL {_format_compact(quote.volume)}")
        if quote.vwap:
            parts.append(f"VWAP {quote.vwap:,.2f}")
        stat_str = "  ·  ".join(parts)
    else:
        rng = quote.range_over(spec["period"])
        stat_str = f"{spec['hdr']} {rng[0]:,.2f} — {rng[1]:,.2f}" if rng else ""
    if stat_str:
        _text_r(draw, right, y, stat_str, _f(8))
    y += _sy(15)

    # ── Chart ─────────────────────────────────────────────────────────────────
    bottom_h = _sy(46)
    chart_y  = y
    chart_h  = H - chart_y - bottom_h - pad_y
    chart_w  = W - 2 * pad_x

    if is_day and quote.market_closed():
        msg = "Market closed"
        sub = quote.time_until_open()
        mw = draw.textlength(msg, font=_f(15))
        sw = draw.textlength(sub, font=_f(15))
        draw.text(((W - int(mw)) // 2, chart_y + chart_h // 3),           msg, font=_f(15), fill=BLACK)
        draw.text(((W - int(sw)) // 2, chart_y + chart_h // 3 + _sy(20)), sub, font=_f(15), fill=BLACK)
    else:
        chart = _chart_intraday(quote, chart_w, chart_h) if is_day else _chart_period(quote, view, chart_w, chart_h)
        _embed_chart(img, chart, pad_x, chart_y, chart_w, chart_h)
    y = chart_y + chart_h + _sy(5)

    # ── Divider ───────────────────────────────────────────────────────────────
    draw.line([(pad_x, y), (right, y)], fill=BLACK, width=1)
    y += _sy(7)

    # ── Bottom: range bar (left) + holding / consensus slot (right) ──────────
    col_w   = (W - 2 * pad_x - _s(20)) // 2
    right_x = pad_x + col_w + _s(20)

    range_period = "one_year" if is_day else spec["period"]
    draw.text((pad_x, y), f"{spec['rng']} RANGE", font=_f(8), fill=BLACK)
    rng = quote.range_over(range_period)
    if rng:
        low, high = rng
        bar_y = y + _sy(18)
        _range_bar(draw, pad_x, bar_y, col_w, low, high, quote.price)
        draw.text((pad_x, bar_y + _sy(5)), f"{low:,.2f}", font=_f(7), fill=BLACK)
        _text_r(draw, pad_x + col_w, bar_y + _sy(5), f"{high:,.2f}", _f(7))

    if position is not None:
        _holding_panel(draw, right_x, y, position, view)
    else:
        # Analyst consensus needs an Avanza login, so the slot stays empty without a holding.
        draw.text((right_x, y), "CONSENSUS vs PRICE", font=_f(8), fill=BLACK)
        stat_y = y + _sy(13)
        draw.text((right_x, stat_y), "—", font=_f(13, bold=True), fill=BLACK)
        draw.text((right_x, stat_y + _sy(16)), "No analyst data (Avanza)", font=_f(8), fill=BLACK)

    _border(draw)
    return img


# ── Screen: portfolio summary ─────────────────────────────────────────────────

def render_portfolio_summary(pf: Portfolio, ip_text: str | None = None) -> Image.Image:
    img, draw = _new()
    pad_x, pad_y = _s(14), _sy(8)
    right = W - pad_x
    counted = [p for p in pf.positions if p.converted]
    y = _header(draw, "PORTFOLIO", f"{len(counted)} HOLDINGS · VALUES IN SEK", ip_text)

    row3 = y
    total = fmt_int(pf.value)
    draw.text((pad_x, row3), total, font=_f(26, bold=True), fill=BLACK)
    tw = draw.textlength(total, font=_f(26, bold=True))
    draw.text((pad_x + int(tw) + _s(10), row3), "SEK", font=_f(26, bold=True), fill=BLACK)
    _text_r(draw, right, row3, _pct_str(pf.day_pct), _f(13, bold=True))
    _text_r(draw, right, row3 + _sy(17), f"{fmt_signed(pf.day)} SEK today", _f(8))
    y = row3 + _sy(32)

    draw.line([(pad_x, y), (right, y)], fill=BLACK, width=1)
    y += _sy(5)
    spec = views.VIEWS[pf.view]
    left_label = f"PORTFOLIO VALUE · {spec['label'].split(' · ')[0]} · AT CURRENT HOLDINGS"
    draw.text((pad_x, y), left_label, font=_f(8), fill=BLACK)
    if pf.cost is not None:
        _text_r(draw, right, y, f"COST {fmt_int(pf.cost)} SEK", _f(8))
    y += _sy(15)

    bottom_h = _sy(46)
    chart_h = H - y - bottom_h - pad_y
    chart_w = W - 2 * pad_x
    if pf.series is not None:
        _embed_chart(img, _chart_value(pf.series, pf.baseline, pf.baseline_label, pf.view, chart_w, chart_h),
                     pad_x, y, chart_w, chart_h)
    else:
        msg = "No history available"
        mw = draw.textlength(msg, font=_f(15))
        draw.text(((W - int(mw)) // 2, y + chart_h // 3), msg, font=_f(15), fill=BLACK)
    y += chart_h + _sy(5)
    draw.line([(pad_x, y), (right, y)], fill=BLACK, width=1)
    y += _sy(7)

    col_w = (W - 2 * pad_x - _s(20)) // 2
    rx = pad_x + col_w + _s(20)
    draw.text((pad_x, y), "RETURN SINCE PURCHASE", font=_f(8), fill=BLACK)
    if pf.total is not None:
        draw.text((pad_x, y + _sy(13)), f"{_arrow(pf.total)} {fmt_signed(pf.total)} SEK",
                  font=_f(13, bold=True), fill=BLACK)
        note = f"{fmt_pct_signed(pf.total_pct)} ON {fmt_int(pf.cost)} SEK COST" if pf.total_pct is not None else ""
        if not pf.cost_complete:
            note = (note + " · PARTIAL").strip(" ·")
        draw.text((pad_x, y + _sy(29)), note, font=_f(8), fill=BLACK)
    else:
        draw.text((pad_x, y + _sy(13)), "—", font=_f(13, bold=True), fill=BLACK)
        draw.text((pad_x, y + _sy(29)), "ADD PURCHASE PRICES IN THE WEB UI", font=_f(8), fill=BLACK)

    draw.text((rx, y), "TODAY'S MOVERS", font=_f(8), fill=BLACK)
    if len(counted) >= 2:
        movers = sorted(counted, key=lambda p: p.day_pct, reverse=True)
        for i, (p, arrow) in enumerate(((movers[0], "▲"), (movers[-1], "▼"))):
            tag = (p.symbol or p.name)[:10].upper()
            draw.text((rx, y + _sy(13 + 14 * i)), f"{arrow} {tag:<10} {p.day_pct:+.2f}%".replace("-", "−"),
                      font=_f(10, bold=True), fill=BLACK)
    _border(draw)
    return img


# ── Screen: holdings table ────────────────────────────────────────────────────

def table_pages(pf: Portfolio) -> int:
    return max(1, math.ceil(len(pf.positions) / TABLE_ROWS_PER_PAGE))


def _rows_per_page(pf: Portfolio) -> int:
    """Spread the rows evenly over the pages (9 holdings -> 5 + 4, not 8 + 1)."""
    return max(1, math.ceil(len(pf.positions) / table_pages(pf)))


def render_portfolio_table(pf: Portfolio, ip_text: str | None = None, page: int = 0) -> Image.Image:
    img, draw = _new()
    pad_x = _s(14)
    right = W - pad_x
    pages = table_pages(pf)
    sub = f"{len(pf.positions)} POSITIONS · SORTED BY VALUE · SEK"
    if pages > 1:
        sub += f" · PAGE {page + 1}/{pages}"
    y = _header(draw, "HOLDINGS", sub, ip_text)
    draw.line([(pad_x, y), (right, y)], fill=BLACK, width=1)
    y += _sy(4)

    # column anchors in px @ 800 wide (monospace, ~12 px per char at 9 pt), scaled for other panels
    k = W / 800
    X_NAME, X_QTY, X_VAL = pad_x, int(262 * k), int(360 * k)
    X_BAR, BAR_W, X_PCT = int(380 * k), int(100 * k), int(540 * k)
    X_DAY, X_RET = int(650 * k), right
    fh = _f(7)
    for x, t, anchor in [(X_NAME, "NAME", "l"), (X_QTY, "QTY", "r"), (X_VAL, "VALUE", "r"),
                         (X_BAR, "SHARE", "l"), (X_DAY, "TODAY", "r"), (X_RET, "RETURN", "r")]:
        if anchor == "l":
            draw.text((x, y), t, font=fh, fill=BLACK)
        else:
            _text_r(draw, x, y, t, fh)
    y += _sy(12)
    draw.line([(pad_x, y), (right, y)], fill=BLACK, width=1)
    y += _sy(3)

    per = _rows_per_page(pf)
    rows = pf.positions[page * per:(page + 1) * per]
    rh, fr = _sy(17), _f(9)
    char_w = getattr(fr, "size", 20) * 0.6                      # monospace advance
    qty_chars = max((len(fmt_qty(p.qty)) for p in pf.positions), default=3)
    name_chars = max(8, int((X_QTY - X_NAME) / char_w) - qty_chars - 1)   # keep clear of the quantity

    for p in rows:
        nm = p.name if len(p.name) <= name_chars else p.name[:name_chars - 1] + "…"
        draw.text((X_NAME, y), nm, font=fr, fill=BLACK)
        _text_r(draw, X_QTY, y, fmt_qty(p.qty), fr)
        _text_r(draw, X_VAL, y, fmt_int(p.value) + ("" if p.converted else f" {p.cur}"), fr)
        if p.converted:
            by, bh = y + _sy(3), _sy(8)
            draw.rectangle([(X_BAR, by), (X_BAR + BAR_W, by + bh)], outline=BLACK, width=1)
            draw.rectangle([(X_BAR, by), (X_BAR + max(2, int(BAR_W * p.share)), by + bh)], fill=BLACK)
            _text_r(draw, X_PCT, y, f"{p.share * 100:.0f}%", fr)
        _text_r(draw, X_DAY, y, _pct_str(p.day_pct), fr)
        _text_r(draw, X_RET, y, fmt_pct_signed(p.total_pct) if p.total_pct is not None else "—", fr)
        y += rh

    y += _sy(2)
    draw.line([(pad_x, y), (right, y)], fill=BLACK, width=2)
    y += _sy(4)
    fb = _f(9, bold=True)
    draw.text((X_NAME, y), "TOTAL", font=fb, fill=BLACK)
    _text_r(draw, X_VAL, y, fmt_int(pf.value), fb)
    _text_r(draw, X_PCT, y, "100%", fb)
    _text_r(draw, X_DAY, y, _pct_str(pf.day_pct), fb)
    _text_r(draw, X_RET, y, fmt_pct_signed(pf.total_pct) if pf.total_pct is not None else "—", fb)
    _border(draw)
    return img


# ── Messages ──────────────────────────────────────────────────────────────────

def render_info(title: str, lines: list[str], ip_text: str | None = None) -> Image.Image:
    img, draw = _new()
    pad_x = _s(14)
    y = _sy(14)
    draw.text((pad_x, y), title, font=_f(22, bold=True), fill=BLACK)
    y += _sy(34)
    for line in lines:
        draw.text((pad_x, y), line, font=_f(11), fill=BLACK)
        y += _sy(17)
    if ip_text:
        draw.text((pad_x, H - _sy(40)), ip_text, font=_f(20, bold=True), fill=BLACK)
    _border(draw)
    return img


def render_splash(ip_text: str | None) -> Image.Image:
    return render_info("AKTIEKURSER",
                       ["Open this address in a browser", "on your network to choose", "what the display shows:"],
                       ip_text or "NO NETWORK")
