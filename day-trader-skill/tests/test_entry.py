"""trader-entry tests on Borsa İstanbul. Bar data is SYNTHETIC (tests/bist_fixtures.py,
not market data): Istanbul = UTC+3, continuous trading 10:00-18:00 TSİ, prices on
the BIST tick.

The main day: THYAO, Monday 2026-11-09, 5-minute bars, an opening range
305.25-310.75 (10:00, 10:05, 10:10 bars), the 10:15 bar closes 312.50 (the trigger,
at 10:20), previous close 306.00 (taban 275.50, tavan 336.50).
"""

import copy
import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT
import bist_fixtures as bf

entry = load("trader-entry", "entry")

DT = datetime.datetime
DAY = "2026-11-09"          # a Monday
HALF = "2026-10-28"         # Cumhuriyet Bayramı arefesi: continuous trading ends 12:30
OPEN_TS = bf.ts(DAY, "10:00")


def tsi(text):
    return entry.parse_now(text)


def orb(day=DAY, trigger_close=312.50, trigger_at="10:15", prev_close=306.0, **kw):
    return bf.orb_day("THYAO", day, or_low=305.25, or_high=310.75, trigger_close=trigger_close,
                      trigger_at=trigger_at, prev_close=prev_close, **kw)


def no_breakout(day=DAY, **kw):
    """Every close after the range stays at 309.00, under the 310.75 high."""
    return orb(day, trigger_close=309.0, **kw)


def chart(d=None, sym="THYAO"):
    return entry.parse_chart(json.dumps(d if d is not None else orb()), sym)


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), tsi(text), **kw)


def keep_until(d, last_start, data_time=None):
    """Only the bars that start at or before `last_start` (HH:MM), like a live answer."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    cut = bf.ts(r_day(r), last_start)
    keep = [i for i, t in enumerate(r["timestamp"]) if t <= cut]
    r["timestamp"] = [r["timestamp"][i] for i in keep]
    q = r["indicators"]["quote"][0]
    for k in q:
        q[k] = [q[k][i] for i in keep]
    if data_time is not None:
        r["meta"]["regularMarketTime"] = data_time
    return d


def r_day(r):
    return entry.to_eastern(entry.utc_of(r["timestamp"][0])).date().isoformat()


def add_tick(d, unix, price):
    """Yahoo's live tick: an off-grid timestamp at the end."""
    r = d["chart"]["result"][0]
    r["timestamp"].append(unix)
    q = r["indicators"]["quote"][0]
    for k in ("open", "high", "low", "close"):
        q[k].append(price)
    q["volume"].append(0)
    r["meta"]["regularMarketTime"] = unix
    return d


def one_minute():
    """SYNTHETIC 1-minute THYAO-like bars around 100 TL (0.05 / 0.10 ticks): range 99.00-100.50,
    a wick above the high (10:26), a close AT the high (10:27), the first close above
    (10:28, 1.5x volume), a 3x-volume close above (10:31), a null bar (10:19), the 10:36
    bar in progress and a live tick at 10:36:30."""
    rows = []
    for k in range(37):                                   # 10:00 ... 10:36
        hhmm = (DT(2026, 11, 9, 10, 0) + datetime.timedelta(minutes=k)).strftime("%H:%M")
        o = c = 99.80
        h, lo, v = 99.90, 99.70, 10000
        if k == 0:
            h = 100.50
        if k == 1:
            lo = 99.00
        if k == 26:
            h, c = 100.80, 100.30
        if k == 27:
            h, c = 100.50, 100.50
        if k == 28:
            h, c, v = 100.60, 100.60, 15000
        if k in (29, 30):
            h, c = 100.40, 100.40
        if k == 31:
            h, c, v = 100.70, 100.70, 30000
        if k > 31:
            h, c = 100.70, 100.70
        rows.append([bf.ts(DAY, hhmm), o, max(h, o, c), min(lo, o, c), c, v])
    doc = bf._doc("ASELS", [tuple(r) for r in rows], 99.0, 1, "1d")
    q = doc["chart"]["result"][0]["indicators"]["quote"][0]
    for key in q:
        q[key][19] = None                                 # 10:19 came as nulls
    return add_tick(doc, bf.ts(DAY, "10:36") + 30, 100.70)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


def save(path, d):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(d, fh)


