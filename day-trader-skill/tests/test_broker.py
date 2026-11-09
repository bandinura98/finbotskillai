"""trader-broker tests (Borsa İstanbul): the emir fişi, the explicit evet, the decision log. Nothing is ever sent."""

import io
import json
import os
import re
import shutil
import socket
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-thyao.json")
PLAN_PATH = os.path.join(FIX, "trader-budget", "plan-thyao.json")
NOW = "2026-11-09 10:30"
TODAY = "2026-11-09"
GARAN = {"symbol": "GARAN", "side": "buy", "notional": "10000", "type": "market", "time_in_force": "day"}
BATCH = ["THYAO", "ASELS", "GARAN"]
THYAO_FIELDS = {"symbol": "THYAO", "side": "buy", "qty": "181", "type": "market", "time_in_force": "day",
                "order_class": "bracket", "take_profit_limit_price": "317.00", "stop_loss_stop_price": "308.00"}
THYAO_PLAIN = ("BUY 181 THYAO, market, good for today / stop-loss 308.00 / take-profit 317.00 / max loss about "
               "994,76 TL with costs if the stop fills at 308.00 (it can fill worse in a fast market)")


def thyao(**over):
    with open(ORDERS_PATH, encoding="utf-8") as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


def no_network(*a, **k):
    raise AssertionError("trader-broker tried to use the network")


class ExplicitEvet(unittest.TestCase):
    def yes(self, reply, order=None, batch=BATCH):
        return ticket.parse_yes(reply, order or thyao(), batch)[0]

    def test_a_plain_evet_or_yes_counts(self):
        for r in ("evet", "Evet", "Evet.", "EVET", "evet!", "yes", "Yes", "YES", "evet, gir", "evet THYAO",
                  "Evet THYAO'yu al", "evet 181 lot", "evet 181 lot THYAO", "evet 308,00", "evet 308.00",
                  "evet hedef 317", "evet zarar 994,76 TL", "yes 181 THYAO"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "   ", "tamam", "olur", "ok", "okay", "peki", "tabii", "sure", "go", "do it", "yeah", "e",
                  "evet ama", "evet ama 150 lot olsun", "evet, ama önce bekle", "evet fakat", "evet belki",
                  "evet sonra", "evet, dur", "evet iptal", "evet değil", "hayır", "hayir", "no", "not yes",
                  "evet?", "yes?", "evet mi?",
                  "evet hepsine", "hepsine evet", "evet hepsi", "evet hepsini gir", "evet ikisine de", "evet tümü",
                  "evet kalanlar", "yes to all", "yes, all of them", "yes both", "yes to the rest",
                  "evet ASELS", "evet GARAN 181", "evet 182", "evet 180 lot", "evet 1.000"):
            self.assertFalse(self.yes(r), r)

    def test_why_it_was_refused(self):
        self.assertIn("starts with \"evet\"", ticket.parse_yes("tamam", thyao(), BATCH)[1])
        self.assertIn("one order at a time", ticket.parse_yes("evet hepsine", thyao(), BATCH)[1])
        self.assertIn("condition or a doubt", ticket.parse_yes("evet ama bekle", thyao(), BATCH)[1])
        self.assertIn("names ASELS, but this order is for THYAO", ticket.parse_yes("evet ASELS", thyao(), BATCH)[1])
        self.assertIn("the reply says 182", ticket.parse_yes("evet 182", thyao(), BATCH)[1])

    def test_a_yes_that_names_a_stock_outside_the_batch(self):
        only = ["THYAO"]
        self.assertFalse(self.yes("evet ASELS", batch=only))           # typed as a code
        self.assertFalse(self.yes("evet asels", batch=only))           # a known BIST code, any case
        self.assertFalse(self.yes("evet KOZAL", batch=only))           # not in the share list, but typed as a code
        self.assertIn("names ASELS", ticket.parse_yes("evet ASELS al", thyao(), only)[1])
        for r in ("EVET THYAO", "EVET", "evet THYAO'yu al", "Evet, 181 LOT", "evet TL"):
            self.assertTrue(self.yes(r, batch=only), r)

    def test_the_amount_must_match_this_order(self):
        sell = {"symbol": "KCHOL", "side": "sell", "qty": "44", "type": "market", "time_in_force": "day"}
        self.assertTrue(self.yes("evet 44 KCHOL sat", sell, ["KCHOL"]))
        self.assertFalse(self.yes("evet 45 sat", sell, ["KCHOL"]))
        self.assertTrue(self.yes("evet 10.000 TL GARAN", GARAN))          # Turkish thousands
        self.assertTrue(self.yes("evet 10000", GARAN))
        self.assertFalse(self.yes("evet 1000", GARAN))

    def test_turkish_number_readings(self):
        self.assertEqual(ticket._numbers("312,50")[0][1], {312.5, 31250.0})
        self.assertIn(1000.0, ticket._numbers("1.000")[0][1])
        self.assertIn(1234.5, ticket._numbers("1.234,50")[0][1])


