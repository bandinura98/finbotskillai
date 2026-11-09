"""trader-alerts on Borsa İstanbul. The quote files in tests/fixtures/trader-alerts are SYNTHETIC
Yahoo v8 answers for .IS symbols (built with tests/bist_fixtures.ts), not market data:

    yahoo-THYAO.json   2026-11-09 10:35:12 TSİ, 285.50, previous close 280.00 (taban 252.00, tavan 308.00)
    yahoo-ASELS.json   155.00, previous close 150.00 (+3.33 %)
    yahoo-GARAN.json   131.00, previous close 120.00 (tavan 132.00: within 1 %)
    yahoo-ZZZZ.json    Yahoo's real "No data found" answer
    plan-thyao.json    the plan trader-exit writes for the THYAO example in SKILL.md
"""

import datetime
import gzip
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

alerts = load("trader-alerts", "alerts")

QDIR = os.path.join(FIX, "trader-alerts")
PLAN = os.path.join(QDIR, "plan-thyao.json")
SPEC = os.path.join(ROOT, "docs", "BIST-SPEC.md")
DT = datetime.datetime
UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"


def tr(text):
    return alerts.parse_now(text)


def status(text):
    return alerts.market_status(tr(text))


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules():
    with open(PLAN, encoding="utf-8") as fh:
        return alerts.rules_from_plan(json.load(fh))


def spec_days(block):
    """The ISO dates of one dict in docs/BIST-SPEC.md (BIST_HOLIDAYS or BIST_HALF_DAYS)."""
    with open(SPEC, encoding="utf-8") as fh:
        text = fh.read()
    body = text.split(block + " = {", 1)[1].split("}", 1)[0]
    return sorted(re.findall(r'"(20\d\d-\d\d-\d\d)":', body))


