"""trader-exit tests on Borsa İstanbul. Bars for the ATR stop and the base price are
SYNTHETIC (tests/bist_fixtures.py, not market data).

The worked example: THYAO, Monday 2026-11-09, entry 312.50 at 10:20 AM (the 10:15
bar, bar 3), opening range 305.25-310.75, previous close 306.00 (taban 275.50,
tavan 336.50). Midpoint stop 308.00, risk 4.50, 1R target 317.00, 2R 321.50.

Costs by hand with the defaults (commission 0.0015, BSMV 0.05 on it, borsa payı
0.00003): one side costs notional x (0.0015 x 1.05 + 0.00003) = notional x 0.001605.
  in at 312.50      0.5015625
  out at 308.00     0.494340     round trip at the stop    0.9959025 -> 0.9959
  out at 317.00     0.508785     round trip at the target  1.0103475 -> 1.0103
  risk net 4.50 + 0.9959 = 5.4959, reward net 4.50 - 1.0103 = 3.4897, 3.4897 / 5.4959 = 0.63
"""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, ROOT
import bist_fixtures as bf

exitp = load("trader-exit", "exit")
entry = load("trader-entry", "entry")

DAY = "2026-11-09"
HALF = "2026-10-28"
LIMITS = {"base": 306.0, "taban": 275.5, "tavan": 336.5}

# trader/plan.json for the worked example (1R), as the contract writes it
PLAN_1R = {"created_by": "trader-exit", "symbol": "THYAO", "side": "buy", "date": DAY, "setup": "orb",
           "entry": 312.5, "entry_time": "10:20 AM", "entry_bar": 3, "interval": "5m",
           "opening_range": {"high": 310.75, "low": 305.25, "minutes": 15},
           "stop": 308.0, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
           "target": 317.0, "target_r": 1.0, "target_label": "1R (one times the risk)",
           "risk_per_share": 4.5, "reward_risk": 1.0, "flat_by": "17:50",
           "tick_stop": 0.25, "tick_target": 0.25,
           "cost_per_share": 0.9959, "cost_per_share_at_target": 1.0103,
           "risk_per_share_net": 5.4959, "reward_per_share_net": 3.4897, "reward_risk_net": 0.63}
COSTS_1R = {"commission_rate": 0.0015, "bsmv_rate": 0.05, "exchange_fee_rate": 0.00003,
            "entry_per_share": 0.5016, "exit_per_share_at_stop": 0.4943, "exit_per_share_at_target": 0.5088,
            "round_trip_at_stop": 0.9959, "round_trip_at_target": 1.0103}
ENTRY = {"created_by": "trader-entry", "symbol": "THYAO", "setup": "orb", "side": "buy", "date": DAY,
         "status": "triggered", "time": "10:20 AM", "bar_start": "10:15", "bar_index": 3, "price": 312.5,
         "level": 310.75, "interval": "5m", "opening_range": {"high": 310.75, "low": 305.25, "minutes": 15},
         "base_price": 306.0, "taban": 275.5, "tavan": 336.5, "limits": dict(LIMITS), "warnings": [],
         "source": "yahoo", "checked_at": "2026-11-09T07:20:00Z"}


def orb(day=DAY, prev_close=306.0, **kw):
    return bf.orb_day("THYAO", day, or_low=305.25, or_high=310.75, trigger_close=312.50, trigger_at="10:15",
                      prev_close=prev_close, **kw)


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=310.75, ol=305.25, atr=None, n=None):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n)


def trio(p):
    return (p["stop"], p["risk"], p["target"])


