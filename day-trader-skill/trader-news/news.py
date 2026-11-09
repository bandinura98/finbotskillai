#!/usr/bin/env python3
"""News monitor for trader-news (Borsa Istanbul). Standard library only.

Watches press headlines for the BIST shares you watch or hold and prints only
what is new since the last check, newest first, with how far each share has
moved today and a flag on anything that touches your codes.

    python3 news.py                                   # watchlist + plan + portfolio codes
    python3 news.py --tickers THYAO ASELS GARAN
    python3 news.py --tickers THYAO --all --hours 72  # everything in the last 3 days, seen or not
    python3 news.py --no-google                       # Yahoo headlines only
    python3 news.py --no-moves                        # skip the price line per code

Your codes, when --tickers is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json. Company names come from those files or
from trader/universe.txt (CODE|Name lines, written by `scan.py universe`).

Sources, one request each per code:
  - Google News RSS in Turkish (press headlines, not official disclosures):
    https://news.google.com/rss/search?q=<CODE>+hisse&hl=tr&gl=TR&ceid=TR:tr
    When the company name is known the query also asks for "<name>".
  - Yahoo Finance headline RSS for <CODE>.IS:
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=<CODE>.IS&region=US&lang=en-US
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against yesterday's close:
    https://query2.finance.yahoo.com/v8/finance/chart/<CODE>.IS?range=1d&interval=5m

Official disclosures (özel durum açıklamaları, financial reports) are on KAP,
https://www.kap.org.tr/tr/bildirim-sorgu . This tool reads press headlines,
not KAP: confirm anything that matters there.

Times print in Istanbul time (TSİ, UTC+3). State: trader/state/news.json
remembers what you have seen (30 days), so an item shows once. Offline:
--offline DIR reads google-<CODE>.xml, yahoo-<CODE>.xml and chart-<CODE>.json
from DIR. The tests use it.

Headlines are leads, not facts: open the story before acting. Nothing here is
investment advice (yatırım tavsiyesi değildir).
"""

import argparse
import datetime as _dt
import datetime
import email.utils
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US"
GOOGLE = "https://news.google.com/rss/search?q=%s&hl=tr&gl=TR&ceid=TR:tr"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
KAP_URL = "https://www.kap.org.tr/tr/bildirim-sorgu"
KAP_NOTE = ("Resmî bildirimler: %s (official disclosures are on KAP; this tool reads press "
            "headlines, not KAP)" % KAP_URL)
BROWSER_UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a share this far from yesterday's close is flagged as moving
TICKER_RX = re.compile(r"^[A-Z][A-Z0-9]{2,5}$")