class Slip(unittest.TestCase):
    def test_the_emir_fisi_of_a_market_bracket(self):
        rows = ticket.slip(thyao())
        self.assertEqual([k for k, _ in rows], ["Hisse", "Yön", "Lot", "Emir tipi", "Fiyat", "Zarar-durdur", "Kâr-al",
                                                "Süre", "Tahmini tutar", "Tahmini maliyet"])
        d = dict(rows)
        self.assertEqual((d["Hisse"], d["Yön"], d["Lot"], d["Emir tipi"], d["Süre"]),
                         ("THYAO", "AL", "181", "Piyasa", "Gün"))
        self.assertEqual(d["Fiyat"], "piyasa (o anki en iyi fiyat)")
        self.assertTrue(d["Zarar-durdur"].startswith("308,00 "))
        self.assertTrue(d["Kâr-al"].startswith("317,00 "))
        # 181 x 312,50 = 56.562,50; x 0,001605 = 90,78; plus 181 x 308 x 0,001605 = 89,48 -> 180,26
        self.assertEqual(d["Tahmini tutar"], "~56.562,50 TL")
        self.assertIn("~90,78 TL bu emir (komisyon + BSMV + borsa payı)", d["Tahmini maliyet"])
        self.assertIn("~180,26 TL gidiş-dönüş", d["Tahmini maliyet"])

    def test_a_limit_price_has_a_turkish_decimal_comma(self):
        d = dict(ticket.slip(thyao(type="limit", limit_price="312.25")))
        self.assertEqual((d["Emir tipi"], d["Fiyat"]), ("Limit", "312,25"))
        d = dict(ticket.slip({"symbol": "KCHOL", "side": "sell", "qty": "44", "type": "limit",
                              "limit_price": "1012.00", "time_in_force": "gtc"}))
        self.assertEqual((d["Yön"], d["Lot"], d["Fiyat"], d["Süre"]),
                         ("SAT", "44", "1012,00", "İptale kadar geçerli (İKG)"))
        self.assertNotIn("Zarar-durdur", d)

    def test_a_notional_order(self):
        d = dict(ticket.slip(GARAN))
        self.assertTrue(d["Tutar"].startswith("10.000,00 TL"))
        self.assertNotIn("Lot", d)

    def test_slip_lines_say_you_type_it_yourself(self):
        text = "\n".join(ticket.slip_lines(thyao()))
        self.assertIn("EMİR FİŞİ (bunu kendi aracı kurum uygulamana SEN gir; Claude hiçbir yere göndermez)", text)
        self.assertRegex(text, r"Hisse +: THYAO\n")
        self.assertRegex(text, r"Lot +: 181\n")
        self.assertIn("Önce AL emrini gir", text)

    def test_plain_words(self):
        self.assertEqual(ticket.plain(GARAN), "BUY 10.000,00 TL of GARAN, market order, good for today")
        self.assertEqual(ticket.plain({"symbol": "KCHOL", "side": "sell", "qty": "1", "type": "limit",
                                       "limit_price": "210", "time_in_force": "gtc"}),
                         "SELL 1 share of KCHOL, limit order at 210.00, good until canceled")
        self.assertEqual(ticket.plain(thyao()), THYAO_PLAIN)
        lim = thyao(type="limit", limit_price="311.00")
        self.assertEqual(ticket.plain(lim).split(" / ")[0], "BUY 181 THYAO, limit 311.00, good for today")
        # 181 x (3,00 + 0,4992 + 0,4943) = 722,82
        self.assertEqual(ticket.max_loss(lim), (722.82, 308.0))
        self.assertEqual(ticket.max_loss(dict(THYAO_FIELDS)), (None, 308.0))

    def test_order_fields_leave_out_why_and_plan(self):
        self.assertEqual(ticket.order_fields(thyao()), THYAO_FIELDS)
        self.assertEqual(ticket.fingerprint(thyao()), ticket.fingerprint(THYAO_FIELDS))
        self.assertEqual(ticket.order_fields({"symbol": "KCHOL", "side": "buy", "qty": 3, "type": "limit",
                                              "limit_price": 210.5, "time_in_force": "day"}),
                         {"symbol": "KCHOL", "side": "buy", "qty": "3", "type": "limit", "limit_price": "210.5",
                          "time_in_force": "day"})

    def test_estimated_costs(self):
        self.assertEqual(ticket.estimated_costs(thyao()), {"notional": 56562.5, "this_side": 90.78, "round_trip": 180.26})
        self.assertEqual(ticket.estimated_costs(GARAN), {"notional": 10000.0, "this_side": 16.05})
        self.assertIsNone(ticket.estimated_costs({"symbol": "KCHOL", "side": "buy", "qty": "3", "type": "market"}))

    def test_no_network_code_at_all(self):
        with open(os.path.join(ROOT, "trader-broker", "ticket.py"), encoding="utf-8") as fh:
            src = fh.read()
        for mod in ("urllib", "http.client", "socket", "requests", "smtplib", "ssl"):
            self.assertIsNone(re.search(r"^\s*(import|from)\s+%s\b" % re.escape(mod), src, re.M), mod)


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "trader", "orders.json")
        os.makedirs(os.path.dirname(self.orders))
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "trader", "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "trader", "state", "risk.json")
        self.state = os.path.join(self.d, "trader", "state", "broker.json")
        self.pf = os.path.join(self.d, "trader", "portfolio.json")
        self.none = os.path.join(self.d, "none")
        # no test here may reach the network: any socket or urlopen fails the test
        patches = [mock.patch.object(socket.socket, "connect", no_network),
                   mock.patch.object(socket, "create_connection", no_network),
                   mock.patch.object(urllib.request, "urlopen", no_network)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.d)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def run_risk(self, now=NOW):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", self.none, "--plan", self.none, "--state", self.state, "--now", now,
                               "--out", self.risk])

    def state_doc(self):
        with open(self.state, encoding="utf-8") as fh:
            return json.load(fh)

    def set_cfg(self, **over):
        with open(self.cfg, encoding="utf-8") as fh:
            c = json.load(fh)
        c.update(over)
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump(c, fh)

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: trader-risk has not checked these orders", text)
        self.assertNotIn("Reply \"evet\"", text)
        self.assertIn("EMİR FİŞİ", text)          # the slip is shown, but it is not ready
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "evet"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)
        self.assertFalse(os.path.exists(self.state))

    def test_the_full_flow(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0, text)
        self.assertIn("ORDER 1 of 1, KAĞIT ÜZERİNDE (sanal), do not type it into a real account, manual entry in "
                      "your broker app", text)
        for part in THYAO_PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("READY: trader-risk PASS, waiting for your evet", text)
        self.assertIn("EMİR FİŞİ", text)
        for row in (r"Hisse +: THYAO", r"Yön +: AL", r"Lot +: 181", r"Emir tipi +: Piyasa",
                    r"Zarar-durdur +: 308,00", r"Kâr-al +: 317,00", r"Süre +: Gün"):
            self.assertRegex(text, row)
        self.assertIn("Reply \"evet\" to approve this one order, or \"hayır\" to skip it. Nothing is sent", text)
        self.assertIn("yatırım tavsiyesi değildir", text)

        for reply in ("tamam", "evet ama bekle", "evet hepsine", "evet ASELS"):
            code, text, _ = run(self.args("approve", "--index", "1", "--reply", reply))
            self.assertEqual(code, 1, reply)
            self.assertIn("Not recorded", text)
            self.assertFalse(os.path.exists(self.state), reply)

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "evet"))
        self.assertEqual(code, 0)
        self.assertIn("Recorded your evet for order 1", text)
        self.assertIn("Nothing was sent anywhere. Type this into your broker app yourself:", text)
        self.assertRegex(text, r"Lot +: 181")
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "evet"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)

        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "123456",
                                      "--filled-qty", "181", "--filled-price", "312.75", "--portfolio", self.pf,
                                      now="2026-11-09 10:31"))
        self.assertEqual(code, 0)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        appr, plc = st["orders"][0], st["orders"][1]
        self.assertEqual((appr["reply"], appr["kind"], appr["max_loss"], appr["date"], appr["market"]),
                         ("evet", "entry", 994.76, TODAY, "BIST"))
        self.assertEqual(appr["at"], "2026-11-09T10:30:00+03:00")
        self.assertEqual(appr["fingerprint"], check.fingerprint(thyao()))
        self.assertEqual((plc["broker_order_id"], plc["max_loss"], plc["kind"]), ("123456", 994.76, "entry"))
        with open(self.pf, encoding="utf-8") as fh:
            pf = json.load(fh)
        # 181 x 312,75 = 56.607,75 + 90,86 costs off 100.000
        self.assertEqual(pf["cash"], 43301.39)
        self.assertEqual((pf["positions"][0]["shares"], pf["currency"]), (181, "TRY"))
        self.assertEqual(st["orders"][2]["fee"], 90.86)

        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: placed", text)
        code, text, _ = run(["log", "--state", self.state, "--now", NOW])
        self.assertIn("1 placed today", text)
        self.assertIn("max loss 994,76 TL", text)
        self.assertIn("emir no 123456", text)
        self.assertIn('reply "evet"', text)

        # trader-risk now counts it: a second THYAO buy today is a duplicate, and 994,76 TL is used
        res = check.check([thyao()], {"budget": 100000}, state=st, now=check.parse_now("2026-11-09 10:45"))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        self.assertEqual((res["today"]["entries_placed"], res["today"]["loss_at_risk"]), (1, 994.76))

    def test_yes_works_as_well_as_evet(self):
        self.run_risk()
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "Yes"))[0], 0)
        self.assertEqual(self.state_doc()["orders"][0]["reply"], "Yes")

    def test_show_json(self):
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1", "--json"))
        self.assertEqual(code, 0)
        t = json.loads(text)
        self.assertEqual((t["ready"], t["market"], t["currency"], t["bracket"], t["max_loss"], t["stop"], t["paper"]),
                         (True, "BIST", "TRY", True, 994.76, 308.0, True))
        self.assertEqual(t["order"], THYAO_FIELDS)
        self.assertEqual((t["emir_fisi"]["Hisse"], t["emir_fisi"]["Lot"], t["emir_fisi"]["Yön"]), ("THYAO", "181", "AL"))
        self.assertEqual(t["estimated_costs"]["round_trip"], 180.26)

    def test_a_named_broker_and_a_live_account(self):
        self.set_cfg(broker="Garanti BBVA Yatırım", paper=False)
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("GERÇEK HESAP, REAL MONEY, manual entry in Garanti BBVA Yatırım", text)
        self.assertIn("you type the emir fisi into Garanti BBVA Yatırım yourself", text)
        self.assertNotIn("Kağıt üzerinde", text)

    def test_flatten_by_the_close(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "evet"))
        run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "1",
                      "--filled-qty", "181", "--filled-price", "312.75", "--portfolio", self.pf))
        late = "2026-11-09 17:50"
        code, text, _ = run(["flatten", "--symbol", "thyao.is", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--now", late])
        self.assertEqual(code, 0)
        self.assertIn("Flatten THYAO: SELL 181 shares of THYAO, market order, good for today (flat by 17:50 TSİ)", text)
        self.assertIn("replaced 1 earlier proposal", text)
        with open(self.orders, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual((doc["market"], doc["currency"], doc["created_at"]), ("BIST", "TRY", "2026-11-09T14:50:00Z"))
        self.assertEqual(ticket.order_fields(doc["orders"][0]),
                         {"symbol": "THYAO", "side": "sell", "qty": "181", "type": "market", "time_in_force": "day"})
        self.assertEqual(self.run_risk(now=late), 0)        # after the 17:30 entry cutoff: an exit still passes
        with open(self.risk, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["results"][0]["kind"], "exit")
        code, text, _ = run(self.args("show", now=late))
        self.assertEqual(code, 0)
        self.assertRegex(text, r"Yön +: SAT")
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "evet 181 THYAO sat", now=late))[0], 0)
        code, _, _ = run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "181",
                                   "--filled-price", "314.00", "--portfolio", self.pf, now="2026-11-09 17:51"))
        self.assertEqual(code, 0)
        with open(self.pf, encoding="utf-8") as fh:
            pf = json.load(fh)
        # 43.301,39 + 181 x 314 (56.834) - 91,22 costs
        self.assertEqual((pf["cash"], pf["positions"]), (100044.17, []))
        self.assertEqual(check.day_trades(self.state_doc(), TODAY), (1, ["2026-11-09 THYAO"]))
        code, _, err = run(["flatten", "--symbol", "THYAO", "--orders", self.orders, "--portfolio", self.pf,
                            "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def test_flatten_with_the_broker_qty(self):
        code, text, _ = run(["flatten", "--symbol", "THYAO", "--qty", "181", "--orders", self.orders,
                             "--portfolio", self.none, "--plan", self.none, "--config", self.cfg, "--now", NOW])
        self.assertEqual(code, 0)
        self.assertNotIn("The qty comes from", text)
        self.assertEqual(run(["flatten", "--symbol", "THYAO", "--qty", "0", "--orders", self.orders])[0], 1)

    def test_an_edited_order_is_not_ready(self):
        self.run_risk()
        with open(self.orders, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["orders"][0]["qty"] = "182"
        with open(self.orders, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("was not in the last risk check", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "evet"))[0], 1)

    def test_yesterdays_check_does_not_count(self):
        self.assertEqual(self.run_risk(now="2026-11-06 10:30"), 0)     # passed, but on Friday
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("the risk check is from 2026-11-06, not today", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "evet"))[0], 1)

    def test_blocked_orders_are_never_ready(self):
        self.assertEqual(self.run_risk(now="2026-11-09 10:05"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-11-09 10:05"))
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED by trader-risk: window: no new entries before 10:15 TSİ", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "evet", now="2026-11-09 10:05"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)

    def test_result_needs_an_approval_and_decline_is_logged(self):
        self.run_risk()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("no evet recorded today", err)
        code, text, _ = run(self.args("decline", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("Nothing was sent", text)
        self.assertEqual(self.state_doc()["orders"][0]["reply"], "hayır")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: declined", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "evet"))[0], 1)

    def test_fill_sell_relieves_the_oldest_lot(self):
        pf = {"cash": 0, "positions": [{"ticker": "KCHOL", "shares": 3, "lots": [
            {"id": "KCHOL-1", "date": "2026-01-02", "shares": 2, "cost": 400.0},
            {"id": "KCHOL-2", "date": "2026-06-02", "shares": 1, "cost": 205.0}]}]}
        with open(self.pf, "w", encoding="utf-8") as fh:
            json.dump(pf, fh)
        code, text, _ = run(["fill", "--symbol", "KCHOL", "--side", "sell", "--qty", "2", "--price", "210",
                             "--date", TODAY, "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 0, text)
        with open(self.pf, encoding="utf-8") as fh:
            pf = json.load(fh)
        self.assertEqual(pf["cash"], 419.33)                 # 420 - 420 x 0,001605
        lots = pf["positions"][0]["lots"]
        self.assertEqual([l["id"] for l in lots], ["KCHOL-2"])
        self.assertEqual((lots[0]["shares"], lots[0]["cost"]), (1, 205.0))
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "filled")
        code, _, err = run(["fill", "--symbol", "KCHOL", "--side", "sell", "--qty", "5", "--price", "210",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertIn("the portfolio holds 1", err)
        code, _, err = run(["fill", "--symbol", "KCHOL", "--side", "buy", "--qty", "2.5", "--price", "210",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertIn("whole shares", err)
        self.assertNotIn("Traceback", err)

    def test_bad_input_is_one_line(self):
        code, _, err = run(self.args("show", "--index", "5"))
        self.assertEqual(code, 1)
        self.assertIn("there is no order 5", err)
        code, _, err = run(self.args("show", now="öğlen"))
        self.assertEqual(code, 1)
        self.assertIn("--now wants", err)
        self.assertEqual(run([])[0], 1)


if __name__ == "__main__":
    unittest.main()