class Ticks(unittest.TestCase):
    def test_tick_table(self):
        for price, t in ((0.5, 0.01), (19.99, 0.01), (20.0, 0.02), (49.98, 0.02), (50.0, 0.05), (99.95, 0.05),
                         (100.0, 0.10), (249.9, 0.10), (250.0, 0.25), (499.75, 0.25), (500.0, 0.50),
                         (999.5, 0.50), (1000.0, 1.0), (2499.0, 1.0), (2500.0, 2.5), (9000.0, 2.5)):
            self.assertEqual(exitp.tick_size(price), t, price)

    def test_round_tick_both_ways(self):
        cases = [  # price, down, up
            (19.999, 19.99, 20.0), (20.01, 20.0, 20.02), (20.03, 20.02, 20.04), (49.99, 49.98, 50.0),
            (99.97, 99.95, 100.0), (100.03, 100.0, 100.1), (249.95, 249.9, 250.0), (250.1, 250.0, 250.25),
            (309.375, 309.25, 309.5), (500.2, 500.0, 500.5), (1000.4, 1000.0, 1001.0), (2501.0, 2500.0, 2502.5),
            (308.0, 308.0, 308.0), (20.0, 20.0, 20.0), (100.0, 100.0, 100.0)]
        for price, down, up in cases:
            self.assertEqual(exitp.round_tick(price, "down"), down, price)
            self.assertEqual(exitp.round_tick(price, "up"), up, price)
            self.assertEqual(exitp.tick_down(price), Decimal(repr(down)), price)
            self.assertEqual(exitp.tick_up(price), Decimal(repr(up)), price)
        self.assertEqual(exitp.round_tick(312.6), 312.5)
        self.assertEqual(exitp.round_tick(312.63), 312.75)

    def test_money_is_turkish(self):
        self.assertEqual(exitp.money(1234.5), "1.234,50 TL")
        self.assertEqual(exitp.money(0), "0,00 TL")
        self.assertEqual(exitp.money(4.5), "4,50 TL")
        self.assertEqual(exitp.money(0.9959), "1,00 TL")
        self.assertEqual(exitp.money(1234567.891), "1.234.567,89 TL")
        self.assertEqual(exitp.money(Decimal("12.345")), "12,35 TL")
        self.assertEqual(exitp.px(312.5), "312.50")

    def test_clock_and_calendar(self):
        self.assertEqual(exitp.parse_now("2026-11-09 10:20"), exitp.datetime.datetime(2026, 11, 9, 7, 20))
        self.assertEqual(exitp.parse_now("2026-07-01 10:20 TSİ"), exitp.datetime.datetime(2026, 7, 1, 7, 20))
        self.assertIsNone(exitp.session(exitp.datetime.date(2026, 10, 29)))
        self.assertEqual(exitp.session(exitp.datetime.date(2026, 10, 28))[1], exitp.datetime.time(12, 30))
        self.assertEqual(exitp.session(exitp.datetime.date(2026, 11, 9)),
                         (exitp.datetime.time(10, 0), exitp.datetime.time(18, 0)))
        self.assertEqual(exitp.DEFAULTS["flat_by"], "17:50")


