"""trader-budget tests (Borsa İstanbul). Every number is checked by hand in the comments.

Costs a share per side at the example rates: price x (0.0015 x 1.05 + 0.00003) = price x 0.001605.
"""

import io
import json
import math
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

size = load("trader-budget", "size")

CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
SKILL = os.path.join(ROOT, "trader-budget", "SKILL.md")
PLAN = os.path.join(FIX, "trader-budget", "plan-thyao.json")
TARGETS = os.path.join(ROOT, "trader-budget", "targets.example.json")
ORDERS = os.path.join(FIX, "trader-risk", "orders-thyao.json")
SPLIT = os.path.join(FIX, "trader-risk", "orders-split.json")
NOW = "2026-11-09 10:30"
TODAY = "2026-11-09"
PRICES = {"THYAO": 312.50, "ASELS": 180.00, "GARAN": 135.50, "BIMAS": 540.00, "KCHOL": 210.00}
# the example config, key for key (BIST: TL amounts, 10:15-17:30 entries, no PDT, costs)
CONTRACT_CFG = {
    "market": "BIST", "currency": "TRY",
    "budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 80000, "min_order_usd": 100,
    "max_loss_per_trade_usd": 1000, "max_daily_loss_usd": 3000, "max_trades_per_day": 3, "max_orders_per_day": 10,
    "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "10:15", "no_entries_after": "17:30", "flat_by": "17:50",
    "fractional": False, "broker": "manual", "paper": True, "blocked": [],
    "allow_options": False, "allow_margin": False, "allow_short": False, "pdt_guard": False,
    "commission_rate": 0.0015, "bsmv_rate": 0.05, "exchange_fee_rate": 0.00003,
}
# weights-mode rules: 100.000 TL, 10% cash, 25% cap, whole shares
V1 = {"budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 25, "max_order_usd": 40000, "min_order_usd": 100,
      "fractional": False, "paper": True, "blocked": []}
NOCOST = {"commission_rate": 0, "bsmv_rate": 0, "exchange_fee_rate": 0}


def cfg(**over):
    with open(CFG, encoding="utf-8") as fh:
        c = json.load(fh)
    c.update(over)
    return c


def v1(**over):
    c = dict(V1)
    c.update(over)
    return c


def plan(**over):
    with open(PLAN, encoding="utf-8") as fh:
        p = json.load(fh)
    p.update(over)
    return p


def rows(path):
    with open(path, encoding="utf-8") as fh:
        return size.parse_weights(json.load(fh))


def amounts(result):
    return {p["ticker"]: p["dollars"] for p in result["positions"]}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def prices_doc(price, quote_time="2026-11-09T07:52:13Z"):      # 10:52:13 TSİ
    return {"as_of": quote_time, "source": "yahoo", "prices": {"THYAO": price},
            "detail": {"THYAO": {"prev_close": 305.0, "time": quote_time, "source": "yahoo"}}}


