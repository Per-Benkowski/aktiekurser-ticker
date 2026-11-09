"""
Holdings math, shared by the e-ink renderer and the web interface so both show the same numbers.

Terms
  value  – quantity × price, converted to SEK with Avanza's spot rate
  cost   – what was paid: the SEK total if the user gave one, otherwise quantity × average price
           per unit (× today's rate for foreign currencies, so currency effects are not included)
  day    – today's change in SEK
  total  – value − cost (return since purchase)

Holdings in a currency we cannot convert stay in that currency and are left out of the totals.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

import avanza_client as av
import views
from avanza_client import Quote

log = logging.getLogger(__name__)


@dataclass
class Position:
    inst: dict
    quote: Quote
    qty: float
    fx: float                  # multiplier into `cur` (1.0 for SEK or when no rate was found)
    cur: str                   # currency the amounts below are in
    converted: bool            # False -> amounts are in the native currency, excluded from totals
    value: float
    cost: float | None
    day: float                 # today's change
    total: float | None        # value - cost
    total_pct: float | None
    share: float = 0.0         # share of portfolio value (0..1)

    @property
    def name(self) -> str:
        return self.inst.get("name") or self.quote.name

    @property
    def symbol(self) -> str:
        return self.quote.ticker_symbol or ""

    @property
    def day_pct(self) -> float:
        return self.quote.change_pct


@dataclass
class Portfolio:
    positions: list[Position] = field(default_factory=list)
    value: float = 0.0
    day: float = 0.0
    day_pct: float = 0.0
    cost: float | None = None          # sum of cost over positions that have one
    total: float | None = None         # return on those positions
    total_pct: float | None = None
    cost_complete: bool = False        # every position has a cost
    skipped: list[str] = field(default_factory=list)    # names left out (no rate / no quote)
    series: pd.Series | None = None    # portfolio value over time (current holdings)
    baseline: float | None = None
    baseline_label: str = ""
    view: str = "year"


def position_for(inst: dict, quote: Quote) -> Position | None:
    """Position for one instrument, or None when no quantity has been entered."""
    qty = inst.get("qty")
    if not qty:
        return None
    rate = av.fx_rate(quote.currency) if quote.kind != "index" else 1.0
    converted = rate is not None
    fx = rate if converted else 1.0
    cur = "SEK" if converted else quote.currency

    value = qty * quote.price * fx
    day_unit = quote.day_change_abs()
    day = qty * day_unit * fx if day_unit is not None else 0.0

    cost = None
    if inst.get("cost_total_sek") is not None and converted:
        cost = float(inst["cost_total_sek"])
    elif inst.get("cost") is not None:
        cost = qty * float(inst["cost"]) * fx
    total = (value - cost) if cost is not None else None
    total_pct = (total / cost * 100.0) if (cost and total is not None) else None
    return Position(inst=inst, quote=quote, qty=float(qty), fx=fx, cur=cur, converted=converted,
                    value=value, cost=cost, day=day, total=total, total_pct=total_pct)


def period_gain(pos: Position, view: str) -> float | None:
    """Change in the holding's value over the shown view (day = today)."""
    if view == "day":
        return pos.day
    ref = pos.quote.reference(view)
    if ref is None:
        return None
    return pos.qty * pos.fx * (pos.quote.price - ref[1])


def _daily(series: pd.Series) -> pd.Series:
    """Last value per calendar day, so markets with different opening hours line up."""
    s = series.groupby(series.index.date).last()
    s.index = pd.DatetimeIndex(s.index)
    return s


def build(cfg: dict, view: str = "year", max_age: float = 600, with_series: bool = True) -> Portfolio:
    """Fetch what is needed (cached) and compute the whole portfolio."""
    pf = Portfolio(view=view)
    held = [i for i in cfg["instruments"] if i.get("qty")]
    period = views.period_of(view)
    series_parts: list[pd.Series] = []

    for inst in held:
        label = inst.get("name") or inst["id"]
        try:
            quote = av.fetch_quote(inst["kind"], inst["id"], inst.get("name") or None,
                                   [view] if with_series else [], max_age=max_age)
        except Exception as exc:
            log.warning("Portfolio: could not fetch %s: %s", label, exc)
            pf.skipped.append(label)
            continue
        pos = position_for(inst, quote)
        if pos is None:
            continue
        if not pos.converted:
            pf.skipped.append(f"{label} ({pos.cur})")
        pf.positions.append(pos)
        if with_series and pos.converted:
            s = quote.series(period)
            if not s.empty:
                series_parts.append(_daily(s) * pos.qty * pos.fx)

    counted = [p for p in pf.positions if p.converted]
    pf.value = sum(p.value for p in counted)
    pf.day = sum(p.day for p in counted)
    prev = pf.value - pf.day
    pf.day_pct = (pf.day / prev * 100.0) if prev else 0.0

    with_cost = [p for p in counted if p.cost is not None]
    if with_cost:
        pf.cost = sum(p.cost for p in with_cost)
        pf.total = sum(p.value for p in with_cost) - pf.cost
        pf.total_pct = (pf.total / pf.cost * 100.0) if pf.cost else None
    pf.cost_complete = bool(counted) and len(with_cost) == len(counted)

    for p in pf.positions:
        p.share = (p.value / pf.value) if (p.converted and pf.value) else 0.0
    pf.positions.sort(key=lambda p: (not p.converted, -p.value))

    if series_parts and len(series_parts) == len(counted):
        df = pd.concat(series_parts, axis=1).sort_index().ffill().dropna()
        if len(df) >= 2:
            pf.series = df.sum(axis=1)
    if pf.series is not None:
        if pf.cost_complete and pf.cost:
            pf.baseline, pf.baseline_label = pf.cost, "COST"
        else:
            pf.baseline, pf.baseline_label = float(pf.series.iloc[0]), views.VIEWS[view]["ago"]
    return pf


def to_json(pf: Portfolio) -> dict:
    """Plain data for the web interface."""
    return {
        "view": pf.view,
        "currency": "SEK",
        "value": pf.value, "day": pf.day, "day_pct": pf.day_pct,
        "cost": pf.cost, "total": pf.total, "total_pct": pf.total_pct,
        "cost_complete": pf.cost_complete,
        "skipped": pf.skipped,
        "baseline": pf.baseline, "baseline_label": pf.baseline_label,
        "series": ([[t.strftime("%Y-%m-%d"), round(float(v), 2)] for t, v in pf.series.items()]
                   if pf.series is not None else []),
        "positions": [{
            "id": p.inst["id"], "name": p.name, "symbol": p.symbol, "qty": p.qty,
            "value": p.value, "share": p.share, "cur": p.cur, "converted": p.converted,
            "day": p.day, "day_pct": p.day_pct,
            "cost": p.cost, "total": p.total, "total_pct": p.total_pct,
        } for p in pf.positions],
    }