class Clock(unittest.TestCase):
    def test_istanbul_is_utc_plus_3_all_year(self):
        for utc in (DT(2026, 1, 15, 7, 0), DT(2026, 7, 15, 7, 0), DT(2026, 11, 9, 7, 0)):
            self.assertEqual(entry.eastern_offset(utc), 3)
            self.assertEqual(entry.to_eastern(utc).time(), datetime.time(10, 0))
            self.assertEqual(entry.eastern_to_utc(entry.to_eastern(utc)), utc)
        self.assertEqual(tsi("2026-11-09 10:30"), DT(2026, 11, 9, 7, 30))
        self.assertEqual(tsi("2026-07-01 10:30"), DT(2026, 7, 1, 7, 30))      # no daylight time
        self.assertEqual(tsi("2026-11-09 10:30 TSİ"), DT(2026, 11, 9, 7, 30))
        self.assertEqual(tsi("2026-11-09 10:30 TSI"), DT(2026, 11, 9, 7, 30))
        self.assertEqual(tsi("2026-11-09T07:30:00Z"), DT(2026, 11, 9, 7, 30))
        self.assertEqual(tsi("2026-11-09T10:30:00+03:00"), DT(2026, 11, 9, 7, 30))
        self.assertEqual(bf.ts(DAY, "10:00") % 300, 0)
        self.assertEqual(entry.to_eastern(entry.utc_of(OPEN_TS)), DT(2026, 11, 9, 10, 0))

    def test_session_constants(self):
        self.assertEqual((entry.OPEN, entry.CLOSE, entry.EARLY),
                         (datetime.time(10, 0), datetime.time(18, 0), datetime.time(12, 30)))
        self.assertEqual(entry.DEFAULTS, {"no_entries_before": "10:15", "no_entries_after": "17:30"})
        self.assertFalse(hasattr(entry, "_nth_sunday"))
        self.assertFalse(hasattr(entry, "NYSE_HOLIDAYS"))

    def test_symbols(self):
        self.assertEqual(entry.to_yahoo("thyao"), "THYAO.IS")
        self.assertEqual(entry.to_yahoo("XU100"), "XU100.IS")
        self.assertEqual(entry.to_yahoo("THYAO.IS"), "THYAO.IS")
        self.assertEqual(entry.to_yahoo("^XU100"), "^XU100")
        self.assertEqual(entry.from_yahoo("THYAO.IS"), "THYAO")
        self.assertEqual(chart()["symbol"], "THYAO")                         # the bare code, never .IS


class TheBreakout(unittest.TestCase):
    def test_triggers_at_1020_on_the_1015_bar(self):
        r = at("2026-11-09 10:20")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["opening_range"]["high"], 310.75)
        self.assertEqual(r["opening_range"]["low"], 305.25)
        self.assertEqual(r["opening_range"]["bars"], 3)
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("10:20 AM", "10:15", 3, 312.5))
        self.assertEqual(t["pct_over"], 0.56)                                # 312.50 / 310.75 - 1
        self.assertEqual(r["level"], 310.75)
        self.assertEqual(r["prev_close"], 306.0)
        self.assertEqual((r["opens"], r["closes"], r["no_entries_before"], r["no_entries_after"]),
                         ("10:00", "18:00", "10:15", "17:30"))

    def test_at_1017_the_1015_bar_is_still_forming(self):
        r = at("2026-11-09 10:17")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "10:10")
        self.assertEqual(r["last"]["close"], 308.0)
        self.assertEqual(r["forming"]["bar_start"], "10:15")
        self.assertEqual(r["forming"]["closes"], "10:20 AM")
        self.assertIn("last close 308.00 (10:10 bar), 2.75 under the range high 310.75 (-0.88%)", r["reason"])
        self.assertEqual(at("2026-11-09 10:19:59")["status"], "waiting")
        self.assertNotIn("$", "\n".join(entry.describe(r)))

    def test_pre_open_and_while_the_range_forms(self):
        r = at("2026-11-09 09:50")                                           # opening auction time
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("BIST opens at 10:00", r["reason"])
        self.assertIn("opening auction 09:40-09:55", r["reason"])
        self.assertIn("range is set at 10:15", r["reason"])
        self.assertIn("NOT OPEN YET", "\n".join(entry.describe(r)))
        r = at("2026-11-09 10:12")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        self.assertIn("2 of 3 range bars closed; the range is set at 10:15", r["reason"])
        r = at("2026-11-09 10:15")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)
        self.assertTrue(r["opening_range"]["complete"])
        # the US open (9:30 New York = 16:30 TSİ) means nothing here
        r = at("2026-11-09 09:30")
        self.assertEqual(r["phase"], "pre-open")

    def test_the_live_tick_and_the_in_progress_bar_are_dropped(self):
        tick_ts = bf.ts(DAY, "10:22") + 45                                    # 10:22:45
        d = add_tick(keep_until(orb(), "10:20"), tick_ts, 312.75)
        c = chart(d)
        self.assertEqual(c["bars"][-1]["t"], tick_ts)                         # the tick is in the file
        r = entry.evaluate(c, entry.utc_of(c["data_time"]))                   # read as of the file's own time
        self.assertEqual(r["now"], "2026-11-09 10:22:45")
        self.assertEqual(r["_bars"][-1]["slot"], 3)                           # the 10:15 bar
        self.assertEqual(r["forming"]["bar_start"], "10:20")
        self.assertEqual(r["bars_closed"], 4)
        self.assertTrue(all(b["t"] % 300 == 0 for b in r["_bars"]))
        self.assertEqual(r["trigger"]["time"], "10:20 AM")

    def test_a_bar_counts_once_yahoo_catches_up(self):
        d = keep_until(orb(), "10:15", data_time=bf.ts(DAY, "10:19") + 45)    # last trade 10:19:45
        r = at("2026-11-09 10:20:05", d)
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["_bars"][-1]["slot"], 2)
        self.assertIn("Yahoo's last trade is 10:19:45", r["lag_note"])
        self.assertIn("Yahoo's last trade is 10:19:45", "\n".join(entry.describe(r)))
        r = at("2026-11-09 10:21:01", d)                                      # a minute later it counts
        self.assertEqual(r["status"], "triggered")
        self.assertIn("before this bar closed", r["data_note"])

    def test_null_bars_are_dropped(self):
        d = orb()
        q = d["chart"]["result"][0]["indicators"]["quote"][0]
        for k in q:
            q[k][4] = None                                                    # the 10:20 bar came as nulls
        c = chart(d)
        self.assertEqual(len(c["bars"]), 95)
        r = entry.evaluate(c, tsi("2026-11-09 10:40"))
        self.assertNotIn(4, [b["slot"] for b in r["_bars"]])
        self.assertEqual(r["trigger"]["bar_index"], 3)

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-11-09 18:05")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "10:20 AM")
        self.assertEqual(r["bars_closed"], 96)                                # 10:00 to 18:00 in 5-minute bars
        text = "\n".join(entry.describe(r))
        self.assertIn("minutes ago", text)
        self.assertNotIn("This is the moment", text)