class Helpers(unittest.TestCase):
    def test_tick_table(self):
        for price, tick in ((19.99, 0.01), (20.0, 0.02), (49.98, 0.02), (50.0, 0.05), (100.0, 0.10),
                            (249.9, 0.10), (250.0, 0.25), (500.0, 0.50), (1000.0, 1.00), (2500.0, 2.50)):
            self.assertEqual(size.tick_size(price), tick, price)

    def test_round_tick_modes(self):
        self.assertEqual(size.round_tick(308.10, "down"), 308.00)
        self.assertEqual(size.round_tick(316.90, "up"), 317.00)
        self.assertEqual(size.round_tick(312.40), 312.50)
        self.assertEqual(size.round_tick(312.30), 312.25)
        self.assertEqual(size.round_tick(52.43), 52.45)
        self.assertEqual(size.round_tick(308.00, "down"), 308.00)     # already on the grid: unchanged
        self.assertEqual(size.round_tick(317.00, "up"), 317.00)

    def test_float32_prices_do_not_lose_a_tick(self):
        # Yahoo sends float32: 48.32 arrives as 48.31999969 and must not round down to 48.30
        self.assertEqual(size.round_tick(48.31999969, "down"), 48.32)
        self.assertEqual(size.round_tick(48.32000031, "up"), 48.32)
        self.assertEqual(size.round_tick(312.4999999, "down"), 312.50)
        self.assertEqual(size.round_tick(48.33, "down"), 48.32)          # a real off-tick price still moves
        s = size.size_trade(plan(stop=307.9999999, target=317.0000001), cfg(), today=TODAY)
        self.assertEqual((s["stop"], s["target"], s["qty"]), (308.0, 317.0, 181))

    def test_money_is_turkish(self):
        self.assertEqual(size.money(1234.5), "1.234,50 TL")
        self.assertEqual(size.money(994.76), "994,76 TL")
        self.assertEqual(size.money(100000), "100.000,00 TL")

    def test_side_cost(self):
        # 312.50 x 0.001605 = 0.5015625 ; 308 x 0.001605 = 0.49434
        self.assertAlmostEqual(size.side_cost(312.5, size.DEFAULT_COSTS), 0.5015625, places=9)
        self.assertAlmostEqual(size.side_cost(308.0, size.DEFAULT_COSTS), 0.49434, places=9)

    def test_bare_codes(self):
        self.assertEqual(size.bare("thyao.is"), "THYAO")
        self.assertEqual(size.weights_from_arg("THYAO.IS=30,asels=25"), {"THYAO": "30", "ASELS": "25"})


