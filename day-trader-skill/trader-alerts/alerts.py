#!/usr/bin/env python3
"""Price alerts and the Borsa İstanbul (BIST) clock for trader-alerts. Standard library only.

    python3 alerts.py clock                          # is BIST open? auction, continuous, next open
    python3 alerts.py quote THYAO --write            # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every ticker in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by-close from trader/plan.json
    python3 alerts.py add --symbol THYAO --stop 282.00
    python3 alerts.py add --symbol THYAO --target 289.00
    python3 alerts.py add --at 17:50 --label "flat by close" --before-close
    python3 alerts.py add --symbol ASELS --pct 3
    python3 alerts.py add --symbol GARAN --near-limit 1   # within 1 % of tavan or taban
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-11-09 17:50" --offline tests/fixtures/trader-alerts
    python3 alerts.py list
    python3 alerts.py reset thyao-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  near_limit      the price is within "pct" percent of today's tavan (+10 %) or
                  taban (-10 %), worked out from the previous close (rings at
                  most once per day)
  opening_bell    BIST continuous trading started today at 10:00 TSİ (rings
                  once per trading day)
  time            the Istanbul clock (TSİ) passed "at" (HH:MM) on a trading day
                  (rings once per trading day); "before_close": true moves it
                  5h30m earlier on a half day (continuous trading ends 12:30),
                  so a 17:50 "flat by close" rings at 12:20 on those days (a
                  time already before 12:30 stays where it is)

A rule with "date" is only watched on that Istanbul date (rules from a plan
are for the plan's day only). An above / below alert rings ONCE and then stays
quiet until you reset it, unless the rule has "rearm": true (it re-arms when
the price crosses back). A pct_move alert rings at most once per day.

Quotes (free, delayed ~15 minutes, unofficial; for alerts only, never for
timing an order -- confirm every price in your broker app):
  yahoo   https://query2.finance.yahoo.com/v8/finance/chart/THYAO.IS (falls back
          to query1 on a 429 or a 5xx; one retry with a pause on a 429; one
          request per ticker, half a second apart; the default)
  stooq   kept only for reading saved --offline files: Stooq has no BIST quotes.
  --offline DIR reads DIR/<source>-<TICKER>.json|csv (or .json.gz) instead
  (TICKER is the bare BIST code, e.g. yahoo-THYAO.json).

BIST calendar: the 2026 and 2027 holidays and half days from the Borsa
İstanbul 2026 tatil listesi (read 2026-11-09); the 2027 bayram dates are
estimates (tahmini). Outside those years the clock says the holiday list is
unknown.
"""

import argparse
import csv
import datetime
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5    # seconds between requests: be polite to free endpoints
BACKOFF = 2.0  # seconds to wait before the one retry after a 429
YAHOO_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