class Rules(unittest.TestCase):
    def test_volume_confirmation_moves_the_trigger(self):
        d = orb()
        q = d["chart"]["result"][0]["indicators"]["quote"][0]
        q["volume"][3] = 30000                                                # the 10:15 breakout bar: 0.6x
        q["volume"][4] = 40000                                                # the 10:20 bar: 0.8x
        q["volume"][5] = 80000                                                # the 10:25 bar: 1.6x
        r = at("2026-11-09 10:40", d, min_volume_x=1)
        self.assertEqual(r["opening_range"]["avg_volume"], 50000)
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_index"], t["price"], t["volume_x"]), ("10:30 AM", 5, 312.5, 1.6))
        r = at("2026-11-09 10:21", d, min_volume_x=1)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("0.60x the range's volume, under your 1.00x", r["reason"])

    def test_level_setup(self):
        r = at("2026-11-09 10:40", setup="level", level=311)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]), ("10:20 AM", 3, 312.5))
        self.assertEqual(r["level"], 311.0)
        # a level under the open: the first eligible close is the 10:10 bar's, at 10:15 (no_entries_before)
        r = at("2026-11-09 10:40", setup="level", level=300)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("10:15 AM", 2))
        r = at("2026-11-09 10:40", setup="level", level=313)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("0.50 under the level 313.00", r["reason"])
        with self.assertRaises(entry.EntryError):
            at("2026-11-09 10:30", setup="level")

    def test_no_breakout_waits_then_expires_at_1730(self):
        d = no_breakout()
        self.assertEqual(at("2026-11-09 10:49", d)["status"], "waiting")
        self.assertEqual(at("2026-11-09 17:29", d)["status"], "waiting")
        r = at("2026-11-09 17:30", d)
        self.assertEqual((r["status"], r["phase"]), ("expired", "after-cutoff"))
        self.assertIn("by 17:30", r["reason"])
        self.assertIn("No entry on this setup today", "\n".join(entry.describe(r)))
        self.assertEqual(at("2026-11-09 18:30", d)["phase"], "after-close")

    def test_the_bar_closing_at_the_cutoff_still_counts_when_yahoo_lags(self):
        d = keep_until(no_breakout(), "10:40", data_time=bf.ts(DAY, "10:44") + 40)
        cfg = {"no_entries_before": "10:15", "no_entries_after": "10:45"}
        r = at("2026-11-09 10:45:10", d, cfg=cfg)                             # the 10:40 bar closed by the clock only
        self.assertEqual(r["status"], "waiting")
        self.assertIn("Yahoo's last trade is 10:44:40", r["lag_note"])
        self.assertEqual(at("2026-11-09 10:46", d, cfg=cfg)["status"], "expired")

    def test_config_times(self):
        r = at("2026-11-09 10:20", cfg={"no_entries_before": "10:15", "no_entries_after": "10:15"})
        self.assertEqual(r["status"], "expired")
        r = at("2026-11-09 10:40", cfg={"no_entries_before": "10:25", "no_entries_after": "17:30"})
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_start"]), ("10:25 AM", "10:20"))

    def test_bad_inputs(self):
        with self.assertRaises(entry.EntryError):
            at("2026-11-09 10:20", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-11-09 10:20", interval="15m")
        for bad in ("tomorrow", "2026-11-09 10:15 ET"):
            with self.assertRaises(entry.EntryError):
                entry.parse_now(bad)
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_hhmm("25:00", "x")
        self.assertIn("Istanbul time", str(cm.exception))

    def test_or_minutes_30(self):
        d = orb(trigger_at="10:35", trigger_close=312.50)
        r = at("2026-11-09 10:50", d, or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)                       # 10:00 ... 10:25
        self.assertEqual(r["opening_range"]["high"], 310.75)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("10:40 AM", 7))
        self.assertIn("range is set at 10:30", at("2026-11-09 09:59", d, or_minutes=30)["reason"])