class Math(unittest.TestCase):
    def test_the_example_or_mid_1r(self):
        p = c(312.50, "or-mid", "1R")
        self.assertEqual(trio(p), (Decimal("308.00"), Decimal("4.50"), Decimal("317.00")))
        self.assertEqual(p["reward_risk"], Decimal("1.00"))
        self.assertEqual(p["target_label"], "1R (one times the risk)")
        self.assertEqual(p["stop_detail"], "(310.75 + 305.25) / 2 = 308")      # already on the tick

    def test_the_default_2r(self):
        p = c(312.50, "or-mid")
        self.assertEqual(p["target"], Decimal("321.50"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")

    def test_midpoint_off_the_tick_rounds_down(self):
        p = c(312.50, "or-mid", "1R", 310.75, 305.50)                        # (310.75 + 305.50) / 2 = 308.125
        self.assertEqual(trio(p), (Decimal("308.00"), Decimal("4.50"), Decimal("317.00")))
        self.assertIn("rounded down to the 0.25 tick", p["stop_detail"])

    def test_raw_yahoo_floats_give_the_same_numbers(self):
        p = c(312.4999999, "or-mid", "1R", 310.7500001, 305.2499999)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (Decimal("312.50"), Decimal("308.00"), Decimal("317.00")))

    def test_stop_down_target_up_across_the_tick_bands(self):
        # 0.01 / 0.02: a stop at 19.995 goes to 19.99; 20.50 + 0.51 = 21.01 goes UP to 21.02
        p = c(20.50, "price:19.995", "1R", None, None)
        self.assertEqual(trio(p), (Decimal("19.99"), Decimal("0.51"), Decimal("21.02")))
        self.assertEqual(p["reward_risk"], Decimal("1.02"))                  # from the rounded numbers: 0.52 / 0.51
        p = c(20.50, "price:20.01", "1R", None, None)                        # 20.01 is off the 0.02 grid
        self.assertEqual(trio(p), (Decimal("20.00"), Decimal("0.50"), Decimal("21.00")))
        # 0.05 / 0.10: 99.97 down to 99.95; 100.50 + 0.55 = 101.05 up to 101.10
        p = c(100.50, "price:99.97", "1R", None, None)
        self.assertEqual(trio(p), (Decimal("99.95"), Decimal("0.55"), Decimal("101.10")))
        self.assertEqual(p["reward_risk"], Decimal("1.09"))                  # 0.60 / 0.55
        p = c(101.00, "price:100.07", "2R", None, None)
        self.assertEqual(trio(p), (Decimal("100.00"), Decimal("1.00"), Decimal("103.00")))
        # 0.25: pct:1 of 312.50 is 309.375, down to 309.25; 1.5R = 317.375, up to 317.50
        p = c(312.50, "pct:1", "1.5R", None, None)
        self.assertEqual(trio(p), (Decimal("309.25"), Decimal("3.25"), Decimal("317.50")))
        self.assertEqual(p["stop_detail"], "312.50 x (1 - 1%) = 309.375, rounded down to the 0.25 tick")
        self.assertEqual(p["target_label"], "1.5R (1.5 times the risk)")
        self.assertEqual(c(312.50, "pct:1", "2R", None, None)["target"], Decimal("319.00"))
        # 0.50 and 1.00
        p = c(600.00, "price:590.30", "1R", None, None)
        self.assertEqual(trio(p), (Decimal("590.00"), Decimal("10.00"), Decimal("610.00")))
        p = c(1250, "pct:2", "1.5R", None, None)                             # 1225 on the tick; 1287.5 up to 1288
        self.assertEqual(trio(p), (Decimal("1225"), Decimal("25"), Decimal("1288")))
        # 2.50
        p = c(2600, "price:2551", "1R", None, None)
        self.assertEqual(trio(p), (Decimal("2550"), Decimal("50"), Decimal("2650")))
        self.assertEqual(c(2600, "price:2551", "price:2651", None, None)["target"], Decimal("2652.5"))

    def test_price_targets_round_up(self):
        p = c(312.50, "or-mid", "price:320.10")
        self.assertEqual(p["target"], Decimal("320.25"))
        self.assertEqual(p["reward_risk"], Decimal("1.72"))                  # 7.75 / 4.50
        self.assertEqual(p["target_label"], "your price (1.72R)")
        self.assertEqual(p["target_r"], Decimal("1.72"))

    def test_other_stops(self):
        p = c(312.50, "or-low")
        self.assertEqual(trio(p), (Decimal("305.25"), Decimal("7.25"), Decimal("327.00")))
        p = c(312.50, "price:308.10")
        self.assertEqual(p["stop"], Decimal("308.00"))                       # rounds away from the entry
        p = c(312.50, "atr:1.5", atr=2.5, n=4)
        self.assertEqual(trio(p), (Decimal("308.75"), Decimal("3.75"), Decimal("320.00")))
        self.assertEqual(p["stop_label"], "1.5 x ATR(4) under the entry")
        self.assertIn("fewer than 14", p["stop_detail"])

    def test_refusals_say_why(self):
        cases = [("price:312.50", "2R", "at or above the entry"),
                 ("price:313", "2R", "at or above the entry"),
                 ("or-mid", "price:312.40", "at or below the entry"),    # 312.40 rounds up to 312.50
                 ("or-mid", "price:312", "at or below the entry")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(312.50, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # one 0.25 tick under 312.50 is 0.08 %: allowed. pct:0.01 even widens to that tick
        self.assertEqual(c(312.50, "price:312.25")["risk"], Decimal("0.25"))
        self.assertEqual(c(312.50, "pct:0.01")["stop"], Decimal("312.25"))
        # at the top of a band one tick is under 0.05 % of the price: 0.02 on 49.98 is 0.04 %
        with self.assertRaises(exitp.PlanError) as cm:
            c(49.98, "price:49.96", "2R", None, None)
        self.assertIn("too tight to survive the spread", str(cm.exception))
        self.assertIn("0,02 TL", str(cm.exception))
        self.assertEqual(c(49.98, "price:49.94", "2R", None, None)["risk"], Decimal("0.04"))
        with self.assertRaises(exitp.PlanError):
            c(305.00, "or-mid")                                              # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(312.50, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(312.50, "or-mid", oh=305.25, ol=310.75)

    def test_turkish_decimal_comma(self):
        # a Turkish user writes 308,50: that is 308.50, never 30850 (and pct:1,5 is 1.5 %, not 15 %)
        self.assertEqual(exitp.parse_stop("pct:1,5"), ("pct", Decimal("1.5")))
        self.assertEqual(exitp.parse_stop("price:308,50"), ("price", Decimal("308.50")))
        self.assertEqual(exitp.parse_stop("price:1.234,50"), ("price", Decimal("1234.50")))
        self.assertEqual(exitp.parse_stop("price:1,234.50"), ("price", Decimal("1234.50")))
        self.assertEqual(exitp.parse_stop("price:1,234"), ("price", Decimal("1234")))
        self.assertEqual(exitp.parse_target("price:320,10"), ("price", Decimal("320.10")))
        self.assertEqual(exitp.parse_target("1,5R"), ("r", Decimal("1.5")))
        self.assertEqual(exitp.D("4,50 TL"), Decimal("4.50"))
        with self.assertRaises(exitp.PlanError):
            exitp.parse_stop("price:1.2.3")
        p = c(312.50, "pct:1,5", "price:320,10", None, None)                 # 307.8125 down to 307.75
        self.assertEqual((p["stop"], p["target"]), (Decimal("307.75"), Decimal("320.25")))

    def test_costs_by_hand(self):
        cfg = exitp.read_config(None)
        k = exitp.costs_of(Decimal("312.50"), Decimal("308.00"), Decimal("317.00"), cfg)
        self.assertEqual(k, COSTS_1R)
        self.assertAlmostEqual(exitp.side_cost(312.5, cfg), 0.5015625, places=12)
        self.assertAlmostEqual(exitp.side_cost(1000.0, cfg), 1.605, places=12)
        cheap = dict(cfg, commission_rate=0.001, exchange_fee_rate=0.0)        # 0.001 x 1.05 = 0.00105 a side
        k = exitp.costs_of(Decimal("312.50"), Decimal("308.00"), Decimal("317.00"), cheap)
        self.assertEqual((k["entry_per_share"], k["round_trip_at_stop"]), (0.3281, 0.6515))   # 0.328125 + 0.3234

    def test_atr_on_the_session_bars(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "THYAO.json")
            with open(p, "w") as fh:
                json.dump(orb(), fh)
            ch = exitp.load_chart("THYAO", "5m", None, p)
        finally:
            shutil.rmtree(d)
        self.assertEqual(ch["prev_close"], 306.0)
        day = exitp.datetime.date(2026, 11, 9)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=3)
        self.assertEqual(len(bars), 4)                                       # 10:00 ... the 10:15 entry bar
        # by hand: 310.75-308, 308-305.25, 0 (a flat bar at the previous close), 312.50-308
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 4)
        self.assertAlmostEqual(atr, (2.75 + 2.75 + 0 + 4.5) / 4, places=9)
        all_bars = exitp.session_bars(ch, day, "5m")
        self.assertEqual(len(all_bars), 96)                                  # 10:00-18:00
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)
        later = exitp.session_bars(ch, day, "5m", now_utc=exitp.parse_now("2026-11-09 10:21"))
        self.assertEqual(len(later), 4)                                      # the 10:20 bar has not closed
        self.assertEqual(exitp.session_bars(ch, exitp.datetime.date(2026, 10, 29), "5m"), [])   # a holiday


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")
        self.write("THYAO.json", orb())

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-11-09 10:21"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, _, _ = run(entry, ["check", "THYAO", "--offline", "THYAO.json", "--now", "2026-11-09 10:20", "--write"])
        self.assertEqual(code, 0)
        code, text, err = self.plan("--stop", "or-mid", "--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in PLAN_1R.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["costs"], COSTS_1R)
        self.assertEqual(p["limits"], dict(LIMITS, source="entry.json"))
        self.assertEqual(p["warnings"], [])
        self.assertIn("  Entry         312.50 at 10:20 AM (bar 3)", text)
        self.assertIn("  Stop          308.00  opening-range midpoint: (310.75 + 305.25) / 2 = 308", text)
        self.assertIn("  Target        317.00  1R (one times the risk)", text)
        self.assertIn("  Risk a share  4,50 TL (1.44% of the entry)", text)
        self.assertIn("  Costs a share 1,00 TL round trip if stopped (0,50 TL in + 0,49 TL out), 1,01 TL at the target",
                      text)
        self.assertIn("  Reward:risk   1.0 gross, 0.63 after costs", text)
        self.assertIn("  Daily limits  taban 275.50, tavan 336.50 (base 306.00, the previous close)", text)
        self.assertIn("  Flat by       17:50 TSİ", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/trader-timemachine", text)
        self.assertNotIn("$", text)
        self.assertNotIn("pro" + "fit", text.lower())   # never on screen
        code, text, _ = self.plan("--write")                                  # the defaults: or-mid, 2R
        p = self.read_plan()
        self.assertEqual((p["target"], p["target_r"]), (321.5, 2.0))
        # 2R costs: out at 321.50 is 0.5160075, round trip 1.0175700; 7.9824 / 5.4959 = 1.45
        self.assertEqual((p["cost_per_share_at_target"], p["reward_risk_net"]), (1.0176, 1.45))
        code, text, _ = run(exitp, ["show", "--now", "2026-11-09 10:25"])
        self.assertEqual(code, 0)
        self.assertIn("321.50", text)
        self.assertIn("2.0 gross, 1.45 after costs", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-11-10 10:20"])
        self.assertIn("not today", text)

    def test_manual_flags_base_price_and_json(self):
        code, text, _ = self.plan("--symbol", "thyao", "--entry", "312.50", "--or-high", "310.75", "--or-low", "305.25",
                                  "--time", "10:20", "--entry-bar", "3", "--date", DAY, "--target", "1R",
                                  "--base-price", "306", "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        for k, v in PLAN_1R.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["limits"], dict(LIMITS, source="--base-price"))
        self.assertEqual(p["warnings"], [])
        code, text, _ = self.plan("--symbol", "THYAO.IS", "--entry", "312.50", "--stop", "pct:1", "--json")
        p = json.loads(text)
        self.assertEqual(p["symbol"], "THYAO")                                # the bare code, never .IS
        self.assertIsNone(p["limits"])
        self.assertIn("No base price", p["warnings"][0])
        code, text, _ = self.plan("--symbol", "THYAO", "--entry", "312.50", "--stop", "pct:1")
        self.assertIn("Note: No base price (previous close), so the taban/tavan were not checked", text)
        code, _, err = self.plan("--symbol", "THYAO", "--entry", "312.50", "--stop", "pct:1", "--base-price", "0")
        self.assertEqual(code, 1)
        self.assertIn("--base-price must be above 0", err)

    def test_limits_warnings(self):
        args = ["--symbol", "THYAO", "--entry", "312.50", "--or-high", "310.75", "--or-low", "305.25", "--json"]
        # stop under the taban: base 345 -> taban 310.50, the 308.00 stop can never fill today
        p = json.loads(self.plan(*(args + ["--base-price", "345"]))[1])
        self.assertEqual((p["limits"]["taban"], p["limits"]["tavan"]), (310.5, 379.5))
        self.assertEqual(len(p["warnings"]), 1)
        self.assertIn("The stop 308.00 is under today's taban 310.50 (base 345.00)", p["warnings"][0])
        # target over the tavan: base 290 -> tavan 319.00, the 2R target is 321.50
        p = json.loads(self.plan(*(args + ["--base-price", "290"]))[1])
        self.assertEqual((p["limits"]["taban"], p["limits"]["tavan"]), (261.0, 319.0))
        self.assertEqual(len(p["warnings"]), 1)
        self.assertIn("The target 321.50 is over today's tavan 319.00 (base 290.00)", p["warnings"][0])
        # entry within 1 % of the tavan: base 285 -> tavan 313.50
        p = json.loads(self.plan(*(args + ["--base-price", "285"]))[1])
        self.assertTrue(any("within 1% of today's tavan 313.50" in w for w in p["warnings"]), p["warnings"])
        self.assertTrue(any("The target 321.50 is over today's tavan 313.50" in w for w in p["warnings"]))
        # an entry outside the limits means the base is wrong
        p = json.loads(self.plan(*(args + ["--base-price", "250"]))[1])
        self.assertTrue(any("outside today's limits 225.00-275.00" in w for w in p["warnings"]), p["warnings"])
        # on screen it is a WARNING, not a note
        code, text, _ = self.plan(*(args[:-1] + ["--base-price", "345"]))
        self.assertEqual(code, 0)
        self.assertIn("WARNING: The stop 308.00 is under today's taban 310.50", text)
        # an entry off the tick
        p = json.loads(self.plan("--symbol", "THYAO", "--entry", "312.60", "--stop", "price:308", "--base-price", "306",
                                 "--json")[1])
        self.assertIn("The entry 312.60 is not on the 0.25 tick: a limit order goes in at 312.50.", p["warnings"])

    def test_base_price_from_the_offline_chart(self):
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, limits=None, base_price=None))
        code, text, err = self.plan("--offline", "THYAO.json", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual(p["limits"], dict(LIMITS, source="chart"))
        # --base-price wins over the chart and entry.json
        p = json.loads(self.plan("--offline", "THYAO.json", "--base-price", "300", "--json")[1])
        self.assertEqual(p["limits"]["source"], "--base-price")
        # entry.json's base is for its own session only
        self.write(os.path.join("trader", "entry.json"), ENTRY)
        p = json.loads(self.plan("--date", "2026-11-10", "--json")[1])
        self.assertIsNone(p["limits"])

    def test_atr_stop_reads_bars(self):
        self.write(os.path.join("trader", "entry.json"), ENTRY)
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", "THYAO.json", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (308.75, 3.75, 320.0))
        self.assertEqual((p["stop_rule"], p["atr_bars"], p["atr"]), ("atr", 4, 2.5))
        self.assertEqual(p["limits"]["source"], "chart")
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", "THYAO.json", "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", ".")
        self.assertEqual(code, 0, err)                                        # a folder of <CODE>.json
        os.mkdir("other")
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", "other")
        self.assertIn("no offline file for THYAO", err)

    def test_config_costs(self):
        self.write(os.path.join("trader", "entry.json"), ENTRY)
        self.write(os.path.join("trader", "config.json"), {"commission_rate": 0.001, "exchange_fee_rate": 0})
        p = json.loads(self.plan("--target", "1R", "--json")[1])
        self.assertEqual((p["costs"]["commission_rate"], p["costs"]["bsmv_rate"]), (0.001, 0.05))
        self.assertEqual((p["cost_per_share"], p["risk_per_share_net"]), (0.6515, 5.1515))
        self.write(os.path.join("trader", "config.json"), {"commission_rate": "lots"})
        code, _, err = self.plan()
        self.assertIn("commission_rate in the config is not a number", err)
        self.write(os.path.join("trader", "config.json"), {"bsmv_rate": -1})
        code, _, err = self.plan()
        self.assertIn("must be 0 or more", err)
        # a commission so big the target earns nothing after costs
        self.write(os.path.join("trader", "config.json"), {"commission_rate": 0.01})
        p = json.loads(self.plan("--target", "1R", "--json")[1])
        self.assertLessEqual(p["reward_risk_net"], 0.6)
        self.write(os.path.join("trader", "config.json"), {"commission_rate": 0.02})
        p = json.loads(self.plan("--stop", "price:311", "--target", "1R", "--json")[1])
        self.assertTrue(any("After costs the target earns nothing" in w for w in p["warnings"]), p["warnings"])

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, status="waiting", time=None, price=None,
                                                              bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("THYAO is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "311.00", "--json")              # plan ahead at the level
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (311.0, 308.0, None))

        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, setup="level", level=311.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("v2 plans longs only", err)
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "THYAO")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "THYAO", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "THYAO", "--entry", "312.50", "--or-high", "310.75")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_config_half_day_and_late_entries(self):
        self.write(os.path.join("trader", "entry.json"), ENTRY)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "17:30", "min_reward_risk": 1.5})
        p = json.loads(self.plan("--target", "1R", "--json")[1])
        self.assertEqual(p["flat_by"], "17:30")
        self.assertIn("under your min_reward_risk 1.5", p["warnings"][0])
        os.remove(os.path.join("trader", "config.json"))

        # half day: continuous trading ends 12:30, the 17:50 flat-by moves up 5h30m to 12:20
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, date=HALF))
        code, text, _ = run(exitp, ["plan", "--now", "2026-10-28 10:21"])
        self.assertEqual(code, 0)
        self.assertIn("Flat by       12:20 TSİ", text)
        self.assertIn("moved up for the half day", text)
        self.assertIn("Cumhuriyet Bayramı arefesi", text)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "17:55"})
        p = json.loads(run(exitp, ["plan", "--now", "2026-10-28 10:21", "--json"])[1])
        self.assertEqual(p["flat_by"], "12:25")
        os.remove(os.path.join("trader", "config.json"))
        code, _, err = run(exitp, ["plan", "--now", "2026-10-28 10:21", "--flat-by", "13:00"])
        self.assertEqual(code, 1)
        self.assertIn("after the 12:30 close", err)
        self.assertEqual(run(exitp, ["plan", "--now", "2026-10-28 10:21", "--flat-by", "12:15"])[0], 0)

        self.write(os.path.join("trader", "entry.json"), ENTRY)
        code, _, err = self.plan("--flat-by", "18:05")
        self.assertIn("after the 18:00 close", err)
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, time="5:55 PM"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, time="5:45 PM"))
        self.assertEqual(self.plan()[0], 0)

    def test_show_without_a_plan(self):
        code, text, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)

    def test_show_repeats_the_limits_warning(self):
        self.write(os.path.join("trader", "entry.json"), ENTRY)
        self.assertEqual(self.plan("--base-price", "290", "--write")[0], 0)
        code, text, _ = run(exitp, ["show", "--now", "2026-11-09 10:25"])
        self.assertEqual(code, 0)
        self.assertIn("WARNING: The target 321.50 is over today's tavan 319.00", text)


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
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            self.text = fh.read()

    def test_skill_md(self):
        text = self.text
        self.assertTrue(text.startswith("---\nname: trader-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertIn("Talk to the user in Turkish", text)
        self.assertIn("## Safety", text)
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "day trading loses most people money",
                     "/trader-timemachine", "/trader-budget", "longs only", "308.00", "317.00", "4,50 tl", "321.50",
                     "too tight to survive the spread", "taban", "tavan", "--base-price", "17:50", "12:20",
                     "bsmv", "borsa payı", "rounds down to the tick", "yatırım tavsiyesi değildir", "thyao"):
            self.assertIn(must, low)
        for never in ("\u2014", "tick " + "by tick", "real " + "time", "pro" + "fit", "tsla", "nyse", "new york",
                      "15:55"):
            self.assertNotIn(never, low)
        self.assertNotRegex(text, r"\$\d")
        self.assertNotRegex(text, r"\bET\b")

    def test_the_example_is_what_the_script_prints(self):
        d = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(d)
        try:
            code, out, err = run(exitp, ["plan", "--symbol", "THYAO", "--entry", "312.50", "--or-high", "310.75",
                                         "--or-low", "305.25", "--base-price", "306", "--date", "2026-10-19",
                                         "--time", "10:20 AM", "--entry-bar", "3", "--stop", "or-mid",
                                         "--target", "1R", "--now", "2026-10-19 10:21"])
            self.assertEqual(code, 0, err)
        finally:
            os.chdir(cwd)
            shutil.rmtree(d)
        blocks = [b for b in code_blocks(self.text) if "Exit plan:" in b]
        self.assertEqual(len(blocks), 1)
        for line in blocks[0].strip().splitlines():
            self.assertIn(line, out.splitlines())


if __name__ == "__main__":
    unittest.main()
