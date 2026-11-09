"""trader-timemachine on Borsa İstanbul, on SYNTHETIC bars from tests/bist_fixtures.py (not market data):
59 random-walk sessions of THYAO.IS up to Friday 2026-11-06 (the half day 2026-10-28 included, the
holiday 2026-10-29 absent, as Yahoo answers) plus Monday 2026-11-09, an opening-range breakout day
(range 299.00-302.00 over 10:00-10:15, the 10:25 bar closes at 303.00)."""

import gzip
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load
from bist_fixtures import chart, orb_day, trading_days, ts

tm = load("trader-timemachine", "timemachine")

DAY = "2026-11-09"
DAYS = trading_days("2026-08-01", DAY)[-60:]        # 2026-08-17 .. 2026-11-09
HALF = "2026-10-28"
NOW = "2026-11-09 10:31"
NOW_UTC = tm.parse_now(NOW)


def merged(*docs):
    """Several bist_fixtures chart docs -> one Yahoo answer (meta of the last one)."""
    out = json.loads(json.dumps(docs[-1]))
    res = out["chart"]["result"][0]
    res["timestamp"] = []
    q = res["indicators"]["quote"][0]
    for k in q:
        q[k] = []
    for d in docs:
        r = d["chart"]["result"][0]
        res["timestamp"] += r["timestamp"]
        for k in q:
            q[k] += r["indicators"]["quote"][0][k]
    return out


def history_doc(days=DAYS, seed=7, today=DAY, **orb):
    hist = chart("THYAO", days[:-1], seed=seed)
    kw = dict(or_low=299.0, or_high=302.0, trigger_close=303.0, prev_close=302.0)
    kw.update(orb)
    return merged(hist, orb_day("THYAO", today, **kw))


DOC = history_doc()
TEXT = json.dumps(DOC)

# the plan as trader-exit writes it: entry_time in the AM/PM form, limits and costs included
PLAN = {"created_by": "trader-exit", "symbol": "THYAO", "side": "buy", "date": DAY, "setup": "orb",
        "entry": 303.0, "entry_time": "10:30 AM", "entry_bar": 5, "interval": "5m",
        "opening_range": {"high": 302.0, "low": 299.0, "minutes": 15},
        "stop": 300.5, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
        "target": 305.5, "target_r": 1.0, "target_label": "1R (one times the risk)",
        "risk_per_share": 2.5, "reward_risk": 1.0, "flat_by": "17:50"}
LIMITS = {"base": 302.0, "taban": 272.0, "tavan": 332.0, "source": "chart"}


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_UTC, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn, doc=DOC):
    """The chart JSON, changed by fn(result), back as text."""
    d = json.loads(json.dumps(doc))
    fn(d["chart"]["result"][0])
    return json.dumps(d)


