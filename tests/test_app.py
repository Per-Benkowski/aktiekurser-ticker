"""
Offline tests: no Avanza access and no display needed.

    cd ~/aktiekurser-web && AKTIE_DRIVER=preview venv/bin/python -m unittest discover -s tests -v
"""
import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import numpy as np
import pandas as pd
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AKTIE_DRIVER", "preview")

import avanza_client as av          # noqa: E402
import config                       # noqa: E402
import portfolio                    # noqa: E402
import renderer                     # noqa: E402
import settings                     # noqa: E402
import views                        # noqa: E402
import webserver                    # noqa: E402
from state import State             # noqa: E402

TZ = pytz.timezone("Europe/Stockholm")
renderer.W, renderer.H = 800, 480


def series(first, last, n=120, intraday=False):
    end = pd.Timestamp.now(tz=TZ).normalize() + pd.Timedelta(hours=12)
    idx = (pd.date_range(end=end, periods=n, freq="30min") if intraday
           else pd.date_range(end=end, periods=n, freq="B"))
    return pd.Series(np.linspace(first, last, n), index=idx)


def make_quote(oid="5247", name="Investor B", price=100.0, pct=1.0, cur="SEK", kind="stock",
               change_abs=1.0, hist=None, views_=("day", "year", "month", "week", "3m", "3y", "5y")):
    charts = {views.period_of(v): series(price * 0.8, price, intraday=(v == "day")) for v in views_}
    charts["one_year"] = series(price * 0.7, price)
    return av.Quote(kind=kind, oid=oid, name=name, price=price, change_pct=pct, currency=cur,
                    country="SE" if cur == "SEK" else "US", charts=charts, ticker_symbol=name[:5].upper(),
                    market_place="Stockholmsbörsen", market_status="OPEN", change_abs=change_abs,
                    volume=1.2e6, vwap=price, prev_close=price - (change_abs or 0),
                    historical=hist if hist is not None else {"oneYear": price * 0.7, "oneMonth": price * 0.9,
                                                              "oneWeek": price * 0.95})


class TempSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = config.SETTINGS_FILE, config.STOCKS_SEED
        config.SETTINGS_FILE = os.path.join(self.tmp.name, "settings.json")
        config.STOCKS_SEED = os.path.join(self.tmp.name, "seed.txt")
        settings._cache = None
        webserver._forget_verified()

    def tearDown(self):
        config.SETTINGS_FILE, config.STOCKS_SEED = self._old
        settings._cache = None
        self.tmp.cleanup()