class Calendar(unittest.TestCase):
    def test_holidays_and_half_days_match_the_spec(self):
        self.assertEqual(sorted(alerts.BIST_HOLIDAYS), spec_days("BIST_HOLIDAYS"))
        self.assertEqual(sorted(alerts.BIST_HALF_DAYS), spec_days("BIST_HALF_DAYS"))
        for d in list(alerts.BIST_HOLIDAYS) + list(alerts.BIST_HALF_DAYS):
            self.assertLess(datetime.date.fromisoformat(d).weekday(), 5, d)
        self.assertEqual(alerts.CALENDAR_YEARS, (2026, 2027))

    def test_istanbul_is_utc_plus_3_all_year(self):
        for utc in (DT(2026, 1, 15, 12), DT(2026, 3, 29, 2), DT(2026, 7, 1, 12), DT(2026, 10, 25, 2)):
            self.assertEqual(alerts.eastern_offset(utc), 3)
            self.assertEqual(alerts.to_eastern(utc), utc + datetime.timedelta(hours=3))

    def test_parse_now(self):
        self.assertEqual(tr("2026-11-09T10:00"), DT(2026, 11, 9, 7, 0))
        self.assertEqual(tr("2026-11-09 17:50"), DT(2026, 11, 9, 14, 50))
        self.assertEqual(tr("2026-07-01 10:00"), DT(2026, 7, 1, 7, 0))      # no summer time
        self.assertEqual(tr("2026-11-09T15:00:00Z"), DT(2026, 11, 9, 15, 0))
        self.assertEqual(tr("2026-11-09T10:00+03:00"), DT(2026, 11, 9, 7, 0))
        with self.assertRaises(alerts.AlertError):
            tr("yarın")

    def test_phases_of_a_normal_day(self):
        want = [("09:00", "pre_open", False), ("09:39", "pre_open", False), ("09:40", "opening_auction", False),
                ("09:54", "opening_auction", False), ("09:55", "pre_open", False), ("09:59", "pre_open", False),
                ("10:00", "continuous", True), ("13:00", "continuous", True), ("17:59", "continuous", True),
                ("18:00", "closing_auction", False), ("18:09", "closing_auction", False),
                ("18:10", "closed", False), ("23:00", "closed", False)]
        for hm, phase, is_open in want:
            st = status("2026-11-09 " + hm)
            self.assertEqual((st["phase"], st["open"]), (phase, is_open), hm)
        st = status("2026-11-09 11:00")
        self.assertEqual((st["opens"], st["closes"], st["opening_auction"], st["closing_auction"]),
                         ("10:00", "18:00", "09:40-09:55", "18:00-18:10"))
        self.assertIsNone(st["half_day"])
        self.assertIn("opening auction", status("2026-11-09 09:45")["reason"])
        self.assertIn("kapanış", status("2026-11-09 18:05")["reason"])

    def test_next_open(self):
        self.assertEqual(status("2026-11-09 09:00")["next_open"], "2026-11-09 10:00")
        self.assertEqual(status("2026-11-09 09:45")["next_open"], "2026-11-09 10:00")
        self.assertEqual(status("2026-11-09 18:30")["next_open"], "2026-11-10 10:00")
        self.assertEqual(status("2026-11-13 18:30")["next_open"], "2026-11-16 10:00")   # Friday -> Monday
        self.assertEqual(status("2026-11-14 12:00")["reason"], "weekend")

    def test_a_holiday(self):
        st = status("2026-10-29 11:00")
        self.assertFalse(st["open"])
        self.assertEqual(st["phase"], "closed")
        self.assertEqual(st["reason"], "BIST tatili: Cumhuriyet Bayramı")
        self.assertNotIn("opens", st)
        self.assertEqual(st["next_open"], "2026-10-30 10:00")
        st = status("2026-05-26 13:00")                      # arefe, then the four-day Kurban Bayramı
        self.assertEqual(st["next_open"], "2026-06-01 10:00")
        self.assertIn("Kurban", status("2026-05-28 10:30")["reason"])

    def test_a_half_day(self):
        st = status("2026-10-28 11:00")
        self.assertTrue(st["open"])
        self.assertEqual((st["closes"], st["closing_auction"]), ("12:30", "12:30-12:40"))
        self.assertEqual(st["half_day"], "yarım gün: Cumhuriyet Bayramı arefesi")
        self.assertEqual(st["early_close"], "Cumhuriyet Bayramı arefesi")
        self.assertTrue(status("2026-10-28 12:29")["open"])
        self.assertEqual(status("2026-10-28 12:30")["phase"], "closing_auction")
        self.assertEqual(status("2026-10-28 12:40")["phase"], "closed")
        self.assertEqual(status("2026-10-28 14:00")["next_open"], "2026-10-30 10:00")   # 29 Ekim is closed

    def test_outside_the_known_years(self):
        st = status("2028-03-01 11:00")
        self.assertFalse(st["calendar_known"])
        self.assertTrue(st["open"])


class Symbols(unittest.TestCase):
    def test_yahoo_suffix(self):
        self.assertEqual(alerts.to_yahoo("thyao"), "THYAO.IS")
        self.assertEqual(alerts.to_yahoo("XU100"), "XU100.IS")
        self.assertEqual(alerts.to_yahoo("THYAO.IS"), "THYAO.IS")
        self.assertEqual(alerts.to_yahoo("^XU100"), "^XU100")
        self.assertEqual(alerts.from_yahoo("thyao.is"), "THYAO")
        self.assertEqual(alerts.from_yahoo("ASELS"), "ASELS")

    def test_money_ticks_and_limits(self):
        self.assertEqual(alerts.money(1234.5), "1.234,50 TL")
        self.assertEqual(alerts.tick_size(19.99), 0.01)
        self.assertEqual(alerts.tick_size(285.5), 0.25)
        self.assertEqual(alerts.daily_limits(280.0), (252.0, 308.0))
        self.assertEqual(alerts.daily_limits(120.0), (108.0, 132.0))
        self.assertEqual(alerts.daily_limits(287.3), (258.75, 316.0))   # rounded inwards to the 0.25 tick


