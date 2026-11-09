"""trader-scanner tests (Borsa İstanbul).

All bar data is SYNTHETIC, built by tests/bist_fixtures.py (not market data):
Yahoo v8 / spark JSON for `.IS` symbols, Istanbul time (UTC+3, no DST),
continuous trading 10:00-18:00 TSİ (12:30 on half days), prices on the BIST
tick grid. Nothing here touches the network.
"""

import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _load import load, ROOT  # noqa: E402
from bist_fixtures import chart, daily, orb_day, spark, trading_days, ts  # noqa: E402

scan = load("trader-scanner", "scan")

DT = datetime.datetime
D = datetime.date
DAY = "2026-11-09"          # a Monday
HALF = "2026-10-28"         # Cumhuriyet Bayramı arefesi: continuous trading ends 12:30
HOLIDAY = "2026-10-29"      # Cumhuriyet Bayramı
SATURDAY = "2026-11-07"
UNTIL = datetime.time(17, 30)
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def series(doc, sym="X"):
    return scan.parse_chart(doc, sym)


def closes_only(s):
    """A v8 series as spark gives it: closes, no highs or lows."""
    return dict(s, h=None, l=None)


# ---- daily closes (oldest first, 40 sessions), hand-checked against find_setups' rules
def c_breakout():
    return [100.0 + i for i in range(40)]           # 139 > 138, RSI 100, run 8 % (no flag)


def c_flag():
    # flat, then up 15.5 % over 10 sessions, then 5 closes inside 0.7 %, under the 115.5 high
    return [100.0] * 25 + [100 + 1.55 * k for k in range(1, 11)] + [115.0, 114.8, 114.6, 114.4, 114.2]


def c_oversold():
    return [200.0 - 2 * i for i in range(40)]       # RSI 0, still falling


def c_bounce():
    c = [200.0 - 2 * i for i in range(39)]
    return c + [c[-1] + 15]                         # RSI 0 yesterday, 36.6 today: up, no longer oversold


def c_reclaim():
    c = [100.0 + (0.5 if i % 2 else 0) for i in range(20)]
    x = 99.5
    for k in range(19):
        x += -0.6 if k % 2 == 0 else 0.3
        c.append(round(x, 2))
    return c + [99.3]                               # 96.2 under its MA20, 99.3 over 97.84, under the 100.5 high


def daily_doc(sym, closes, end_day="2026-11-06", price=None, volume=2000000):
    doc = daily(sym, closes, end_day)
    meta = doc["chart"]["result"][0]["meta"]
    meta["regularMarketPrice"] = closes[-1] if price is None else price
    meta["regularMarketVolume"] = volume
    return doc


def thyao_orb(**kw):
    args = dict(or_low=305.25, or_high=310.75, trigger_close=312.50)
    args.update(kw)
    return orb_day("THYAO", kw.pop("day", DAY), **{k: v for k, v in args.items() if k != "day"})