class SettingsTests(TempSettings):
    def test_first_run_imports_old_stocks_txt(self):
        with open(config.STOCKS_SEED, "w") as f:
            f.write("# c\n5247   \n5277\t\n649096\n649096\nFUND 41567\nFUND 1509082 DNB\n1913988 ETF namn\nbogus line\n")
        cfg = settings.get()
        ids = [i["id"] for i in cfg["instruments"]]
        self.assertEqual(ids, ["5247", "5277", "649096", "41567", "1509082", "1913988"])   # duplicate dropped
        by = {i["id"]: i for i in cfg["instruments"]}
        self.assertEqual(by["41567"]["kind"], "fund")
        self.assertEqual(by["41567"]["views"], ["year"])
        self.assertEqual(by["5247"]["views"], ["day", "year"])
        self.assertEqual(by["1509082"]["name"], "DNB")
        self.assertEqual(oct(os.stat(config.SETTINGS_FILE).st_mode & 0o777), "0o600")

    def test_real_seed_file_imports(self):
        config.STOCKS_SEED = os.path.join(config.APP_DIR, "stocks.txt.imported-seed")
        cfg = settings.get()
        self.assertGreaterEqual(len(cfg["instruments"]), 5)
        self.assertEqual(len({i["id"] for i in cfg["instruments"]}), len(cfg["instruments"]))

    def test_roundtrip_and_comma_decimals(self):
        raw = {"seconds_per_view": 20, "show_ip": False,
               "portfolio": {"summary": True, "table": False, "period": "month"},
               "instruments": [{"id": "5247", "kind": "stock", "views": ["year", "day"], "qty": "150",
                                "cost": "330,50", "cost_total_sek": ""}]}
        saved = settings.save(raw)
        i = saved["instruments"][0]
        self.assertEqual((i["qty"], i["cost"], i["cost_total_sek"]), (150.0, 330.5, None))
        self.assertEqual(i["views"], ["day", "year"])             # catalog order
        settings._cache = None
        again = settings.get()
        self.assertEqual(again["seconds_per_view"], 20)
        self.assertFalse(again["show_ip"])
        self.assertEqual(again["portfolio"]["period"], "month")

    def test_validation(self):
        ok = {"id": "5247", "kind": "stock", "views": ["day"]}
        bad = [
            ({"seconds_per_view": 2, "instruments": []}, "Sekunder"),
            ({"seconds_per_view": 99999, "instruments": []}, "Sekunder"),
            ({"seconds_per_view": "abc", "instruments": []}, "tal"),
            ({"instruments": [dict(ok, id="abc")]}, "orderbook"),
            ({"instruments": [dict(ok, id="1; DROP")]}, "orderbook"),
            ({"instruments": [dict(ok, kind="crypto")]}, "Typ"),
            ({"instruments": [dict(ok, views=["day", "hour"])]}, "vy"),
            ({"instruments": [dict(ok, views=[])]}, "minst en vy"),
            ({"instruments": [dict(ok, qty=-1)]}, "Antal"),
            ({"instruments": [dict(ok, qty="x")]}, "tal"),
            ({"instruments": [dict(ok, qty=float("inf"))]}, "Antal"),
            ({"instruments": [ok, ok]}, "finns redan"),
            ({"instruments": [ok] * 51}, "Högst"),
            ({"portfolio": {"period": "day"}, "instruments": []}, "period"),
            ("not a dict", "Ogiltiga"),
        ]
        for raw, needle in bad:
            with self.subTest(raw=str(raw)[:60]):
                with self.assertRaises(settings.ValidationError) as cm:
                    settings.save(raw)
                self.assertIn(needle.lower(), str(cm.exception).lower())

    def test_corrupt_file_falls_back_to_defaults(self):
        settings.get()
        with open(config.SETTINGS_FILE, "w") as f:
            f.write("{not json")
        settings._cache = None
        self.assertEqual(settings.get()["instruments"], [])

    def test_password(self):
        self.assertFalse(settings.has_password())
        self.assertTrue(settings.check_password("anything"))
        settings.set_password("hemligt1")
        self.assertTrue(settings.has_password())
        self.assertTrue(settings.check_password("hemligt1"))
        self.assertFalse(settings.check_password("fel"))
        with open(config.SETTINGS_FILE) as f:
            self.assertNotIn("hemligt1", f.read())                           # only a hash on disk
        settings.save({"instruments": []})                                   # saving keeps the password
        self.assertTrue(settings.has_password())
        self.assertNotIn("password_hash", settings.public(settings.get()))
        self.assertTrue(settings.public(settings.get())["password_set"])
        self.assertNotIn("password", settings.public(settings.get()))
        with self.assertRaises(settings.ValidationError):
            settings.set_password("abc")
        settings.set_password(None)
        self.assertFalse(settings.has_password())


