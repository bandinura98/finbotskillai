import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # _load, from any directory
from _load import FIX, load

news = load("trader-news", "news")
DIR = os.path.join(FIX, "trader-news")
# The fixtures are real answers fetched Monday 2026-11-09 at 08:37 TSİ (before the open),
# trimmed: google-THYAO.xml (7 of 56 items), yahoo-THYAO.xml (4 of 15), chart-THYAO.json.
NOW = "2026-11-09T08:37:00+03:00"
UTC = datetime.timezone.utc


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


def yahoo_rss(title, pub, guid="g1", link="https://finance.yahoo.com/news/x.html?.tsrc=rss"):
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><item>'
            "<title>%s</title><link>%s</link><guid>%s</guid><pubDate>%s</pubDate>"
            "<description></description></item></channel></rss>" % (title, link, guid, pub)).encode("utf-8")


class Feeds(unittest.TestCase):
    def test_google_news_rss(self):
        items = news.parse_google(raw("google-THYAO.xml"), "THYAO")
        self.assertEqual(len(items), 7)
        first = items[0]
        # "<title>... için... - Hisse.net</title>" with <source>Hisse.net</source>: publisher split off
        self.assertEqual(first["title"], "Borsada 46 hisseye hedef fiyat! THYAO, ASELS ve TUPRS için...")
        self.assertEqual(first["publisher"], "Hisse.net")
        self.assertEqual(first["source"], "Google News")
        self.assertEqual(first["time"], datetime.datetime(2026, 11, 8, 9, 30, 40, tzinfo=UTC))
        self.assertTrue(first["id"].startswith("gn:CBMi"))
        self.assertTrue(first["link"].startswith("https://news.google.com/rss/articles/"))
        self.assertNotIn("<a", first["summary"])  # the HTML description is flattened
        pubs = {i["publisher"] for i in items}
        self.assertIn("Türkiye Gazetesi", pubs)
        self.assertIn("Investing.com Türkiye", pubs)

    def test_yahoo_headline_rss(self):
        items = news.parse_rss(raw("yahoo-THYAO.xml"), "THYAO")
        self.assertEqual(len(items), 4)
        first = items[0]
        self.assertTrue(first["title"].startswith("AerCap Signs Lease Agreements with Turkish Airlines"))
        self.assertEqual(first["time"], datetime.datetime(2026, 10, 29, 11, 0, tzinfo=UTC))
        self.assertEqual(first["id"], "rss:69fd53fe-1ccd-3102-a6d5-e451ad5ed1f6")
        self.assertTrue(first["link"].startswith("https://finance.yahoo.com/"))
        self.assertEqual(first["source"], "Yahoo Finance")

    def test_bad_xml_is_a_plain_error(self):
        with self.assertRaises(news.NewsError):
            news.parse_google(b"<html>blocked</html", "THYAO")
        with self.assertRaises(news.NewsError):
            news.parse_rss(b"not xml", "THYAO")

    def test_times_are_istanbul_utc_plus_3_all_year(self):
        t = news.parse_time("Sun, 08 Nov 2026 09:30:40 GMT")
        self.assertEqual(news.to_eastern(t).strftime("%Y-%m-%d %H:%M"), "2026-11-08 12:30")
        for month in (1, 7):  # no daylight saving time in Turkey
            utc = datetime.datetime(2026, month, 15, 7, 0, tzinfo=UTC)
            self.assertEqual(news.to_eastern(utc).hour, 10)
        # --now without an offset is TSİ
        self.assertEqual(news.parse_now("2026-11-09T10:15").isoformat(), "2026-11-09T07:15:00+00:00")
        self.assertEqual(news.parse_now(NOW).isoformat(), "2026-11-09T05:37:00+00:00")
        self.assertIsNone(news.parse_time("soon"))

    def test_bist_symbols(self):
        self.assertEqual(news.to_yahoo("thyao"), "THYAO.IS")
        self.assertEqual(news.to_yahoo("XU100"), "XU100.IS")
        self.assertEqual(news.to_yahoo("^XU100"), "^XU100")
        self.assertEqual(news.from_yahoo("THYAO.IS"), "THYAO")
        self.assertIn("q=THYAO+hisse", news.GOOGLE % news.google_query("THYAO"))
        self.assertEqual(news.google_query("THYAO", "TÜRK HAVA YOLLARI A.O."),
                         "THYAO+hisse+OR+%22T%C3%9CRK+HAVA+YOLLARI%22")
        self.assertTrue((news.YAHOO % news.to_yahoo("THYAO")).startswith(
            "https://feeds.finance.yahoo.com/rss/2.0/headline?s=THYAO.IS&"))