def write(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(doc, str):
            fh.write(doc)
        else:
            json.dump(doc, fh)


class Time(unittest.TestCase):
    def test_istanbul_clock_has_no_dst(self):
        for utc in (DT(2026, 1, 15, 12), DT(2026, 3, 29, 2), DT(2026, 7, 1), DT(2026, 10, 25, 1), DT(2026, 12, 1)):
            self.assertEqual(scan.eastern_offset(utc), 3)
        self.assertEqual(scan.to_eastern(DT(2026, 11, 9, 7, 0)), DT(2026, 11, 9, 10, 0))
        self.assertEqual(scan.eastern_to_utc(DT(2026, 7, 1, 10, 0)), DT(2026, 7, 1, 7, 0))
        self.assertEqual(scan.ts_to_eastern(ts(DAY, "10:00")), DT(2026, 11, 9, 10, 0))
        self.assertEqual(scan.ts_to_eastern(ts("2026-07-01", "17:55")), DT(2026, 7, 1, 17, 55))

    def test_parse_now_and_labels(self):
        self.assertEqual(scan.parse_now("2026-11-09 10:30"), DT(2026, 11, 9, 7, 30))   # bare = TSİ
        self.assertEqual(scan.parse_now("2026-07-01 10:30"), DT(2026, 7, 1, 7, 30))     # summer too
        self.assertEqual(scan.parse_now("2026-11-09T07:30:00Z"), DT(2026, 11, 9, 7, 30))
        self.assertEqual(scan.parse_now("2026-11-09T10:30:00+03:00"), DT(2026, 11, 9, 7, 30))
        self.assertEqual(scan.clock_label(DT(2026, 11, 9, 10, 30)), "10:30")
        self.assertEqual(scan.clock_label(datetime.time(17, 5)), "17:05")
        self.assertEqual(scan.day_label(D(2026, 11, 9)), "Mon 2026-11-09")
        self.assertEqual(scan.parse_hhmm("10:30"), datetime.time(10, 30))
        for bad in ("25:00", "10:61", "1030", "", None):
            with self.assertRaises(scan.ScanError):
                scan.parse_hhmm(bad)
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_now("yarın")
        self.assertIn("Istanbul time", str(cm.exception))

    def test_calendar(self):
        self.assertEqual(scan.trading_day(D(2026, 11, 9)), (True, ""))
        self.assertEqual(scan.trading_day(D(2026, 11, 7)), (False, "weekend"))
        self.assertEqual(scan.trading_day(D(2026, 10, 29)), (False, "BIST tatili: Cumhuriyet Bayramı"))
        self.assertEqual(scan.trading_day(D(2026, 5, 27))[1], "BIST tatili: Kurban Bayramı 1. gün")
        self.assertEqual(scan.trading_day(D(2026, 11, 26)), (True, ""))     # US Thanksgiving: BIST trades
        self.assertEqual(scan.trading_day(D(2026, 7, 3)), (True, ""))       # US July 3rd too
        self.assertEqual(scan.session(D(2026, 11, 9)), (datetime.time(10, 0), datetime.time(18, 0)))
        self.assertEqual(scan.session(D(2026, 10, 28)), (datetime.time(10, 0), datetime.time(12, 30)))
        self.assertIsNone(scan.session(D(2026, 10, 29)))
        self.assertEqual(scan.SESSION_BARS, 96)
        # the fixture generator and the script agree on the calendar
        for d in trading_days("2026-01-01", "2027-12-31"):
            self.assertTrue(scan.trading_day(D.fromisoformat(d))[0], d)
        self.assertEqual(set(scan.BIST_HALF_DAYS), {"2026-03-19", "2026-05-26", "2026-10-28", "2027-03-08",
                                                     "2027-10-28"})


class Symbols(unittest.TestCase):
    def test_yahoo_mapping_and_urls(self):
        self.assertEqual(scan.to_yahoo("thyao"), "THYAO.IS")
        self.assertEqual(scan.to_yahoo("XU100"), "XU100.IS")
        self.assertEqual(scan.to_yahoo("THYAO.IS"), "THYAO.IS")
        self.assertEqual(scan.to_yahoo("^XU100"), "^XU100")
        self.assertEqual(scan.from_yahoo("thyao.is"), "THYAO")
        self.assertEqual(scan.from_yahoo("ASELS"), "ASELS")
        self.assertEqual(scan.from_yahoo(None), "")
        self.assertEqual(scan.spark_path(["THYAO", "asels", "GARAN.IS"], "3mo", "1d"),
                         "/v7/finance/spark?symbols=THYAO.IS,ASELS.IS,GARAN.IS&range=3mo&interval=1d")
        self.assertEqual(scan.chart_path("THYAO", "1d", "5m"), "/v8/finance/chart/THYAO.IS?range=1d&interval=5m")

    def test_symbol_files(self):
        d = tempfile.mkdtemp()
        try:
            txt = os.path.join(d, "list.txt")
            write(txt, "# my list\nTHYAO|Türk Hava Yolları\nasels\n\nTHYAO\nGARAN.IS\n")
            self.assertEqual(scan.symbols_from_file(txt), ["THYAO", "ASELS", "THYAO", "GARAN.IS"])
            js = os.path.join(d, "wl.json")
            write(js, {"hits": [{"symbol": "thyao"}, {"symbol": "ASELS"}]})
            self.assertEqual(scan.symbols_from_file(js), ["THYAO", "ASELS"])
            arr = os.path.join(d, "arr.json")
            write(arr, ["GARAN", {"symbol": "akbnk"}])
            self.assertEqual(scan.symbols_from_file(arr), ["GARAN", "AKBNK"])
            bad = os.path.join(d, "bad.json")
            write(bad, "{nope")
            with self.assertRaises(scan.ScanError):
                scan.symbols_from_file(bad)
            with self.assertRaises(scan.ScanError):
                scan.symbols_from_file(os.path.join(d, "missing.txt"))

            class A:
                symbols, watchlist, universe, limit = None, txt, None, None
            self.assertEqual(scan.read_symbols(A()), ["THYAO", "ASELS", "GARAN"])   # deduped, .IS dropped
            A.watchlist, A.symbols, A.limit = None, "thyao, asels.is GARAN", 2
            self.assertEqual(scan.read_symbols(A()), ["THYAO", "ASELS"])
        finally:
            shutil.rmtree(d)


class Universe(unittest.TestCase):
    def test_build_universe_rules(self):
        text = ("# comment\n\nTHYAO|Türk Hava Yolları\nasels.is|Aselsan   Elektronik\nA1CAP|A1 Capital\n"
                "XU100|BIST 100\nGARAN|Garanti\nGARAN|Garanti again\nTHY-AO|bad\nAB|too short\n"
                "TOOLONGX|too long\n1ABC|starts with a digit\n")
        kept, counts = scan.build_universe(text)
        self.assertEqual(kept, [("A1CAP", "A1 Capital"), ("ASELS", "Aselsan Elektronik"), ("GARAN", "Garanti"),
                                ("THYAO", "Türk Hava Yolları"), ("XU100", "BIST 100")])
        self.assertEqual(counts, {"rows": 10, "not a BIST code": 4, "duplicate": 1})

    def test_the_shipped_list(self):
        with open(scan.BIST_UNIVERSE, encoding="utf-8") as fh:
            text = fh.read()
        kept, counts = scan.build_universe(text)
        codes = [c for c, _ in kept]
        self.assertEqual(scan.BIST_UNIVERSE, os.path.join(ROOT, "trader-scanner", "bist_universe.txt"))
        self.assertGreaterEqual(len(kept), 90)
        self.assertEqual(counts["not a BIST code"], 0)
        self.assertEqual(counts["duplicate"], 0)
        for c in ("THYAO", "ASELS", "GARAN", "AKBNK"):
            self.assertIn(c, codes)
        self.assertTrue(all(name for _, name in kept))
        self.assertFalse(any(c.endswith(".IS") for c in codes))

    def test_cli_universe_default_and_from(self):
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "trader", "universe.txt")
            code, text, _ = run(["universe", "--out", out])
            self.assertEqual(code, 0)
            self.assertIn("Kept", text)
            self.assertIn("BIST codes", text)
            with open(out, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            self.assertIn("THYAO|Türk Hava Yolları", lines)
            self.assertIn("Kept %d BIST codes." % len(lines), text)
            self.assertEqual(lines, sorted(lines))
            self.assertTrue(all("|" in ln and not ln.startswith("#") for ln in lines))

            src = os.path.join(d, "mine.txt")
            write(src, "﻿# mine\nTHYAO|Türk Hava Yolları\nASELS|Aselsan\nBAD-1|x\n")
            code, text, _ = run(["universe", "--from", src, "--out", out, "--json"])
            self.assertEqual(code, 0)
            doc = json.loads(text)
            self.assertEqual((doc["count"], doc["rows"], doc["dropped"]), (2, 3, {"not a BIST code": 1}))
            with open(out, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "ASELS|Aselsan\nTHYAO|Türk Hava Yolları\n")

            code, _, err = run(["universe", "--from", os.path.join(d, "nope.txt"), "--out", out])
            self.assertEqual(code, 1)
            self.assertIn("no BIST share list", err)
            empty = os.path.join(d, "empty.txt")
            write(empty, "# nothing\n")
            code, _, _ = run(["universe", "--from", empty, "--no-write"])
            self.assertEqual(code, 1)
        finally:
            shutil.rmtree(d)


class Spark(unittest.TestCase):
    def test_is_symbols_come_back_bare(self):
        docs = {"THYAO": chart("THYAO", trading_days("2026-11-02", DAY), seed=1, interval=5),
                "XU100": chart("XU100", [DAY], seed=2, start=10500.0)}
        data = scan.parse_spark(json.dumps(spark(docs)))
        self.assertEqual(sorted(data), ["THYAO", "XU100"])
        self.assertEqual(data["THYAO"]["meta"]["symbol"], "THYAO.IS")
        self.assertEqual(len(data["THYAO"]["t"]), 6 * 96)
        # with a wanted map (what fetch_spark passes), our own spelling wins
        data = scan.parse_spark(spark(docs), {"THYAO.IS": "thyao-mine"})
        self.assertIn("thyao-mine", data)
        self.assertIn("XU100", data)

    def test_spark_errors(self):
        too_many = '{"spark":{"result":null,"error":{"code":"Bad Request",' \
                   '"description":"Number of symbols needs to be less than or equal to 20"}}}'
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_spark(too_many)
        self.assertIn("less than or equal to 20", str(cm.exception))
        self.assertEqual(scan.parse_spark('{"spark":{"result":[],"error":null}}'), {})
        self.assertEqual(scan.parse_spark({"spark": {"result": [{"symbol": "NOPE.IS", "response": []}]}}), {})
        with self.assertRaises(scan.ScanError):
            scan.parse_spark('{"chart":{}}')

    def test_parse_chart(self):
        s = series(thyao_orb(), "THYAO")
        self.assertEqual(len(s["t"]), 96)
        self.assertEqual(s["meta"]["exchangeTimezoneName"], "Europe/Istanbul")
        with self.assertRaises(scan.ScanError) as cm:
            scan.parse_chart({"chart": {"result": None, "error": {"description": "No data found"}}}, "NOPE")
        self.assertIn("no data for NOPE: No data found", str(cm.exception))
        with self.assertRaises(scan.ScanError):
            scan.parse_chart("<html>", "THYAO")
        short = {"chart": {"result": [{"meta": {}, "timestamp": [1, 2, 3],
                                       "indicators": {"quote": [{"close": [1.0]}]}}]}}
        self.assertEqual(scan.parse_chart(short)["c"], [1.0, None, None])   # padded, never IndexError


class Network(unittest.TestCase):
    """429s and 5xx without the network."""

    SYMS = ["THYAO", "ASELS", "GARAN", "AKBNK", "BIMAS", "EREGL", "FROTO", "KCHOL", "SAHOL", "SISE", "TUPRS",
            "YKBNK", "ISCTR", "PGSUS", "TCELL", "TTKOM", "KOZAL", "PETKM", "ARCLK"]

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = scan.http_get, scan.SLEEP
        scan.SLEEP = self.sleeps.append
        docs = {s: daily_doc(s, c_breakout()) for s in self.SYMS}
        self.body = json.dumps(spark(docs))

    def tearDown(self):
        scan.http_get, scan.SLEEP = self._get, self._sleep

    def answers(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split("/v")[0])
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return self.body
        scan.http_get = fake

    def test_429_falls_back_then_waits_once(self):
        self.answers([429, 429, 200])
        text = scan.yahoo_get("/v7/finance/spark?symbols=THYAO.IS&range=3mo&interval=1d")
        self.assertEqual(text, self.body)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com",
                                      "https://query2.finance.yahoo.com"])
        self.assertEqual(self.sleeps, [scan.BACKOFF])

    def test_5xx_goes_to_query1_without_waiting(self):
        self.answers([503, 200])
        scan.yahoo_get("/v8/finance/chart/THYAO.IS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [])

    def test_a_404_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError) as cm:
            scan.yahoo_get("/v8/finance/chart/NOPE.IS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("HTTP 404", str(cm.exception))

    def test_a_404_with_yahoo_json_is_returned(self):
        body = b'{"chart":{"result":null,"error":{"code":"Not Found","description":"No data found"}}}'

        def fake(url, timeout=20):
            raise urllib.error.HTTPError(url, 404, "x", {}, io.BytesIO(body))
        scan.http_get = fake
        text = scan.yahoo_get("/v8/finance/chart/NOPE.IS?range=1d&interval=5m")
        with self.assertRaises(scan.ScanError):
            scan.parse_chart(text, "NOPE")

    def test_network_down(self):
        def fake(url, timeout=20):
            self.calls.append(url)
            raise urllib.error.URLError("no route")
        scan.http_get = fake
        with self.assertRaises(scan.ScanError) as cm:
            scan.yahoo_get("/v7/finance/spark?symbols=THYAO.IS&range=1d&interval=5m")
        self.assertIn("no route", str(cm.exception))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.sleeps, [])

    def test_429_for_good_is_reported_as_partial(self):
        self.answers([200] + [429] * 10)
        syms = self.SYMS + ["ZZZZZ"] + ["X%04d" % i for i in range(20)]
        data, failed = scan.fetch_spark(syms, "3mo", "1d", 0.3)
        self.assertEqual(len(data), 19)
        self.assertEqual(failed["ZZZZZ"], "no data from Yahoo")    # Yahoo silently drops a code it does not know
        self.assertEqual(len(failed), 21)
        self.assertIn("HTTP 429", failed["X0000"])
        self.assertEqual(self.sleeps, [0.3, scan.BACKOFF])           # a pause between batches, one backoff
        hits, counts, _ = scan.scan_daily(data, syms, scan.parse_now("2026-11-09 09:00"), 1, 1000000)
        self.assertEqual((counts["requested"], counts["fetched"], counts["scanned"]), (40, 19, 19))
        self.assertEqual(len(hits), 19)