# Keyword patterns run on fold()ed text: lower case, Turkish letters mapped to
# ASCII (ı/İ/I -> i, ş -> s, ğ -> g, ç -> c, ö -> o, ü -> u, â -> a), so one
# pattern catches "Bilanço", "BİLANÇO" and "bilanco".
KEYWORDS = [
    ("earnings", r"\bbilanco|finansal sonuc|mali tablo|\bkar(?:i|ini|inda|ini|da|lilik|liligi)?\b"
                 r"|\bnet kar|\bzarar|\bhasilat|\bciro(?:su)?\b|\bfaaliyet sonuc"
                 r"|\bearnings\b|\bresults\b|\bprofit\b|\brevenue\b|\bguidance\b|\bbeklenti"),
    ("dividend", r"\btemettu|kar payi|\bdividend"),
    ("capital", r"\bbedelsiz|\bbedelli|sermaye artiri|\bhalka arz|\bpay satis|\brights issue"
                r"|\bbonus (?:issue|shares)|\bcapital increase|\bstock split|\boffering\b|\bipo\b"),
    ("buyback", r"pay geri alim|\bgeri alim|\bbuyback|\brepurchase"),
    ("deal", r"\bbirlesme|\bdevral|\bdevir\b|satin al(?:ma|di|acak|iyor|im)|\bortaklik anlasma"
             r"|\bacqui(?:re|res|red|sition)\b|\bmerger\b|\btakeover\b|\bdeal\b"),
    ("contract", r"yeni is iliskisi|\bsozlesme|\bihale|\bsiparis|\bcontract\b|\btender\b"),
    ("regulator", r"\bspk\b|sermaye piyasasi kurulu|\bsorusturma|\bceza|\brekabet kurulu|\bbddk\b"
                  r"|\bprobe\b|\binvestigation\b|\bfine[ds]?\b|\bregulator"),
    ("kap", r"\bkap\b|ozel durum aciklama|kamuyu aydinlatma"),
    ("analyst", r"hedef fiyat|\bal tavsiye|\btut tavsiye|\bsat tavsiye|model portfoy"
                r"|\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|\boutperform\b|\bunderperform\b"),
    ("people", r"genel mudur|yonetim kurulu baskan|\bistifa|\bataman|\batandi|\bceo\b|\bcfo\b"
               r"|\bresign|\bsteps down\b|\bappoint"),
    ("halt", r"islem(?:ler|leri)? durdur|islem yasag|\bbrut takas|\btek fiyat|\bvbts\b|\btedbir"
             r"|\bdevre kesici|\bhalt(?:s|ed)?\b|\btrading (?:pause|suspen)"),
    ("limit", r"\btavan(?:a|da|dan|i|ini|ina)?\b|\btaban(?:a|da|dan)? (?:oldu|vurdu|seri)"
              r"|\btabanda\b|\btabana\b|\blimit (?:up|down)\b"),
    ("risk", r"\bkonkordato|\bifla|\bsiber|\bdefault\b|\bbankrupt|\bhack|\bbreach\b"),
    ("macro", r"\btcmb\b|merkez bankasi|\bpolitika faizi|\bfaiz karar|\benflasyon|\btufe\b"
              r"|\bfed\b|\bfomc\b|\binflation\b|\brate (?:cut|hike)"),
]
IMPORTANT_TAGS = ("earnings", "capital", "regulator", "halt", "risk")

_FOLD = str.maketrans({"İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
                       "Ç": "c", "ç": "c", "Ö": "o", "ö": "o", "Ü": "u", "ü": "u",
                       "Â": "a", "â": "a", "Î": "i", "î": "i", "Û": "u", "û": "u",
                       "’": "'", "̇": None})


def fold(text):
    """Turkish-safe matching form: 'BİLANÇO Kârı' -> 'bilanco kari'."""
    return (text or "").translate(_FOLD).lower()


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

TR_OFFSET = 3  # Europe/Istanbul, UTC+3 all year since 2016, no DST


def eastern_offset(utc):
    """Hours from UTC for Istanbul (TSİ): +3 all year, no daylight time."""
    return TR_OFFSET


def to_eastern(utc):
    """naive UTC -> naive Istanbul time (name kept for the diff)."""
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=TR_OFFSET)


def eastern_to_utc(local):
    return local - datetime.timedelta(hours=TR_OFFSET)


# ---------------------------------------------------------------- symbols

def to_yahoo(sym):
    """THYAO -> THYAO.IS. A symbol that already has a suffix or starts with ^ is left alone."""
    s = sym.strip().upper()
    if s.startswith("^") or "." in s:
        return s
    return s + ".IS"


def from_yahoo(ysym):
    s = (ysym or "").strip().upper()
    return s[:-3] if s.endswith(".IS") else s