class Tagging(unittest.TestCase):
    def test_turkish_keywords_on_real_headlines(self):
        items = news.tag(news.parse_google(raw("google-THYAO.xml"), "THYAO"), {"THYAO": None})
        by = {i["title"][:24]: i for i in items}
        self.assertEqual(by["THYAO 2026 3. çeyrek bil"]["tags"], ["earnings"])      # bilançosu, Ciro
        self.assertTrue(by["THYAO 2026 3. çeyrek bil"]["important"])
        self.assertEqual(by["1,3 milyar dolar kâr açı"]["tags"], ["earnings", "analyst"])  # kâr, hedef fiyat
        self.assertEqual(by["THY TEMETTÜ ÖDEME TAKVİM"]["tags"], ["dividend"])       # upper-case Ü
        self.assertFalse(by["THY TEMETTÜ ÖDEME TAKVİM"]["important"])
        self.assertEqual(by["Borsa İstanbul Devre Kes"]["tags"], ["halt"])            # İşlemleri Durdurdu
        self.assertTrue(by["Borsa İstanbul Devre Kes"]["important"])
        self.assertEqual(by["BofA tavan olan THYAO’da"]["tags"], ["limit"])
        self.assertEqual(by["Borsada 46 hisseye hedef"]["tags"], ["analyst"])

    def test_dotted_and_dotless_i_fold(self):
        self.assertEqual(news.fold("BİLANÇO Kârı"), "bilanco kari")
        self.assertEqual(news.fold("IŞIK İŞLEM ığdır"), "isik islem igdir")
        self.assertEqual(news.fold("i̇şlem"), "islem")  # i + combining dot above (lower() of İ)
        for title, tag in (("THYAO'DA İŞLEMLER DURDURULDU", "halt"),
                           ("Şirket KÂR PAYI dağıtacak", "dividend"),
                           ("SPK'DAN İDARİ PARA CEZASI", "regulator"),
                           ("TCMB POLİTİKA FAİZİNİ SABİT TUTTU", "macro"),
                           ("Pay geri alım programı", "buyback"),
                           ("Bedelsiz sermaye artırımı onaylandı", "capital"),
                           ("Yeni İş İlişkisi: 120 milyon dolarlık sözleşme", "contract")):
            item = {"id": "x", "source": "Google News", "ticker": "THYAO", "title": title,
                    "summary": "", "link": "", "time": None}
            self.assertIn(tag, news.tag([item], {"THYAO": None})[0]["tags"], title)

    def test_touches_by_code_and_by_name(self):
        watch = {"THYAO": "TÜRK HAVA YOLLARI A.O.", "ASELS": None, "PGSUS": None}
        self.assertEqual(news.short_name("TÜRK HAVA YOLLARI A.O."), "TÜRK HAVA YOLLARI")
        items = [
            {"id": "a", "source": "Google News", "ticker": "ASELS", "link": "", "time": None,
             "title": "Türk Hava Yolları ve ASELSAN iş birliği", "summary": ""},
            {"id": "b", "source": "Yahoo Finance", "ticker": "ASELS", "link": "", "time": None,
             "title": "PGSUS.IS and THYAOX are different", "summary": ""},
        ]
        news.tag(items, watch)
        self.assertEqual(items[0]["touches"], ["ASELS", "THYAO"])   # name, folded: Türk -> turk
        self.assertEqual(items[1]["touches"], ["ASELS", "PGSUS"])   # THYAOX is not THYAO
        real = news.tag(news.parse_google(raw("google-THYAO.xml"), "THYAO"), watch)
        self.assertEqual(real[0]["touches"], ["ASELS", "THYAO"])    # "THYAO, ASELS ve TUPRS"

    def test_dedupe_across_sources_and_codes(self):
        g = news.parse_google(raw("google-THYAO.xml"), "THYAO")
        g_asels = news.parse_google(raw("google-THYAO.xml"), "ASELS")  # the same story in both searches
        # Yahoo carries the same story with the publisher still in the title, later the same TSİ day
        y = news.parse_rss(yahoo_rss("Borsada 46 hisseye hedef fiyat! THYAO, ASELS ve TUPRS için... - Hisse.net",
                                     "Sun, 08 Nov 2026 15:00:00 +0000"), "THYAO")
        # same title on another day is another story
        other = news.parse_rss(yahoo_rss("Borsada 46 hisseye hedef fiyat! THYAO, ASELS ve TUPRS için...",
                                         "Mon, 09 Nov 2026 05:00:00 +0000", guid="g2",
                                         link="https://finance.yahoo.com/news/y.html"), "THYAO")
        items = news.dedupe(news.tag(g + g_asels + y + other, {"THYAO": None, "ASELS": None}))
        self.assertEqual(len(items), 8)  # 7 Google stories + the other-day one
        first = items[0]
        self.assertEqual(first["source"], "Google News")
        self.assertEqual(first["touches"], ["ASELS", "THYAO"])
        self.assertEqual(sum(1 for i in items if i["source"] == "Yahoo Finance"), 1)
        self.assertEqual(news.norm_link("https://Finance.Yahoo.com/news/x.html?.tsrc=rss"),
                         "finance.yahoo.com/news/x.html")


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        none = os.path.join(self.tmp.name, "none.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW,
                     "--portfolio", none, "--watchlist", none, "--plan", none,
                     "--universe", os.path.join(self.tmp.name, "none.txt")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_new_since_last_check_newest_first(self):
        code, out, err = run(["--tickers", "THYAO", "--hours", "120", "--json", "--no-moves"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        # 120 h before 2026-11-09 05:37 UTC: four Google items, no Yahoo item that recent
        self.assertEqual(len(data["items"]), 4)
        self.assertEqual(data["items"][0]["time_tsi"], "2026-11-08 15:54")
        self.assertEqual(data["checked_tsi"], "2026-11-09 08:37")
        self.assertEqual(data["kap"], news.KAP_URL)
        with open(self.state) as fh:
            self.assertEqual(json.load(fh)["last_check"], "2026-11-09T05:37:00+00:00")
        # a wider window later: only what was not shown before
        code, out, err = run(["--tickers", "THYAO", "--hours", "288", "--json", "--no-moves"] + self.base)
        self.assertEqual(code, 0, err)
        titles = [i["title"] for i in json.loads(out)["items"]]
        self.assertEqual(len(titles), 3, titles)
        self.assertTrue(titles[0].startswith("THYAO 2026 3. çeyrek bilançosu"))
        self.assertTrue(titles[1].startswith("GE's Commercial Engines"))
        self.assertTrue(titles[2].startswith("AerCap Signs"))
        code, out, _ = run(["--tickers", "THYAO", "--hours", "288", "--no-moves"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)

    def test_no_google(self):
        code, out, err = run(["--tickers", "THYAO", "--hours", "288", "--json", "--no-moves",
                              "--no-google", "--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        items = json.loads(out)["items"]
        self.assertEqual([i["source"] for i in items], ["Yahoo Finance", "Yahoo Finance"])
        self.assertFalse(os.path.exists(self.state))  # --dry-run

    def test_text_output_tsi_and_kap_pointer(self):
        code, out, err = run(["--tickers", "THYAO", "--hours", "120"] + self.base)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertIn("! 2026-11-06 14:40 TSİ  THYAO       Google News/Türkiye Gazetesi 1,3 milyar dolar "
                      "kâr açıkladı! 9 kurumdan Türk Hava Yolları (THYAO) hissesi için hedef fiyat", lines)
        self.assertIn("  2026-11-08 12:30 TSİ  THYAO       Google News/Hisse.net Borsada 46 hisseye hedef "
                      "fiyat! THYAO, ASELS ve TUPRS için...", lines)
        self.assertIn("Resmî bildirimler: https://www.kap.org.tr/tr/bildirim-sorgu (official disclosures "
                      "are on KAP; this tool reads press headlines, not KAP)", lines)
        self.assertIn("Checked 2026-11-09 08:37 TSİ.", out)
        self.assertIn("yatırım tavsiyesi değildir", out)
        self.assertNotIn(" ET ", out)
        self.assertNotIn("SEC", out)
        # the move: Monday before the open, so the chart is Friday's session
        self.assertIn("THYAO  299.25 +0.76% vs the close before 297.00 at 2026-11-06 18:09 TSİ (last session)",
                      lines[0])

    def test_two_codes_one_story(self):
        code, out, err = run(["--tickers", "THYAO", "ASELS", "--hours", "120", "--json", "--no-moves",
                              "--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        touched = [i["touches"] for i in data["items"]]
        self.assertIn(["ASELS", "THYAO"], touched)
        # no ASELS files offline: the missing Google file is quiet, Yahoo's is a note
        self.assertTrue(any("Yahoo headlines for ASELS" in n for n in data["notes"]))
        self.assertFalse(any("Google News for ASELS" in n for n in data["notes"]))

    def test_tickers_from_watchlist_and_plan(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [{"symbol": "THYAO.IS", "setups": ["orb"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "trader-exit", "symbol": "ASELS"}, fh)
        self.assertEqual(sorted(news.watch_from_files("/nonexistent.json", wl, plan)), ["ASELS", "THYAO"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        args[args.index("--plan") + 1] = plan
        code, out, err = run(["--json", "--dry-run", "--hours", "120", "--no-moves"] + args)
        self.assertEqual(code, 0, err)
        self.assertEqual({t for i in json.loads(out)["items"] for t in i["touches"]}, {"ASELS", "THYAO"})

    def test_no_codes_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no codes", err)
        self.assertIn("--tickers THYAO", err)

    def test_move_from_the_thyao_chart(self):
        m = news.parse_chart(raw("chart-THYAO.json"), "THYAO")
        self.assertEqual((m["price"], m["prev_close"], m["pct"]), (299.25, 297.0, 0.76))
        self.assertEqual(news.to_eastern(m["time"]).strftime("%Y-%m-%d %H:%M"), "2026-11-06 18:09")
        same_day = news.parse_now("2026-11-06T18:30")
        line = news.render_moves([m], now=same_day)[0]
        self.assertIn("vs yesterday's close 297.00 at 18:09 TSİ", line)
        self.assertNotIn("MOVING", line)
        self.assertIn("MOVING", news.render_moves([dict(m, pct=-3.2)], now=same_day)[0])
        self.assertIn("MOVING", news.render_moves([m], move_pct=0.5)[0])
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZZ")
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": [{"meta": {"regularMarketPrice": 1}}]}}', "ZZZZ")

    def test_no_chart_offline_is_quiet(self):
        d = os.path.join(self.tmp.name, "feeds")
        os.makedirs(d)
        for name in ("google-THYAO.xml", "yahoo-THYAO.xml"):
            shutil.copy(os.path.join(DIR, name), d)
        args = list(self.base)
        args[args.index("--offline") + 1] = d
        code, out, _ = run(["--tickers", "THYAO", "--hours", "120", "--dry-run"] + args)
        self.assertEqual(code, 0)
        self.assertNotIn("close", out.splitlines()[0])
        self.assertNotIn("note:", out)

    def test_state_forgets_after_30_days_and_keeps_future_items_unseen(self):
        now = news.parse_now(NOW)
        state = {"seen": {"old": "2026-10-01T00:00:00+00:00"}}
        later = {"id": "gn:later", "time": now + datetime.timedelta(hours=2)}
        shown = {"id": "gn:shown", "time": now - datetime.timedelta(hours=2)}
        news.remember([later, shown], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertIn(news.fingerprint(shown), state["seen"])
        self.assertNotIn(news.fingerprint(later), state["seen"])  # select() hid it; show it next time
        self.assertEqual(state["last_check"], now.isoformat())


if __name__ == "__main__":
    unittest.main()