class Daily(unittest.TestCase):
    EXPECT = {"THYAO": ["breakout_20d"], "ASELS": ["bull_flag"], "GARAN": ["reclaim_ma20"],
              "AKBNK": ["oversold"], "SISE": ["oversold_bounce"]}

    def setUp(self):
        self.data = {"THYAO": series(daily_doc("THYAO", c_breakout(), volume=9000000)),
                     "ASELS": series(daily_doc("ASELS", c_flag(), volume=8000000)),
                     "GARAN": series(daily_doc("GARAN", c_reclaim(), volume=7000000)),
                     "AKBNK": series(daily_doc("AKBNK", c_oversold(), volume=6000000)),
                     "SISE": series(daily_doc("SISE", c_bounce(), volume=5000000)),
                     "FLAT": series(daily_doc("FLAT", [50.0] * 40, volume=5000000))}
        self.syms = sorted(self.data)

    def test_one_chart_per_setup(self):
        hits, counts, last = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-09 09:00"), 1, 1000000)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.EXPECT)
        self.assertEqual(last, D(2026, 11, 6))
        self.assertEqual(counts, {"requested": 6, "fetched": 6, "illiquid": 0, "short": 0, "scanned": 6, "hits": 5})
        # most lira traded first (price x volume)
        # 139 x 9M, 114.2 x 8M, 122 x 6M, 99.3 x 7M, 139 x 5M
        self.assertEqual([h["symbol"] for h in hits], ["THYAO", "ASELS", "AKBNK", "GARAN", "SISE"])
        th = hits[0]
        self.assertEqual((th["price"], th["rsi"], th["close_date"], th["volume"]), (139.0, 100.0, "2026-11-06",
                                                                                   9000000))

    def test_the_holiday_is_skipped_in_the_bars(self):
        d = series(daily_doc("THYAO", c_breakout(), end_day="2026-10-30"))
        days = [x for x, _ in scan.daily_closes(d, scan.parse_now("2026-11-02 09:00"))]
        self.assertNotIn(D(2026, 10, 29), days)
        self.assertEqual(days[-2:], [D(2026, 10, 28), D(2026, 10, 30)])

    def test_todays_bar_waits_for_the_close(self):
        d = series(daily_doc("THYAO", c_breakout(), end_day=DAY))
        self.assertEqual(scan.daily_closes(d, scan.parse_now("2026-11-09 17:59"))[-1][0], D(2026, 11, 6))
        self.assertEqual(scan.daily_closes(d, scan.parse_now("2026-11-09 18:00"))[-1], (D(2026, 11, 9), 139.0))
        self.assertEqual(scan.daily_closes(d, scan.parse_now("2026-11-09 09:30"))[-1][0], D(2026, 11, 6))
        # a bar dated after the clock never counts
        self.assertEqual(scan.daily_closes(d, scan.parse_now("2026-11-05 20:00"))[-1][0], D(2026, 11, 5))

    def test_half_day_bar_completes_at_1230(self):
        d = series(daily_doc("THYAO", c_breakout(), end_day=HALF))
        self.assertEqual(scan.daily_closes(d, scan.parse_now("2026-10-28 12:29"))[-1][0], D(2026, 10, 27))
        self.assertEqual(scan.daily_closes(d, scan.parse_now("2026-10-28 12:30"))[-1][0], D(2026, 10, 28))

    def test_liquidity_filter(self):
        data = dict(self.data)
        data["THYAO"] = series(daily_doc("THYAO", c_breakout(), volume=900000))           # under 1M shares
        data["ASELS"] = series(daily_doc("ASELS", [x / 200 for x in c_flag()], volume=5000000))   # under 1 TL
        hits, counts, _ = scan.scan_daily(data, self.syms, scan.parse_now("2026-11-09 09:00"), 1, 1000000)
        self.assertEqual(sorted(h["symbol"] for h in hits), ["AKBNK", "GARAN", "SISE"])
        self.assertEqual((counts["illiquid"], counts["scanned"]), (2, 4))
        hits, counts, _ = scan.scan_daily(data, self.syms, scan.parse_now("2026-11-09 09:00"), 0, 0)
        self.assertEqual(len(hits), 5)

    def test_short_and_empty_charts(self):
        s = series(daily_doc("NEW", [10.0] * 10, volume=5000000))
        hits, counts, last = scan.scan_daily({"NEW": s, "E": {"t": [], "c": [], "meta": {}}}, ["NEW", "E", "GONE"],
                                             scan.parse_now("2026-11-09 09:00"), 1, 1000000)
        self.assertEqual(hits, [])
        self.assertEqual((counts["fetched"], counts["short"], counts["illiquid"], counts["scanned"]), (2, 1, 1, 0))
        self.assertIsNone(last)

    def test_rsi_math(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        self.assertEqual(scan.rsi([float(x) for x in range(1, 40)]), 100.0)
        self.assertEqual(scan.rsi([float(x) for x in range(40, 1, -1)]), 0.0)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)  # Wilder's textbook example