class PortfolioTests(TempSettings):
    def inst(self, oid, **kw):
        return dict({"id": oid, "kind": "stock", "name": "", "views": ["year"], "enabled": True,
                     "qty": None, "cost": None, "cost_total_sek": None}, **kw)

    def test_position_sek(self):
        q = make_quote(price=400.0, pct=0.5, change_abs=2.0)
        with mock.patch.object(av, "fx_rate", return_value=1.0):
            p = portfolio.position_for(self.inst("5247", qty=150, cost=330), q)
        self.assertEqual(round(p.value), 60000)
        self.assertEqual(round(p.cost), 49500)
        self.assertEqual(round(p.total), 10500)
        self.assertAlmostEqual(p.total_pct, 10500 / 49500 * 100)
        self.assertEqual(round(p.day), 300)                                # 150 × 2.0

    def test_no_quantity_means_no_position(self):
        self.assertIsNone(portfolio.position_for(self.inst("5247"), make_quote()))
        self.assertIsNone(portfolio.position_for(self.inst("5247", qty=0), make_quote()))

    def test_foreign_currency_uses_fx_and_sek_cost_override(self):
        q = make_quote(price=9.0, cur="USD", change_abs=0.1)
        with mock.patch.object(av, "fx_rate", return_value=10.0):
            a = portfolio.position_for(self.inst("1", qty=100, cost=8.0), q)
            b = portfolio.position_for(self.inst("1", qty=100, cost=8.0, cost_total_sek=7000), q)
        self.assertEqual(round(a.value), 9000)
        self.assertEqual(round(a.cost), 8000)          # per-unit cost × today's rate
        self.assertEqual(round(b.cost), 7000)          # explicit SEK cost wins (includes currency effect)
        self.assertEqual(round(b.total), 2000)
        self.assertEqual(round(a.day), 100)

    def test_unconvertible_currency_stays_native(self):
        q = make_quote(price=9.0, cur="XYZ")
        with mock.patch.object(av, "fx_rate", return_value=None):
            p = portfolio.position_for(self.inst("1", qty=10, cost=8.0), q)
        self.assertFalse(p.converted)
        self.assertEqual(p.cur, "XYZ")
        self.assertEqual(round(p.value), 90)

    def test_fund_day_change_derived_from_percent(self):
        q = make_quote(kind="fund", price=110.0, pct=10.0, change_abs=None)
        with mock.patch.object(av, "fx_rate", return_value=1.0):
            p = portfolio.position_for(self.inst("1", kind="fund", qty=10), q)
        self.assertAlmostEqual(p.day, 10 * 10.0)       # 110 vs 100 before → +10 per unit

    def test_period_gain(self):
        q = make_quote(price=100.0, change_abs=1.0, hist={"oneYear": 70.0, "oneMonth": 90.0})
        with mock.patch.object(av, "fx_rate", return_value=1.0):
            p = portfolio.position_for(self.inst("1", qty=10), q)
        self.assertAlmostEqual(portfolio.period_gain(p, "day"), 10.0)
        self.assertAlmostEqual(portfolio.period_gain(p, "year"), 300.0)
        self.assertAlmostEqual(portfolio.period_gain(p, "month"), 100.0)

    def test_build_totals_shares_and_series(self):
        quotes = {"1": make_quote("1", "Aaa", 100.0, 1.0, change_abs=1.0),
                  "2": make_quote("2", "Bbb", 50.0, -2.0, change_abs=-1.0),
                  "3": make_quote("3", "Ccc", 10.0, 0.0, change_abs=0.0)}
        cfg = {"instruments": [self.inst("1", qty=10, cost=80), self.inst("2", qty=20, cost=60),
                               self.inst("3", qty=None)]}
        with mock.patch.object(av, "fetch_quote", side_effect=lambda k, i, n, v, max_age=0: quotes[i]), \
             mock.patch.object(av, "fx_rate", return_value=1.0):
            pf = portfolio.build(cfg, "year")
        self.assertEqual([p.inst["id"] for p in pf.positions], ["1", "2"])        # unheld one excluded
        self.assertEqual(round(pf.value), 2000)
        self.assertEqual(round(pf.cost), 2000)
        self.assertEqual(round(pf.total), 0)
        self.assertTrue(pf.cost_complete)
        self.assertAlmostEqual(sum(p.share for p in pf.positions), 1.0)
        self.assertEqual(round(pf.day), -10)
        self.assertIsNotNone(pf.series)
        self.assertEqual(pf.baseline_label, "COST")
        self.assertAlmostEqual(float(pf.series.iloc[-1]), 2000.0, places=3)
        data = portfolio.to_json(pf)
        json.dumps(data)                                                         # serialisable
        self.assertEqual(len(data["positions"]), 2)

    def test_build_partial_cost_and_failed_fetch(self):
        quotes = {"1": make_quote("1", "Aaa", 100.0)}

        def fake(kind, oid, name, v, max_age=0):
            if oid not in quotes:
                raise OSError("down")
            return quotes[oid]
        cfg = {"instruments": [self.inst("1", qty=10), self.inst("2", name="Broken", qty=5)]}
        with mock.patch.object(av, "fetch_quote", side_effect=fake), mock.patch.object(av, "fx_rate", return_value=1.0):
            pf = portfolio.build(cfg, "month")
        self.assertEqual(pf.skipped, ["Broken"])
        self.assertIsNone(pf.total)                       # no cost entered anywhere
        self.assertFalse(pf.cost_complete)
        self.assertEqual(pf.baseline_label, views.VIEWS["month"]["ago"])