class TheExample(unittest.TestCase):
    """SKILL.md: THYAO 312,50 / stop 308,00 / target 317,00, 100.000 TL, 10% buffer, 1.000 TL max loss.

    risk 4,50; costs 0,5015625 + 0,49434 = 0,9959 a share; 1.000 / 5,4959 = 181,95 -> 181 lots.
    181 x 312,50 = 56.562,50 TL; 181 x 4,50 = 814,50; 181 x 0,9959 = 180,26; max loss 994,76 TL."""

    def test_config_example_is_the_contract(self):
        with open(CFG, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), CONTRACT_CFG)
        self.assertIs(CONTRACT_CFG["pdt_guard"], False)

    def test_181_lots_bound_by_the_max_loss(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 181)
        self.assertEqual(s["qty"], math.floor(1000 / (4.50 + 0.9959)))
        self.assertEqual(s["bound_by"], "max loss")
        self.assertEqual((s["entry"], s["stop"], s["target"]), (312.5, 308.0, 317.0))
        self.assertEqual((s["risk_per_share"], s["cost_per_share"]), (4.5, 0.9959))
        self.assertEqual(s["cost_per_share"], plan()["cost_per_share"])     # agrees with trader-exit
        self.assertEqual((s["cost"], s["loss_at_stop"], s["costs_round_trip"], s["max_loss"]),
                         (56562.5, 814.5, 180.26, 994.76))
        self.assertEqual(s["reward_risk"], 1.0)
        # after costs a win sells at the target: (4,50 - (0,5016 + 0,5088)) / 5,4959 = 0,63 (trader-exit says 0,63)
        self.assertEqual(s["reward_risk_net"], 0.63)
        self.assertEqual(s["reward_risk_net"], plan()["reward_risk_net"])
        self.assertEqual(s["cash_buffer"], 10000)
        # cap 75.000 / 312,50 = 240; max order 80.000 / 312,50 = 256; cash 90.000 / 313,0016 = 287
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 181), ("position cap", 240), ("max order", 256), ("cash", 287)])
        self.assertEqual([l["name"] for l in s["limits"] if l["binds"]], ["max loss"])
        self.assertEqual(s["daily_limits"], {"base": 305.0, "taban": 274.5, "tavan": 335.5})
        self.assertEqual(s["currency"], "TRY")
        self.assertIsNone(s["refusal"])

    def test_the_bracket_order(self):
        o = size.bracket_order(size.size_trade(plan(), cfg(), today=TODAY))
        why = o.pop("why")
        self.assertEqual(o, {
            "symbol": "THYAO", "side": "buy", "qty": "181", "type": "market", "time_in_force": "day",
            "order_class": "bracket", "take_profit_limit_price": "317.00", "stop_loss_stop_price": "308.00",
            "plan": {"entry": 312.5, "stop": 308.0, "target": 317.0, "risk_per_share": 4.5, "cost_per_share": 0.9959,
                     "max_loss": 994.76, "setup": "orb", "entry_time": "10:30 AM", "flat_by": "17:50",
                     "limits": {"base": 305.0, "taban": 274.5, "tavan": 335.5}}})
        self.assertIn("bound by your 1.000,00 TL max loss per trade", why)
        self.assertIn("181 x (4.50 risk + 1.00 costs) = 994,76 TL at the stop", why)

    def test_the_fixture_orders_file_is_what_it_writes(self):
        with open(ORDERS, encoding="utf-8") as fh:
            fixture = json.load(fh)["orders"][0]
        self.assertEqual(size.bracket_order(size.size_trade(plan(), cfg(), today=TODAY)), fixture)

    def test_the_skill_md_example_is_what_it_writes(self):
        with open(SKILL, encoding="utf-8") as fh:
            text = fh.read()
        block = re.search(r"```json\n(.*?)```", text, re.S).group(1)
        self.assertEqual(json.loads(block), size.bracket_order(size.size_trade(plan(), cfg(), today=TODAY)))
        for words in ("181 lot", "56.562,50 TL", "814,50 TL", "180,26 TL", "994,76 TL", "0,9959", "| 240 |",
                      "| 256 |", "| 287 |", "0,63"):
            self.assertIn(words, text)

    def test_a_limit_order(self):
        o = size.bracket_order(size.size_trade(plan(), cfg(), limit=312.50, today=TODAY))
        self.assertEqual((o["type"], o["limit_price"], o["qty"]), ("limit", "312.50", "181"))
        # limit 311,00: risk 3,00, costs 0,499155 + 0,49434 = 0,9935; max loss 1.000 / 3,9935 = 250,
        # but the cap 75.000 / 311 = 241 binds. 241 x 3 = 723 + 241 x 0,9935 = 239,43 -> 962,43
        s = size.size_trade(plan(), cfg(), limit=311.00, today=TODAY)
        self.assertEqual((s["risk_per_share"], s["cost_per_share"], s["qty"], s["bound_by"], s["max_loss"]),
                         (3.0, 0.9935, 241, "position cap", 962.43))
        self.assertEqual([l["shares"] for l in s["limits"]], [250, 241, 257, 288])
        self.assertEqual(size.bracket_order(s)["limit_price"], "311.00")

    def test_cli_writes_one_bracket_order(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--prices", os.path.join(d, "none.json"), "--now", NOW, "--write", out])
            self.assertEqual(code, 1)          # a --prices file that does not exist is an error, not ignored
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 0, text)
            self.assertIn("Size: 181 shares (lots) of THYAO = 56.562,50 TL at 312.50", text)
            self.assertIn("181 x 4.50 = 814,50 TL, plus 180,26 TL round-trip costs = 994,76 TL", text)
            self.assertIn("Today's limits: taban 274.50, tavan 335.50 (base 305.00", text)
            self.assertIn("<- binds", text)
            self.assertIn("Flat by 17:50 TSİ", text)
            self.assertIn("yatırım tavsiyesi değildir", text)
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual((doc["market"], doc["currency"], doc["status"], doc["created_by"]),
                             ("BIST", "TRY", "proposed", "trader-budget"))
            self.assertIs(doc["paper"], True)
            self.assertEqual(doc["created_at"], "2026-11-09T07:30:00Z")       # 10:30 TSİ = 07:30 UTC
            self.assertEqual(len(doc["orders"]), 1)
            o = doc["orders"][0]
            self.assertEqual((o["order_class"], o["take_profit_limit_price"], o["stop_loss_stop_price"]),
                             ("bracket", "317.00", "308.00"))
            self.assertEqual(o["plan"]["max_loss"], 994.76)
            self.assertNotIn("TSLA", json.dumps(doc))

    def test_cli_json_and_an_off_tick_limit(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                             "--limit", "312.40", "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["market"], out["currency"]), ("BIST", "TRY"))
        self.assertEqual(out["sizing"]["qty"], 181)
        self.assertEqual(out["orders"][0]["limit_price"], "312.50")      # 312,40 is off the 0,25 grid
        self.assertIn("off the BIST tick grid (tick 0.25 at this price): using 312.50", out["notes"][0])
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                             "--limit", "312,50", "--json"])
        self.assertEqual(json.loads(text)["orders"][0]["limit_price"], "312.50")     # a Turkish comma works