class Orb(unittest.TestCase):
    def setUp(self):
        self.day = D(2026, 11, 9)
        self.thyao = series(thyao_orb(), "THYAO")

    def window(self, now, at=None, until=UNTIL, or_minutes=15):
        return scan.orb_window(scan.parse_now(now), at, or_minutes, until)

    def test_a_breakout_on_the_1000_to_1015_range(self):
        day, cut = self.window("2026-11-09 10:30")
        self.assertEqual((day, cut), (self.day, DT(2026, 11, 9, 10, 30)))
        hits, counts, failed = scan.scan_orb({"THYAO": self.thyao}, ["THYAO"], day, cut, 15)
        self.assertEqual(hits, [{"symbol": "THYAO", "setups": ["orb"], "price": 312.5, "at": "10:30",
                                 "bar_start": "10:25", "bar_index": 5, "or_high": 310.75, "or_low": 305.25,
                                 "pct_over": 0.56}])
        self.assertEqual((counts["scanned"], counts["candidates"], counts["confirmed"]), (1, 1, 1))
        self.assertEqual(failed, {})

    def test_the_bar_counts_only_once_complete(self):
        hits, _, _ = scan.scan_orb({"THYAO": self.thyao}, ["THYAO"], self.day, DT(2026, 11, 9, 10, 29), 15)
        self.assertEqual(hits, [])
        bars = scan.session_bars(self.thyao, self.day, DT(2026, 11, 9, 10, 29))
        self.assertEqual([b["start"].strftime("%H:%M") for b in bars],
                         ["10:00", "10:05", "10:10", "10:15", "10:20"])
        # later in the day the first close above the range is still the hit
        hits, _, _ = scan.scan_orb({"THYAO": series(thyao_orb(after="up"))}, ["THYAO"], self.day,
                                   DT(2026, 11, 9, 15, 0), 15)
        self.assertEqual((hits[0]["at"], hits[0]["price"]), ("10:30", 312.5))

    def test_a_close_at_the_range_high_is_not_a_breakout(self):
        s = series(thyao_orb(trigger_close=310.75))
        hits, counts, _ = scan.scan_orb({"T": s}, ["T"], self.day, DT(2026, 11, 9, 12, 0), 15)
        self.assertEqual(hits, [])
        self.assertEqual(counts["candidates"], 1)          # stage 1 only says "maybe"
        s = series(thyao_orb(trigger_close=300.0, after="down"))
        hits, counts, _ = scan.scan_orb({"T": s}, ["T"], self.day, DT(2026, 11, 9, 12, 0), 15)
        self.assertEqual(hits, [])
        self.assertEqual(counts["candidates"], 1)          # 10:15 and 10:20 sit at the range's top close

    def test_auction_prints_live_tick_and_nulls(self):
        doc = thyao_orb()
        r = doc["chart"]["result"][0]
        q = r["indicators"]["quote"][0]

        def add(unix, c):
            r["timestamp"].append(unix)
            for k in ("open", "high", "low", "close"):
                q[k].append(c)
            q["volume"].append(10)
        add(ts(DAY, "09:55"), 400.0)        # opening auction: not a continuous bar
        add(ts(DAY, "18:00"), 400.0)        # the closing-auction print
        add(ts(DAY, "17:58") + 7, 400.0)    # Yahoo's live tick, off the 5-minute grid
        q["close"][20] = None               # a null close
        s = series(doc)
        bars = scan.session_bars(s, self.day, DT(2026, 11, 9, 18, 10))
        self.assertEqual(len(bars), 95)                     # 96 slots, one null
        self.assertEqual(bars[0]["start"], DT(2026, 11, 9, 10, 0))
        self.assertEqual(bars[-1]["start"], DT(2026, 11, 9, 17, 55))
        self.assertTrue(all(b["c"] != 400.0 for b in bars))
        self.assertFalse(scan.usable(bars, self.day, DT(2026, 11, 9, 18, 0), 3))   # the null is a hole
        bars = scan.session_bars(s, self.day, DT(2026, 11, 9, 11, 0))
        self.assertTrue(scan.usable(bars, self.day, DT(2026, 11, 9, 11, 0), 3))

    def test_a_missing_bar_makes_a_chart_unusable(self):
        doc = thyao_orb()
        r = doc["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
        for k in ("open", "high", "low", "close", "volume"):
            del q[k][4]
        del r["timestamp"][4]                              # the 10:20 bar is gone (a halt)
        hits, counts, _ = scan.scan_orb({"T": series(doc)}, ["T"], self.day, DT(2026, 11, 9, 10, 30), 15)
        self.assertEqual((hits, counts["unusable"], counts["scanned"]), ([], 1, 0))
        # a chart from another day is unusable too
        hits, counts, _ = scan.scan_orb({"T": series(thyao_orb(day="2026-11-06"))}, ["T"], self.day,
                                        DT(2026, 11, 9, 10, 30), 15)
        self.assertEqual(counts["unusable"], 1)

    def test_price_floor_and_sorting(self):
        data = {"THYAO": self.thyao,
                "ASELS": series(orb_day("ASELS", DAY, 148.0, 150.0, 151.0, trigger_at="10:15")),
                "PENNY": series(orb_day("PENNY", DAY, 0.80, 0.85, 0.90, trigger_at="10:20")),
                "GARAN": series(orb_day("GARAN", DAY, 120.0, 122.0, 121.0))}
        syms = sorted(data)
        hits, counts, _ = scan.scan_orb(data, syms, self.day, DT(2026, 11, 9, 11, 0), 15, 1.0, 0)
        self.assertEqual([(h["symbol"], h["at"]) for h in hits], [("ASELS", "10:20"), ("THYAO", "10:30")])
        self.assertEqual(counts["illiquid"], 1)            # PENNY under 1 TL
        hits, counts, _ = scan.scan_orb(data, syms, self.day, DT(2026, 11, 9, 11, 0), 15, 0, 0)
        self.assertEqual([h["symbol"] for h in hits], ["ASELS", "PENNY", "THYAO"])

    def test_confirm_uses_the_v8_highs_and_the_cap(self):
        full = {"THYAO": self.thyao, "ASELS": series(orb_day("ASELS", DAY, 148.0, 150.0, 151.0)),
                "GARAN": series(orb_day("GARAN", DAY, 120.0, 122.0, 119.0, after="down"))}
        sparky = {k: closes_only(v) for k, v in full.items()}
        self.assertIsNone(scan.orb_hit(scan.session_bars(sparky["THYAO"], self.day, DT(2026, 11, 9, 10, 30)), 3))
        asked = []

        def confirm(sym):
            asked.append(sym)
            return full[sym]
        cut = DT(2026, 11, 9, 10, 30)
        hits, counts, _ = scan.scan_orb(sparky, sorted(full), self.day, cut, 15, 0, 0, confirm)
        self.assertEqual([h["symbol"] for h in hits], ["ASELS", "THYAO"])
        self.assertEqual(sorted(asked), ["ASELS", "GARAN", "THYAO"])
        asked.clear()
        hits, counts, _ = scan.scan_orb(sparky, sorted(full), self.day, cut, 15, 0, 0, confirm, max_confirm=1)
        self.assertEqual(asked, ["THYAO"])                  # the most lira traded so far
        self.assertEqual((counts["confirmed"], counts["unconfirmed"]), (1, 2))
        self.assertEqual([h["symbol"] for h in hits], ["THYAO"])

        def broken(sym):
            raise scan.ScanError("Yahoo answered HTTP 429 for /v8/finance/chart/%s.IS" % sym)
        hits, counts, failed = scan.scan_orb(sparky, ["THYAO"], self.day, cut, 15, 0, 0, broken)
        self.assertEqual(hits, [])
        self.assertIn("429", failed["THYAO"])

    def test_stage_one_is_a_necessary_condition(self):
        cut = DT(2026, 11, 9, 15, 0)
        for seed in range(30):
            s = series(chart("R%d" % seed, [DAY], seed=seed, vol=0.004))
            bars = scan.session_bars(s, self.day, cut)
            self.assertTrue(scan.usable(bars, self.day, cut, 3))
            if scan.orb_hit(bars, 3):
                self.assertTrue(scan.orb_candidate(bars, 3), seed)

    def test_half_day_and_a_30_minute_range(self):
        s = series(orb_day("THYAO", HALF, 305.25, 310.75, 312.5, trigger_at="10:30", or_minutes=30))
        self.assertEqual(len(s["t"]), 30)                  # 10:00 to 12:25
        day = D(2026, 10, 28)
        hits, counts, _ = scan.scan_orb({"T": s}, ["T"], day, DT(2026, 10, 28, 12, 30), 30)
        self.assertEqual(counts["scanned"], 1)
        self.assertEqual((hits[0]["at"], hits[0]["bar_index"]), ("10:35", 6))
        # a cutoff after 12:30 still wants only the bars up to the half-day close
        self.assertTrue(scan.usable(scan.session_bars(s, day, DT(2026, 10, 28, 14, 0)), day,
                                    DT(2026, 10, 28, 14, 0), 6))
        with self.assertRaises(scan.ScanError) as cm:
            self.window("2026-10-28 12:40")
        self.assertIn("closed at 12:30 TSİ", str(cm.exception))
        with self.assertRaises(scan.ScanError) as cm:
            self.window("2026-10-28 15:00", at=datetime.time(12, 40))
        self.assertIn("after the 12:30 close", str(cm.exception))
        with self.assertRaises(scan.ScanError) as cm:
            self.window("2026-10-28 10:30", or_minutes=30)
        self.assertIn("Run orb again after 10:35", str(cm.exception))

    def test_window_messages(self):
        def msg(now, at=None, until=UNTIL, or_minutes=15):
            with self.assertRaises(scan.ScanError) as cm:
                self.window(now, at, until, or_minutes)
            return str(cm.exception)
        self.assertIn("No session on Sat 2026-11-07 (weekend)", msg("2026-11-07 10:30"))
        self.assertIn("BIST tatili: Cumhuriyet Bayramı", msg("2026-10-29 10:30"))
        self.assertIn("BIST tatili: Yılbaşı", msg("2027-01-01 11:00"))
        m = msg("2026-11-09 10:10")
        self.assertIn("opening range runs 10:00 to 10:15", m)
        self.assertIn("Run orb again after 10:20", m)
        self.assertIn("Run orb again after 10:20", msg("2026-11-09 09:45"))   # opening auction time
        self.assertIn("Run orb again after 10:20", msg("2026-11-09 10:19"))
        self.assertEqual(self.window("2026-11-09 10:20")[1], DT(2026, 11, 9, 10, 20))
        self.assertEqual(self.window("2026-11-09 17:30")[1], DT(2026, 11, 9, 17, 30))
        self.assertIn("expired", msg("2026-11-09 17:31"))
        self.assertIn("after 17:30", msg("2026-11-09 17:45"))
        self.assertIn("expired", msg("2026-11-09 11:30", until=datetime.time(11, 0)))
        self.assertIn("closed at 18:00 TSİ", msg("2026-11-09 18:30"))
        self.assertIn("later than now", msg("2026-11-09 10:20", at=datetime.time(10, 30)))
        self.assertIn("after the 18:00 close", msg("2026-11-09 19:00", at=datetime.time(18, 30)))
        self.assertEqual(self.window("2026-11-09 18:30", at=datetime.time(10, 30)),
                         (self.day, DT(2026, 11, 9, 10, 30)))
        self.assertEqual(self.window("2026-11-09 10:30", until=None)[1], DT(2026, 11, 9, 10, 30))
        self.assertIn("multiple of 5", msg("2026-11-09 10:30", or_minutes=7))
        self.assertIn("multiple of 5", msg("2026-11-09 10:30", or_minutes=125))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = os.path.join(self.d, "trader")
        self.wl = os.path.join(self.t, "watchlist.json")
        self.orb_bundle = os.path.join(self.d, "orb-2026-11-09.json")
        write(self.orb_bundle, {
            "THYAO.IS": thyao_orb(),
            "ASELS.IS": orb_day("ASELS", DAY, 148.0, 150.0, 151.0, trigger_at="10:15"),
            "GARAN.IS": orb_day("GARAN", DAY, 120.0, 122.0, 121.0),
            "GONE.IS": {"chart": {"result": None, "error": {"description": "No data found, symbol may be delisted"}}},
        })
        self.daily_bundle = os.path.join(self.d, "daily-2026-11-06.json")
        write(self.daily_bundle, spark({"THYAO": daily_doc("THYAO", c_breakout(), volume=9000000),
                                        "ASELS": daily_doc("ASELS", c_flag(), volume=8000000),
                                        "GARAN": daily_doc("GARAN", c_reclaim(), volume=7000000),
                                        "FLAT": daily_doc("FLAT", [50.0] * 40, volume=5000000)}))

    def tearDown(self):
        shutil.rmtree(self.d)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_daily_orb_end_to_end(self):
        src = os.path.join(self.d, "mine.txt")
        write(src, "THYAO|Türk Hava Yolları\nASELS|Aselsan\nKCHOL|Koç Holding\nPGSUS|Pegasus\n")
        uni = os.path.join(self.t, "universe.txt")
        code, text, _ = run(["universe", "--from", src, "--out", uni])
        self.assertEqual(code, 0)
        self.assertIn("Kept 4 BIST codes", text)

        # daily over that universe: only THYAO and ASELS have charts, so partial
        code, text, _ = run(["daily", "--universe", uni, "--offline", self.daily_bundle, "--now", "2026-11-09 09:00",
                             "--out", self.wl])
        self.assertEqual(code, 0)
        self.assertIn("Daily scan, Mon 2026-11-09 09:00 TSİ, closes through 2026-11-06", text)
        self.assertIn("Fetched 2 of 4 charts (2 failed or had no data)", text)
        self.assertIn("(1 TL+ and 1,000,000+ shares)", text)
        self.assertIn("THYAO      139.00  RSI 100.0  20-day closing-high breakout", text)
        doc = self.read(self.wl)
        self.assertEqual((doc["mode"], doc["universe"], doc["scanned"], doc["failed"], doc["as_of"]),
                         ("daily", 4, 2, 2, "2026-11-06 close"))
        self.assertEqual(doc["hits"][0], {"symbol": "THYAO", "setups": ["breakout_20d"], "price": 139.0,
                                          "close_date": "2026-11-06", "rsi": 100.0, "volume": 9000000})
        self.assertEqual([h["symbol"] for h in doc["hits"]], ["THYAO", "ASELS"])

        # the opening-range scan at 10:30 TSİ
        code, text, err = run(["orb", "--offline", self.orb_bundle, "--now", "2026-11-09 10:30", "--out", self.wl])
        self.assertEqual(code, 0, err)
        self.assertIn("Opening-range breakout scan, Mon 2026-11-09 10:30 TSİ: 15-minute range (10:00 to 10:15)",
                      text)
        self.assertIn("Fetched 3 of 4 charts (1 failed or had no data); 3 usable; 3 candidates", text)
        self.assertIn("2 of 3 charts closed above their opening-range high by 10:30:", text)
        self.assertIn("     10:30  THYAO      312.50  range 305.25 to 310.75  +0.56% over", text)
        self.assertIn(NOTE, text)
        self.assertNotIn("$", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "as_of", "universe", "scanned",
                                              "failed", "source", "or_minutes", "offline")},
                         {"created_by": "trader-scanner", "created_at": "2026-11-09T07:30:00Z", "mode": "orb",
                          "as_of": "2026-11-09 10:30 TSİ", "universe": 4, "scanned": 3, "failed": 1,
                          "source": "yahoo", "or_minutes": 15, "offline": "orb-2026-11-09.json"})
        self.assertEqual([(h["symbol"], h["at"]) for h in doc["hits"]], [("ASELS", "10:20"), ("THYAO", "10:30")])

        # the hits read back as a watchlist, with --at instead of --now (the day comes from the file)
        code, text, _ = run(["orb", "--offline", self.orb_bundle, "--at", "10:30", "--watchlist", self.wl,
                             "--json", "--no-write"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], len(out["hits"]), out["as_of"]),
                         (2, 2, 2, "2026-11-09 10:30 TSİ"))

    def test_plain_messages_and_exit_codes(self):
        def err_of(argv):
            code, _, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertNotIn("Traceback", err)
            return err
        b = self.orb_bundle
        self.assertIn("No session on Sat 2026-11-07 (weekend)",
                      err_of(["orb", "--offline", b, "--now", "2026-11-07 10:30", "--out", self.wl]))
        self.assertIn("BIST tatili: Cumhuriyet Bayramı",
                      err_of(["orb", "--offline", b, "--now", "2026-10-29 10:30", "--out", self.wl]))
        self.assertIn("Run orb again after 10:20",
                      err_of(["orb", "--offline", b, "--now", "2026-11-09 10:14", "--out", self.wl]))
        self.assertIn("expired", err_of(["orb", "--offline", b, "--now", "2026-11-09 17:40", "--out", self.wl]))
        self.assertIn("closed at 18:00", err_of(["orb", "--offline", b, "--now", "2026-11-09 18:05"]))
        self.assertFalse(os.path.exists(self.wl))          # nothing written on any of those
        os.makedirs(self.t)
        cfg = os.path.join(self.t, "config.json")
        write(cfg, {"no_entries_after": "10:25"})
        self.assertIn("after 10:25", err_of(["orb", "--offline", b, "--now", "2026-11-09 10:30", "--config", cfg]))
        code, _, _ = run(["orb", "--offline", b, "--now", "2026-11-09 10:30", "--config", cfg, "--until", "17:30",
                          "--no-write"])
        self.assertEqual(code, 0)                          # --until wins over the config
        self.assertIn("--at HH:MM", err_of(["orb", "--offline", b]))
        self.assertIn("--at", err_of(["orb", "--offline", b, "--at", "9.30"]))
        code, text, _ = run(["orb", "--offline", b, "--now", "2026-11-09 10:20", "--symbols", "THYAO",
                             "--out", self.wl])
        self.assertEqual(code, 1)                          # no breakout yet, but the empty list is written
        self.assertIn("No chart closed above its opening-range high by 10:20.", text)
        self.assertEqual(self.read(self.wl)["hits"], [])
        self.assertIn("scan.py universe", err_of(["daily", "--universe", os.path.join(self.d, "nope.txt")]))
        self.assertIn("no offline file", err_of(["daily", "--offline", os.path.join(self.d, "nothing-here")]))
        empty = os.path.join(self.d, "empty.json")
        write(empty, "{}")
        self.assertIn("no symbols to scan", err_of(["daily", "--offline", empty]))
        code, _, _ = run([])
        self.assertEqual(code, 1)

    def test_offline_folder_of_v8_files(self):
        folder = os.path.join(self.d, "charts")
        os.makedirs(folder)
        write(os.path.join(folder, "THYAO.json"), thyao_orb())
        write(os.path.join(folder, "ASELS.IS.json"), orb_day("ASELS", DAY, 148.0, 150.0, 151.0))
        write(os.path.join(folder, "GONE.json"), {"chart": {"result": None, "error": {"description": "No data"}}})
        write(os.path.join(folder, "notes.txt"), "ignored")
        code, text, _ = run(["orb", "--offline", folder + os.sep, "--now", "2026-11-09 10:30", "--no-write",
                             "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], out["failed"]), (3, 2, 1))
        self.assertEqual([h["symbol"] for h in out["hits"]], ["ASELS", "THYAO"])
        self.assertEqual(out["offline"], "charts")

    def test_networked_orb_with_a_fake_yahoo(self):
        """The networked path: stage 1 spark closes, stage 2 v8, the cap said as PARTIAL."""
        docs = {"THYAO": thyao_orb(), "ASELS": orb_day("ASELS", DAY, 148.0, 150.0, 151.0),
                "GARAN": orb_day("GARAN", DAY, 120.0, 122.0, 123.0, trigger_at="10:20")}
        for i in range(25):
            docs["F%03d" % i] = chart("F%03d" % i, [DAY], seed=i, start=40.0, vol=0.0005)
        calls = []

        def get(path):
            calls.append(path)
            if path.startswith("/v7/finance/spark"):
                res = []
                for y in path.split("symbols=")[1].split("&")[0].split(","):
                    self.assertTrue(y.endswith(".IS"), y)
                    d = docs.get(y[:-3])
                    if d:
                        r = d["chart"]["result"][0]
                        q = r["indicators"]["quote"][0]
                        res.append({"symbol": y, "response": [dict(r, indicators={"quote": [
                            {"close": q["close"], "volume": q["volume"]}]})]})
                return json.dumps({"spark": {"result": res, "error": None}})
            y = path.split("/chart/")[1].split("?")[0]
            self.assertTrue(y.endswith(".IS"), y)
            return json.dumps(docs[y[:-3]])
        old_get, old_sleep, real_now = scan.yahoo_get, scan.SLEEP, scan.parse_now
        scan.yahoo_get, scan.SLEEP = get, (lambda s: None)
        scan.parse_now = lambda text: real_now(text or "2026-11-09 10:31")   # the machine clock, pinned
        uni = os.path.join(self.d, "u.txt")
        write(uni, "\n".join(sorted(docs) + ["NOPE"]) + "\n")
        try:
            code, text, err = run(["orb", "--universe", uni, "--now", "2026-11-09 10:30", "--max-confirm", "2",
                                   "--no-write"])
        finally:
            scan.yahoo_get, scan.SLEEP, scan.parse_now = old_get, old_sleep, real_now
        self.assertEqual(code, 0, err)
        self.assertIn("about 15 minutes late", text)
        self.assertIn("Fetched 28 of 29 charts (1 failed or had no data)", text)
        self.assertIn("PARTIAL:", text)
        self.assertIn("of the 2 checked charts closed above their opening-range high by 10:30", text)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v8/")), 2)
        self.assertEqual(sum(1 for c in calls if c.startswith("/v7/")), 2)   # 29 symbols, 20 a request
        self.assertTrue(all("range=1d&interval=5m" in c for c in calls))
        v8 = [c.split("/chart/")[1].split("?")[0] for c in calls if c.startswith("/v8/")]
        self.assertEqual(sorted(v8), ["ASELS.IS", "THYAO.IS"])             # the most lira traded (same volume)

    def test_daily_json_and_symbol_flags(self):
        code, text, _ = run(["daily", "--offline", self.daily_bundle, "--now", "2026-11-09 09:00", "--json",
                             "--no-write"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual([h["symbol"] for h in out["hits"]], ["THYAO", "ASELS", "GARAN"])
        self.assertEqual((out["universe"], out["scanned"], out["note"]), (4, 4, NOTE))
        code, text, _ = run(["daily", "--offline", self.daily_bundle, "--symbols", "thyao.is,FLAT",
                             "--now", "2026-11-09 09:00", "--json", "--no-write"])
        self.assertEqual(code, 0)
        self.assertEqual([h["symbol"] for h in json.loads(text)["hits"]], ["THYAO"])
        code, text, _ = run(["daily", "--offline", self.daily_bundle, "--limit", "1", "--now", "2026-11-09 09:00",
                             "--json", "--no-write"])
        self.assertEqual(json.loads(text)["universe"], 1)
        code, text, _ = run(["daily", "--offline", self.daily_bundle, "--symbols", "FLAT", "--now",
                             "2026-11-09 09:00", "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("No setups.", text)


if __name__ == "__main__":
    unittest.main()