BIST_HOLIDAYS = {  # Borsa İstanbul closed all day (weekends are handled separately)
    "2026-01-01": "Yılbaşı",
    "2026-03-20": "Ramazan Bayramı 1. gün",
    "2026-04-23": "Ulusal Egemenlik ve Çocuk Bayramı",
    "2026-05-01": "Emek ve Dayanışma Günü",
    "2026-05-19": "Atatürk'ü Anma, Gençlik ve Spor Bayramı",
    "2026-05-27": "Kurban Bayramı 1. gün",
    "2026-05-28": "Kurban Bayramı 2. gün",
    "2026-05-29": "Kurban Bayramı 3. gün",
    "2026-07-15": "Demokrasi ve Milli Birlik Günü",
    "2026-10-29": "Cumhuriyet Bayramı",
    "2027-01-01": "Yılbaşı",
    "2027-03-09": "Ramazan Bayramı 1. gün (tahmini)",
    "2027-03-10": "Ramazan Bayramı 2. gün (tahmini)",
    "2027-03-11": "Ramazan Bayramı 3. gün (tahmini)",
    "2027-04-23": "Ulusal Egemenlik ve Çocuk Bayramı",
    "2027-05-17": "Kurban Bayramı 2. gün (tahmini)",
    "2027-05-18": "Kurban Bayramı 3. gün (tahmini)",
    "2027-05-19": "Kurban Bayramı 4. gün / Gençlik ve Spor Bayramı (tahmini)",
    "2027-07-15": "Demokrasi ve Milli Birlik Günü",
    "2027-08-30": "Zafer Bayramı",
    "2027-10-29": "Cumhuriyet Bayramı",
}
BIST_HALF_DAYS = {  # continuous trading ends 12:30
    "2026-03-19": "Ramazan Bayramı arefesi",
    "2026-05-26": "Kurban Bayramı arefesi",
    "2026-10-28": "Cumhuriyet Bayramı arefesi",
    "2027-03-08": "Ramazan Bayramı arefesi (tahmini)",
    "2027-10-28": "Cumhuriyet Bayramı arefesi",
}
CALENDAR_YEARS = (2026, 2027)
OPEN = datetime.time(10, 0)
CLOSE = datetime.time(18, 0)
EARLY = datetime.time(12, 30)   # half days (arefe / 28 Ekim): continuous trading ends 12:30
SESSION_BARS = 96               # 10:00 to 18:00 in 5-minute bars (was 78)
AUCTION_OPEN = (datetime.time(9, 40), datetime.time(9, 55))   # açılış seansı (opening auction)
CLOSING_AUCTION = datetime.timedelta(minutes=10)              # kapanış seansı: close to close + 10 min


def _minus(t, delta):
    return (datetime.datetime.combine(datetime.date(2000, 1, 1), t) - delta).time()


def _plus(t, delta):
    return (datetime.datetime.combine(datetime.date(2000, 1, 1), t) + delta).time()


HALF_DAY_SHIFT = (datetime.datetime.combine(datetime.date(2000, 1, 1), CLOSE)
                  - datetime.datetime.combine(datetime.date(2000, 1, 1), EARLY))   # 5h30m


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- symbols, money, ticks

def to_yahoo(sym):
    """THYAO -> THYAO.IS. A symbol that already has a suffix or starts with ^ is left alone."""
    s = sym.strip().upper()
    if s.startswith("^") or "." in s:
        return s
    return s + ".IS"


def from_yahoo(ysym):
    s = (ysym or "").strip().upper()
    return s[:-3] if s.endswith(".IS") else s


def money(x):
    """1234.5 -> '1.234,50 TL' (Turkish grouping)."""
    s = "{:,.2f}".format(float(x))
    return s.replace(",", "_").replace(".", ",").replace("_", ".") + " TL"


TICKS = (  # (price from, tick)
    (0.0, 0.01), (20.0, 0.02), (50.0, 0.05), (100.0, 0.10),
    (250.0, 0.25), (500.0, 0.50), (1000.0, 1.00), (2500.0, 2.50),
)


def tick_size(price):
    t = TICKS[0][1]
    for lo, step in TICKS:
        if price >= lo:
            t = step
    return t


def round_tick(price, mode="nearest"):
    """Round to the BIST tick. mode: 'down' (stops for longs, buy limits),
    'up' (targets), 'nearest'."""
    import math
    t = tick_size(price)
    q = round(price / t, 3)  # float32 noise from Yahoo (48.31999969 is 48.32), legit off-tick >= 0.004 ticks
    q = math.floor(q + 1e-9) if mode == "down" else math.ceil(q - 1e-9) if mode == "up" else round(q)
    return round(q * t, 2)


def daily_limits(base):
    """(taban, tavan) for a previous close: -10 % / +10 %, rounded to the tick, inwards."""
    return round_tick(base * 0.90, "up"), round_tick(base * 1.10, "down")


# ---------------------------------------------------------------- time (Istanbul, TSİ)

TR_OFFSET = 3  # Europe/Istanbul, UTC+3 all year since 2016, no DST


def eastern_offset(utc):
    """Hours from UTC for Istanbul (TSİ): +3 all year, no daylight time."""
    return TR_OFFSET


def to_eastern(utc):
    """naive UTC -> naive Istanbul time (name kept for the diff)."""
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=TR_OFFSET)