class PlanRules(unittest.TestCase):
    def test_stop_and_target_snap_to_the_tick(self):
        s = size.size_trade(plan(stop=308.10, target=316.90), cfg(), today=TODAY)
        self.assertEqual((s["stop"], s["target"]), (308.0, 317.0))         # stop down, target up
        o = size.bracket_order(s)
        self.assertEqual((o["stop_loss_stop_price"], o["take_profit_limit_price"]), ("308.00", "317.00"))
        s = size.size_trade(plan(entry=52.43, stop=51.02, target=54.01, limits=None), cfg(), today=TODAY)
        self.assertEqual((s["entry"], s["stop"], s["target"]), (52.45, 51.0, 54.05))

    def test_risk_is_not_snapped_to_a_tick(self):
        # stop 291,25: risk 21,25 (a tick-rounded 21,24 would be wrong). costs 0,5016 + 0,4675 = 0,9690;
        # 1.000 / 22,219 = 45 lots
        s = size.size_trade(plan(stop=291.25), cfg(min_reward_risk=0), today=TODAY)
        self.assertEqual((s["risk_per_share"], s["cost_per_share"], s["qty"]), (21.25, 0.969, 45))
        why = size.bracket_order(s)["why"]
        self.assertIn("45 x (21.25 risk + 0.97 costs)", why)
        text = size.plan_report(s, size.bracket_order(s))
        self.assertIn("Risk 21.25 a share", text)
        self.assertIn("45 x 21.25 = 956,25 TL", text)

    def test_stop_under_the_taban_is_refused(self):
        s = size.size_trade(plan(stop=274.00), cfg(min_reward_risk=0), today=TODAY)
        self.assertIn("the stop 274.00 is under today's taban (lower limit) 274.50", s["refusal"])
        self.assertIsNone(size.bracket_order(s))
        s = size.size_trade(plan(stop=274.50), cfg(min_reward_risk=0), today=TODAY)       # at the taban is fine
        self.assertIsNone(s["refusal"])

    def test_target_over_the_tavan_is_refused(self):
        s = size.size_trade(plan(target=336.00), cfg(), today=TODAY)
        self.assertIn("the target 336.00 is over today's tavan (upper limit) 335.50", s["refusal"])
        self.assertIsNone(size.bracket_order(s))
        self.assertIsNone(size.size_trade(plan(target=335.50), cfg(), today=TODAY)["refusal"])
        # no limits in the plan: nothing to check against
        self.assertIsNone(size.size_trade(plan(target=336.00, limits=None), cfg(), today=TODAY)["refusal"])

    def test_refused_by_the_cli_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "plan.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(plan(target=336.00), fh)
            out = os.path.join(d, "orders.json")
            code, text, _ = run(["--plan", p, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 1)
            self.assertIn("No order: the target 336.00 is over today's tavan", text)
            self.assertFalse(os.path.exists(out))

    def test_zero_shares_is_a_plain_refusal(self):
        s = size.size_trade(plan(), cfg(max_loss_per_trade_usd=5), today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("more than your 5,00 TL max loss per trade", s["refusal"])
        self.assertIsNone(size.bracket_order(s))

    def test_each_limit_can_bind(self):
        # 10.000 / 312,50 = 32
        s = size.size_trade(plan(), cfg(max_order_usd=10000), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (32, "max order"))
        # 10% of 100.000 = 10.000 / 312,50 = 32
        s = size.size_trade(plan(), cfg(max_position_pct=10), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (32, "position cap"))
        # cash 20.000 minus the 10.000 buffer = 10.000 / 313,0016 = 31,9 -> 31
        s = size.size_trade(plan(), cfg(), cash=20000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["cash_available"]), (31, "cash", 10000))
        # the cash limit counts the buy costs: 10.000 / 312,50 would be exactly 32
        s = size.size_trade(plan(), cfg(cash_buffer_pct=0), cash=10000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (31, "cash"))

    def test_a_fresh_higher_price_means_a_bigger_risk(self):
        # 314,00: risk 6,00, costs 0,50397 + 0,49434 = 0,9983; 1.000 / 6,9983 = 142;
        # 142 x 6 = 852 + 142 x 0,9983 = 141,76 -> 993,76. reward 3 / risk 6 = 0,5
        s = size.size_trade(plan(), cfg(min_reward_risk=0), price=314.00, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"], s["cost_per_share"]),
                         (314.0, "price", 6.0, 0.9983))
        self.assertEqual((s["qty"], s["max_loss"], s["reward_risk"]), (142, 993.76, 0.5))
        self.assertIsNone(s["refusal"])
        s = size.size_trade(plan(), cfg(), price=314.00, today=TODAY)
        self.assertIn("reward:risk 0.50 (target 317.00, entry 314.00, stop 308.00) is under your minimum 1",
                      s["refusal"])
        self.assertIsNone(size.bracket_order(s))
        lower = size.size_trade(plan(), cfg(), price=311.00, today=TODAY)
        self.assertEqual((lower["entry"], lower["qty"]), (312.5, 181))

    def test_prices_file_is_used_only_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w", encoding="utf-8") as fh:
                json.dump(prices_doc(314.00), fh)
            base = ["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"), "--prices", pp, "--json"]
            code, text, _ = run(base + ["--now", "2026-11-09 11:00"])
            out = json.loads(text)
            self.assertEqual((code, out["sizing"]["qty"], out["orders"]), (1, 142, []))
            self.assertIn("THYAO last 314.00 (as of 10:52 TSİ) is above the plan's entry 312.50", out["notes"][0])
            out = json.loads(run(base + ["--now", "2026-11-09 11:30"])[1])
            self.assertEqual(out["sizing"]["qty"], 181)
            self.assertIn("not used", out["notes"][0])

    def test_the_portfolio_cash_caps_the_size(self):
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "portfolio.json")
            with open(pf, "w", encoding="utf-8") as fh:
                json.dump({"cash": 20000, "positions": []}, fh)
            out = json.loads(run(["--plan", PLAN, "--config", CFG, "--portfolio", pf, "--now", NOW, "--json"])[1])
            self.assertEqual((out["sizing"]["qty"], out["sizing"]["bound_by"]), (31, "cash"))

    def test_bad_plans(self):
        for p, word in ((plan(stop=313.0), "not below the entry"),
                        (plan(target=312.0), "already happened"),
                        (plan(target=None), "positive target"),
                        (plan(side="sell"), "long (buy) day trades only"),
                        (plan(symbol=""), "no symbol")):
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(p, cfg(), today=TODAY)
            self.assertIn(word, str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), price=318.00, today=TODAY)
        self.assertIn("already happened", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(blocked=["thyao"]), today=TODAY)
        self.assertIn("blocked list", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), today="2026-11-10")
        self.assertIn("make a new plan", str(e.exception))

    def test_a_yahoo_symbol_is_stored_bare(self):
        s = size.size_trade(plan(symbol="THYAO.IS"), cfg(), today=TODAY)
        self.assertEqual(size.bracket_order(s)["symbol"], "THYAO")

    def test_under_the_minimum_order(self):
        # 4,00 / 3,00 / 5,00: costs 0,00642 + 0,00482 = 0,0112; 50 / 1,0112 = 49 lots = 196 TL
        cheap = plan(symbol="KOZAL", entry=4.0, stop=3.0, target=5.0, limits=None)
        s = size.size_trade(cheap, cfg(max_loss_per_trade_usd=50, min_order_usd=500), today=TODAY)
        self.assertEqual((s["qty"], s["cost"]), (49, 196.0))
        self.assertIn("49 shares of KOZAL is 196,00 TL, under your 500,00 TL minimum order", s["refusal"])
        self.assertIsNone(size.bracket_order(s))

    def test_missing_plan_and_weights_only_flags(self):
        code, _, err = run(["--plan", "/nonexistent/plan.json", "--config", CFG, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--top", "2"])
        self.assertEqual(code, 1)
        self.assertIn("--weights", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", "tomorrow"])
        self.assertEqual(code, 1)
        self.assertIn("--now wants", err)
        self.assertNotIn("Traceback", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--limit", "abc"])
        self.assertEqual(code, 1)
        self.assertIn("--limit wants a price", err)


class WeightsMode(unittest.TestCase):
    """Split 100.000 TL over five BIST 30 names, 10% cash, 25% cap."""

    def test_the_example_targets_file(self):
        r = rows(TARGETS)
        self.assertEqual([x["ticker"] for x in r], ["THYAO", "ASELS", "GARAN", "BIMAS", "KCHOL"])
        self.assertEqual(sum(x["weight"] for x in r), 100)

    def test_lira_split_without_costs(self):
        # 90.000 investable; THYAO 30% = 27.000 is over the 25.000 cap, so it is pinned and 65.000 is
        # shared by 70: ASELS 23.214,29 GARAN 18.571,43 BIMAS 13.928,57 KCHOL 9.285,71 -> whole lira,
        # the extra lira from rounding comes off the biggest uncapped (ASELS)
        r = size.size(rows(TARGETS), v1(fractional=True, **NOCOST))
        self.assertEqual(amounts(r), {"THYAO": 25000, "ASELS": 23214, "GARAN": 18571, "BIMAS": 13929, "KCHOL": 9286})
        self.assertEqual((r["cash_buffer"], r["investable"], r["invested"], r["idle"], r["cap"]),
                         (10000, 90000, 90000, 0, 25000))
        self.assertEqual([p["ticker"] for p in r["positions"] if p["capped"]], ["THYAO"])

    def test_the_buy_costs_are_kept_out_of_the_split(self):
        # 90.000 / 1,001605 = 89.855,78 investable, 144,22 TL kept for the commissions
        r = size.size(rows(TARGETS), v1(fractional=True))
        self.assertEqual((r["investable"], r["cost_reserve"]), (89855.78, 144.22))
        self.assertLessEqual(r["invested"] * (1 + 0.001605), 90000)

    def test_whole_shares_with_prices_is_the_fixture(self):
        r = size.size(rows(TARGETS), v1(), prices=PRICES)
        self.assertTrue(r["whole_shares"])
        shares = {p["ticker"]: p["shares"] for p in r["positions"]}
        self.assertEqual(shares, {"THYAO": 80, "ASELS": 128, "GARAN": 136, "BIMAS": 26, "KCHOL": 44})
        for p in r["positions"]:
            self.assertAlmostEqual(p["dollars"], p["shares"] * PRICES[p["ticker"]], places=2)
            self.assertLessEqual(p["dollars"], 25000)
        self.assertEqual(r["invested"], 89748.0)
        # orders plus their buy costs stay out of the 10.000 TL buffer: trader-risk would block otherwise
        self.assertLessEqual(r["invested"] + size.side_cost(r["invested"], size.DEFAULT_COSTS), 90000)
        orders = size.orders_from(r)
        with open(SPLIT, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual([(o["symbol"], o["qty"]) for o in orders],
                         [(o["symbol"], o["qty"]) for o in doc["orders"]])
        self.assertTrue(all("qty" in o and "notional" not in o for o in orders))
        self.assertEqual((doc["market"], doc["currency"]), ("BIST", "TRY"))

    def test_whole_shares_need_prices(self):
        with self.assertRaises(size.SizeError) as e:
            size.size(rows(TARGETS), v1())
        self.assertIn("need a price for", str(e.exception))
        with self.assertRaises(size.SizeError):
            size.size(rows(TARGETS), cfg())          # the example config is whole shares (BIST)

    def test_fractional_orders_are_notional(self):
        r = size.size(rows(TARGETS), v1(fractional=True, **NOCOST))
        orders = size.orders_from(r, "örnek")
        self.assertEqual(orders[0], {"symbol": "THYAO", "side": "buy", "notional": "25000", "type": "market",
                                     "time_in_force": "day", "why": orders[0]["why"]})
        self.assertIn("capped at 25% of the 100.000,00 TL budget", orders[0]["why"])
        self.assertIn("1 lot = 1 share", size.report(r, orders))

    def test_cents_mode_keeps_the_total(self):
        r = size.size(rows(TARGETS), v1(fractional=True, **NOCOST), cents=True)
        d = amounts(r)
        self.assertAlmostEqual(sum(d.values()), 90000, places=2)
        self.assertAlmostEqual(d["ASELS"], 23214.29, places=2)
        self.assertEqual(size.orders_from(r)[1]["notional"], "23214.29")

    def test_cap_redistributes_until_nobody_is_over(self):
        w = size.parse_weights({"A1CAP": 60, "GARAN": 30, "KCHOL": 10})
        r = size.size(w, dict(NOCOST, budget=1000, cash_buffer_pct=0, max_position_pct=40, min_order_usd=1))
        self.assertEqual(amounts(r), {"A1CAP": 400, "GARAN": 400, "KCHOL": 200})
        self.assertEqual(sorted(p["ticker"] for p in r["positions"] if p["capped"]), ["A1CAP", "GARAN"])

    def test_everything_capped_leaves_money_idle(self):
        w = size.parse_weights({"THYAO": 50, "ASELS": 50})
        r = size.size(w, v1(fractional=True, **NOCOST))
        self.assertEqual(amounts(r), {"THYAO": 25000, "ASELS": 25000})
        self.assertEqual(r["idle"], 40000)
        self.assertIn("every stock is at its cap", size.report(r, size.orders_from(r)))

    def test_slices_under_the_minimum_are_dropped_and_shared_out(self):
        w = size.parse_weights({"THYAO": 90, "ASELS": 7, "GARAN": 3})
        r = size.size(w, dict(NOCOST, budget=1000, cash_buffer_pct=10, max_position_pct=100, min_order_usd=50))
        self.assertNotIn("GARAN", amounts(r))
        self.assertEqual(sum(amounts(r).values()), 900)
        self.assertEqual(r["dropped"][0]["ticker"], "GARAN")

    def test_blocked_tickers_are_dropped_first(self):
        r = size.size(rows(TARGETS), v1(fractional=True, blocked=["kchol"], **NOCOST))
        self.assertNotIn("KCHOL", amounts(r))
        self.assertEqual(r["invested"], 90000)
        self.assertIn("blocked", r["dropped"][0]["reason"])

    def test_top_n(self):
        r = size.size(rows(TARGETS), v1(fractional=True, max_position_pct=100), top=2)
        self.assertEqual(sorted(amounts(r)), ["ASELS", "THYAO"])

    def test_no_budget_and_duplicates_are_errors(self):
        with self.assertRaises(size.SizeError):
            size.size(rows(TARGETS), {"cash_buffer_pct": 10})
        with self.assertRaises(size.SizeError):
            size.size([{"ticker": "THYAO", "name": "", "weight": 1}, {"ticker": "THYAO", "name": "", "weight": 2}],
                      v1())

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w", encoding="utf-8") as fh:
                json.dump(v1(), fh)
            pp = os.path.join(d, "prices.json")
            with open(pp, "w", encoding="utf-8") as fh:
                json.dump({"prices": PRICES}, fh)
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--targets", TARGETS, "--config", c, "--prices", pp, "--now", NOW, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("left idle", text)
            self.assertIn("144,22 TL kept for the buy costs", text)
            self.assertIn("require_stop", text)
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual([o["symbol"] for o in doc["orders"]], ["THYAO", "ASELS", "GARAN", "BIMAS", "KCHOL"])
            self.assertEqual((doc["market"], doc["currency"]), ("BIST", "TRY"))
            code, text, _ = run(["--weights", "THYAO=40,GARAN=60", "--config", c, "--prices", pp, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(text)["result"]["currency"], "TRY")
            code, _, err = run(["--targets", "/nonexistent/targets.json", "--config", c])
            self.assertEqual(code, 1)
            self.assertIn("no weights file", err)


if __name__ == "__main__":
    unittest.main()