def keep_bars(result, keep):
    """Keep only the bars where keep(ts) is true."""
    q = result["indicators"]["quote"][0]
    idx = [i for i, t in enumerate(result["timestamp"]) if keep(t)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def add_bar(result, t, price=300.0):
    result["timestamp"].append(t)
    for k, v in (("open", price), ("high", price), ("low", price), ("close", price), ("volume", 100)):
        result["indicators"]["quote"][0][k].append(v)


def local_day(t):
    return tm.to_eastern(tm.utc_of(t)).date().isoformat()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


class Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_numbers(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "18:00"))
        self.assertEqual(m["pool_sessions"], 58)                    # 59 earlier sessions minus the half day
        self.assertEqual(m["bars_left"], 90)                        # 10:30 to 18:00
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-08-17", "2026-11-06"))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(m["r_at_target"], 1.0)
        self.assertTrue(0 < m["p_target"] < 1 and 0 < m["p_stop"] < 1, m)
        self.assertTrue(m["p5_close"] <= m["median_close"] <= m["p95_close"])
        self.assertEqual(sum(m["hist"]["bins"]) > 9000, True)
        self.assertIsNone(m["limits"])
        self.assertNotIn("avg_R_after_costs", m)

    def test_pinned_numbers_for_seed_19(self):
        # same plan, same data, same seed: these exact numbers on any machine (a change here means the
        # simulation or bist_fixtures changed)
        m = self.doc["mc"]
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"], m["p_green"], m["avg_R"]),
                         (0.5049, 0.4951, 0.0, 0.5049, 0.01))
        self.assertEqual((m["median_close"], m["p5_close"], m["p95_close"]), (303.41, 291.54, 315.39))
        self.assertEqual((sha(m["bands"]), sha(m["sample_paths"])), ("d860ce0e994b5d4d", "9e71f4d9035f9562"))
        self.assertEqual(m["hist"]["bins"][:4], [102, 119, 185, 236])
        h = self.doc["history"]
        self.assertEqual((h["triggered"], h["target"], h["stop"], h["close"], h["avg_R"]), (47, 24, 22, 1, 0.043))
        self.assertEqual(self.doc["verdict"], "Close to a coin flip: target first 50.5 % vs stop first 49.5 %.")

    def test_bands_start_at_the_entry_and_end_at_18(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [303.0] * 5)
        self.assertEqual(len(m["bands"]), 91)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("10:30", "18:00", 91))
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertTrue(all(p[0] == 303.0 and len(p) == 91 for p in m["sample_paths"]))

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual(h["sessions"], 58)
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0})
        self.assertEqual(h["triggered"], h["target"] + h["stop"] + h["close"])
        dates = [t["date"] for t in h["trades"]]
        self.assertNotIn(HALF, dates)
        self.assertNotIn("2026-10-29", dates)
        self.assertNotIn(DAY, dates)
        for t in h["trades"]:
            self.assertTrue(t["stop"] < t["entry"] < t["target"], t)
            self.assertTrue("10:20" <= t["time"] <= "18:00", t)
            self.assertEqual(t["stop"], tm.round_tick(t["stop"], "down"), t)   # on the BIST tick
            self.assertEqual(t["target"], tm.round_tick(t["target"], "up"), t)

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["entry"], p["stop"], p["target"], p["entry_bar"], p["entry_time"]),
                         ("THYAO", 303.0, 300.5, 305.5, 5, "10:30"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"]), (2.5, 1.0, "17:50"))
        self.assertEqual(self.doc["warnings"], [])
        self.assertEqual(self.doc["data"]["symbol"], "THYAO")
        self.assertEqual(self.doc["data"]["last_bar"], "2026-11-09 10:25 TSİ")
        self.assertEqual(self.doc["data"]["short_sessions_left_out"], [{"date": HALF, "bars": 30}])

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("THYAO buy, Monday 2026-11-09: entry 303.00 at 10:30 (bar 5), stop 300.50, target 305.50 "
                     "(1R, risk 2.50 a share).",
                     "10,000 replays of the rest of the session, 10:30 to 18:00 (90 five-minute bars), seed 19.",
                     "58 full sessions (2026-08-17 to 2026-11-06)", "still open at 18:00",
                     "Left out 1 short session(s) (half days or gaps): 2026-10-28.", "not a prediction",
                     "--seed 19", self.doc["verdict"]):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "$", " AM", " PM", "ET ", "New York"):
            self.assertNotIn(word, text)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)
        b = machine(replays=2000, seed=5)
        self.assertEqual(a["mc"], b["mc"])
        self.assertEqual(a["history"], b["history"])

    def test_the_same_seed_on_a_fresh_copy_of_the_data(self):
        again = json.dumps(history_doc())
        self.assertEqual(sha(machine(text=again, replays=1000)["mc"]), sha(machine(replays=1000)["mc"]))

    def test_another_seed_gives_other_numbers(self):
        a = machine(replays=3000)["mc"]
        b = machine(replays=3000, seed=7)["mc"]
        self.assertNotEqual(sha(a["bands"]), sha(b["bands"]))
        self.assertNotEqual(a["sample_paths"], b["sample_paths"])
        self.assertAlmostEqual(b["p_target"] + b["p_stop"] + b["p_neither"], 1.0, places=6)

    def test_the_draws_are_stable_across_pythons(self):
        # random.Random(seed), randint and choice are stable since Python 3.2
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(59)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)

    def test_the_simulator_on_a_known_pool(self):
        # every bar +1 %: from 100 the target 102 is hit on the second bar, never the stop
        pool = [[(1.0, 1.01, 1.0, 1.01)] for _ in range(tm.SESSION_BARS)]
        mc = tm.simulate(pool, 100.0, 99.0, 102.0, 5, 95, replays=50, seed=1)
        self.assertEqual((mc["p_target"], mc["p_stop"], mc["p_green"], mc["avg_R"]), (1.0, 0.0, 1.0, 2.0))
        self.assertEqual(mc["bars_left"], 90)
        down = [[(1.0, 1.0, 0.99, 0.99)] for _ in range(tm.SESSION_BARS)]
        mc = tm.simulate(down, 100.0, 99.0, 102.0, 5, 95, replays=50, seed=1)
        self.assertEqual((mc["p_stop"], mc["avg_R"]), (1.0, -1.0))
        both = [[(1.0, 1.05, 0.95, 1.0)] for _ in range(tm.SESSION_BARS)]   # touches both: the stop counts
        self.assertEqual(tm.simulate(both, 100.0, 99.0, 102.0, 5, 95, replays=20)["p_stop"], 1.0)


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "THYAO")

    def test_istanbul_is_utc_plus_3(self):
        self.assertEqual(tm.eastern_offset(NOW_UTC), 3)
        self.assertEqual(NOW_UTC, tm.datetime.datetime(2026, 11, 9, 7, 31))
        self.assertEqual(tm.parse_now("2026-07-01 10:00"), tm.datetime.datetime(2026, 7, 1, 7, 0))
        self.assertEqual(tm.parse_now("2026-11-09T07:31:00Z"), NOW_UTC)
        with self.assertRaises(tm.TMError) as cm:
            tm.parse_now("yarın")
        self.assertIn("Istanbul", str(cm.exception))

    def test_96_bar_sessions(self):
        self.assertEqual(tm.SESSION_BARS, 96)
        meta, bars, dropped = tm.parse_bars(self.result, NOW_UTC)
        days = tm.split_sessions(bars)
        full, short = tm.full_sessions(days, DAY)
        self.assertEqual(len(full), 58)
        for d, D in full:
            self.assertEqual(len(D), 96, d)
            self.assertEqual((D[0]["start"], D[95]["start"]), ("10:00", "17:55"), d)
        self.assertEqual(short, [{"date": HALF, "bars": 30}])
        self.assertEqual(max(days[HALF]), 29)                       # the half day's last bar: 12:25
        self.assertEqual(days[HALF][29]["start"], "12:25")

    def test_the_in_progress_bars_are_dropped(self):
        meta, bars, dropped = tm.parse_bars(self.result, NOW_UTC)
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), (DAY, "10:25", 5))
        self.assertEqual(last["c"], 303.0)
        self.assertEqual(dropped, {"no_price": 0, "off_grid": 0, "not_complete": 90, "outside_hours": 0})
        # the data's own regularMarketTime caps the clock too
        def rmt(res):
            res["meta"]["regularMarketTime"] = ts(DAY, "10:20") + 30
        bars = tm.parse_bars(tm.chart_result(edited(rmt), "THYAO"), NOW_UTC)[1]
        self.assertEqual(bars[-1]["start"], "10:15")

    def test_auction_prints_and_the_live_tick_are_not_bars(self):
        def extra(res):
            for hm in ("09:40", "09:55", "18:00", "18:05"):       # açılış / kapanış seansı prints
                add_bar(res, ts("2026-11-06", hm))
            add_bar(res, ts(DAY, "10:30") + 45)                   # the live tick, off the 5-minute grid
        meta, bars, dropped = tm.parse_bars(tm.chart_result(edited(extra), "THYAO"), tm.parse_now("2026-11-09 18:30"))
        self.assertEqual((dropped["outside_hours"], dropped["off_grid"]), (4, 1))
        nov6 = [b for b in bars if b["date"] == "2026-11-06"]
        self.assertEqual((len(nov6), nov6[0]["start"], nov6[-1]["start"]), (96, "10:00", "17:55"))

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(self.result, tm.parse_now("2026-11-09 10:12"))[1]
        self.assertEqual(len([b for b in bars if b["date"] == DAY]), 2)
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-11-09 10:12"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("10:05 bar", str(cm.exception))
        self.assertEqual(machine(now=tm.parse_now("2026-11-09 10:30"), replays=200)["mc"]["pool_sessions"], 58)

    def test_the_half_day_and_the_holiday_stay_out_of_the_sample(self):
        doc = machine(replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 58)
        self.assertEqual(doc["history"]["sessions"], 58)
        self.assertIn("2026-10-27", DAYS)
        self.assertNotIn("2026-10-29", DAYS)
        # a half day that somehow has all 96 bars is still left out by the calendar
        def padded(res):
            for k in range(30, 96):
                add_bar(res, ts(HALF, "10:00") + 300 * k)
        doc = machine(text=edited(padded), replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 58)
        self.assertEqual(doc["data"]["short_sessions_left_out"], [{"date": HALF, "bars": 96}])

    def test_a_gap_day_is_left_out(self):
        def cut(res):   # 2026-09-15 stops at 14:00 (a halt)
            keep_bars(res, lambda t: not (local_day(t) == "2026-09-15" and tm.to_eastern(tm.utc_of(t)).hour >= 14))
        doc = machine(text=edited(cut), replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 57)
        self.assertIn({"date": "2026-09-15", "bars": 48}, doc["data"]["short_sessions_left_out"])
        self.assertNotIn("2026-09-15", [t["date"] for t in doc["history"]["trades"]])

    def test_a_missing_price_is_dropped(self):
        def hole(res):
            res["indicators"]["quote"][0]["close"][5] = None
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 1)
        self.assertEqual(doc["mc"]["pool_sessions"], 57)

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "THYAO.IS"}, "timestamp": [],
                                                  "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        first = DAYS.index("2026-10-09")                     # 20 trading days before 11-09, one a half day
        def trim(res):
            keep_bars(res, lambda t: local_day(t) >= DAYS[first])
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(trim))
        self.assertIn("only 19 full session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))

        def twenty(res):
            keep_bars(res, lambda t: local_day(t) >= DAYS[first - 1])
        self.assertEqual(machine(text=edited(twenty), replays=200)["mc"]["pool_sessions"], 20)

    def test_a_plan_on_the_half_day_ends_at_12_30(self):
        days = trading_days("2026-07-01", HALF)[-40:]
        text = json.dumps(history_doc(days, today=HALF))
        p = plan(date=HALF)
        later = tm.parse_now("2026-10-28 13:00")
        doc = machine(p, text=text, now=later, replays=300)
        self.assertEqual((doc["mc"]["until"], doc["mc"]["bars_left"], doc["mc"]["pool_sessions"]), ("12:30", 24, 39))
        self.assertIn("10:30 to 12:30 (24 five-minute bars)", "\n".join(tm.report(doc)))
        with self.assertRaises(tm.TMError) as cm:
            machine(p, text=text, now=later, until="17:50", replays=10)
        self.assertIn("no later than 12:30 (yarım gün: Cumhuriyet Bayramı arefesi)", str(cm.exception))
        self.assertEqual(machine(p, text=text, now=later, until="12:20", replays=10)["mc"]["bars_left"], 22)

    def test_a_plan_on_a_holiday_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-10-29"))
        self.assertIn("BIST tatili: Cumhuriyet Bayramı", str(cm.exception))

    def test_the_data_must_be_the_plans_ticker(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="ASELS"))
        self.assertIn("data is for THYAO but the plan is for ASELS", str(cm.exception))
        self.assertEqual(plan(symbol="thyao.is")["symbol"], "THYAO")


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-11-10"))
        self.assertIn("no bars for that day", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:      # 10:35 has not closed in this data yet
            machine(plan(entry_time="10:35 AM", entry_bar=None))
        self.assertIn("not in the data", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="10:27 AM", entry_bar=None), "5-minute grid"),
            (dict(entry_time="18:00", entry_bar=None), "between 10:05 and 17:55 TSİ"),
            (dict(entry_time="10:00", entry_bar=None), "between 10:05 and 17:55 TSİ"),
            (dict(entry_bar=6), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=304.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "BIST code like THYAO"),
            (dict(symbol="TOOLONGX"), "BIST code like THYAO"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="9 Kasım"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
            (dict(limits={"taban": 330, "tavan": 300}), "0 < taban < tavan"),
            (dict(limits={"taban": "x", "tavan": 300}), "numbers for taban and tavan"),
            (dict(cost_per_share=-0.1), "zero or more"),
            (dict(cost_per_share="abc"), "must be a number"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)

    def test_entry_time_in_either_format(self):
        # trader-exit writes "10:30 AM"; people and the flags write "10:30"
        for text in ("10:30 AM", "10:30", "10:30am", "10:30 a.m."):
            p = plan(entry_time=text, entry_bar=None)
            self.assertEqual((p["entry_bar"], p["entry_time"]), (5, "10:30"), text)
        self.assertEqual(plan(entry_time=None)["entry_time"], "10:30")
        for text, bar in (("1:30 PM", 41), ("13:30", 41), ("1:30", 41), ("12:00 PM", 23), ("5:55 PM", 94),
                          ("5:55", 94)):
            self.assertEqual(plan(entry_time=text, entry_bar=None)["entry_bar"], bar, text)
        a = machine(plan(entry_time="10:30 AM"), replays=1000)
        b = machine(plan(entry_time="10:30"), replays=1000)
        self.assertEqual(a["mc"], b["mc"])
        self.assertEqual(a["plan"]["entry_time"], b["plan"]["entry_time"])

    def test_until_defaults_to_18_and_cuts_at_flat_by(self):
        self.assertEqual(machine(replays=300)["mc"]["until"], "18:00")
        doc = machine(until="17:50", replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("17:50", 88, "17:50"))
        self.assertIn("still open at 17:50", "\n".join(tm.report(doc)))
        self.assertEqual(machine(until="5:50 PM", replays=500)["mc"], m)
        self.assertEqual(machine(until="5:50", replays=500)["mc"], m)            # bare 5:50 is 17:50 on BIST
        self.assertEqual(machine(until=doc["plan"]["flat_by"], replays=500)["mc"], m)
        for bad, words in (("10:30", "nothing to replay"), ("18:05", "no later than 18:00"),
                           ("17:52", "5-minute grid"), ("noon", "Istanbul (TSİ) time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_limits_clamp_every_replay(self):
        # tavan 304.00 just above the 303.00 entry: the 305.50 target can never fill today
        lim = {"base": 276.5, "taban": 249.0, "tavan": 304.0}
        doc = machine(plan(limits=lim), replays=3000)
        m = doc["mc"]
        self.assertEqual(m["p_target"], 0.0)
        self.assertEqual(m["limits"], {"taban": 249.0, "tavan": 304.0})
        self.assertTrue(all(b[4] <= 304.0 for b in m["bands"]))
        self.assertTrue(all(max(p) <= 304.0 for p in m["sample_paths"]))
        self.assertLessEqual(m["p95_close"], 304.0)
        self.assertIn("the target 305.50 is beyond the tavan 304.00: it cannot fill today", doc["warnings"])
        text = "\n".join(tm.report(doc))
        self.assertIn("Prices clamped to today's limits: taban 249.00, tavan 304.00.", text)
        self.assertIn("Note: the target 305.50 is beyond the tavan 304.00", text)
        # wide limits that nothing reaches change no number: the clamp never touches the draws
        free = machine(replays=3000)["mc"]
        wide = machine(plan(limits=LIMITS), replays=3000)["mc"]
        self.assertEqual(dict(wide, limits=None), free)
        self.assertEqual(machine(plan(limits=LIMITS), replays=300)["warnings"], [])

    def test_limits_on_the_stop_side(self):
        # taban 301.00 above the 300.50 stop: the stop cannot fill, the price floors at the taban
        doc = machine(plan(limits={"taban": 301.0, "tavan": 332.0}), replays=2000)
        self.assertEqual(doc["mc"]["p_stop"], 0.0)
        self.assertTrue(all(min(p) >= 301.0 for p in doc["mc"]["sample_paths"]))
        self.assertIn("the stop 300.50 is beyond the taban 301.00: it cannot fill today", doc["warnings"])
        doc = machine(plan(limits={"taban": 303.5, "tavan": 332.0}), replays=10)
        self.assertIn("outside today's limits", doc["warnings"][0])

    def test_cost_per_share_gives_after_cost_fields(self):
        base = machine(replays=2000)["mc"]
        doc = machine(plan(cost_per_share=0.9), replays=2000)
        m = doc["mc"]
        self.assertEqual(m["avg_R"], base["avg_R"])
        self.assertEqual((m["cost_per_share"], m["cost_R"]), (0.9, 0.36))
        self.assertEqual(m["avg_R_after_costs"], round(base["avg_R"] - 0.36, 3))
        self.assertEqual(m["expectancy_per_share_after_costs"], round(base["avg_R"] * 2.5 - 0.9, 4))
        self.assertEqual(doc["plan"]["cost_per_share"], 0.9)
        text = "\n".join(tm.report(doc))
        self.assertIn("After costs: average R %+.3f (round-trip costs 0,90 TL a share = 0.360R; "
                      % m["avg_R_after_costs"], text)
        self.assertNotIn("cost_per_share", machine(replays=50)["plan"])       # absent unless given
        zero = machine(plan(cost_per_share=0), replays=200)["mc"]
        self.assertEqual(zero["avg_R_after_costs"], zero["avg_R"])

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=310.0, stop=307.5, target=312.5, stop_rule="pct"), replays=200)
        self.assertIn("outside that bar's range 300.50 to 303.00", doc["warnings"][0])

    def test_stops_round_to_the_tick_away_from_the_entry(self):
        self.assertEqual(tm.cents_away(300.625, True), 300.5)
        self.assertEqual(tm.cents_away(300.625, False), 300.75)
        self.assertEqual(tm.cents_away(300.5, True), 300.5)
        # Yahoo sends float32 prices: a price already on the tick must not move a tick
        self.assertEqual(tm.cents_away(48.31999969482422, True), 48.32)        # float32 of 48.32
        self.assertEqual(tm.cents_away(19.989999771118164, True), 19.99)       # float32 of 19.99
        self.assertEqual(tm.cents_away(300.50000001, False), 300.5)
        self.assertEqual(tm.cents_away(15.005, True), 15.0)                    # a real half cent still floors
        self.assertEqual(tm.cents_away(48.33, True), 48.32)
        self.assertEqual(tm.cents_away(1234.4, False), 1235.0)

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=299.0, target=307.0), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 1.0))
        rule = machine(plan(stop_rule="atr", stop=300.0, target=309.0), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 0.9901, 2.0))

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="flag"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 58)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", entry=301.0, stop=302.0, target=300.0, stop_rule="or_high",
                           limits={"taban": 300.5, "tavan": 332.0}), replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(m["p_target"], 0.0)                             # the taban 300.50 is above the target
        self.assertIn("the target 300.00 is beyond the taban 300.50", doc["warnings"][-1])
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
        self.assertIn("below the entry", "\n".join(tm.report(doc)))

    def test_the_verdict_thresholds_at_1r(self):
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "17:50"}),
                         "No replay reached the target or the stop by 17:50: the price at 17:50 decides every one.")
        self.assertEqual(tm.verdict_notes({"p_target": 0.3, "p_stop": 0.26, "p_neither": 0.44, "until": "18:00"}),
                         ["44.0 % of the replays hit neither by 18:00; the price at 18:00 decides those."])

    def test_a_2r_target_is_judged_against_its_break_even(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertTrue(tm.verdict(lean).startswith("Leans toward the stop: at 2R the target has to come first in "
                                                    "33 % of the replays that hit one side; it did in 21 %"))
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertTrue(tm.verdict(dict(mc, p_target=0.30, p_stop=0.40)).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict(dict(mc, p_target=0.40, p_stop=0.40)).startswith("Strongly toward the target"))
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_the_verdict_matches_the_numbers(self):
        doc = machine(plan(target=308.0, stop_rule="pct"), replays=2000)
        m = doc["mc"]
        self.assertEqual(m["r_at_target"], 2.0)
        self.assertEqual(doc["verdict"], tm.verdict(m))
        self.assertIn("target first %.1f %% vs stop first %.1f %%" % (100 * m["p_target"], 100 * m["p_stop"]),
                      doc["verdict"])


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = tm._get, tm._sleep
        tm._sleep = self.sleeps.append

    def tearDown(self):
        tm._get, tm._sleep = self._get, self._sleep

    def answer(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split(".finance")[0].split("//")[1])
            self.urls.append(url)
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return TEXT
        self.urls = []
        tm._get = fake

    def test_url_and_user_agent(self):
        self.assertEqual(tm.chart_url("thyao"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/THYAO.IS?range=60d&interval=5m")
        self.assertIn("/chart/XU100.IS?", tm.chart_url("XU100", "query1"))
        self.assertIn("/chart/THYAO.IS?", tm.chart_url("THYAO.IS"))
        self.assertIn("/chart/%5EXU100?", tm.chart_url("^XU100"))
        self.assertEqual(tm.UA, "Mozilla/5.0 (compatible; day-trader-skill/2.0; "
                                "+https://github.com/Jakeschincariol/day-trader-skill)")

    def test_429_waits_retries_then_falls_back_to_query1(self):
        self.answer([429, 429, 200])
        self.assertEqual(tm.fetch_chart("THYAO"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2", "query1"])
        self.assertEqual(self.sleeps, [tm.BACKOFF, tm.PAUSE])

    def test_one_429_then_ok(self):
        self.answer([429, 200])
        self.assertEqual(tm.fetch_chart("THYAO"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2"])

    def test_5xx_falls_back(self):
        self.answer([503, 200])
        self.assertEqual(tm.fetch_chart("THYAO"), TEXT)
        self.assertEqual(self.calls, ["query2", "query1"])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.answer([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("THYAO")
        self.assertIn("rate limiting", str(cm.exception))

    def test_404(self):
        self.answer([404])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("ZZZZ")
        self.assertIn("check the ticker", str(cm.exception))

    def test_run_over_the_network_path(self):
        self.answer([200])
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "plan.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(PLAN, fh)
            code, text, err = run(["run", "--plan", p, "--now", NOW, "--replays", "300"])
            self.assertEqual(code, 0, err)
            self.assertEqual(self.calls, ["query2"])
            self.assertIn("/chart/THYAO.IS?range=60d&interval=5m", self.urls[0])
        finally:
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.gz = os.path.join(self.d, "THYAO-60d5m.json.gz")
        with gzip.open(self.gz, "wt", encoding="utf-8") as fh:
            fh.write(TEXT)
        self.plan = os.path.join(self.d, "trader", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w", encoding="utf-8") as fh:
            json.dump(dict(PLAN, limits=LIMITS, cost_per_share=0.9), fh, indent=2)
        self.out = os.path.join(self.d, "trader", "timemachine.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", self.gz, "--now", NOW, "--write", self.out,
                               "--replays", "2000"])
        self.assertEqual(code, 0, err)
        self.assertIn("THYAO buy, Monday 2026-11-09: entry 303.00 at 10:30 (bar 5)", text)
        self.assertIn("After costs: average R", text)
        self.assertIn("Prices clamped to today's limits: taban 272.00, tavan 332.00.", text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/trader-budget", text)
        with open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["created_by"], "trader-timemachine")
        self.assertEqual(doc["source"], "offline")
        self.assertEqual(doc["plan"]["from"], self.plan)
        self.assertEqual(doc["plan"]["limits"]["tavan"], 332.0)
        self.assertEqual(doc["mc"]["cost_R"], 0.36)
        self.assertEqual(len(doc["mc"]["sample_paths"]), 160)
        self.assertEqual(doc["data"]["last_bar"], "2026-11-09 10:25 TSİ")

    def test_json_output(self):
        code, text, _ = run(["run", "--plan", self.plan, "--offline", self.gz, "--now", NOW, "--json",
                             "--replays", "500"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertEqual(doc["mc"]["until"], "18:00")
        self.assertFalse(os.path.exists(self.out))

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", self.gz, "--now", NOW, "--json",
                               "--replays", "1000"])
        self.assertEqual(code, 0, err)
        from_file = json.loads(text)
        code, text, err = run(["run", "--symbol", "thyao", "--date", DAY, "--entry", "303", "--stop", "300.50",
                               "--target", "305.50", "--entry-time", "10:30", "--offline", self.gz, "--now", NOW,
                               "--json", "--replays", "1000"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual({k: v for k, v in doc["mc"].items() if k not in ("limits",)},
                         {k: v for k, v in from_file["mc"].items()
                          if k not in ("limits", "cost_per_share", "cost_R", "avg_R_after_costs",
                                       "expectancy_per_share_after_costs")})
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")
        self.assertEqual(doc["history"], from_file["history"])
        code, _, err = run(["run", "--symbol", "THYAO", "--entry", "303", "--offline", self.gz])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_offline_folder_and_default_paths(self):
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(os.path.join("trader", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("trader", "timemachine.json"), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
            code, _, err = run(["run", "--symbol", "ASELS", "--entry", "1", "--stop", "0.5", "--target", "2",
                                "--entry-time", "10:30", "--offline", ".", "--now", NOW])
            self.assertEqual(code, 1)
            self.assertIn("no ASELS-60d5m.json(.gz)", err)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     ["run", "--plan", self.plan, "--offline", self.gz, "--now", NOW, "--until", "9:00"],
                     ["run", "--plan", self.plan, "--offline", self.gz, "--now", NOW, "--until", "18:30"],
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(["run", "--plan", self.plan, "--offline", self.gz, "--now", NOW, "--write", self.out, "--replays", "500"])
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("THYAO 2026-11-09, 2,000 replay paths from the 10:30 entry (seed 19)", lines[0])
        self.assertTrue(any(l.startswith("   305.50 |") and l.endswith("  target") for l in lines))
        self.assertTrue(any(l.startswith("   300.50 |") and l.endswith("  stop") for l in lines))
        self.assertTrue(any(l.startswith("   303.00 |*") and l.endswith("  entry") for l in lines))
        self.assertIn("10:30", text)
        self.assertIn("18:00", text)
        with open(self.out, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(lines[-1 - len(doc["verdict_notes"])], doc["verdict"])
        code, again, _ = run(["chart", "--plan", self.plan, "--offline", self.gz, "--now", NOW, "--replays", "500"])
        self.assertEqual(again, text)


if __name__ == "__main__":
    unittest.main()