class LimitsAndWarnings(unittest.TestCase):
    def test_limits_from_the_previous_close(self):
        r = at("2026-11-09 10:20")
        self.assertEqual(r["limits"], {"base": 306.0, "taban": 275.5, "tavan": 336.5})
        self.assertEqual(r["warnings"], [])
        self.assertIn("Daily limits: taban 275.50, tavan 336.50 (base 306.00, the previous close).",
                      entry.describe(r))
        self.assertEqual(entry.daily_limits(None), None)
        self.assertEqual(entry.daily_limits(0), None)
        # on the tick: taban rounds up, tavan rounds down
        self.assertEqual(entry.daily_limits(19.0), {"base": 19.0, "taban": 17.1, "tavan": 20.9})
        self.assertEqual(entry.daily_limits(47.0), {"base": 47.0, "taban": 42.3, "tavan": 51.7})
        self.assertEqual(entry.daily_limits(95.55), {"base": 95.55, "taban": 86.0, "tavan": 105.1})

    def test_near_the_tavan_warns(self):
        r = at("2026-11-09 10:20", orb(prev_close=285.0))                      # tavan 313.50
        self.assertEqual(r["limits"]["tavan"], 313.5)
        self.assertEqual(len(r["warnings"]), 1)
        w = r["warnings"][0]
        self.assertIn("the trigger price 312.50 is within 1% of today's tavan 313.50", w)
        self.assertIn("may not fill", w)
        self.assertIn("WARNING: " + w + ".", entry.describe(r))
        # waiting: it is the last close that is near
        r = at("2026-11-09 10:17", orb(prev_close=280.0, trigger_close=309.0))  # tavan 308.00, last 308.00
        self.assertEqual(r["status"], "waiting")
        self.assertIn("the last close 308.00 is within 1% of today's tavan 308.00", r["warnings"][0])
        # far from it: no warning
        self.assertEqual(at("2026-11-09 10:20", orb(prev_close=290.0))["warnings"], [])

    def test_base_price_from_an_earlier_session_in_the_chart(self):
        days = bf.trading_days("2026-11-05", DAY)
        d = bf.chart("THYAO", days, seed=7)
        r0 = d["chart"]["result"][0]
        prev = [c for t, c in zip(r0["timestamp"], r0["indicators"]["quote"][0]["close"]) if t < bf.ts(DAY, "00:00")][-1]
        r = at("2026-11-09 11:00", d)
        self.assertEqual(r["limits"]["base"], round(prev, 2))
        self.assertEqual(r["limits"]["tavan"], entry.round_tick(prev * 1.1, "down"))

    def test_no_base_price_no_limits(self):
        d = orb()
        meta = d["chart"]["result"][0]["meta"]
        del meta["chartPreviousClose"], meta["previousClose"]
        r = at("2026-11-09 10:20", d)
        self.assertIsNone(r["limits"])
        self.assertNotIn("Daily limits", "\n".join(entry.describe(r)))


