#!/usr/bin/env python3
"""
E-ink stock ticker with a web interface — Raspberry Pi, Avanza public data.

The display loop runs in the main thread (it owns the e-ink driver). The web server runs in a
background thread and edits settings.json; saving there wakes this loop through `state`.
"""
import logging
import signal
import sys
import time

import avanza_client as av
import config
import display as display_mod
import netinfo
import portfolio
import renderer
import settings
import views
import webserver
from state import State

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

SPLASH_SECONDS = 6
RETRY_SECONDS = 30


class Loop:
    def __init__(self, drv, state: State):
        self.drv = drv
        self.state = state
        self.cards = 0          # instrument / portfolio cards shown in the current pass
        self._info_key = None   # last info screen drawn, so a static message is not redrawn

    # ── helpers ──────────────────────────────────────────────────────────────
    def ip_text(self, cfg: dict) -> str | None:
        return netinfo.display_url(self.state.port) if cfg["show_ip"] else None

    def show(self, img, info: dict) -> None:
        try:
            self.state.publish(img, info)
            self.drv.show(img)
        except Exception:
            log.exception("Display error")

    def info(self, key: str, title: str, lines: list[str], ip: str, name: str) -> None:
        """Message screen; only redrawn when it changes (saves e-ink refreshes)."""
        if self._info_key == key:
            return
        self._info_key = key
        self.show(renderer.render_info(title, lines, ip), {"kind": "info", "name": name, "view": ""})

    def dwell(self, cfg: dict) -> str | None:
        return self.state.wait(cfg["seconds_per_view"])

    # ── one instrument: its chosen views, one after another ──────────────────
    def show_instrument(self, cfg: dict, inst: dict) -> str | None:
        """Returns 'reload' if settings changed meanwhile, 'failed' if no data, else None."""
        label = inst["name"] or inst["id"]
        chosen = views.effective_views(inst["kind"], inst["views"])
        try:
            quote = av.fetch_quote(inst["kind"], inst["id"], inst["name"] or None, chosen)
        except Exception as exc:
            log.error("Failed to fetch %s: %s", label, exc)
            return "failed"
        log.info("  %s  %.2f  %+.2f%%", quote.name, quote.price, quote.change_pct)

        position = portfolio.position_for(inst, quote)

        def make(view):
            try:
                return renderer.render_focus(quote, view, self.ip_text(cfg), position)
            except Exception:
                log.exception("Render error (%s, %s)", label, view)
                return None

        img = make(chosen[0])
        for n, view in enumerate(chosen):
            if img is not None:
                self.cards += 1
                self._info_key = None
                self.show(img, {"kind": "instrument", "id": inst["id"], "name": quote.name, "view": view})
                log.info("Displayed %s [%s]", quote.name, view)
            shown_at = time.monotonic()
            # Draw the next view while this one is on screen, so the cadence stays at
            # seconds_per_view even when rendering is slow on the Pi.
            img = make(chosen[n + 1]) if n + 1 < len(chosen) else None
            left = cfg["seconds_per_view"] - (time.monotonic() - shown_at) if n < len(chosen) else 0
            if self.state.wait(max(0.0, left)) == "reload":
                return "reload"
        return None

    # ── portfolio screens ────────────────────────────────────────────────────
    def show_portfolio(self, cfg: dict) -> str | None:
        pcfg = cfg["portfolio"]
        if not (pcfg["summary"] or pcfg["table"]):
            return None
        try:
            pf = portfolio.build(cfg, pcfg["period"], max_age=600, with_series=pcfg["summary"])
        except Exception:
            log.exception("Could not build portfolio")
            return None
        if not pf.positions:
            return None
        ip = self.ip_text(cfg)
        screens = []
        if pcfg["summary"]:
            screens.append(("summary", lambda: renderer.render_portfolio_summary(pf, ip)))
        if pcfg["table"]:
            for page in range(renderer.table_pages(pf)):
                screens.append((f"table {page + 1}", lambda page=page: renderer.render_portfolio_table(pf, ip, page)))
        for name, make in screens:
            try:
                img = make()
            except Exception:
                log.exception("Render error (portfolio %s)", name)
                continue
            self.cards += 1
            self._info_key = None
            self.show(img, {"kind": "portfolio", "name": "Portfölj", "view": name})
            log.info("Displayed portfolio [%s]  %.0f SEK", name, pf.value)
            if self.dwell(cfg) == "reload":
                return "reload"
        return None

    # ── main loop ────────────────────────────────────────────────────────────
    def run(self) -> None:
        first = settings.get()
        self.show(renderer.render_splash(self.ip_text(first) or netinfo.display_url(self.state.port)),
                  {"kind": "splash", "name": "Start", "view": ""})
        self.state.wait(SPLASH_SECONDS)

        while True:
            cfg = settings.get()
            items = [i for i in cfg["instruments"] if i["enabled"]]
            has_holdings = any(i.get("qty") for i in cfg["instruments"])

            if not items and not has_holdings:
                url = netinfo.display_url(self.state.port)
                self.info(f"empty|{url}", "NOTHING TO SHOW",
                          ["No instruments are selected yet.", "Open the address below in a browser", "and add some."],
                          url, "Inga instrument")
                self.state.wait(600)
                continue

            self.cards = 0
            interrupted = False
            for inst in items:
                if self.show_instrument(cfg, inst) == "reload":
                    interrupted = True
                    break
            if interrupted:
                continue
            if self.show_portfolio(cfg) == "reload":
                continue

            if self.cards == 0:        # nothing could be shown this pass: do not hammer Avanza
                url = self.ip_text(cfg) or netinfo.display_url(self.state.port)
                self.info(f"nodata|{url}", "NO DATA",
                          ["Could not reach Avanza.", "Check the network connection.", "Trying again shortly."],
                          url, "Ingen data")
                self.state.wait(RETRY_SECONDS)


def main() -> None:
    state = State()
    drv = display_mod.get_driver()
    state.port = webserver.start(state)
    settings.get()          # creates settings.json (importing the old stocks.txt) on first run
    log.info("Web interface: %s", netinfo.display_url(state.port))

    def shutdown(sig, frame):
        log.info("Shutting down…")
        try:
            drv.sleep()
        finally:
            sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    Loop(drv, state).run()


if __name__ == "__main__":
    main()