def yahoo_symbol(ticker):
    return to_yahoo(ticker)


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def load_universe(path="trader/universe.txt"):
    """{CODE: name} from a CODE|Name file; {} when there is none."""
    out = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            code, _, name = line.partition("|")
            code = from_yahoo(code)
            if code:
                out[code] = name.strip() or None
    return out


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{code: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[from_yahoo(sym)] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(from_yahoo(pl["symbol"]), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("ticker") or p.get("symbol")
        if sym:
            code = from_yahoo(sym)
            out[code] = p.get("name") or out.get(code)
    return out


_LEGAL_WORDS = {"as", "tas", "ao", "anonim", "sirketi", "sanayi", "sanayii", "san", "ticaret",
                "tic", "ve", "ltd", "sti"}


def short_name(name):
    """'TÜRK HAVA YOLLARI A.O.' -> 'TÜRK HAVA YOLLARI' (legal suffixes dropped), or None."""
    if not name:
        return None
    keep = []
    for w in name.replace(",", " ").split():
        if re.sub(r"[^a-z]", "", fold(w)) in _LEGAL_WORDS:
            break
        keep.append(w)
    s = " ".join(keep).strip(" .-")
    return s if len(s) >= 4 else None


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.3):
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        headers = {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip",
                   "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url, exc.reason))


def google_query(code, name=None):
    q = "%s hisse" % code
    sn = short_name(name)
    if sn:
        q = '%s OR "%s"' % (q, sn)
    return urllib.parse.quote_plus(q)


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_now(value):
    """--now: ISO time; without an offset it is Istanbul time (TSİ)."""
    value = (value or "").strip()
    try:
        d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return parse_time(value)
    if d.tzinfo is None:
        return eastern_to_utc(d).replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def _strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\s+", " ", s).strip()


def parse_rss(data, ticker):
    """Yahoo headline RSS -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline",
                    "ticker": ticker, "title": title, "summary": _text(it, "description"),
                    "link": link, "time": parse_time(_text(it, "pubDate")), "important": False})
    return out


def parse_google(data, ticker):
    """Google News RSS -> items. Titles end in ' - <publisher>'; that part is split off."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("Google News RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        publisher = ""
        for c in it:
            if _local(c.tag) == "source":
                publisher = (c.text or "").strip()
        if publisher and title.endswith(" - " + publisher):
            title = title[:-len(" - " + publisher)].rstrip()
        out.append({"id": "gn:" + guid, "source": "Google News", "kind": "headline",
                    "publisher": publisher or None, "ticker": ticker, "title": title,
                    "summary": _strip_html(_text(it, "description")),
                    "link": link, "time": parse_time(_text(it, "pubDate")), "important": False})
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            try:
                data = fetcher.get(CHART % ("query2", to_yahoo(t)), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", to_yahoo(t)), name)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def mention_patterns(watch):
    """{code: (code regex on the raw text, name regex on fold()ed text or None)}."""
    pats = {}
    for t, name in watch.items():
        code_rx = re.compile(r"(?<![A-Za-z0-9])[#$(]?%s(?:\.IS)?\b" % re.escape(t))
        name_rx = None
        sn = short_name(name)
        if sn:
            name_rx = re.compile(r"\b%s\b" % re.escape(fold(sn)).replace(r"\ ", r"[\s-]+"))
        pats[t] = (code_rx, name_rx)
    return pats


def tag(items, watch):
    """Which of your codes each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        fhay = fold(hay)
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, (code_rx, name_rx) in pats.items():
            if code_rx.search(hay) or (name_rx is not None and name_rx.search(fhay)):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, fhay)]
        it["important"] = bool(it.get("important")) or any(t in IMPORTANT_TAGS for t in it["tags"])
    return items


def norm_title(title):
    """Title key for dedupe across sources: folded, publisher suffix and punctuation dropped."""
    t = fold(title)
    t = re.sub(r"\s+[-|]\s+[^-|]{2,40}$", "", t)  # ' - Dünya', ' | Bloomberg HT'
    return re.sub(r"\W+", " ", t).strip()


def norm_link(link):
    """Link key: scheme, query and fragment dropped, lower-case host, no trailing slash."""
    if not link:
        return ""
    p = urllib.parse.urlsplit(link.strip())
    return (p.netloc.lower() + p.path.rstrip("/")) or link.strip()


def dedupe(items):
    """One item per story: same id, or same link, or same normalized title the same TSİ day."""
    seen, out = {}, []
    for it in items:
        nt = norm_title(it["title"])
        keys = [it["id"], norm_link(it["link"]),
                ("%s|%s" % (nt, to_eastern(it["time"]).date() if it["time"] else "")) if nt else ""]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            for t in it.get("tags", []):
                if t not in hit.get("tags", []):
                    hit.setdefault("tags", []).append(t)
            hit["important"] = bool(hit.get("important") or it.get("important"))
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, state, google=True, errors=None):
    errors = [] if errors is None else errors
    items = []
    for t in sorted(watch):
        if google:
            try:
                data = fetcher.get(GOOGLE % google_query(t, watch.get(t)), "google-%s.xml" % t)
                items += parse_google(data, t)
            except NewsError as exc:
                if not (fetcher.offline and "no file" in str(exc)):
                    errors.append("Google News for %s: %s" % (t, exc))
        try:
            items += parse_rss(fetcher.get(YAHOO % to_yahoo(t), "yahoo-%s.xml" % t), t)
        except NewsError as exc:
            errors.append("Yahoo headlines for %s: %s" % (t, exc))
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    ahead = now + _dt.timedelta(minutes=10)
    for it in items:
        if it["time"] is not None and it["time"] > ahead:
            continue  # select() hid it as "from the future": it has not been shown yet
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT, now=None):
    """One line per code. Before the open (or on a holiday) Yahoo's 1-day chart is the
    last session, so a price from an earlier TSİ day than `now` carries its date."""
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when, prev = "", "yesterday's close"
        if m.get("time"):
            local = to_eastern(m["time"])
            if now is not None and local.date() != to_eastern(now).date():
                when = " at %s TSİ (last session)" % local.strftime("%Y-%m-%d %H:%M")
                prev = "the close before"
            else:
                when = " at %s TSİ" % local.strftime("%H:%M")
        out.append("%-6s %s %+.2f%% vs %s %s%s  %s" % (
            m["ticker"], "{:,.2f}".format(m["price"]), m["pct"], prev,
            "{:,.2f}".format(m["prev_close"]), when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct, now)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = to_eastern(it["time"]).strftime("%Y-%m-%d %H:%M TSİ") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        src = it["source"]
        if it.get("publisher"):
            src = "%s/%s" % (src, it["publisher"])
        out.append("%s %s  %-11s %-13s %s" % (mark, when, who, src[:30], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append(KAP_NOTE)
    out.append("Checked %s. Headlines are leads, not facts; open the story (and KAP) before acting. "
               "Not investment advice (yatırım tavsiyesi değildir)."
               % to_eastern(now).strftime("%Y-%m-%d %H:%M TSİ"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        d["time_tsi"] = to_eastern(it["time"]).strftime("%Y-%m-%d %H:%M") if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Turkish text (TSİ) on a non-UTF-8 Windows pipe
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="BIST codes, e.g. THYAO ASELS GARAN "
                                                 "(default: watchlist + plan + portfolio)")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--universe", default="trader/universe.txt",
                    help="CODE|Name lines, for company names in the search")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-google", action="store_true", help="Yahoo headlines only, skip Google News")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per code")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time for reproducible runs; no offset = Istanbul time (TSİ)")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    known = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if args.tickers:
        watch = {from_yahoo(t): None for t in args.tickers}
        watch.update({t: known[t] for t in watch if known.get(t)})
    else:
        watch = known
    if not watch:
        print("news: no codes. Pass --tickers THYAO, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    errors = []
    bad = [t for t in watch if not TICKER_RX.match(t)]
    for t in bad:
        errors.append("%s does not look like a BIST code (THYAO, ASELS, XU100); tried anyway" % t)
    names = load_universe(args.universe)
    for t in watch:
        if not watch[t] and names.get(t):
            watch[t] = names[t]
    now = parse_now(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    if now is None:
        print("news: --now is not an ISO time: %s" % args.now, file=sys.stderr)
        return 2
    state = load_json(args.state) or {}
    fetcher = Fetcher(offline=args.offline)
    items = collect(fetcher, watch, state, google=not args.no_google, errors=errors)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   time_tsi=to_eastern(m["time"]).strftime("%Y-%m-%d %H:%M") if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(),
                          "checked_tsi": to_eastern(now).strftime("%Y-%m-%d %H:%M"),
                          "moves": mv, "items": _json_ready(shown),
                          "notes": errors, "kap": KAP_URL}, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(shown, state, now)  # only what was shown: a wider --hours later still finds the rest
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