class OneMinute(unittest.TestCase):
    def c(self):
        return entry.parse_chart(json.dumps(one_minute()), "ASELS")

    def test_wick_and_close_at_the_high_do_not_trigger(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        self.assertEqual(r["now"], "2026-11-09 10:36:30")
        self.assertEqual(r["opening_range"], {"high": 100.5, "low": 99.0, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 10000})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("10:29 AM", "10:28", 28, 100.6))
        r = entry.evaluate(c, tsi("2026-11-09 10:28:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "10:28")
        self.assertEqual(r["last"]["close"], 100.5)                          # the 10:27 bar closed AT the high

    def test_null_bar_tick_and_volume(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m", min_volume_x=2)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["volume_x"]),
                         ("10:32 AM", 31, 3.0))
        slots = [b["slot"] for b in r["_bars"]]
        self.assertNotIn(19, slots)                                           # 10:19 came as nulls
        self.assertEqual(slots[-1], 35)                                       # 10:36 still forming, 10:36:30 tick dropped
        self.assertEqual(r["forming"]["bar_start"], "10:36")

    def test_the_file_and_the_interval_must_agree(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "ASELS.json")
            save(p, one_minute())
            code, _, err = run(["check", "ASELS", "--offline", p])
            self.assertEqual(code, 1)
            self.assertIn("1m bars, not 5m", err)
        finally:
            shutil.rmtree(d)


class Calendar(unittest.TestCase):
    def test_half_day_moves_the_cutoff_up(self):
        self.assertEqual(entry.session(datetime.date(2026, 10, 28)), (datetime.time(10, 0), datetime.time(12, 30)))
        d = no_breakout(HALF)
        self.assertEqual(len(d["chart"]["result"][0]["timestamp"]), 30)        # 10:00 to 12:30
        r = at("2026-10-28 11:59", d)
        self.assertEqual(r["status"], "waiting")
        self.assertEqual((r["closes"], r["no_entries_after"]), ("12:30", "12:00"))
        self.assertEqual(r["early_close"], "Cumhuriyet Bayramı arefesi")
        r = at("2026-10-28 12:00", d)
        self.assertEqual(r["status"], "expired")
        self.assertIn("by 12:00", r["reason"])
        self.assertIn("half day: continuous trading ends 12:30", r["reason"])
        self.assertEqual(at("2026-10-28 12:40", d)["phase"], "after-close")
        r = at("2026-10-28 10:20", orb(HALF))
        self.assertEqual(r["trigger"]["time"], "10:20 AM")
        self.assertIn("Half day today (yarım gün, Cumhuriyet Bayramı arefesi): continuous trading ends 12:30, "
                      "no new entries after 12:00.", entry.describe(r))
        # a cutoff already before the half-day close stays put
        cfg = {"no_entries_before": "10:15", "no_entries_after": "11:00"}
        self.assertEqual(entry.evaluate(chart(d), tsi("2026-10-28 10:30"), cfg=cfg)["no_entries_after"], "11:00")
        # every half day in the list
        for day in ("2026-03-19", "2026-05-26", "2026-10-28", "2027-03-08", "2027-10-28"):
            self.assertEqual(entry.session(datetime.date.fromisoformat(day))[1], datetime.time(12, 30), day)

    def test_weekend_holiday_and_empty_data(self):
        r = at("2026-11-07 10:30")                                            # Saturday
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertIn("weekend", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-09 10:00")
        r = at("2026-10-29 10:30")
        self.assertEqual(r["status"], "no-data")
        self.assertIn("BIST tatili: Cumhuriyet Bayramı", r["reason"])
        self.assertEqual(r["next_open"], "2026-10-30 10:00")
        self.assertIn("Next open: 2026-10-30 10:00 Istanbul time (TSİ).", entry.describe(r))
        self.assertEqual(at("2026-05-27 10:30")["next_open"], "2026-06-01 10:00")   # Kurban Bayramı, then a weekend
        for day in ("2026-01-01", "2026-04-23", "2026-05-01", "2026-05-19", "2026-07-15", "2027-08-30"):
            ok, why = entry.trading_day(datetime.date.fromisoformat(day))
            self.assertFalse(ok, day)
            self.assertTrue(why.startswith("BIST tatili: "), day)
        # US holidays are ordinary BIST days
        for day in ("2026-11-26", "2026-07-03", "2026-12-25"):
            self.assertTrue(entry.trading_day(datetime.date.fromisoformat(day))[0], day)
        empty = orb()
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-11-09 10:20", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = at("2026-11-10 10:20")                                            # Yahoo sent yesterday's bars only
        self.assertEqual(r["status"], "no-data")
        self.assertTrue(at("2027-06-01 10:30")["calendar_known"])
        r = at("2028-03-01 10:30")
        self.assertFalse(r["calendar_known"])
        self.assertIn("check borsaistanbul.com", "\n".join(entry.describe(r)))

    def test_yahoo_errors(self):
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "THYAO")


class Network(unittest.TestCase):
    """Yahoo is never called: _open_url and _sleep are swapped for fakes."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep = entry._open_url, entry._sleep
        entry._sleep = lambda s: self.sleeps.append(s)
        self.body = json.dumps(orb())

    def tearDown(self):
        entry._open_url, entry._sleep = self._open, self._sleep

    def answers(self, *codes):
        seq = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return self.body
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def get(self, sym="THYAO"):
        d = datetime.date(2026, 11, 9)
        return entry.get_chart(sym, "5m", d, today=d)

    def test_url_has_the_is_suffix_and_429_retries_once_after_a_pause(self):
        self.answers(429, 200)
        c = self.get("thyao")
        self.assertEqual(len(c["bars"]), 96)
        self.assertEqual(c["symbol"], "THYAO")
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/THYAO.IS?range=1d&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_429_twice_falls_back_to_query1(self):
        self.answers(429, 429, 200)
        self.get()
        self.assertEqual([u.split("/")[2] for u in self.calls],
                         ["query2.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com"])

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        self.get()
        self.assertEqual(self.sleeps, [])
        self.assertIn("query1", self.calls[1])

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "THYAO", "--now", "2026-11-09 10:20", "--config", "nope.json"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_ticker_404(self):
        self.answers(404)
        with self.assertRaises(entry.EntryError) as cm:
            self.get("ZZZZ")
        self.assertIn("No data found", str(cm.exception))

    def test_ranges_for_past_sessions(self):
        today = datetime.date(2026, 11, 10)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 11, 9), today, "5m"), "5d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 20), today, "5m"), "1mo")
        self.assertEqual(entry.chart_range(datetime.date(2026, 9, 21), today, "5m"), "60d")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 10, 20), today, "1m")
        with self.assertRaises(entry.EntryError):
            entry.chart_range(datetime.date(2026, 8, 3), today, "5m")
        self.assertIn("/chart/XU100.IS?", entry.chart_path("xu100", "1d", "5m"))
        self.assertIn("/chart/^XU100?", entry.chart_path("^XU100", "1d", "5m"))


def live_provider(d, lag_until=None, calls=None):
    """Bars the way Yahoo serves them at `now`: closed bars final, the in-progress bar
    showing a spike to 311.00 (above the 310.75 high, then it closes elsewhere), plus a
    live tick. lag_until: before this UTC time Yahoo's last trade lags 30 seconds behind."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        lagging = lag_until is not None and now < lag_until
        data_ts = nts - 30 if lagging else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = 311.00, max(b["h"], 311.00)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": 311.0, "h": 311.0, "l": 311.0, "c": 311.0, "v": 0})
        return {"symbol": "THYAO", "meta": {}, "bars": bars, "prev_close": 306.0, "data_time": data_ts}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def late(self):
        """The breakout one bar later: the 10:25 bar closes 312.50, at 10:30."""
        return orb(trigger_at="10:25")

    def loop(self, provider, start, **kw):
        clock = entry.SimClock(tsi(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(self.late(), calls=calls), "2026-11-09 10:17", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:30 AM")
        self.assertEqual(fired[0][0]["trigger"]["price"], 312.5)
        self.assertEqual([entry.to_eastern(c).strftime("%H:%M:%S") for c in calls],
                         ["10:17:00", "10:20:02", "10:25:02", "10:30:02"])
        text = "\n".join(lines)
        self.assertIn("10:20 bar  closed 308.00  2.75 under 310.75", text)
        self.assertIn("10:25 bar  closed 312.50  TRIGGERED", text)
        self.assertEqual(text.count("TRIGGERED"), 1)
        self.assertIn("TSİ", lines[0])

    def test_waits_for_yahoo_to_catch_up(self):
        lag = tsi("2026-11-09 10:30:30")
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(self.late(), lag_until=lag, calls=calls),
                                                   "2026-11-09 10:27", every=30)
        self.assertEqual(code, 0)
        times = [entry.to_eastern(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times, ["10:27:00", "10:30:02", "10:30:32"])   # 10:30:02: Yahoo still at 10:29:32
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:30 AM")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(self.late()), "2026-11-09 09:30", every=30)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn("not open yet", lines[0])
        self.assertIn("watching from the first bar's close at 10:05", lines[0])
        self.assertIn("10:10 bar  closed 308.00  opening range set: high 310.75, low 305.25", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 6)    # 10:00 ... 10:25, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(tsi("2026-11-09 10:06"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(self.late(), calls=calls), clock.now, clock.sleep, lines.append,
                                     dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        times = [entry.to_eastern(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times[:2], ["10:06:00", "10:24:30"])   # straight after waking, not a long sleep later
        text = "\n".join(lines)
        for start in ("10:05", "10:10", "10:15"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "10:30 AM")

    def test_expires_and_until(self):
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-11-09 17:00", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(entry.to_eastern(clock.now()).strftime("%H:%M"), "17:30")
        until = DT(2026, 11, 9, 11, 0)
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-11-09 10:30", every=300,
                                                   until=until)
        self.assertEqual(code, 1)
        self.assertIn("--until 11:00", lines[-1])

    def test_half_day_watch_expires_at_noon(self):
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout(HALF)), "2026-10-28 11:30", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(entry.to_eastern(clock.now()).strftime("%H:%M"), "12:00")

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(orb()), "2026-11-07 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])
        code, _, lines, _, _ = self.loop(live_provider(orb()), "2026-10-29 10:00")
        self.assertIn("BIST tatili", lines[0])

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-11-09 10:05", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        save("THYAO.json", orb())

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "THYAO", "--offline", "THYAO.json", "--now", "2026-11-09 10:20", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("THYAO  Mon 2026-11-09 10:20 TSİ", text)
        self.assertIn("TRIGGERED 10:20 AM at 312.50", text)
        self.assertIn("Daily limits: taban 275.50, tavan 336.50 (base 306.00, the previous close).", text)
        self.assertIn("/trader-exit", text)
        self.assertNotIn("$", text)
        self.assertNotRegex(text, r"\bET\b")
        with open(os.path.join("trader", "entry.json"), encoding="utf-8") as fh:
            e = json.load(fh)
        want = {"created_by": "trader-entry", "symbol": "THYAO", "setup": "orb", "side": "buy", "date": DAY,
                "status": "triggered", "time": "10:20 AM", "bar_start": "10:15", "bar_index": 3, "price": 312.5,
                "level": 310.75, "interval": "5m", "opening_range": {"high": 310.75, "low": 305.25, "minutes": 15},
                "base_price": 306.0, "taban": 275.5, "tavan": 336.5,
                "limits": {"base": 306.0, "taban": 275.5, "tavan": 336.5}, "warnings": [],
                "source": "yahoo", "checked_at": "2026-11-09T07:20:00Z"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)

    def test_near_tavan_warning_goes_into_entry_json(self):
        save("THYAO.json", orb(prev_close=285.0))
        code, text, _ = run(["check", "THYAO", "--offline", "THYAO.json", "--now", "2026-11-09 10:20", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("WARNING: the trigger price 312.50 is within 1% of today's tavan 313.50", text)
        with open(os.path.join("trader", "entry.json"), encoding="utf-8") as fh:
            e = json.load(fh)
        self.assertEqual(e["limits"], {"base": 285.0, "taban": 256.5, "tavan": 313.5})
        self.assertEqual(len(e["warnings"]), 1)
        self.assertIn("tavan 313.50", e["warnings"][0])

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "THYAO", "--offline", "THYAO.json", "--now", "2026-11-09 10:17", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "THYAO", "--offline", "THYAO.json", "--now", "2026-11-09 10:17",
                             "--write", "e.json"])
        with open("e.json", encoding="utf-8") as fh:
            e = json.load(fh)
        self.assertEqual((e["status"], e["price"], e["time"]), ("waiting", None, None))
        self.assertEqual(e["tavan"], 336.5)
        self.assertIn("still forming", text)

    def test_offline_gz_folder_and_mismatch(self):
        os.mkdir("charts")
        with open("THYAO.json", "rb") as src, gzip.open(os.path.join("charts", "THYAO.json.gz"), "wb") as dst:
            dst.write(src.read())
        code, text, _ = run(["check", "thyao", "--offline", "charts", "--now", "2026-11-09 10:20"])
        self.assertEqual(code, 0)
        os.mkdir("yahoo")
        shutil.copy("THYAO.json", os.path.join("yahoo", "THYAO.IS.json"))    # Yahoo's own file name works too
        code, text, _ = run(["check", "THYAO.IS", "--offline", "yahoo", "--now", "2026-11-09 10:20"])
        self.assertEqual(code, 0)
        self.assertIn("THYAO  Mon", text)
        code, _, err = run(["check", "ASELS", "--offline", "THYAO.json"])
        self.assertEqual(code, 1)
        self.assertIn("holds THYAO, not ASELS", err)

    def test_offline_without_now_reads_as_of_the_file(self):
        save("live.json", add_tick(keep_until(orb(), "10:20"), bf.ts(DAY, "10:22") + 45, 312.75))
        code, text, _ = run(["check", "THYAO", "--offline", "live.json", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["now"], "2026-11-09 10:22:45")

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "THYAO", "--offline", "THYAO.json", "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("v2 plans longs only", err)
        code, _, err = run(["check", "THYAO", "--offline", "THYAO.json", "--level", "300"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "THYAO", "--offline", "THYAO.json", "--setup", "level"])
        self.assertIn("needs --level", err)

    def test_config_is_read(self):
        os.mkdir("trader")
        save(os.path.join("trader", "config.json"), {"no_entries_after": "10:15"})
        code, text, _ = run(["check", "THYAO", "--offline", "THYAO.json", "--now", "2026-11-09 10:20"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "THYAO", "--offline", "THYAO.json"])
        self.assertIn("not valid JSON", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, text, _ = run(["watch", "THYAO", "--offline", "THYAO.json", "--now", "2026-11-09 09:50", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn("10:15 bar  closed 312.50  TRIGGERED", text)
        with open(os.path.join("trader", "entry.json"), encoding="utf-8") as fh:
            e = json.load(fh)
        self.assertEqual((e["bar_index"], e["symbol"], e["tavan"]), (3, "THYAO", 336.5))
        code, text, _ = run(["watch", "THYAO", "--offline", "THYAO.json", "--now", "2026-11-09 09:50",
                             "--until", "10:10", "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist(self):
        os.mkdir("charts")
        shutil.copy("THYAO.json", os.path.join("charts", "THYAO.json"))
        save(os.path.join("charts", "ASELS.json"), one_minute())
        os.mkdir("trader")
        save(os.path.join("trader", "watchlist.json"), {"created_by": "trader-scanner", "hits": [
            {"symbol": "THYAO", "setups": ["orb"], "or_high": 310.75, "or_low": 305.25},
            {"symbol": "GARAN", "setups": ["orb"]}, {"symbol": "ASELS.IS", "setups": ["orb"]}]})
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-11-09 10:20"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(rows[1].startswith("THYAO   triggered  10:20 AM  312.50    310.75"), rows[1])
        self.assertIn("GARAN   error: no offline file", rows[2])
        self.assertIn("ASELS   error: ASELS has 1m bars", rows[3])
        self.assertIn("1 of 3 triggered", text)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-11-09 10:17", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)[0]["result"]["status"], "waiting")
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /trader-scanner first", err)


def code_blocks(text):
    """The bodies of the plain ``` fences (not ```bash)."""
    out, cur, lang = [], None, None
    for line in text.splitlines():
        if line.startswith("```"):
            if cur is None:
                cur, lang = [], line[3:].strip()
            else:
                if not lang:
                    out.append("\n".join(cur))
                cur = None
        elif cur is not None:
            cur.append(line)
    return out


class SkillDoc(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "trader-entry", "SKILL.md"), encoding="utf-8") as fh:
            self.text = fh.read()

    def test_skill_md(self):
        text = self.text
        self.assertTrue(text.startswith("---\nname: trader-entry\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertIn("Talk to the user in Turkish", text)
        self.assertIn("## Safety", text)
        low = " ".join(text.lower().split())
        for must in ("bar by bar", "can lag", "day trading loses most people money", "/trader-exit", "longs only",
                     "in the background", "/loop", "nothing was placed", "10:20 am", "312.50", "bar 3",
                     "10:00-18:00", "12:30", "taban", "tavan", "thyao", "yatırım tavsiyesi değildir",
                     "istanbul time", "10:15", "17:30"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "tsla", "nyse",
                      "new york", "9:30", "15:30"):
            self.assertNotIn(never, low)
        self.assertNotRegex(text, r"\$\d")
        self.assertNotRegex(text, r"\bET\b")

    def test_the_example_is_what_the_script_prints(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "THYAO.json")
            save(p, orb("2026-10-19"))
            outs = []
            for now in ("2026-10-19 10:17", "2026-10-19 10:20"):
                code, out, _ = run(["check", "THYAO", "--offline", p, "--now", now, "--config", "none.json"])
                outs.append(out)
            blocks = [b for b in code_blocks(self.text) if "THYAO" in b or "WAITING" in b]
            self.assertEqual(len(blocks), 2)
            for block, out in zip(blocks, outs):
                for line in block.strip().splitlines():
                    self.assertIn(line, out.splitlines())
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
