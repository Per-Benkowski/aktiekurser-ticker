"""
Catalog of the chart views an instrument can show.

  period – Avanza `timePeriod` for the chart endpoint
  hist   – key in Avanza's `historicalClosingPrices` (price one period ago)
  label  – left label above the chart (display is in English)
  hdr    – prefix for the high/low text on the right of that row
  rng    – prefix for the bottom range-bar label
  ago    – label of the dashed reference line
  sv     – name in the Swedish web interface
"""

VIEWS = {
    "day":   dict(period="today",        hist=None,           label="INTRADAY · SINCE OPEN",
                  hdr="RANGE",  rng="52-WEEK", ago="PREV",   sv="Dag"),
    "week":  dict(period="one_week",     hist="oneWeek",      label="1 WEEK",
                  hdr="RANGE",  rng="1-WEEK",  ago="1W AGO", sv="Vecka"),
    "month": dict(period="one_month",    hist="oneMonth",     label="1 MONTH",
                  hdr="RANGE",  rng="1-MONTH", ago="1M AGO", sv="Månad"),
    "3m":    dict(period="three_months", hist="threeMonths",  label="3 MONTHS · DAILY",
                  hdr="RANGE",  rng="3-MONTH", ago="3M AGO", sv="3 mån"),
    "year":  dict(period="one_year",     hist="oneYear",      label="1 YEAR · DAILY",
                  hdr="52-WK",  rng="52-WEEK", ago="1Y AGO", sv="År"),
    "3y":    dict(period="three_years",  hist="threeYears",   label="3 YEARS · WEEKLY",
                  hdr="RANGE",  rng="3-YEAR",  ago="3Y AGO", sv="3 år"),
    "5y":    dict(period="five_years",   hist="fiveYears",    label="5 YEARS · WEEKLY",
                  hdr="RANGE",  rng="5-YEAR",  ago="5Y AGO", sv="5 år"),
}
VIEW_ORDER = ["day", "week", "month", "3m", "year", "3y", "5y"]

# Views that make sense for the portfolio value curve (no intraday)
PORTFOLIO_VIEWS = ["week", "month", "3m", "year", "3y", "5y"]

KINDS = ("stock", "fund")


def period_of(view: str) -> str:
    return VIEWS[view]["period"]


def effective_views(kind: str, views: list[str]) -> list[str]:
    """Views that can actually be shown for this kind of instrument, in catalog order.

    Funds have no intraday chart. If nothing valid is left, fall back to the year view.
    """
    wanted = [v for v in VIEW_ORDER if v in views]
    if kind == "fund":
        wanted = [v for v in wanted if v != "day"]
    return wanted or ["year"]


def periods_needed(views: list[str]) -> list[str]:
    """Chart periods to fetch for these views.

    The day view still shows the 52-week range bar at the bottom, so it needs the year series too.
    """
    out = []
    for v in views:
        p = VIEWS[v]["period"]
        if p not in out:
            out.append(p)
    if "day" in views and "one_year" not in out:
        out.append("one_year")
    return out