class RendererTests(unittest.TestCase):
    def check(self, img):
        self.assertEqual(img.size, (800, 480))
        self.assertIsNotNone(img.getbbox())

    def position(self, q, **inst):
        base = {"id": q.oid, "kind": q.kind, "name": "", "views": ["year"], "enabled": True,
                "qty": 150, "cost": 80.0, "cost_total_sek": None}
        with mock.patch.object(av, "fx_rate", return_value=1.0):
            return portfolio.position_for(dict(base, **inst), q)

    def test_every_view_renders_with_and_without_holding(self):
        q = make_quote()
        for v in views.VIEW_ORDER:
            with self.subTest(view=v):
                self.check(renderer.render_focus(q, v, "http://192.168.1.50"))
                self.check(renderer.render_focus(q, v, None, self.position(q)))

    def test_fund_without_day_series_and_missing_data(self):
        fund = make_quote(oid="9", name="Avanza Zero", kind="fund", change_abs=None, hist={})
        fund.charts.pop("today", None)
        self.check(renderer.render_focus(fund, "year", "http://x"))
        empty = make_quote()
        empty.charts.clear()
        for v in ("year", "month", "day"):
            self.check(renderer.render_focus(empty, v, None))                    # no charts at all

    def test_index_has_no_currency_and_closed_market(self):
        q = make_quote(kind="index", name="OMX")
        q.market_status = "CLOSED"
        self.check(renderer.render_focus(q, "day", None))

    def test_portfolio_screens_and_paging(self):
        cfg_inst = []
        quotes = {}
        for n in range(12):
            oid = str(100 + n)
            quotes[oid] = make_quote(oid, f"Holding number {n} long name", 50.0 + n, (-1) ** n * 1.5, change_abs=0.5)
            cfg_inst.append({"id": oid, "kind": "stock", "name": "", "views": ["year"], "enabled": True,
                             "qty": 10 + n, "cost": 40.0, "cost_total_sek": None})
        with mock.patch.object(av, "fetch_quote", side_effect=lambda k, i, nm, v, max_age=0: quotes[i]), \
             mock.patch.object(av, "fx_rate", return_value=1.0):
            pf = portfolio.build({"instruments": cfg_inst}, "year")
        self.assertEqual(renderer.table_pages(pf), 2)
        self.check(renderer.render_portfolio_summary(pf, "http://192.168.1.50"))
        self.check(renderer.render_portfolio_table(pf, "http://192.168.1.50", 0))
        self.check(renderer.render_portfolio_table(pf, "http://192.168.1.50", 1))
        pf.series = None                                                          # no history -> still renders
        pf.total = pf.total_pct = None
        self.check(renderer.render_portfolio_summary(pf, None))

    def test_info_and_splash(self):
        self.check(renderer.render_info("NO DATA", ["a", "b"], "http://x"))
        self.check(renderer.render_splash(None))


class StateTests(unittest.TestCase):
    def test_wait_times_out(self):
        s = State()
        t = time.monotonic()
        self.assertIsNone(s.wait(0.2))
        self.assertGreaterEqual(time.monotonic() - t, 0.19)

    def test_reload_and_skip_wake_immediately(self):
        s = State()
        threading.Timer(0.1, s.request_reload).start()
        t = time.monotonic()
        self.assertEqual(s.wait(5), "reload")
        self.assertLess(time.monotonic() - t, 1.0)
        s.request_skip()
        self.assertEqual(s.wait(5), "next")
        s.request_skip(); s.request_reload()
        self.assertEqual(s.wait(5), "reload")                                      # reload wins, skip cleared
        self.assertIsNone(s.wait(0.05))

    def test_publish_png(self):
        s = State()
        s.publish(renderer.render_splash("x"), {"kind": "splash"})
        png, cur = s.snapshot()
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(cur["kind"], "splash")


