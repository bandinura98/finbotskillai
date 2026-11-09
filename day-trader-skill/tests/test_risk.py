"""trader-risk tests (Borsa İstanbul). Costs a share per side at the example rates: price x 0.001605."""

import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("trader-risk", "check")
ticket = load("trader-broker", "ticket")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-thyao.json")
SPLIT_PATH = os.path.join(FIX, "trader-risk", "orders-split.json")
PLAN_PATH = os.path.join(FIX, "trader-budget", "plan-thyao.json")
NOW = "2026-11-09 10:30"          # Monday, Istanbul time
TODAY = "2026-11-09"
PRICES = {"THYAO": 312.50, "ASELS": 180.00, "GARAN": 135.50, "BIMAS": 540.00, "KCHOL": 210.00}
QUOTE_TIME = datetime.datetime(2026, 11, 9, 7, 52, 13)    # 10:52:13 TSİ, as naive UTC
# weights-mode rules with every day-trade check switched off: the account checks still hold
V1 = {"budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 25, "max_order_usd": 40000,
      "min_order_usd": 100, "max_orders_per_day": 10, "require_stop": False, "no_entries_before": None,
      "no_entries_after": None, "max_trades_per_day": 0, "max_daily_loss_usd": 0, "max_loss_per_trade_usd": 0,
      "min_reward_risk": 0}


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH, encoding="utf-8") as fh:
        c = json.load(fh)
    c.update(over)
    return c


def v1(**over):
    c = dict(V1)
    c.update(over)
    return c


def thyao(**over):
    """The 181-lot THYAO bracket trader-budget writes (entry 312,50 / stop 308,00 / target 317,00)."""
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    for k, v in over.items():
        if v is None:
            o.pop(k, None)
        else:
            o[k] = v
    return o


def bracket(sym, qty, entry, stop, target):
    return {"symbol": sym, "side": "buy", "qty": str(qty), "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit_limit_price": "%.2f" % target,
            "stop_loss_stop_price": "%.2f" % stop,
            "plan": {"entry": entry, "stop": stop, "target": target}}


def split_orders():
    with open(SPLIT_PATH, encoding="utf-8") as fh:
        return json.load(fh)["orders"]


def buy(sym, tl):
    return {"symbol": sym, "side": "buy", "notional": str(tl), "type": "market", "time_in_force": "day"}


def placed(sym, side="buy", day=TODAY, max_loss=994.76, kind=None, status="placed"):
    r = {"symbol": sym, "side": side, "status": status, "date": day, "at": day + "T10:20:00+03:00"}
    if max_loss is not None:
        r["max_loss"] = max_loss
    if kind:
        r["kind"] = kind
    return r


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


class TheExample(unittest.TestCase):
    def test_the_thyao_bracket_passes_every_check(self):
        res = check.check([thyao()], cfg(), now=at(NOW))
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["warnings"], [])
        self.assertEqual((r["kind"], r["entry_ref"], r["stop"], r["target"]), ("entry", 312.5, 308.0, 317.0))
        # 181 x (4,50 + 0,9959) = 994,76
        self.assertEqual((r["risk_per_share"], r["cost_per_share"], r["max_loss"], r["reward_risk"]),
                         (4.5, 0.9959, 994.76, 1.0))
        self.assertEqual(r["value_try"], 56562.5)
        self.assertEqual(r["limits"], {"base": 305.0, "taban": 274.5, "tavan": 335.5})
        # 100.000 - 56.562,50 - 56.562,50 x 0,001605 (90,78)
        self.assertEqual(res["cash_after"], 43346.72)
        self.assertEqual((res["date"], res["market"], res["currency"], res["now_istanbul"]),
                         (TODAY, "BIST", "TRY", "2026-11-09 10:30"))
        self.assertIs(res["paper"], True)
        self.assertEqual(res["warnings"], [])

    def test_the_max_loss_counts_the_costs(self):
        # 182 x 4,50 = 819 would pass without costs; 182 x 5,4959 = 1.000,25 does not
        res = check.check([thyao(qty="182")], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertEqual(res["results"][0]["reasons"],
                         ["loss: 182 shares x (4.50 risk + 1.00 costs a share) = 1.000,25 TL at the stop, over your "
                          "1.000,00 TL max loss per trade"])
        # cheaper costs in the config: 182 x (4,50 + 2 x ~0,155) fits
        cheap = cfg(commission_rate=0.0005)
        self.assertEqual(verdicts(check.check([thyao(qty="182")], cheap, now=at(NOW))), ["PASS"])

    def test_fingerprint_is_shared_with_the_broker(self):
        o = thyao()
        self.assertEqual(check.fingerprint(o), ticket.fingerprint(o))
        self.assertEqual(check.fingerprint(dict(o, why="anything", plan={})), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, qty="182")), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, stop_loss_stop_price="307.75")), check.fingerprint(o))
        self.assertEqual(check.ORDER_FIELDS, ticket.ORDER_FIELDS)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "state", "risk.json")
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", ORDERS_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", none, "--state", none, "--plan", none, "--now", NOW, "--out", out])
            text = buf.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("PASS  #1 buy THYAO 181 shares, entry", text)
            self.assertIn("entry 312.50 (the plan's entry), stop 308.00, target 317.00: risk 4.50 + costs 1.00 a share",
                          text)
            self.assertIn("max loss 994,76 TL with costs (limit 1.000,00 TL), reward:risk 1.00 (min 1)", text)
            self.assertIn("today's band: taban 274.50, tavan 335.50", text)
            self.assertIn("today before this: 0 of 3 trades, 0,00 TL of 3.000,00 TL daily loss", text)
            self.assertIn("1 passed, 0 blocked at 2026-11-09 10:30 TSİ", text)
            self.assertIn("kağıt üzerinde / sanal", text)
            self.assertIn("yatırım tavsiyesi değildir", text)
            with open(out, encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["checked_at"], "2026-11-09T07:30:00Z")     # --now pins the record too
            self.assertEqual(rec["results"][0]["kind"], "entry")
            self.assertEqual(rec["results"][0]["fingerprint"], check.fingerprint(thyao()))