class Quotes(unittest.TestCase):
    def test_yahoo_fixtures(self):
        with open(os.path.join(QDIR, "yahoo-THYAO.json"), encoding="utf-8") as fh:
            q = alerts.parse_yahoo(fh.read(), "THYAO")
        self.assertEqual((q["symbol"], q["price"], q["prev_close"], q["open"]), ("THYAO", 285.5, 280.0, 282.0))
        self.assertEqual((q["high"], q["low"], q["currency"]), (285.75, 279.5, "TRY"))
        self.assertEqual(q["time"], "2026-11-09T07:35:12Z")       # 10:35:12 TSİ

    def test_stooq_parser_still_reads_saved_files(self):
        q = alerts.parse_stooq("Symbol,Date,Time,Open,High,Low,Close,Volume\n"
                               "THYAO,2026-11-09,10:35:12,282,285.75,279.5,285.5,3215400\n", "THYAO")
        self.assertEqual((q["price"], q["open"], q["source"]), (285.5, 282.0, "stooq"))
        with self.assertRaises(alerts.AlertError):
            alerts.parse_stooq("Symbol,Date,Time,Open,High,Low,Close,Volume\nZZZZ,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n",
                               "ZZZZ")

    def test_live_stooq_is_refused(self):
        q, errors = alerts.get_quotes(["THYAO"], "stooq")
        self.assertEqual(q, {})
        self.assertIn("Stooq has no BIST quotes", errors[0])

    def test_urls_and_user_agent(self):
        self.assertEqual(alerts.UA, UA)
        self.assertEqual(alerts.yahoo_url("thyao"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/THYAO.IS?range=1d&interval=1d")
        self.assertIn("//query1.finance.yahoo.com/v8/finance/chart/XU100.IS?",
                      alerts.yahoo_url("XU100", alerts.YAHOO_HOSTS[1]))

    def test_offline_quotes_map_back_to_the_bare_code(self):
        q, errors = alerts.get_quotes(["THYAO.IS", "asels", "ZZZZ", "NOPE"], "yahoo", QDIR)
        self.assertEqual(sorted(q), ["ASELS", "THYAO"])
        self.assertEqual((q["THYAO"]["symbol"], q["THYAO"]["price"]), ("THYAO", 285.5))
        self.assertEqual(q["ASELS"]["price"], 155.0)
        self.assertEqual(len(errors), 2)
        self.assertIn("No data found", errors[0])
        self.assertIn("no offline quote file", errors[1])

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-THYAO.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-THYAO.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["THYAO"], "yahoo", d)
            self.assertEqual((q["THYAO"]["price"], errors), (285.5, []))
        finally:
            shutil.rmtree(d)


class FakeNet(object):
    """Stands in for urllib: answers each request with the next status code."""

    def __init__(self, codes, body=None):
        self.codes, self.calls, self.sleeps = list(codes), [], []
        with open(os.path.join(QDIR, "yahoo-THYAO.json"), encoding="utf-8") as fh:
            self.body = body or fh.read()

    def urlopen(self, req, timeout=None):
        self.calls.append((req.full_url.split("/")[2], req.get_header("User-agent"), req.full_url))
        code = self.codes.pop(0)
        if code != 200:
            fp = io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found",'
                            b'"description":"No data found, symbol may be delisted"}}}' if code == 404 else b"")
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, fp)
        return io.BytesIO(self.body.encode("utf-8"))


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._urlopen, alerts._sleep)

    def tearDown(self):
        alerts._urlopen, alerts._sleep = self.saved

    def net(self, codes):
        f = FakeNet(codes)
        alerts._urlopen = f.urlopen
        alerts._sleep = f.sleeps.append
        return f

    def hosts(self, f):
        return [c[0].split(".")[0] for c in f.calls]

    def test_query2_first_with_the_is_suffix_and_the_user_agent(self):
        f = self.net([200])
        q, errors = alerts.get_quotes(["THYAO"])
        self.assertEqual((q["THYAO"]["price"], errors), (285.5, []))
        self.assertEqual(f.calls[0][:2], ("query2.finance.yahoo.com", UA))
        self.assertIn("/chart/THYAO.IS?", f.calls[0][2])

    def test_one_retry_with_a_pause_on_429(self):
        f = self.net([429, 200])
        self.assertEqual(alerts.get_quotes(["THYAO"])[0]["THYAO"]["price"], 285.5)
        self.assertEqual(self.hosts(f), ["query2", "query2"])
        self.assertEqual(f.sleeps, [alerts.BACKOFF])

    def test_falls_back_to_query1_on_429_and_5xx(self):
        f = self.net([429, 429, 200])
        self.assertIn("THYAO", alerts.get_quotes(["THYAO"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query2", "query1"])
        f = self.net([503, 200])
        self.assertIn("THYAO", alerts.get_quotes(["THYAO"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query1"])
        f = self.net([500, 502])
        q, errors = alerts.get_quotes(["THYAO"])
        self.assertEqual(q, {})
        self.assertIn("query1.finance.yahoo.com/v8/finance/chart/THYAO.IS answered HTTP 502", errors[0])

    def test_a_404_is_yahoos_answer_and_a_403_is_not_retried(self):
        f = self.net([404])
        q, errors = alerts.get_quotes(["NOPE"])
        self.assertIn("No data found", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])
        f = self.net([403])
        q, errors = alerts.get_quotes(["THYAO"])
        self.assertIn("HTTP 403", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])

    def test_a_pause_between_tickers(self):
        f = self.net([200, 200])
        alerts.get_quotes(["THYAO", "ASELS"])
        self.assertEqual(f.sleeps, [alerts.PAUSE])


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, errors = alerts.get_quotes(["THYAO", "ASELS", "GARAN"], "yahoo", QDIR)
        self.assertEqual(errors, [])
        self.now = tr("2026-11-09 10:36")
        self.open = alerts.market_status(self.now)

    def at(self, text, rules, state=None, quotes=None):
        now = tr(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.market_status(now),
                               state or {}, now)

    def test_a_target_rings_once(self):
        rules = [{"id": "thyao-285", "symbol": "THYAO", "type": "above", "price": 285}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0]["message"], "THYAO is 285.50: at or above your alert level 285.")
        self.assertEqual(st["rung"]["thyao-285"], "2026-11-09T07:36:00Z")
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        rings, _ = self.at("2026-11-09 19:00", [dict(rules[0], id="late")])
        self.assertIn("THYAO last 285.50", rings[0]["message"])          # after the close: "last"

    def test_a_suffixed_symbol_in_a_rule_matches_the_bare_quote(self):
        rules = [{"id": "x", "symbol": "THYAO.IS", "type": "target", "price": 285.5}]
        rings, _ = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual((rings[0]["symbol"], rings[0]["message"]),
                         ("THYAO", "THYAO is 285.50: at or above your target 285.50."))

    def test_stop_and_rearm(self):
        rules = [{"id": "asels-stop", "symbol": "ASELS", "type": "stop", "price": 156, "rearm": True}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("ASELS is 155.00: at or below your stop 156.", rings[0]["message"])
        self.assertEqual(alerts.evaluate(rules, self.q, self.open, st, self.now)[0], [])
        self.q["ASELS"]["price"] = 157.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("asels-stop", st["rung"])
        self.q["ASELS"]["price"] = 155.5
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(len(rings), 1)

    def test_a_stop_without_rearm_stays_quiet(self):
        rules = [{"id": "s", "symbol": "ASELS", "type": "stop", "price": 156}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.q["ASELS"]["price"] = 157.0
        st = alerts.evaluate(rules, self.q, self.open, st, self.now)[1]
        self.q["ASELS"]["price"] = 150.0
        self.assertEqual(alerts.evaluate(rules, self.q, self.open, st, self.now)[0], [])

    def test_pct_move_from_previous_close(self):
        big = [{"id": "asels-4", "symbol": "ASELS", "type": "pct_move", "pct": 4}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])
        three = [{"id": "asels-3", "symbol": "ASELS", "type": "pct_move", "pct": 3}]
        rings, st = alerts.evaluate(three, self.q, self.open, {}, self.now)
        self.assertEqual(rings[0]["message"], "ASELS moved +3.33% to 155.00 (from 150.00).")
        self.assertEqual(alerts.evaluate(three, self.q, self.open, st, tr("2026-11-09 15:00"))[0], [])
        rings, _ = self.at("2026-11-10 10:05", three, st)                  # a new day re-arms it
        self.assertEqual(len(rings), 1)
        down = [{"id": "asels-dn", "symbol": "ASELS", "type": "pct_move", "pct": 1, "direction": "down"}]
        self.assertEqual(alerts.evaluate(down, self.q, self.open, {}, self.now)[0], [])
        ref = [{"id": "asels-ref", "symbol": "ASELS", "type": "pct_move", "pct": 2, "ref": 160.0, "direction": "down"}]
        self.assertIn("-3.12% to 155.00 (from 160.00)", alerts.evaluate(ref, self.q, self.open, {}, self.now)[0][0]
                      ["message"])

    def test_near_limit(self):
        rules = [{"id": "garan-near", "symbol": "GARAN", "type": "near_limit", "pct": 1}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(rings[0]["message"], "GARAN 131.00 is within 1% of today's tavan 132.00 (previous close "
                                              "120.00; taban 108.00, tavan 132.00).")
        self.assertEqual(alerts.evaluate(rules, self.q, self.open, st, tr("2026-11-09 16:00"))[0], [])
        # half a percent is not close enough at 131.00 (needs 131.34 or more)
        half = [dict(rules[0], id="garan-half", pct=0.5)]
        self.assertEqual(alerts.evaluate(half, self.q, self.open, {}, self.now)[0], [])
        q = json.loads(json.dumps(self.q))
        q["GARAN"]["price"] = 108.9                                        # within 1 % of the taban 108.00
        rings, _ = alerts.evaluate(rules, q, self.open, {}, self.now)
        self.assertIn("of today's taban 108.00", rings[0]["message"])
        q["GARAN"]["prev_close"] = None
        self.assertEqual(alerts.evaluate(rules, q, self.open, {}, self.now)[0], [])
        with self.assertRaises(alerts.AlertError):
            alerts.validate({"id": "x", "symbol": "GARAN", "type": "near_limit", "pct": 25})

    def test_opening_bell_at_10_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        for hm in ("09:00", "09:45", "09:58"):                              # pre-open and the auction: quiet
            self.assertEqual(self.at("2026-11-09 " + hm, rules)[0], [], hm)
        rings, st = self.at("2026-11-09 10:00", rules)
        self.assertEqual(rings[0]["message"], "Opening bell: BIST continuous trading is open (2026-11-09 10:00 TSİ).")
        self.assertEqual(self.at("2026-11-09 11:00", rules, st)[0], [])
        self.assertEqual(len(self.at("2026-11-10 10:01", rules, st)[0]), 1)
        self.assertEqual(self.at("2026-10-29 10:01", rules)[0], [])        # holiday
        rings, _ = self.at("2026-10-28 10:00", rules)                      # half day says so
        self.assertIn("half day: continuous trading ends at 12:30 today, Cumhuriyet Bayramı arefesi",
                      rings[0]["message"])

    def test_the_plan_gives_a_stop_a_target_and_flat_by_close(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("thyao-plan-stop", "stop"), ("thyao-plan-target", "target"), ("thyao-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (282.0, 289.0))
        self.assertEqual((rules[2]["at"], rules[2]["label"], rules[2]["before_close"]), ("17:50", "flat by close", True))
        self.assertTrue(all(r["date"] == "2026-11-09" and r["symbol"] == "THYAO" for r in rules))
        rings, st = self.at("2026-11-09 10:36", rules)                     # 285.50: between stop and target
        self.assertEqual(rings, [])
        q = json.loads(json.dumps(self.q))
        q["THYAO"]["price"] = 281.75
        rings, st = self.at("2026-11-09 11:30", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["thyao-plan-stop"])
        self.assertEqual(rings[0]["message"], "THYAO is 281.75: at or below your stop 282 "
                                              "(plan stop: opening-range midpoint).")
        self.assertEqual(self.at("2026-11-09 11:35", rules, st, q)[0], [])  # once
        q["THYAO"]["price"] = 289.0
        self.assertEqual([r["id"] for r in self.at("2026-11-09 12:00", rules, st, q)[0]], ["thyao-plan-target"])

    def test_a_plan_with_a_suffixed_symbol_and_the_default_flat_by(self):
        rules = alerts.rules_from_plan({"symbol": "THYAO.IS", "stop": 282.0, "target": 289.0})
        self.assertEqual([r["id"] for r in rules], ["thyao-plan-stop", "thyao-plan-target", "thyao-flat-by"])
        self.assertEqual(rules[2]["at"], "17:50")
        self.assertNotIn("date", rules[0])
        self.assertEqual(alerts.rules_from_plan({"symbol": "THYAO", "stop": 1, "target": 2}, "17:45")[2]["at"],
                         "17:45")

    def test_flat_by_close_rings_once_per_trading_day(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-11-09 17:49", rules)[0], [])
        rings, st = self.at("2026-11-09 17:50", rules)
        self.assertEqual(len(rings), 1)
        self.assertIn("Flat by close: it is 17:50 TSİ (set for 17:50), continuous trading ends at 18:00.",
                      rings[0]["message"])
        self.assertIn("trader-broker flatten --symbol THYAO", rings[0]["message"])
        self.assertEqual(self.at("2026-11-09 17:59", rules, st)[0], [])
        rings, _ = self.at("2026-11-09 18:05", rules)                      # closing auction
        self.assertIn("continuous trading has ended", rings[0]["message"])
        self.assertEqual(self.at("2026-11-10 17:56", rules)[0], [])        # plan rules: their day only

    def test_before_close_moves_17_50_to_12_20_on_a_half_day(self):
        rule = [{"id": "flat", "type": "time", "at": "17:50", "label": "flat by close", "before_close": True}]
        self.assertEqual(alerts.ring_time(rule[0], status("2026-10-28 11:00")), datetime.time(12, 20))
        self.assertEqual(alerts.ring_time(rule[0], status("2026-11-09 11:00")), datetime.time(17, 50))
        self.assertEqual(self.at("2026-10-28 12:19", rule)[0], [])
        rings, st = self.at("2026-10-28 12:20", rule)
        self.assertEqual(rings[0]["message"], "Flat by close: it is 12:20 TSİ (set for 12:20), continuous trading "
                                              "ends at 12:30.")
        self.assertEqual(st["rung"]["flat"], "2026-10-28T09:20:00Z")
        self.assertEqual(self.at("2026-10-28 17:50", rule, st)[0], [])
        plain = [{"id": "t", "type": "time", "at": "17:50", "label": "check"}]
        self.assertEqual(self.at("2026-10-28 12:20", plain)[0], [])        # without before_close: no shift
        self.assertEqual(self.at("2026-11-14 17:56", rule)[0], [])         # Saturday: no session
        self.assertEqual(self.at("2026-10-29 17:56", rule)[0], [])         # Cumhuriyet Bayramı
        rings, st = self.at("2026-11-09 17:50", rule)
        self.assertEqual(st["rung"]["flat"], "2026-11-09T14:50:00Z")

    def test_a_half_day_plan_already_moved_by_trader_exit_is_not_moved_again(self):
        # trader-exit writes flat_by "12:20" for a 2026-10-28 plan; shifting it again gave 06:50
        rules = alerts.rules_from_plan({"symbol": "THYAO", "date": "2026-10-28", "stop": 282.0, "target": 289.0,
                                        "flat_by": "12:20"})
        flat = rules[2]
        self.assertEqual(alerts.ring_time(flat, status("2026-10-28 10:00")), datetime.time(12, 20))
        self.assertEqual(self.at("2026-10-28 10:05", [flat])[0], [])
        self.assertEqual(len(self.at("2026-10-28 12:20", [flat])[0]), 1)

    def test_a_rang_stamp_from_yesterday_evening_is_not_today(self):
        # 23:30 TSİ is 20:30Z the same UTC day; 01:00 TSİ the next day is still the previous UTC date
        self.assertTrue(alerts._rang_today("2026-11-09T20:30:00Z", "2026-11-09"))
        self.assertTrue(alerts._rang_today("2026-11-09T22:00:00Z", "2026-11-10"))
        self.assertFalse(alerts._rang_today("2026-11-09T20:30:00Z", "2026-11-10"))

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "trader-alerts", "alerts.example.json"), encoding="utf-8") as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("thyao-plan") or r["id"] == "thyao-flat-by"],
                         plan_rules())
        rings, _ = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(sorted(r["id"] for r in rings), ["asels-move-3", "garan-near-limit-1", "opening-bell"])

    def test_bad_rules(self):
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "moon"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "above", "symbol": "THYAO"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "time", "at": "6pm"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "THYAO", "stop": 282.0})
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "THYAO", "stop": 282.0, "target": 289.0, "flat_by": "5:50 PM"})


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--offline", QDIR, "--now", now])

    def test_the_thyao_trade_from_the_plan(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", os.path.join(self.d, "none")])
        self.assertEqual(code, 0)
        self.assertIn("Added thyao-flat-by", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)

        code, text, _ = self.check("2026-11-09 09:45")                    # opening auction: nothing yet
        self.assertEqual(code, 1)
        self.assertIn("BIST closed (2026-11-09 09:45 TSİ, opening auction", text)
        code, text, _ = self.check("2026-11-09 10:36")
        self.assertEqual(code, 0)                                          # the opening bell rings first
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-11-09 10:37")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 4 rule(s) watched today, THYAO 285.50.", text)
        with open(self.prices, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"THYAO": 285.5})
        self.assertEqual(doc["detail"]["THYAO"]["time"], "2026-11-09T07:35:12Z")
        self.assertEqual(doc["detail"]["THYAO"]["fetched"], "2026-11-09T07:37:00Z")

        code, text, _ = self.check("2026-11-09 17:50")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by close: it is 17:50 TSİ", text)
        self.assertIn("not an order", text)
        self.assertEqual(self.check("2026-11-09 17:57")[0], 1)
        code, text, _ = self.check("2026-11-10 10:00")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-11-10 10:05")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state])
        with open(self.rules, encoding="utf-8") as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["opening-bell", "thyao-flat-by", "thyao-plan-stop", "thyao-plan-target"])
        with open(self.state, encoding="utf-8") as fh:
            self.assertNotIn("thyao-flat-by", json.load(fh)["rung"])

    def test_flat_by_from_the_config_when_the_plan_has_none(self):
        with open(PLAN, encoding="utf-8") as fh:
            p = json.load(fh)
        del p["flat_by"]
        plan = os.path.join(self.d, "plan.json")
        cfg = os.path.join(self.d, "config.json")
        with open(plan, "w", encoding="utf-8") as fh:
            json.dump(p, fh)
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"flat_by": "17:40"}, fh)
        self.assertEqual(run(["add", "--from-plan", "--plan", plan, "--rules", self.rules, "--config", cfg])[0], 0)
        with open(self.rules, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["rules"][2]["at"], "17:40")

    def test_a_watch_list_evening_and_morning(self):
        for args in (["--opening-bell"], ["--symbol", "THYAO", "--above", "290"], ["--symbol", "asels", "--stop", "150"],
                     ["--symbol", "GARAN", "--near-limit", "1"]):
            code, _, err = run(["add", "--rules", self.rules] + args)
            self.assertEqual(code, 0, err)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "THYAO", "--above", "290"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        with open(self.rules, encoding="utf-8") as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(ids, ["opening-bell", "thyao-above-290", "asels-stop-150", "garan-near_limit-1"])

        code, text, _ = self.check("2026-11-08T21:00")                    # Sunday evening: the last quote
        self.assertEqual(code, 0)
        self.assertIn("BIST closed (2026-11-08 21:00 TSİ, weekend).", text)
        self.assertIn("ALERT GARAN 131.00 is within 1% of today's tavan 132.00", text)
        self.assertNotIn("Opening bell", text)
        self.assertEqual(self.check("2026-11-08T21:05")[0], 1)
        with open(self.prices, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["prices"], {"ASELS": 155.0, "GARAN": 131.0, "THYAO": 285.5})

        code, text, _ = self.check("2026-11-09T10:01")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-11-09T10:15")
        self.assertEqual(code, 1)

        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("rang", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_add_time_and_target(self):
        code, text, _ = run(["add", "--rules", self.rules, "--at", "17:50", "--label", "flat by close",
                             "--before-close", "--symbol", "thyao.is"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "THYAO", "--target", "289.00",
                             "--date", "2026-11-09"])
        self.assertEqual(code, 0)
        with open(self.rules, encoding="utf-8") as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1750", "type": "time", "at": "17:50", "label": "flat by close",
                                    "symbol": "THYAO", "before_close": True})
        self.assertEqual(rules[1], {"id": "thyao-target-289", "symbol": "THYAO", "type": "target",
                                    "price": 289.0, "date": "2026-11-09"})
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "6pm"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_quote_write_merges(self):
        code, text, _ = run(["quote", "THYAO.IS", "GARAN", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-11-09 10:36"])
        self.assertEqual(code, 0)
        self.assertIn("THYAO  285.50 (+1.96% vs prev close 280.00; taban 252.00, tavan 308.00)", text)
        self.assertIn("GARAN  131.00 (+9.17% vs prev close 120.00; taban 108.00, tavan 132.00)", text)
        self.assertIn("delayed", text)
        self.assertNotIn("$", text)
        run(["quote", "ASELS", "--offline", QDIR, "--write", self.prices])
        with open(self.prices, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["ASELS", "GARAN", "THYAO"])
        self.assertEqual(doc["detail"]["THYAO"]["prev_close"], 280.0)
        self.assertEqual(doc["detail"]["THYAO"]["fetched"], "2026-11-09T07:36:00Z")

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w", encoding="utf-8") as fh:
            json.dump({"cash": 0, "positions": [{"ticker": "ASELS.IS", "shares": 1}]}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["ASELS", "THYAO"])

    def test_clock_exit_codes_and_text(self):
        code, text, _ = run(["clock", "--now", "2026-11-09 10:30"])
        self.assertEqual(code, 0)
        self.assertIn("BIST OPEN, continuous trading", text)
        self.assertIn("opening auction 09:40-09:55, continuous trading 10:00 to 18:00, closing auction 18:00-18:10",
                      text)
        code, text, _ = run(["clock", "--now", "2026-10-29T10:00"])
        self.assertEqual(code, 1)
        self.assertIn("BIST tatili: Cumhuriyet Bayramı", text)
        self.assertIn("Next continuous-trading open: 2026-10-30 10:00 TSİ.", text)
        code, text, _ = run(["clock", "--now", "2026-10-28 11:00"])
        self.assertIn("10:00 to 12:30, closing auction 12:30-12:40 (yarım gün: Cumhuriyet Bayramı arefesi)", text)
        code, text, _ = run(["clock", "--now", "2026-11-09 09:41", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)["phase"], "opening_auction")
        self.assertIn("Holiday list covers 2026 and 2027 only", run(["clock", "--now", "2028-01-03 11:00"])[1])

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)


if __name__ == "__main__":
    unittest.main()