class WebTests(TempSettings):
    def setUp(self):
        super().setUp()
        self.state = State()
        self.state.publish(renderer.render_splash("x"), {"kind": "splash", "name": "Start", "view": ""})
        handler = type("H", (webserver.Handler,), {"state": self.state})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, path, body=None, headers=None, ctype="application/json", method=None):
        h = dict(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            if ctype:
                h["Content-Type"] = ctype
        r = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_index_and_state(self):
        code, body, hdr = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Aktiekurser", body)
        self.assertEqual(hdr["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", hdr["Content-Security-Policy"])
        code, body, _ = self.req("/api/state")
        d = json.loads(body)
        self.assertEqual(code, 200)
        self.assertNotIn("password", d["settings"])
        self.assertFalse(d["settings"]["password_set"])
        self.assertEqual([v["key"] for v in d["views"]], views.VIEW_ORDER)
        code, body, hdr = self.req("/api/current.png")
        self.assertEqual((code, hdr["Content-Type"]), (200, "image/png"))
        self.assertEqual(self.req("/nope")[0], 404)

    def test_save_settings_triggers_reload(self):
        payload = {"seconds_per_view": 15, "show_ip": True, "portfolio": {"summary": True, "table": True, "period": "year"},
                   "instruments": [{"id": "5247", "kind": "stock", "views": ["day", "month", "year"], "qty": 100, "cost": "330,5"}]}
        code, body, _ = self.req("/api/settings", payload)
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body)["settings"]["instruments"][0]["cost"], 330.5)
        self.assertEqual(self.state.wait(1), "reload")
        self.assertEqual(settings.get()["seconds_per_view"], 15)

    def test_invalid_settings_rejected_and_not_saved(self):
        before = settings.get()
        code, body, _ = self.req("/api/settings", {"seconds_per_view": 1, "instruments": []})
        self.assertEqual(code, 400)
        self.assertIn("Sekunder", json.loads(body)["error"])
        self.assertEqual(settings.get(), before)
        self.assertIsNone(self.state.wait(0.05))                                  # no reload requested

    def test_writes_require_json_content_type_and_size_limit(self):
        code, _, _ = self.req("/api/settings", b"seconds_per_view=10", ctype="application/x-www-form-urlencoded")
        self.assertEqual(code, 415)                                               # blocks cross-site form posts
        code, _, _ = self.req("/api/settings", b"{" + b" " * (webserver.MAX_BODY + 10) + b"}")
        self.assertEqual(code, 413)
        code, _, _ = self.req("/api/settings", b"{not json")
        self.assertEqual(code, 400)

    def test_next(self):
        self.assertEqual(self.req("/api/next", {})[0], 200)
        self.assertEqual(self.state.wait(1), "next")

    def test_search_and_portfolio_endpoints(self):
        with mock.patch.object(av, "search", return_value=[{"id": "5240", "name": "Ericsson B"}]) as m:
            code, body, _ = self.req("/api/search?q=erics")
            self.assertEqual(json.loads(body)["results"][0]["id"], "5240")
            m.assert_called_once_with("erics")
            code, body, _ = self.req("/api/search?q=e")                           # too short: no upstream call
            self.assertEqual(json.loads(body)["results"], [])
            self.assertEqual(m.call_count, 1)
        self.assertEqual(self.req("/api/portfolio?view=day")[0], 400)             # no intraday for the portfolio
        with mock.patch.object(portfolio, "build", return_value=portfolio.Portfolio()):
            code, body, _ = self.req("/api/portfolio?view=month")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["view"], "year")                         # default Portfolio().view
        with mock.patch.object(av, "search", side_effect=OSError("boom")):
            code, body, _ = self.req("/api/search?q=ericsson")
        self.assertEqual(code, 502)

    def auth(self, pw, user="x"):
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}

    def test_password_protection(self):
        code, body, _ = self.req("/api/password", {"password": "hemligt1"})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["password_set"])
        for path in ("/", "/api/state", "/api/current.png"):
            code, _, hdr = self.req(path)
            self.assertEqual(code, 401, path)
            self.assertIn("Basic", hdr["WWW-Authenticate"])
        self.assertEqual(self.req("/api/state", headers=self.auth("fel"))[0], 401)
        self.assertEqual(self.req("/api/settings", {"instruments": []})[0], 401)  # writes locked too
        self.assertEqual(self.req("/api/state", headers=self.auth("hemligt1"))[0], 200)
        self.assertEqual(self.req("/api/state", headers=self.auth("hemligt1", user="annat"))[0], 200)
        self.assertEqual(self.req("/api/state", headers=self.auth("hemligt1"))[0], 200)   # cached verify path
        code, _, _ = self.req("/api/settings", {"instruments": []}, headers=self.auth("hemligt1"))
        self.assertEqual(code, 200)
        # remove the password again
        code, body, _ = self.req("/api/password", {"password": ""}, headers=self.auth("hemligt1"))
        self.assertFalse(json.loads(body)["password_set"])
        self.assertEqual(self.req("/api/state")[0], 200)
        self.assertEqual(self.req("/api/state", headers=self.auth("hemligt1"))[0], 200)


if __name__ == "__main__":
    unittest.main()
