import copy
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # _load, from any directory
from _load import FIX, ROOT, load

facts = load("trader-analyst", "facts")
DIR = os.path.join(FIX, "trader-analyst")
# Real İş Yatırım MaliTablo answers fetched 2026-11-09, trimmed to the lines the tool reads
# plus a few it must NOT read (balance-sheet "Dönem Net Kar/Zararı", 3DEA "Faaliyet Karı Öncesi ..."):
#   thyao-bundle.json      `facts.py THYAO --save` (XI_29, 16 periods 2026/9 .. 2022/12)
#   mali-GARAN-XI_29.json  a bank asked for the industrial layout: {"ok": true, "value": []}
#   mali-GARAN-UFRS.json   the bank layout, periods 2025/9, 2025/6, 2025/3, 2024/12
BUNDLE = os.path.join(DIR, "thyao-bundle.json")
GARAN_PERIODS = [(2025, 9), (2025, 6), (2025, 3), (2024, 12)]


def read(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = facts.main(args)
    return code, out.getvalue(), err.getvalue()


def thyao():
    return facts.load_offline(BUNDLE)["THYAO"]


def garan_answer(value=None):
    doc = read("mali-GARAN-UFRS.json") if value is None else {"ok": True, "value": value}
    return {"code": "GARAN", "group": "UFRS", "periods": list(GARAN_PERIODS), "url": None,
            "value": facts.check_shape(doc)}


class FakeFetcher(object):
    """Serves the fixtures the way Fetcher.mali answers; a period it has no file for comes
    back with every value null (what the endpoint does for a period not reported yet)."""
    calls = []

    def __init__(self, pause=0):
        self.answers = {}
        for a in read("thyao-bundle.json")["companies"]["THYAO"]["answers"]:
            self.answers[("THYAO", "XI_29", tuple(tuple(p) for p in a["periods"]))] = a["value"]
        self.answers[("GARAN", "UFRS", tuple(GARAN_PERIODS))] = read("mali-GARAN-UFRS.json")["value"]

    def mali(self, code, group, periods):
        FakeFetcher.calls.append((code, group, [tuple(p) for p in periods]))
        if code == "GARAN" and group == "XI_29":
            doc = read("mali-GARAN-XI_29.json")
        else:
            value = self.answers.get((code, group, tuple(tuple(p) for p in periods)))
            if value is None:
                like = self.answers[("GARAN", "UFRS", tuple(GARAN_PERIODS))]
                value = [dict(it, value1=None, value2=None, value3=None, value4=None) for it in like]
            doc = {"ok": True, "errorCode": None, "errorDescription": None, "value": value}
        url = facts.MALI_URL % (code, group, facts.period_query(periods))
        return {"code": code, "group": group, "periods": list(periods), "url": url,
                "value": facts.check_shape(doc)}


class Patched(object):
    """facts.Fetcher -> FakeFetcher and a pinned TSİ date, for the CLI."""

    def __init__(self, today):
        self.today = today

    def __enter__(self):
        self.saved = facts.Fetcher, facts.today_tsi
        facts.Fetcher = FakeFetcher
        facts.today_tsi = lambda: self.today
        FakeFetcher.calls = []
        return self

    def __exit__(self, *exc):
        facts.Fetcher, facts.today_tsi = self.saved


class Mapping(unittest.TestCase):
    def test_real_xi29_lines(self):
        ans = thyao()["answers"][0]
        found = {k: ans["value"][i]["itemCode"] for k, i in facts.map_items(ans["value"]).items()}
        self.assertEqual(found, {"revenue": "3C", "gross_profit": "3D", "operating_income": "3DF",
                                 "net_income_total": "3L", "net_income_parent": "3Z"})
        # 3DEA "Faaliyet Karı Öncesi Diğer Gelir ve Giderler" comes first and must not match
        codes = [i["itemCode"] for i in ans["value"]]
        self.assertLess(codes.index("3DEA"), codes.index("3DF"))
        # 3ZD "Hisse Başına Kazanç" is whole lira (and 0 for a full year): not read
        self.assertNotIn("eps", found)

    def test_real_bank_lines(self):
        ans = garan_answer()
        found = {k: ans["value"][i]["itemCode"] for k, i in facts.map_items(ans["value"]).items()}
        self.assertEqual(found, {"net_income_total": "3Z", "net_income_parent": "3ZA"})
        self.assertEqual(facts.label_key("XXIII. NET DÖNEM KARI/ZARARI (XVII+XXII)"),
                         "net donem kari/zarari (xvii+xxii)")
        self.assertEqual(facts.label_key("23.1 Grubun Karı/Zararı"), "grubun kari/zarari")

    def test_balance_sheet_lines_are_never_read(self):
        items = [{"itemCode": "2OCF", "itemDescTr": "  Dönem Kârı", "itemDescEng": "Current Year Income",
                  "value1": "1"},
                 {"itemCode": "3L", "itemDescTr": "DÖNEM KARI (ZARARI)", "itemDescEng": "NET PROFIT AFTER TAXES",
                  "value1": "2"}]
        self.assertEqual(facts.map_items(items), {"net_income_total": 1})

    def test_periods(self):
        self.assertEqual(facts.period_list(datetime.date(2026, 11, 9), 5),
                         [(2026, 9), (2026, 6), (2026, 3), (2025, 12), (2025, 9)])
        self.assertEqual(facts.period_list(datetime.date(2026, 2, 10), 1), [(2025, 12)])
        self.assertEqual(facts.period_query([(2026, 9), (2026, 6)]),
                         "&year1=2026&period1=9&year2=2026&period2=6")
        self.assertEqual(facts.quarter_end(2026, 6), "2026-06-30")


class Quarters(unittest.TestCase):
    def setUp(self):
        slot = thyao()
        self.r = facts.analyse(slot["answers"], "THYAO", quarters=8, group="XI_29")
        self.rows = {r["fiscal"]: r for r in self.r["rows"]}

    def test_cumulative_to_discrete(self):
        # 2026 year to date, mn TL: Q1 257,961  H1 585,069  9M 981,030
        q1, q2, q3 = self.rows["Q1 2026"], self.rows["Q2 2026"], self.rows["Q3 2026"]
        self.assertEqual(q1["revenue"], 257961e6)
        self.assertFalse(q1["derived"])
        self.assertEqual(q2["revenue"], 585069e6 - 257961e6)   # 327,108
        self.assertEqual(q3["revenue"], 981030e6 - 585069e6)   # 395,961
        self.assertTrue(q2["derived"] and q3["derived"])
        # operating profit H1 -5,098, 9M 39,026: Q3 = 44,124
        self.assertEqual(q3["operating_income"], 39026e6 - (-5098e6))
        # Q4 2025 = full year 2025 (1,187,816,196,737; first request) - 9M 2025 (690,825; second request)
        self.assertEqual(self.rows["Q4 2025"]["revenue"], 1187816196737 - 690825e6)
        self.assertEqual(self.rows["Q2 2025"]["revenue"], 408036e6 - 176712e6)
        self.assertEqual([r["fiscal"] for r in self.r["rows"]],
                         ["Q3 2026", "Q2 2026", "Q1 2026", "Q4 2025", "Q3 2025", "Q2 2025", "Q1 2025", "Q4 2024"])

    def test_margins_and_yoy(self):
        q3 = self.rows["Q3 2026"]
        self.assertEqual(self.r["net_income_basis"], "parent")
        self.assertEqual(q3["net_income"], 80330e6 - 18864e6)            # Ana Ortaklık Payları: 61,466
        self.assertEqual(q3["net_income_total"], 80228e6 - 18766e6)      # Dönem Kârı: 61,462
        self.assertEqual(q3["gross_margin"], 17.1)                        # (110,381 - 42,587) / 395,961
        self.assertEqual(q3["operating_margin"], 11.1)                    # 44,124 / 395,961
        self.assertEqual(q3["net_margin"], 15.5)                          # 61,466 / 395,961
        self.assertEqual(q3["revenue_yoy"], 40.0)                         # vs 690,825 - 408,036 = 282,789
        self.assertEqual(q3["net_income_yoy"], 9.7)                       # vs 81,064 - 25,013 = 56,051
        self.assertEqual(self.rows["Q1 2026"]["net_income_yoy"], 645.4)   # 9,915 vs a loss of 1,818
        self.assertIsNone(q3["eps"])

    def test_trailing_twelve_months(self):
        self.assertEqual(self.r["ttm"]["revenue"], 395961e6 + 327108e6 + 257961e6 + 496991196737)
        self.assertEqual(self.r["ttm"]["net_income"], 61466e6 + 8949e6 + 9915e6 + 65888895515)
        self.assertNotIn("eps", self.r["ttm"])

    def test_what_changed_in_words(self):
        lines = facts.changes(self.r)
        self.assertEqual(lines[0], "Quarter ending 2026-09-30 (Q3 2026, year-to-date minus the quarter "
                                   "before): revenue 395.961,0 mn TL (+40.0% vs a year earlier), net income "
                                   "to the parent 61.466,0 mn TL (+9.7%).")
        self.assertIn("Gross margin 17.1% (+10.7 points vs the quarter before).", lines)
        self.assertTrue(any(l.startswith("Net income is above operating profit") for l in lines))

    def test_a_period_not_reported_yet_is_ignored(self):
        lines = [{"itemCode": "3C", "itemDescTr": "Satış Gelirleri", "value1": "0", "value2": "100"},
                 {"itemCode": "3L", "itemDescTr": "DÖNEM KARI (ZARARI)", "value1": None, "value2": "10"}]
        r = facts.analyse([{"periods": [(2026, 6), (2026, 3)], "value": lines}], "X")
        self.assertEqual([(x["fiscal"], x["revenue"]) for x in r["rows"]], [("Q1 2026", 100.0)])

    def test_missing_earlier_cumulative_figure_leaves_the_quarter_out(self):
        ans = garan_answer()
        r = facts.analyse([dict(ans, periods=[(2025, 9), (2025, 3), (2024, 12), (2023, 12)])], "GARAN")
        self.assertEqual([x["fiscal"] for x in r["rows"]], ["Q1 2025"])  # no June 2025, no Sept 2024


class Banks(unittest.TestCase):
    def test_bank_layout_prints_raw_lines(self):
        r = facts.analyse([garan_answer()], "GARAN", group="UFRS")
        self.assertEqual([x["net_income"] for x in r["rows"]],
                         [84042e6 - 53610e6, 53610e6 - 25284346000, 25284346000.0])
        self.assertIsNone(r["rows"][0]["revenue"])
        self.assertIsNone(r["rows"][0]["net_margin"])
        raw = r["raw"]
        self.assertEqual(raw["period"], "2025-09-30")
        self.assertEqual(raw["lines"][0]["desc"], "I. FAİZ GELİRLERİ")
        self.assertEqual(raw["lines"][0]["value"], 518777e6)
        self.assertEqual(raw["lines"][-1]["desc"], "XXIII. NET DÖNEM KARI/ZARARI (XVII+XXII)")
        self.assertEqual(len(raw["lines"]), 16)   # numbered main lines with a number, no sub-lines
        out = facts.render("GARAN", r)
        self.assertIn("No revenue line in this layout", out)
        self.assertIn("III. NET FAİZ GELİRİ/GİDERİ (I - II)", out)
        self.assertIn("net income to the parent 30.432,0 mn TL", out)
        self.assertIn("Net income is the parent's share (23.1 Grubun Karı/Zararı).", out)

    def test_nothing_maps_raw_only(self):
        value = [it for it in read("mali-GARAN-UFRS.json")["value"] if not it["itemCode"].startswith("3Z")]
        r = facts.analyse([garan_answer(value)], "GARAN", group="UFRS")
        self.assertEqual(r["rows"], [])
        self.assertEqual(r["raw"]["lines"][0]["desc"], "I. FAİZ GELİRLERİ")
        out = facts.render("GARAN", r)
        self.assertIn("Could not map revenue or net income", out)
        self.assertIn("XVII. SÜRDÜRÜLEN FAALİYETLER DÖNEM NET K/Z (XV±XVI)", out)
        self.assertNotIn("Kredilerden", out)   # sub-lines stay out

    def test_xi29_falls_back_to_ufrs(self):
        FakeFetcher.calls = []
        group, answers = facts.fetch_company(FakeFetcher(), "GARAN", 2, today=datetime.date(2025, 11, 1))
        self.assertEqual(group, "UFRS")
        self.assertEqual([(c[1], c[2][0]) for c in FakeFetcher.calls],
                         [("XI_29", (2025, 9)), ("UFRS", (2025, 9)), ("UFRS", (2024, 9))])
        self.assertEqual(len(answers), 2)
        r = facts.analyse(answers, "GARAN", quarters=2, group=group)
        self.assertEqual([x["fiscal"] for x in r["rows"]], ["Q3 2025", "Q2 2025"])

    def test_no_numbers_in_either_layout(self):
        FakeFetcher.calls = []
        with self.assertRaises(facts.FactsError) as cm:
            facts.fetch_company(FakeFetcher(), "GARAN", 2, today=datetime.date(2020, 1, 1))
        self.assertIn("tried financialGroup XI_29, UFRS", str(cm.exception))
        self.assertIn("kap.org.tr", str(cm.exception))


class Shape(unittest.TestCase):
    def test_shape_change_is_a_plain_message(self):
        for doc in ({"data": []}, [1, 2], {"value": {"rows": []}},
                    {"value": [{"code": "3C", "desc": "Satış", "v": 1}]}):
            with self.assertRaises(facts.FactsError) as cm:
                facts.check_shape(doc)
            self.assertIn("no longer has the expected shape", str(cm.exception))
            self.assertIn(facts.KAP_URL, str(cm.exception))
        with self.assertRaises(facts.FactsError) as cm:
            facts.check_shape({"ok": False, "errorCode": "E42", "errorDescription": "Geçersiz şirket kodu",
                               "value": None})
        self.assertIn("Geçersiz şirket kodu", str(cm.exception))
        self.assertEqual(facts.check_shape({"ok": True, "value": []}), [])
        self.assertEqual(facts.check_shape({"ok": True, "value": None}), [])

    def test_the_fetcher_reports_a_changed_answer(self):
        class Resp(object):
            headers = {}

            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        real = facts.urllib.request.urlopen
        try:
            for body in (b"<html>Bakim calismasi</html>", b'{"result": {"rows": []}}'):
                facts.urllib.request.urlopen = lambda req, timeout=None, b=body: Resp(b)
                with self.assertRaises(facts.FactsError) as cm:
                    facts.Fetcher(pause=0).mali("THYAO", "XI_29", [(2026, 9)])
                self.assertIn("no longer has the expected shape", str(cm.exception))
            facts.urllib.request.urlopen = lambda req, timeout=None: Resp(
                json.dumps(read("mali-GARAN-XI_29.json")).encode("utf-8"))
            ans = facts.Fetcher(pause=0).mali("GARAN", "XI_29", [(2026, 9)])
            self.assertEqual(ans["value"], [])
            self.assertIn("companyCode=GARAN&exchange=TRY&financialGroup=XI_29&year1=2026&period1=9",
                          ans["url"])
        finally:
            facts.urllib.request.urlopen = real

    def test_cli_shape_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w") as fh:
                json.dump({"periods": [[2026, 9]], "value": {"rows": []}}, fh)
            code, out, err = run(["THYAO", "--offline", path])
        self.assertEqual(code, 2)
        self.assertIn("no longer has the expected shape", err)
        self.assertEqual(out, "")


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.none = os.path.join(self.tmp.name, "none.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def test_offline_bundle_table(self):
        code, out, err = run(["THYAO", "--offline", BUNDLE, "--universe", self.none])
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("Türk Hava Yolları (THYAO), financialGroup XI_29."))
        self.assertIn("2026-09-30  Q3 2026     395.961   +40.0%   17.1%   11.1%   15.5%     61.466    +9.7%"
                      "  derived", lines)
        self.assertIn("Last 4 quarters: revenue 1.478.021,2 mn TL, net income 146.218,9 mn TL.", lines)
        self.assertIn("Net income is the parent's share (Ana Ortaklık Payları).", lines)
        self.assertIn("kap.org.tr", out)
        self.assertNotIn("EPS", out)
        self.assertNotIn("$", out)

    def test_save_then_offline_round_trip(self):
        saved = os.path.join(self.tmp.name, "bundle.json")
        metrics = os.path.join(self.tmp.name, "trader", "metrics.json")
        with Patched(datetime.date(2025, 11, 1)):
            code, out1, err = run(["GARAN", "--save", saved, "--universe", self.none, "--quarters", "2"])
        self.assertEqual(code, 0, err)
        self.assertIn("saved the raw answers to %s" % saved, out1)
        with open(saved, encoding="utf-8") as fh:
            bundle = json.load(fh)
        g = bundle["companies"]["GARAN"]
        self.assertEqual(g["financialGroup"], "UFRS")
        self.assertEqual(g["answers"][0]["periods"], [list(p) for p in GARAN_PERIODS])
        self.assertIn("financialGroup=UFRS", g["answers"][0]["url"])
        code, out2, err = run(["GARAN", "--offline", saved, "--universe", self.none, "--quarters", "2",
                               "--metrics", metrics])
        self.assertEqual(code, 0, err)
        self.assertEqual(out2.replace("wrote %s (1 companies)\n" % metrics, ""),
                         out1.replace("saved the raw answers to %s (read them back with --offline)\n" % saved, ""))
        with open(metrics, encoding="utf-8") as fh:
            row = json.load(fh)["companies"][0]
        self.assertEqual((row["ticker"], row["financial_group"], row["period_end"]), ("GARAN", "UFRS", "2025-09-30"))
        self.assertIsNone(row["revenue_ttm"])
        self.assertEqual(row["net_income_basis"], "parent")

    def test_live_path_with_the_real_thyao_answers(self):
        saved = os.path.join(self.tmp.name, "thyao.json")
        with Patched(datetime.date(2026, 11, 9)):
            code, out, err = run(["THYAO.IS", "--save", saved, "--json", "--universe", self.none])
        self.assertEqual(code, 0, err)
        self.assertEqual([c[1] for c in FakeFetcher.calls], ["XI_29"] * 4)  # 8 quarters + a year: 16 periods
        data = json.loads(out)
        self.assertEqual(data[0]["ticker"], "THYAO")
        self.assertEqual(data[0]["result"]["rows"][0]["fiscal"], "Q3 2026")
        self.assertEqual(data[0]["result"]["items"]["net_income_parent"], "Ana Ortaklık Payları")
        code, out, err = run(["THYAO", "--offline", saved, "--json", "--universe", self.none])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)[0]["result"]["ttm"]["revenue"], 1478021196737.0)

    def test_metrics_file_for_the_screener(self):
        path = os.path.join(self.tmp.name, "trader", "metrics.json")
        code, out, err = run(["THYAO", "--offline", BUNDLE, "--metrics", path, "--universe", self.none])
        self.assertEqual(code, 0, err)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        row = data["companies"][0]
        self.assertEqual(data["currency"], "TRY")
        self.assertEqual(row["revenue_ttm"], 1478021196737.0)
        self.assertEqual(row["net_income_ttm"], 146218895515.0)
        self.assertEqual(row["revenue_growth_yoy"], 40.0)
        self.assertEqual(row["net_income_growth_yoy"], 9.7)
        self.assertIsNone(row["eps_ttm"])
        self.assertIn("wrote", out)

    def test_example_metrics_file_matches_what_the_tool_writes(self):
        with open(os.path.join(ROOT, "trader-analyst",
                               "metrics.example.json"), encoding="utf-8") as fh:
            example = json.load(fh)
        self.assertNotIn("_note", example)
        r = facts.analyse(thyao()["answers"], "THYAO", quarters=8, name="Türk Hava Yolları", group="XI_29")
        row = dict(facts.metrics_row("THYAO", r))
        ex = next(c for c in example["companies"] if c["ticker"] == "THYAO")
        self.assertEqual(sorted(ex), sorted(row))
        self.assertEqual(ex, row)

    def test_unknown_code_offline(self):
        code, _, err = run(["ASELS", "THYAO", "--offline", BUNDLE, "--universe", self.none])
        self.assertEqual(code, 1)
        self.assertIn("no saved answer for ASELS", err)
        code, out, _ = run(["--universe", self.none])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