def eastern_to_utc(local):
    return local - datetime.timedelta(hours=TR_OFFSET)


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as Istanbul time (TSİ)."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise AlertError("--now wants a time like \"2026-11-09 10:30\" (Istanbul, TSİ) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def trading_day(d):
    """(is_trading_day, reason). Weekends and the BIST holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = BIST_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "BIST tatili: " + name
    return True, ""


def session(d):
    """(open_time, close_time) of continuous trading in Istanbul time, or None if closed all day."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, (EARLY if d.isoformat() in BIST_HALF_DAYS else CLOSE)


def market_status(now_utc):
    """BIST status. "open" is True during continuous trading (sürekli işlem) only;
    "phase" also names the opening auction (09:40-09:55) and the closing auction
    (close to close + 10 min)."""
    local = to_eastern(now_utc)
    d = local.date()
    ok, reason = trading_day(d)
    half = BIST_HALF_DAYS.get(d.isoformat())
    st = {"now_istanbul": local.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(),
          "weekday": local.strftime("%A"), "calendar_known": d.year in CALENDAR_YEARS,
          "open": False, "phase": "closed", "reason": reason, "early_close": half,
          "half_day": ("yarım gün: " + half) if half else None}
    s = session(d)
    if s:
        o, c = s
        ca_end = _plus(c, CLOSING_AUCTION)
        st["opens"], st["closes"] = o.strftime("%H:%M"), c.strftime("%H:%M")
        st["opening_auction"] = "%s-%s" % (AUCTION_OPEN[0].strftime("%H:%M"), AUCTION_OPEN[1].strftime("%H:%M"))
        st["closing_auction"] = "%s-%s" % (c.strftime("%H:%M"), ca_end.strftime("%H:%M"))
        now = local.time()
        if o <= now < c:
            st["open"] = True
            st["phase"] = "continuous"
            st["reason"] = "continuous trading (sürekli işlem)"
        elif AUCTION_OPEN[0] <= now < AUCTION_OPEN[1]:
            st["phase"] = "opening_auction"
            st["reason"] = "opening auction (açılış seansı), continuous trading from %s" % st["opens"]
        elif AUCTION_OPEN[1] <= now < o:
            st["phase"] = "pre_open"
            st["reason"] = "opening auction done, continuous trading from %s" % st["opens"]
        elif now < AUCTION_OPEN[0]:
            st["phase"] = "pre_open"
            st["reason"] = "before the opening auction"
        elif c <= now < ca_end:
            st["phase"] = "closing_auction"
            st["reason"] = "closing auction (kapanış seansı)"
        else:
            st["phase"] = "closed"
            st["reason"] = "after the close"
    # next open
    nd = d if (s and local.time() < s[0]) else d + datetime.timedelta(days=1)
    for _ in range(15):
        if session(nd):
            st["next_open"] = "%s %s" % (nd.isoformat(), OPEN.strftime("%H:%M"))
            break
        nd += datetime.timedelta(days=1)
    return st


# ---------------------------------------------------------------- quotes

def stooq_url(sym):
    """Kept for the diff: Stooq carries no BIST shares, so a live stooq fetch is refused."""
    return "https://stooq.com/q/l/?s=%s&f=sd2t2ohlcv&h&e=csv" % sym.lower()


def yahoo_url(sym, host=YAHOO_HOSTS[0]):
    return "https://%s/v8/finance/chart/%s?range=1d&interval=1d" % (host, to_yahoo(sym))


def parse_stooq(text, sym):
    """Symbol,Date,Time,Open,High,Low,Close,Volume -> quote. N/D means no data."""
    rows = list(csv.DictReader(io.StringIO(text.strip())))
    if not rows:
        raise AlertError("Stooq returned no rows for %s" % sym)
    r = {k.strip().lower(): (v or "").strip() for k, v in rows[0].items() if k}
    close = r.get("close", "")
    if not close or close.upper() == "N/D":
        raise AlertError("Stooq has no data for %s (check the ticker)" % sym)

    def f(k):
        v = r.get(k, "")
        try:
            return float(v)
        except ValueError:
            return None
    return {"symbol": sym.upper(), "price": float(close), "open": f("open"), "high": f("high"),
            "low": f("low"), "prev_close": None, "time": ("%s %s" % (r.get("date", ""), r.get("time", ""))).strip(),
            "source": "stooq"}