class Calendar(unittest.TestCase):
    def test_istanbul_is_utc_plus_3_all_year(self):
        self.assertEqual(at("2026-11-09 10:30"), datetime.datetime(2026, 11, 9, 7, 30))
        self.assertEqual(at("2026-07-01 10:30"), datetime.datetime(2026, 7, 1, 7, 30))     # no summer time
        self.assertEqual(check.to_eastern(at("2026-11-09T07:30:00Z")), datetime.datetime(2026, 11, 9, 10, 30))

    def test_holiday_and_half_day_lists(self):
        self.assertEqual(check.trading_day(datetime.date(2026, 10, 29)), (False, "BIST tatili: Cumhuriyet Bayramı"))
        self.assertEqual(check.trading_day(datetime.date(2026, 11, 7))[1], "weekend")
        self.assertTrue(check.trading_day(datetime.date(2026, 10, 28))[0])
        self.assertIn("2026-10-28", check.BIST_HALF_DAYS)
        self.assertEqual(check.business_days(datetime.date(2026, 11, 2)),
                         ["2026-11-02", "2026-10-30", "2026-10-28", "2026-10-27", "2026-10-26"])


class DayTradeChecks(unittest.TestCase):
    def one(self, order, now=NOW, **over):
        return check.check([order], cfg(**over), now=at(now))

    def test_entry_window_istanbul_time(self):
        cases = [("2026-11-09 10:14", "no new entries before 10:15 TSİ (it is 10:14)"),
                 ("2026-11-09 10:00", "no new entries before 10:15 TSİ (it is 10:00)"),
                 ("2026-11-09 17:31", "no new entries after 17:30 TSİ (it is 17:31)"),
                 ("2026-11-09 09:50", "the market is closed (Mon 2026-11-09 09:50 TSİ; continuous trading is "
                                      "10:00 to 18:00)"),
                 ("2026-11-09 18:00", "the market is closed"),
                 ("2026-11-07 11:00", "the market is closed today (weekend"),
                 ("2026-10-29 11:00", "the market is closed today (BIST tatili: Cumhuriyet Bayramı"),
                 ("2026-10-28 12:01", "no new entries after 12:00 TSİ (it is 12:01)"),
                 ("2026-10-28 12:30", "the market is closed (Wed 2026-10-28 12:30 TSİ; continuous trading is "
                                      "10:00 to 12:30)"),
                 ("2026-11-09T07:14:00Z", "no new entries before 10:15 TSİ (it is 10:14)")]
        for now, want in cases:
            res = self.one(thyao(), now=now)
            self.assertIn("window: " + want, reasons(res), now)
        for now in ("2026-11-09 10:15", "2026-11-09 17:30", "2026-10-28 12:00", "2026-10-28 10:15",
                    "2026-11-09T07:15:00Z", "2026-11-09T14:30:00Z"):
            self.assertEqual(verdicts(self.one(thyao(), now=now)), ["PASS"], now)

    def test_half_day_warns_with_the_shifted_window(self):
        res = self.one(thyao(), now="2026-10-28 11:00")
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("yarım gün: Cumhuriyet Bayramı arefesi, continuous trading ends 12:30 today: no new entries "
                      "after 12:00", " ".join(res["warnings"]))

    def test_window_off_and_bad_window_times(self):
        res = self.one(thyao(), now="2026-11-07 11:00", no_entries_before=None, no_entries_after=None)
        self.assertEqual(verdicts(res), ["PASS"])
        with self.assertRaises(check.RiskError):
            self.one(thyao(), no_entries_before="10.15am")

    def test_outside_the_calendar_years_warns(self):
        res = self.one(thyao(), now="2028-11-09 10:30")
        self.assertIn("the holiday list covers 2026 and 2027 only", " ".join(res["warnings"]))

    def test_prices_must_sit_on_the_tick_grid(self):
        res = self.one(thyao(stop_loss_stop_price="308.10"))
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertIn("tick: stop_loss_stop_price 308.10 is not on the BIST tick grid (tick 0.25 at this price; "
                      "nearest 308.00)", reasons(res))
        res = self.one(thyao(take_profit_limit_price="316.90"))
        self.assertIn("tick: take_profit_limit_price 316.90", reasons(res))
        res = self.one(thyao(type="limit", limit_price="312.40"))
        self.assertIn("tick: limit_price 312.40 is not on the BIST tick grid (tick 0.25 at this price; nearest 312.50)",
                      reasons(res))
        self.assertEqual(verdicts(self.one(thyao(type="limit", limit_price="312.25"))), ["PASS"])
        # under 20 TL the tick is 0,01: 15.37 is fine
        self.assertEqual(verdicts(self.one(bracket("KOZAL", 100, 15.37, 15.00, 16.00))), ["PASS"])

    def test_float32_noise_is_on_the_tick(self):
        self.assertTrue(check.on_tick(312.4999999))
        self.assertTrue(check.on_tick(48.31999969))
        self.assertFalse(check.on_tick(312.40))
        self.assertFalse(check.on_tick(48.33))
        self.assertEqual(check.round_tick(48.31999969, "down"), 48.32)
        res = self.one(thyao(stop_loss_stop_price=307.9999999, take_profit_limit_price=317.0000001))
        self.assertEqual(verdicts(res), ["PASS"], reasons(res))

    def test_a_plan_only_stop_off_the_tick_blocks(self):
        o = thyao(order_class=None, take_profit_limit_price=None, stop_loss_stop_price=None)
        o["plan"] = dict(o["plan"], stop=308.10)
        self.assertIn("tick: the plan's stop 308.10 is not on the BIST tick grid (nearest 308.00)",
                      reasons(self.one(o)))

    def test_a_limit_price_outside_the_band(self):
        res = self.one(thyao(type="limit", limit_price="336.00"))
        self.assertIn("band: the limit price 336.00 is over today's tavan 335.50", reasons(res))
        res = self.one(thyao(type="limit", limit_price="274.00"))
        self.assertIn("band: the limit price 274.00 is under today's taban 274.50", reasons(res))

    def test_the_band_comes_from_trader_plan_json_too(self):
        o = thyao(type="limit", limit_price="336.00")
        o["plan"] = {k: v for k, v in o["plan"].items() if k != "limits"}
        self.assertNotIn("band:", reasons(self.one(o)))
        with open(PLAN_PATH, encoding="utf-8") as fh:
            plan_doc = json.load(fh)
        res = check.check([o], cfg(), now=at(NOW), plan_doc=plan_doc)
        self.assertIn("band: the limit price 336.00 is over today's tavan 335.50", reasons(res))
        # yesterday's plan or another symbol's plan does not count
        res = check.check([o], cfg(), now=at("2026-11-10 10:30"), plan_doc=plan_doc)
        self.assertNotIn("band:", reasons(res))
        res = check.check([o], cfg(), now=at(NOW), plan_doc=dict(plan_doc, symbol="ASELS"))
        self.assertNotIn("band:", reasons(res))

    def test_a_stop_under_the_taban_or_target_over_the_tavan(self):
        res = self.one(thyao(qty="25", stop_loss_stop_price="274.00"), min_reward_risk=0)
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertEqual(res["results"][0]["reasons"],
                         ["limits: the stop 274.00 is under today's taban 274.50 (base 305.00): it can never fill today"])
        res = self.one(thyao(take_profit_limit_price="336.00"))
        self.assertEqual(res["results"][0]["reasons"],
                         ["limits: the target 336.00 is over today's tavan 335.50 (base 305.00): it can never fill "
                          "today"])
        self.assertEqual(verdicts(self.one(thyao(qty="25", stop_loss_stop_price="274.50"), min_reward_risk=0)),
                         ["PASS"])

    def test_fractional_qty_is_blocked(self):
        res = self.one(thyao(qty="181.5"))
        self.assertIn("format: BIST trades whole shares (1 lot = 1 share), got qty '181.5'", reasons(res))

    def test_a_stop_is_required(self):
        o = thyao(order_class=None, take_profit_limit_price=None, stop_loss_stop_price=None)
        o["plan"] = {"entry": 312.5}
        self.assertIn("stop: no stop", reasons(self.one(o)))
        res = self.one(o, require_stop=False, min_reward_risk=0)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no stop", warnings(res))

    def test_a_plan_stop_counts_but_warns(self):
        o = thyao(order_class=None, take_profit_limit_price=None, stop_loss_stop_price=None)
        res = self.one(o)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("in the plan only", warnings(res))
        self.assertEqual(res["results"][0]["max_loss"], 994.76)

    def test_stop_and_target_on_the_right_side(self):
        self.assertIn("must be below the entry 312.50", reasons(self.one(thyao(stop_loss_stop_price="312.50"))))
        res = self.one(thyao(take_profit_limit_price="312.25"))
        self.assertIn("target: the target 312.25 must be above the entry 312.50", reasons(res))

    def test_reward_risk(self):
        # (315,50 - 312,50) / 4,50 = 0,67
        res = self.one(thyao(take_profit_limit_price="315.50"))
        self.assertIn("reward:risk: 0.67", reasons(res))
        self.assertEqual(verdicts(self.one(thyao(take_profit_limit_price="315.50"), min_reward_risk=0.5)), ["PASS"])

    def test_a_fresh_higher_price_is_the_entry(self):
        # 314,00: risk 6,00, costs 0,9983; 181 x 6,9983 = 1.266,69
        res = check.check([thyao()], cfg(), prices={"THYAO": 314.00}, now=at("2026-11-09 11:00"),
                          price_times={"THYAO": QUOTE_TIME})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (314.0, "price", 6.0))
        self.assertIn("loss: 181 shares x (6.00 risk + 1.00 costs a share) = 1.266,69 TL", reasons(res))
        self.assertIn("THYAO last 314.00 is above the plan's entry 312.50", " ".join(r["info"]))

    def test_a_stale_or_future_price_is_not_used(self):
        for now in ("2026-11-09 11:30", "2026-11-09 10:30"):
            res = check.check([thyao()], cfg(), prices={"THYAO": 314.00}, now=at(now),
                              price_times={"THYAO": QUOTE_TIME})
            self.assertEqual(verdicts(res), ["PASS"], now)
            self.assertEqual(res["results"][0]["entry_ref"], 312.5)
            self.assertIn("older than 15 minutes", warnings(res))

    def test_a_lower_price_does_not_shrink_the_risk(self):
        res = check.check([thyao()], cfg(), prices={"THYAO": 311.00}, now=at(NOW))
        self.assertEqual(res["results"][0]["entry_ref"], 312.5)

    def test_a_limit_order_uses_its_limit_price(self):
        # 311,00: risk 3,00 + costs 0,9935 -> 181 x 3,9935 = 722,82; reward 6 / risk 3 = 2
        res = check.check([thyao(type="limit", limit_price="311.00")], cfg(), prices={"THYAO": 314.0}, now=at(NOW))
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["max_loss"], r["reward_risk"], r["verdict"]), (311.0, 722.82, 2.0, "PASS"))

    def test_daily_loss_and_trades_per_day(self):
        two = {"orders": [placed("ASELS"), placed("GARAN")]}
        res = check.check([thyao()], cfg(), state=two, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("today before this: 2 of 3 trades, 1.989,52 TL of 3.000,00 TL daily loss",
                      res["results"][0]["info"][-1])
        three = {"orders": [placed("ASELS"), placed("GARAN"), placed("BIMAS")]}
        why = reasons(check.check([thyao()], cfg(), state=three, now=at(NOW)))
        self.assertIn("trades: 3 entries placed today plus 0 passed earlier in this batch, your limit is 3 a day", why)
        self.assertIn("daily loss: 2.984,28 TL already at risk today plus 994,76 TL for this one = 3.979,04 TL, over "
                      "your 3.000,00 TL daily loss limit", why)
        # the daily loss alone: two big entries already at risk
        big = {"orders": [placed("ASELS", max_loss=1500), placed("GARAN", max_loss=600)]}
        why = reasons(check.check([thyao()], cfg(), state=big, now=at(NOW)))
        self.assertIn("daily loss: 2.100,00 TL already at risk today", why)
        self.assertNotIn("trades:", why)
        yesterday = {"orders": [placed(s, day="2026-11-06") for s in ("ASELS", "GARAN", "BIMAS")]}
        self.assertEqual(verdicts(check.check([thyao()], cfg(), state=yesterday, now=at(NOW))), ["PASS"])

    def test_exits_and_declines_do_not_count_as_trades(self):
        st = {"orders": [placed("ASELS"), placed("ASELS", side="sell", kind="exit", max_loss=None),
                         placed("GARAN", status="declined"), placed("BIMAS", status="failed")]}
        res = check.check([thyao()], cfg(), state=st, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["today"]["entries_placed"], 1)

    def test_the_batch_counts_toward_the_daily_limits(self):
        orders = [bracket(s, 10, 180.0, 170.0, 200.0) for s in ("ASELS", "GARAN", "BIMAS", "KCHOL")]
        res = check.check(orders, cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "PASS", "BLOCK"])
        self.assertIn("trades: 0 entries placed today plus 3 passed earlier", reasons(res, 3))

    def test_pdt_is_off_by_default(self):
        def trade(day):
            return [placed("X" + day[-2:] + "Y", day=day),
                    placed("X" + day[-2:] + "Y", side="sell", day=day, kind="exit", status="filled", max_loss=None)]
        st = {"orders": trade("2026-11-04") + trade("2026-11-05") + trade("2026-11-06")}
        res = check.check([thyao()], cfg(), state=st, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIsNone(res["today"]["day_trades_5d"])
        self.assertIs(check.DEFAULTS["pdt_guard"], False)
        self.assertEqual(verdicts(check.check([thyao()], {"budget": 100000}, state=st, now=at(NOW))), ["PASS"])
        # a self-imposed cap only when switched on
        res = check.check([thyao()], cfg(pdt_guard=True), state=st, now=at(NOW))
        self.assertIn("pdt: 3 day trades in the last 5 business days", reasons(res))
        self.assertIn("no such rule exists on BIST", reasons(res))
        # 2026-10-29 is a holiday: the 5 business days back from 11-03 are 11-03, 11-02, 10-30, 10-28, 10-27
        self.assertEqual(check.day_trades({"orders": trade("2026-10-27")}, "2026-11-03")[0], 1)
        self.assertEqual(check.day_trades({"orders": trade("2026-10-26")}, "2026-11-03")[0], 0)

    def test_a_short_entry_when_allowed(self):
        short = {"symbol": "THYAO", "side": "sell", "qty": "181", "type": "market", "time_in_force": "day",
                 "order_class": "bracket", "take_profit_limit_price": "308.00", "stop_loss_stop_price": "317.00",
                 "plan": {"entry": 312.5}}
        res = self.one(short)
        self.assertIn("short: selling more THYAO than you hold", reasons(res))
        res = self.one(short, allow_short=True)
        r = res["results"][0]
        # 181 x (4,50 + 1,0103) = 997,36
        self.assertEqual((r["verdict"], r["kind"], r["max_loss"], r["reward_risk"]), ("PASS", "entry", 997.36, 1.0))
        res = self.one(dict(short, stop_loss_stop_price="310.00"), allow_short=True)
        self.assertIn("must be above the entry 312.50 for a sell", reasons(res))

    def test_bracket_format(self):
        self.assertIn("whole-share qty", reasons(self.one(dict(thyao(qty=None), notional="56000"))))
        self.assertIn("time_in_force day or gtc", reasons(self.one(thyao(time_in_force="ioc"))))
        self.assertEqual(verdicts(self.one(thyao(time_in_force="gtc"))), ["PASS"])
        self.assertIn("needs both take_profit_limit_price and stop_loss_stop_price",
                      reasons(self.one(thyao(take_profit_limit_price=None))))
        self.assertIn("needs a stop_loss_stop_price",
                      reasons(self.one(thyao(stop_loss_stop_price=None, stop_loss_limit_price="307.00"))))
        self.assertEqual(verdicts(self.one(thyao(stop_loss_stop_price=308.0, take_profit_limit_price=317))), ["PASS"])

    def test_extended_hours_is_never_allowed(self):
        res = self.one(thyao(type="limit", limit_price="312.50", extended_hours=True))
        self.assertIn("extended hours: this pack trades BIST continuous trading (10:00-18:00) only", reasons(res))


class Exits(unittest.TestCase):
    PF = {"cash": 43346.72, "positions": [{"ticker": "THYAO", "shares": 181,
                                            "lots": [{"date": TODAY, "shares": 181, "cost": 56653.28}]}]}
    SELL = {"symbol": "THYAO", "side": "sell", "qty": "181", "type": "market", "time_in_force": "day"}

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        full = {"orders": [placed("THYAO"), placed("ASELS"), placed("GARAN")] +
                          [placed("T%dXX" % i, max_loss=1) for i in range(6)] +
                          [placed("THYAO", side="sell", kind="exit", max_loss=None)]}
        c = cfg(blocked=["THYAO"], max_order_usd=1000, max_daily_loss_usd=50)
        res = check.check([self.SELL], c, portfolio=self.PF, state=full, now=at("2026-11-09 17:55"))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        why = warnings(res)
        for word in ("blocked:", "duplicate:", "daily limit:"):
            self.assertIn("exit, not blocked: " + word, why)
        res = check.check([self.SELL], cfg(), portfolio=self.PF, now=at("2026-11-07 12:00"))
        self.assertEqual(verdicts(res), ["PASS"])

    def test_a_partial_exit_and_the_rest(self):
        half = dict(self.SELL, qty="90")
        res = check.check([half, half], cfg(), portfolio=self.PF, prices={"THYAO": 315.0}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual([r["kind"] for r in res["results"]], ["exit", "exit"])
        self.assertIn("exit, not blocked: duplicate", warnings(res, 1))
        res = check.check([half, half, half], cfg(), portfolio=self.PF, prices={"THYAO": 315.0}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK"])
        self.assertIn("short:", reasons(res, 2))

    def test_format_ticks_and_short_still_apply_to_a_sell(self):
        res = check.check([dict(self.SELL, type="limit")], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("needs a limit_price", reasons(res))
        res = check.check([dict(self.SELL, type="limit", limit_price="315.10")], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("tick: limit_price 315.10", reasons(res))
        res = check.check([dict(self.SELL, qty="182")], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("short: selling more THYAO than you hold (181 shares)", reasons(res))
        res = check.check([self.SELL], cfg(), now=at(NOW))
        self.assertIn("record the fill first", reasons(res))

    def test_buy_then_sell_in_one_batch(self):
        res = check.check([thyao(), self.SELL], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual(res["results"][1]["kind"], "exit")


class AccountChecks(unittest.TestCase):
    """The account checks of the weights mode (day-trade checks off)."""

    def one(self, order, **over):
        return check.check([order], v1(**over), prices=PRICES, today=TODAY)["results"][0]

    def test_the_split_passes_on_a_fresh_100000_paper_account(self):
        res = check.check(split_orders(), v1(), prices=PRICES, today=TODAY)
        self.assertEqual(verdicts(res), ["PASS"] * 5, [r["reasons"] for r in res["results"]])
        # 100.000 - 89.748 - 89.748 x 0,001605 (144,05) = 10.107,95, still above the 10.000 buffer
        self.assertEqual(res["cash_after"], 10107.95)
        self.assertEqual(res["results"][0]["value_try"], 25000)
        self.assertEqual(res["results"][0]["amount"], "80 shares")

    def test_a_sixth_buy_is_blocked_three_ways(self):
        res = check.check(split_orders() + [buy("THYAO", 1000)], v1(), prices=PRICES, today=TODAY)
        self.assertEqual(verdicts(res), ["PASS"] * 5 + ["BLOCK"])
        why = reasons(res, 5)
        self.assertIn("cash:", why)
        self.assertIn("position:", why)
        self.assertIn("duplicate:", why)

    def test_the_example_config_blocks_buys_without_a_stop(self):
        res = check.check(split_orders(), cfg(), prices=PRICES, now=at(NOW))
        self.assertEqual(verdicts(res)[0], "BLOCK")
        self.assertIn("stop: no stop", reasons(res))

    def test_max_and_min_order(self):
        self.assertIn("50.000,00 TL is over your 40.000,00 TL max order",
                      " ".join(self.one(buy("GARAN", 50000), max_position_pct=100)["reasons"]))
        self.assertIn("50,00 TL is under your 100,00 TL minimum order", " ".join(self.one(buy("GARAN", 50))["reasons"]))

    def test_blocked_list(self):
        r = self.one(buy("GARAN", 1000), blocked=["garan"])
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("blocked list", r["reasons"][0])

    def test_derivatives_off_unless_allowed(self):
        for sym in ("O_THYAOE1126C300.00", "F_XU0301226", "BE270617C00100000"):
            opt = {"symbol": sym, "side": "buy", "qty": "10", "type": "limit", "limit_price": "12.50",
                   "time_in_force": "day"}
            r = self.one(opt)
            self.assertEqual(r["verdict"], "BLOCK", sym)
            self.assertIn("derivative", " ".join(r["reasons"]))
            r = self.one(opt, allow_options=True)
            self.assertEqual(r["verdict"], "PASS", sym)
            self.assertTrue(r["warnings"])

    def test_only_bist_share_codes(self):
        crypto = {"symbol": "BTC/USD", "side": "buy", "notional": "500", "type": "market", "time_in_force": "day"}
        self.assertIn("does not look like a BIST share code", " ".join(self.one(crypto)["reasons"]))
        yahoo = dict(buy("THYAO.IS", 1000))
        self.assertIn("use the bare code THYAO, without .IS", " ".join(self.one(yahoo)["reasons"]))
        self.assertEqual(self.one(buy("A1CAP", 1000))["verdict"], "PASS")

    def test_format_rules(self):
        both = dict(buy("GARAN", 1000), qty="1")
        self.assertIn("exactly one of qty or notional", " ".join(self.one(both)["reasons"]))
        lim = dict(buy("GARAN", 1000), type="limit", limit_price="135.50")
        self.assertIn("market order with time_in_force day", " ".join(self.one(lim)["reasons"]))
        nolimit = {"symbol": "GARAN", "side": "buy", "qty": "1", "type": "limit", "time_in_force": "day"}
        self.assertIn("needs a limit_price", " ".join(self.one(nolimit)["reasons"]))
        bad = {"symbol": "GARAN", "side": "hold", "notional": "1000"}
        self.assertIn("side must be buy or sell", " ".join(self.one(bad)["reasons"]))
        self.assertIn("positive number", " ".join(self.one(dict(buy("GARAN", 1000), notional="-5"))["reasons"]))

    def test_share_orders_need_a_price(self):
        q = {"symbol": "ZZZZZ", "side": "buy", "qty": "2", "type": "market", "time_in_force": "day"}
        self.assertIn("no price for ZZZZZ", " ".join(self.one(q)["reasons"]))
        q["symbol"] = "KCHOL"
        r = self.one(q)
        self.assertEqual(r["verdict"], "PASS")
        self.assertAlmostEqual(r["value_try"], 420.0)

    def test_margin(self):
        r = check.check([buy("GARAN", 1500)], v1(cash_buffer_pct=0, max_position_pct=100),
                        portfolio={"cash": 1000, "positions": []}, today=TODAY)["results"][0]
        self.assertIn("margin", " ".join(r["reasons"]))

    PF = {"cash": 500.0, "positions": [{"ticker": "KCHOL", "shares": 44,
                                         "lots": [{"date": "2026-11-06", "shares": 44, "cost": 9254.83}]}]}

    def test_short_sales_are_blocked(self):
        sell = {"symbol": "KCHOL", "side": "sell", "qty": "50", "type": "market", "time_in_force": "day"}
        r = check.check([sell], v1(), portfolio=self.PF, prices=PRICES, today=TODAY)["results"][0]
        self.assertIn("short:", " ".join(r["reasons"]))
        sell["qty"] = "3"
        r = check.check([sell], v1(), portfolio=self.PF, prices=PRICES, today=TODAY)["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"))

    def test_a_sell_frees_cash_for_a_later_buy(self):
        orders = [{"symbol": "KCHOL", "side": "sell", "qty": "20", "type": "market", "time_in_force": "day"},
                  buy("GARAN", 4000)]
        loose = v1(cash_buffer_pct=0, max_position_pct=100)
        res = check.check(orders, loose, portfolio=self.PF, prices=PRICES, today=TODAY)
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        res = check.check([buy("GARAN", 4000)], loose, portfolio=self.PF, prices=PRICES, today=TODAY)
        self.assertEqual(verdicts(res), ["BLOCK"])

    def test_daily_limit_and_already_placed_today(self):
        st = {"orders": [{"symbol": "T%dXX" % i, "side": "buy", "status": "placed", "at": TODAY + "T10:00:00+03:00"}
                         for i in range(10)]}
        res = check.check([buy("GARAN", 1000)], v1(), state=st, today=TODAY)
        self.assertIn("daily limit", reasons(res))
        res = check.check([buy("GARAN", 1000)], v1(), state=st, today="2026-11-10")
        self.assertEqual(verdicts(res), ["PASS"])
        one = {"orders": [{"symbol": "GARAN", "side": "buy", "status": "placed", "at": TODAY + "T10:00:00+03:00"}]}
        self.assertIn("already placed today", reasons(check.check([buy("GARAN", 1000)], v1(), state=one, today=TODAY)))
        declined = {"orders": [dict(one["orders"][0], status="declined")]}
        self.assertEqual(verdicts(check.check([buy("GARAN", 1000)], v1(), state=declined, today=TODAY)), ["PASS"])

    def test_live_account_warns(self):
        res = check.check([buy("GARAN", 1000)], v1(paper=False), today=TODAY)
        self.assertIn("LIVE", res["warnings"][0])

    def test_no_portfolio_and_no_budget_is_an_error(self):
        with self.assertRaises(check.RiskError):
            check.check([buy("GARAN", 1000)], {"budget": None}, today=TODAY)


class Cli(unittest.TestCase):
    def test_prices_file_freshness(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w", encoding="utf-8") as fh:
                json.dump({"as_of": "2026-11-09T07:55:00Z", "prices": {"THYAO": 314.00},
                           "detail": {"THYAO": {"time": "2026-11-09T07:52:13Z", "fetched": "2026-11-09T07:55:00Z"}}},
                          fh)
            none = os.path.join(d, "none")
            base = ["--orders", ORDERS_PATH, "--config", CFG_PATH, "--portfolio", none, "--prices", pp,
                    "--state", none, "--plan", none, "--no-write"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(check.main(base + ["--now", "2026-11-09 11:00"]), 1)
            self.assertIn("181 shares x (6.00 risk + 1.00 costs a share) = 1.266,69 TL", buf.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(base + ["--now", "2026-11-09 11:30"]), 0)

    def test_the_plan_file_gives_the_band(self):
        with tempfile.TemporaryDirectory() as d:
            o = thyao(type="limit", limit_price="336.00")
            o["plan"] = {k: v for k, v in o["plan"].items() if k != "limits"}
            op = os.path.join(d, "orders.json")
            with open(op, "w", encoding="utf-8") as fh:
                json.dump({"orders": [o]}, fh)
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", op, "--config", CFG_PATH, "--portfolio", none, "--prices", none,
                                   "--state", none, "--plan", PLAN_PATH, "--now", NOW, "--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("band: the limit price 336.00 is over today's tavan 335.50", buf.getvalue())

    def test_split_orders_with_a_config_file(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w", encoding="utf-8") as fh:
                json.dump(v1(), fh)
            pp = os.path.join(d, "prices.json")
            with open(pp, "w", encoding="utf-8") as fh:
                json.dump({"prices": PRICES}, fh)
            none = os.path.join(d, "none")
            out = os.path.join(d, "state", "risk.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", SPLIT_PATH, "--config", c, "--portfolio", none, "--prices", pp,
                                   "--state", none, "--plan", none, "--now", NOW, "--out", out])
            self.assertEqual(code, 0, buf.getvalue())
            self.assertIn("5 passed, 0 blocked", buf.getvalue())
            with open(out, encoding="utf-8") as fh:
                rec = json.load(fh)
            self.assertEqual((rec["date"], rec["market"], rec["currency"]), (TODAY, "BIST", "TRY"))
            self.assertTrue(all(len(r["fingerprint"]) == 16 for r in rec["results"]))

    def test_errors_are_one_line(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", "/nonexistent.json", "--no-write"]), 1)
        self.assertIn("no orders file", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", ORDERS_PATH, "--now", "noon", "--no-write"]), 1)
        self.assertIn("--now wants", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", ORDERS_PATH, "--today", "pazartesi", "--no-write"]), 1)
        self.assertIn("--today wants", err.getvalue())


if __name__ == "__main__":
    unittest.main()