def parse_yahoo(text, sym):
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AlertError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise AlertError("Yahoo has no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    m = res[0].get("meta", {})
    price = m.get("regularMarketPrice")
    if price is None:
        raise AlertError("Yahoo has no price for %s" % sym)
    t = m.get("regularMarketTime")
    when = (datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if t else "")
    q = (res[0].get("indicators", {}).get("quote") or [{}])[0]
    opens = [x for x in (q.get("open") or []) if x is not None]
    return {"symbol": sym.upper(), "price": float(price), "open": opens[0] if opens else None,
            "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"),
            "prev_close": m.get("previousClose") or m.get("chartPreviousClose"), "time": when,
            "currency": m.get("currency"), "source": "yahoo"}


_urlopen = urllib.request.urlopen   # tests swap these two
_sleep = time.sleep


def _get(url, timeout=20):
    """(HTTP status, body). A network failure is an AlertError."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with _urlopen(req, timeout=timeout) as r:
            return 200, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            try:
                e.close()
            except Exception:
                pass
        return e.code, body
    except urllib.error.URLError as e:
        raise AlertError("could not reach %s: %s" % (url.split("/")[2], e.reason))


def fetch(url, timeout=20):
    code, text = _get(url, timeout)
    if code != 200:
        raise AlertError("%s answered HTTP %d" % (url.split("?")[0], code))
    return text


def fetch_yahoo(sym, timeout=20):
    """query2 first. A 429 waits BACKOFF seconds and tries once more; a 429 or a 5xx
    after that moves on to query1. A 404 comes back as Yahoo's own error JSON."""
    last = None
    for host in YAHOO_HOSTS:
        url = yahoo_url(sym, host)
        code, text = _get(url, timeout)
        if code == 429:
            _sleep(BACKOFF)
            code, text = _get(url, timeout)
        if code == 200:
            return text
        if code == 404 and text.strip().startswith("{"):
            return text
        last = (url, code)
        if code != 429 and code < 500:
            break
    raise AlertError("%s answered HTTP %d" % (last[0].split("?")[0], last[1]))


def _offline_text(folder, source, sym):
    ext = "csv" if source == "stooq" else "json"
    base = os.path.join(folder, "%s-%s.%s" % (source, sym, ext))
    if os.path.exists(base):
        with open(base, encoding="utf-8") as fh:
            return fh.read()
    if os.path.exists(base + ".gz"):
        with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    raise AlertError("no offline quote file %s" % base)


def get_quotes(symbols, source="yahoo", offline=None, pause=PAUSE):
    """{SYM: quote} and [errors]. One request per ticker, with a pause between."""
    quotes, errors = {}, []
    for i, sym in enumerate(symbols):
        sym = from_yahoo(sym)   # keys and state keep the bare BIST code: THYAO
        try:
            if offline:
                text = _offline_text(offline, source, sym)
            else:
                if source == "stooq":
                    raise AlertError("Stooq has no BIST quotes for %s: use --source yahoo" % sym)
                if i and pause:
                    _sleep(pause)
                text = fetch_yahoo(sym)
            quotes[sym] = parse_stooq(text, sym) if source == "stooq" else parse_yahoo(text, sym)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now_utc, source):
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "time": q.get("time"), "source": q.get("source"),
                     "fetched": stamp}
    doc = {"as_of": stamp, "source": source,
           "note": ("Delayed quotes (Yahoo .IS, can be ~15 minutes behind) for alerts and sizing checks. "
                    "Your broker app's price is the one that fills. detail.fetched is when each price was read."),
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "near_limit", "opening_bell", "time"}
NO_QUOTE = {"opening_bell", "time"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t in ("pct_move", "near_limit") and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "near_limit" and not 0 < rule["pct"] < 20:
        raise AlertError("rule %s: near_limit pct must be between 0 and 20" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an Istanbul (TSİ) time like \"17:50\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that Istanbul date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    """Did a rule that rang at this UTC stamp ring on this Istanbul date?"""
    if not stamp:
        return False
    try:
        utc = datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return stamp[:10] == today
    return to_eastern(utc).date().isoformat() == today


def ring_time(rule, status):
    """The time a time rule rings today, moved CLOSE - EARLY (5h30m) earlier on a BIST
    half day if "before_close": a 17:50 flat-by rings at 12:20. A time already before the
    half day's 12:30 close is left alone: trader-exit writes flat_by "12:20" into a
    half-day plan, and moving that again would ring at 06:50."""
    at = _hhmm(rule.get("at"))
    if rule.get("before_close") and status.get("early_close") and at >= EARLY:
        at = _minus(at, HALF_DAY_SHIFT)
    return at


def evaluate(rules, quotes, status, state, now_utc):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    local = to_eastern(now_utc)
    stamp = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message":
                              "Opening bell: BIST continuous trading is open (%s TSİ)%s."
                              % (status["now_istanbul"], (", half day: continuous trading ends at %s today, %s"
                                                          % (EARLY.strftime("%H:%M"), status["early_close"]))
                                 if status.get("early_close") else "")})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = ring_time(r, status)
            if status.get("opens") and local.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s TSİ (set for %s)" % (label[:1].upper(), label[1:],
                                                            local.strftime("%H:%M"), at.strftime("%H:%M"))
                msg += (", continuous trading ends at %s." % status["closes"]) if status["open"] else \
                    ", continuous trading has ended."
                sym = from_yahoo(str(r.get("symbol") or ""))
                if sym:
                    msg += (" If %s is still open, close it: trader-broker flatten --symbol %s builds the sell "
                            "order slip (emir fişi) for your broker app, then trader-risk and your yes."
                            % (sym, sym))
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = from_yahoo(r["symbol"])
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s %.2f: %s your %s %s%s."
                              % (sym, "is" if status["open"] else "last", p, word, label, _n(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "near_limit":
            base = q.get("prev_close")
            if not base:
                continue
            taban, tavan = daily_limits(float(base))
            near = r["pct"] / 100.0
            side = None
            if p >= tavan * (1 - near):
                side, lim = "tavan", tavan
            elif p <= taban * (1 + near):
                side, lim = "taban", taban
            if side and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message":
                              "%s %.2f is within %s%% of today's %s %.2f (previous close %.2f; taban %.2f, "
                              "tavan %.2f)." % (sym, p, _n(r["pct"]), side, lim, float(base), taban, tavan)})
                rung[rid] = stamp
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to %.2f (from %.2f)."
                              % (sym, move, p, float(ref))})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> a stop alert, a target alert and a flat-by-close time alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run trader-exit first)")
    sym = from_yahoo(str(plan["symbol"]))   # bare BIST code: THYAO
    out = []
    for key, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run trader-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    flat = plan.get("flat_by") or cfg_flat_by or "17:50"
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not a time like 17:50" % flat)
    out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat,
                "label": "flat by close", "before_close": True})
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        syms.append(from_yahoo(str(p.get("ticker") or p.get("symbol") or "")))
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(from_yahoo(str(plan["symbol"])))
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def cmd_clock(a):
    st = market_status(parse_now(a.now))
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print("Istanbul %s TSİ (%s): BIST %s, %s." % (st["now_istanbul"], st["weekday"],
                                                      "OPEN" if st["open"] else "closed", st["reason"]))
        if st.get("opens"):
            print("Today: opening auction %s, continuous trading %s to %s, closing auction %s%s." % (
                st["opening_auction"], st["opens"], st["closes"], st["closing_auction"],
                (" (%s)" % st["half_day"]) if st.get("half_day") else ""))
        if not st["open"] and st.get("next_open"):
            print("Next continuous-trading open: %s TSİ." % st["next_open"])
        if not st["calendar_known"]:
            print("Holiday list covers %s only: check borsaistanbul.com for this date."
                  % " and ".join(map(str, CALENDAR_YEARS)))
        print("Quotes here come from Yahoo (.IS) and can be ~15 minutes delayed: confirm in your broker app.")
    return 0 if st["open"] else 1


def cmd_quote(a):
    syms = _dedupe([from_yahoo(s) for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which tickers? alerts.py quote THYAO ASELS, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, a.source, a.offline)
    if a.write and quotes:
        write_prices(a.write, quotes, now, a.source)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    taban, tavan = daily_limits(float(q["prev_close"]))
                    chg = " (%+.2f%% vs prev close %.2f; taban %.2f, tavan %.2f)" % (
                        100.0 * (q["price"] / q["prev_close"] - 1), q["prev_close"], taban, tavan)
                print("%-6s %.2f%s  %s  [%s]" % (s, q["price"], chg, q.get("time", ""), q["source"]))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Quotes are free, unofficial and can be ~15 minutes delayed: for alerts only, not exchange-grade. "
              "Confirm in your broker app; its price is the one that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    now = parse_now(a.now)
    status = market_status(now)
    watched = [r for r in rules if active(r, status["date"])]
    syms = _dedupe([from_yahoo(str(r.get("symbol", ""))) for r in watched if r.get("type") not in NO_QUOTE])
    quotes, errors = (get_quotes(syms, a.source, a.offline) if syms else ({}, []))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now, a.source)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": {s: q["price"] for s, q in quotes.items()}}, indent=2))
    else:
        print("BIST %s (%s TSİ, %s)." % ("OPEN" if status["open"] else "closed", status["now_istanbul"],
                                         status["reason"]))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s %.2f" % (s, quotes[s]["price"]) for s in syms if s in quotes))
                if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order, and the Yahoo quote can be ~15 minutes delayed: "
                  "confirm the price in your broker app. If you entered a stop or take-profit order there, "
                  "check whether it filled and record it (ticket.py fill). To close a position yourself: "
                  "ticket.py flatten prints the emir fişi, then trader-risk and your yes.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run trader-exit first)" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"))
        ids = {r["id"] for r in new}
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r)))
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "Free Yahoo quotes can be ~15 minutes delayed; the stop order in your broker app is what "
              "actually stops you out.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an Istanbul (TSİ) time like 17:50")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = from_yahoo(a.symbol)
        if a.before_close:
            rule["before_close"] = True
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        sym = from_yahoo(a.symbol)
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        elif a.near_limit is not None:
            rule = {"type": "near_limit", "pct": a.near_limit}
        else:
            raise AlertError("give one of --above, --below, --stop, --target, --pct or --near-limit")
        val = rule.get("price", rule.get("pct"))
        rule = dict({"id": a.id or ("%s-%s-%s" % (sym.lower(), rule["type"], _n(val)))}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-24s %s%s" % (rid, json.dumps({k: v for k, v in r.items() if k != "id"}),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Turkish text (TSİ) on a non-UTF-8 Windows pipe
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Price alerts and the Borsa İstanbul clock (trader-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--source", default="yahoo", choices=["yahoo", "stooq"],
                       help="yahoo (.IS); stooq only for saved --offline files")
        p.add_argument("--offline", metavar="DIR", help="read <source>-<TICKER>.json|csv files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = Istanbul time, TSİ)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is BIST open? (auctions, continuous trading, half days)")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest delayed quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add tickers from your portfolio and plan")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--near-limit", type=float, metavar="PCT",
                   help="ring when the price is within PCT %% of today's tavan or taban (from the previous close)")
    g.add_argument("--opening-bell", action="store_true", help="ring when continuous trading opens at 10:00 TSİ")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, Istanbul time (TSİ), e.g. 17:50")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by-close from the plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json", help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by close\"")
    p.add_argument("--before-close", action="store_true",
                   help="a time alert that moves 5h30m earlier on a BIST half day (close 12:30): 17:50 -> 12:20")
    p.add_argument("--date", help="watch this rule on this Istanbul date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
