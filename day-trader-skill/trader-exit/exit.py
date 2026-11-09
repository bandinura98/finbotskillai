#!/usr/bin/env python3
"""Exit Planner for trader-exit: sets the stop and the target BEFORE you
enter, so you know when to get out. Standard library only. Long side only.
It never places an order.

    python3 exit.py plan                                    # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write  # example: stop 308.00, target 317.00
    python3 exit.py plan --symbol THYAO --entry 312.50 --or-high 310.75 --or-low 305.25 --base-price 306
    python3 exit.py plan --stop atr:1.5 --offline THYAO.json # ATR needs the session's bars
    python3 exit.py plan --symbol ASELS --entry 190.30 --setup level --stop pct:1
    python3 exit.py show                                    # the current trader/plan.json

Borsa Istanbul (BIST), Istanbul time (TSİ). Prices sit on the BIST tick
(fiyat adımı: 0.01 under 20 TL, 0.02 to 50, 0.05 to 100, 0.10 to 250, 0.25 to
500, 0.50 to 1000, 1.00 to 2500, 2.50 above).

Stops (a long's stop rounds DOWN to the tick, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price (a Turkish decimal comma works: price:308,50)
  pct:X      X percent under the entry (pct:1,5 is 1.5 %)
  atr:K      K times the 14-bar ATR under the entry, on the session's bars up
             to the entry bar (--offline FILE or Yahoo). With fewer than 14
             bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (round UP to the tick):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop (the rounded stop)
  price:X                      your price

Refused, with the reason: a stop at or above the entry, a target at or below
it, a risk under 0.05% of the price (too tight to survive the spread), an
entry.json that has not triggered (unless you give --entry), a short (v2
plans longs only).

Daily limits (taban / tavan): +/-10 % of the base price (the previous close),
on the tick. The base comes from --base-price, else the chart's meta
(chartPreviousClose / previousClose, when bars are read: an atr stop or
--offline), else trader/entry.json. A stop under the taban or a target over the
tavan cannot fill today: it is warned and recorded in plan.json ("limits",
"warnings"); trader-risk blocks it.

Costs (round trip, per share): broker commission + BSMV on it + borsa payı,
per side, from "commission_rate" (0.0015), "bsmv_rate" (0.05) and
"exchange_fee_rate" (0.00003) in trader/config.json. The plan shows
reward:risk gross and after costs.

flat_by (default 17:50, or "flat_by" in trader/config.json): out by then, win
or lose. On a half day (continuous trading ends 12:30) it moves up with the
close (12:20). "min_reward_risk" in the config (default 1.0) only warns here;
trader-risk enforces it.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULT_COSTS = {
    "commission_rate": 0.0015,   # broker commission per side, fraction of notional (binde 1,5); set yours
    "bsmv_rate": 0.05,           # BSMV on the commission
    "exchange_fee_rate": 0.00003,  # borsa payı per side (approx.)
}
DEFAULTS = dict({"flat_by": "17:50", "min_reward_risk": 1.0}, **DEFAULT_COSTS)
ATR_BARS = 14
MIN_RISK_PCT = Decimal("0.05")     # risk under 0.05% of the price is inside the spread's noise
CENT = Decimal("0.01")
EPOCH = datetime.datetime(1970, 1, 1)
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."

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

TICKS = (  # (price from, tick)
    (0.0, 0.01), (20.0, 0.02), (50.0, 0.05), (100.0, 0.10),
    (250.0, 0.25), (500.0, 0.50), (1000.0, 1.00), (2500.0, 2.50),
)


class PlanError(Exception):
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


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_now(text):
    """--now -> naive UTC. A time without an offset is Istanbul time (TSİ)."""
    if not text:
        return utc_now()
    t = text.strip()
    for suffix in (" TSİ", " TSI", " TRT"):
        if t.upper().endswith(suffix):
            t = t[:-len(suffix)].strip()
            break
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_eastern(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise PlanError('--now wants an Istanbul time (TSİ) like "2026-10-19 10:30" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise PlanError("%s wants an Istanbul time (TSİ) like 17:50, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_clock(text, what):
    """10:15 AM, 10:15, 3:05 PM -> time."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text))
    if not m:
        raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
    return datetime.time(h, mi)


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def session(d):
    """(open_time, close_time) of continuous trading in Istanbul time (TSİ), or None if closed all day."""
    if d.weekday() >= 5 or d.isoformat() in BIST_HOLIDAYS:
        return None
    return OPEN, (EARLY if d.isoformat() in BIST_HALF_DAYS else CLOSE)


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


# ---------------------------------------------------------------- ticks, daily limits, costs

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
    """{base, taban, tavan}: +/-10 % of the base price (the previous close), on the tick. None without a base."""
    try:
        b = float(base)
    except (TypeError, ValueError):
        return None
    if b <= 0:
        return None
    return {"base": round(b, 2), "taban": round_tick(b * 0.90, "up"), "tavan": round_tick(b * 1.10, "down")}


def side_cost(notional, c):
    com = notional * c["commission_rate"]
    return com * (1 + c["bsmv_rate"]) + notional * c["exchange_fee_rate"]


# ---------------------------------------------------------------- money math (Decimal, to the kuruş)

def D(x, what="a number"):
    try:
        if isinstance(x, float):
            return Decimal(repr(x))
        s = str(x).strip().lstrip("$")
        if s.upper().endswith("TL"):
            s = s[:-2].strip()
        return Decimal(plain_number(s))
    except (InvalidOperation, ValueError):
        raise PlanError("%s is not a number: %r" % (what, x))


def plain_number(s):
    """Turkish or English digits -> '1234.5'. Turkish users write a decimal comma:
    '308,50' and '1.234,50' (Turkish), '1,234.50' (English). A lone comma with 1-2
    digits after it is a decimal comma; with 3 it groups thousands."""
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            return s.replace(".", "").replace(",", ".")
        return s.replace(",", "")
    if re.match(r"^-?\d+,\d{1,2}$", s):
        return s.replace(",", ".")
    return s.replace(",", "")


def cents(x):
    """Round half up to the kuruş (0.01)."""
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


def floor_cents(x):
    """Round down to the kuruş (0.01)."""
    return D(x).quantize(CENT, rounding=ROUND_FLOOR)


def tick_down(x):
    """A long's stop: round DOWN to the BIST tick, away from the entry."""
    return D(round_tick(float(x), "down"))


def tick_up(x):
    """A long's target: round UP to the BIST tick."""
    return D(round_tick(float(x), "up"))


def money(x):
    """1234.5 -> '1.234,50 TL' (Turkish grouping)."""
    s = "{:,.2f}".format(float(x))
    return s.replace(",", "_").replace(".", ",").replace("_", ".") + " TL"


def px(x):
    """A share price, no currency: 312.50 (already on the tick)."""
    return format(D(x).quantize(CENT, rounding=ROUND_HALF_UP), "f")


def cost4(x):
    """A per-share cost for JSON: 4 decimals."""
    return round(float(x), 4) + 0.0


def num(x):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    s = format(D(x).normalize(), "f")
    return s


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(price|pct|atr):\s*\$?([0-9][0-9.,]*)$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X or atr:K (got %r)" % text)
    kind, val = m.group(1), D(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*[.,]?[0-9]+)\s*[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*\$?([0-9][0-9.,]*)$", t.lower())
    if m:
        return "price", D(m.group(1), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None):
    """The stop (down to the tick), the target (up to the tick) and the risk from those
    rounded numbers. Raises PlanError with the reason."""
    e = cents(entry)
    if e <= 0:
        raise PlanError("the entry price must be above 0")
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = cents(or_high), cents(or_low)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (px(oh), px(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = %s" % (px(oh), px(ol), num(raw))
        else:
            raw, label = ol, "opening-range low"
    elif stop_rule == "price":
        raw, label = stop_param, "your price"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "%s%% under the entry" % num(stop_param)
        detail = "%s x (1 - %s%%) = %s" % (px(e), num(stop_param), num(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        a = D(round(atr, 6))
        raw = e - stop_param * a
        label = "%s x ATR(%d) under the entry" % (num(stop_param), atr_bars)
        detail = "ATR %s over the %d bars to the entry%s" % (
            num(a.quantize(Decimal("0.0001"))), atr_bars, " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    if D(raw) <= 0:
        raise PlanError("the stop works out to %s: pick a stop above 0" % num(raw))
    stop = tick_down(raw)
    if stop != D(raw):
        note = "rounded down to the %s tick" % num(D(repr(tick_size(float(raw)))))
        detail = (detail + ", " + note if detail else "%s %s" % (num(raw), note))
    if stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (px(stop), label, px(e)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above 0" % px(stop))
    risk = e - stop
    floor_risk = e * MIN_RISK_PCT / 100
    if risk < floor_risk:
        raise PlanError("the stop is %s under the entry, less than 0.05%% of the price (%s on %s): too tight to "
                        "survive the spread, normal wiggle would stop you out. Widen it."
                        % (money(risk), money(floor_risk.quantize(CENT, rounding=ROUND_HALF_UP)), px(e)))
    if target_rule == "r":
        target = tick_up(e + target_param * risk)
        t_label = r_label(target_param)
    else:
        target = tick_up(target_param)
        t_label = None
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (px(target), px(e)))
    rr = ((target - e) / risk).quantize(CENT, rounding=ROUND_HALF_UP)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    return {"entry": e, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr}


# ---------------------------------------------------------------- bars for ATR (same reader as trader-entry)

def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """query2 first, query1 on 429 or 5xx; one retry after a pause on 429."""
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown ticker
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise PlanError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise PlanError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise PlanError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_chart(sym, interval, day, offline=None):
    """Bars (and the previous close from the meta) for one BIST code: Yahoo THYAO.IS or a saved file."""
    ysym = to_yahoo(sym)
    if offline:
        path = offline
        if os.path.isdir(offline):
            path = None
            for name in (from_yahoo(ysym) + ".json", from_yahoo(ysym) + ".json.gz", ysym + ".json", ysym + ".json.gz"):
                if os.path.exists(os.path.join(offline, name)):
                    path = os.path.join(offline, name)
                    break
            if path is None:
                raise PlanError("no offline file for %s in %s (want %s.json)" % (from_yahoo(ysym), offline,
                                                                                  from_yahoo(ysym)))
        if not os.path.exists(path):
            raise PlanError("no offline file %s" % path)
        try:
            text = read_text(path)
        except (OSError, ValueError) as e:
            raise PlanError("could not read %s: %s" % (path, e))
    else:
        back = (to_eastern(utc_now()).date() - day).days
        rng = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if interval == "1m" and back > 6:
            raise PlanError("Yahoo keeps 1-minute bars for about a week")
        text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s" % (ysym, rng, interval))
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise PlanError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    if from_yahoo(str(meta.get("symbol") or ysym)) != from_yahoo(ysym):
        raise PlanError("the chart file holds %s, not %s" % (meta.get("symbol"), sym))
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        h, lo = at("high", i), at("low", i)
        bars.append({"t": int(t), "h": float(c if h is None else h), "l": float(c if lo is None else lo),
                     "c": float(c)})
    return {"bars": bars, "data_time": meta.get("regularMarketTime"),
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose")}


def chart_base(chart, day):
    """The base price for `day` from a chart: the last close of an earlier session in it (a
    multi-day range), else the meta's previousClose / chartPreviousClose. None when unknown."""
    prev = None
    for b in chart.get("bars") or []:
        if to_eastern(utc_of(b["t"])).date() < day:
            prev = b["c"]
    return prev if prev is not None else chart.get("prev_close")


def session_bars(chart, day, interval, upto_slot=None, now_utc=None):
    """Closed, on-grid bars of one Istanbul session, oldest first (the live tick is dropped)."""
    step = INTERVALS[interval]
    s = session(day)
    if not s:
        return []
    open_dt = datetime.datetime.combine(day, s[0])
    close_dt = datetime.datetime.combine(day, s[1])
    if now_utc is None and chart.get("data_time"):
        now_utc = utc_of(chart["data_time"])
    local = to_eastern(now_utc) if now_utc else None
    out = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue
        et = to_eastern(utc_of(b["t"]))
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        if local and et + datetime.timedelta(seconds=step) > local:
            continue
        slot = int((et - open_dt).total_seconds()) // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        for k in DEFAULTS:
            if doc.get(k) is not None:
                cfg[k] = doc[k]
    for k in DEFAULT_COSTS:
        try:
            cfg[k] = float(cfg[k])
        except (TypeError, ValueError):
            raise PlanError("%s in the config is not a number: %r" % (k, cfg[k]))
        if cfg[k] < 0:
            raise PlanError("%s in the config must be 0 or more, not %r" % (k, cfg[k]))
    return cfg


def costs_of(entry, stop, target, cfg):
    """Per-share costs (komisyon + BSMV + borsa payı), per side and round trip."""
    c = {k: cfg[k] for k in DEFAULT_COSTS}
    cin = side_cost(float(entry), c)
    out_stop = side_cost(float(stop), c)
    out_target = side_cost(float(target), c)
    return dict(c, entry_per_share=cost4(cin), exit_per_share_at_stop=cost4(out_stop),
                exit_per_share_at_target=cost4(out_target),
                round_trip_at_stop=cost4(cin + out_stop), round_trip_at_target=cost4(cin + out_target))


def resolve_flat_by(day, explicit, cfg, entry_time):
    """(HH:MM, note). The config's flat-by moves up with a half day's 12:30 close."""
    t = parse_hhmm(explicit or cfg.get("flat_by") or DEFAULTS["flat_by"], "--flat-by" if explicit else "flat_by")
    note = ""
    s = session(day) if day else None
    if s:
        close_dt = datetime.datetime.combine(day, s[1])
        at = datetime.datetime.combine(day, t)
        if at >= close_dt:
            if explicit:
                raise PlanError("--flat-by %s is at or after the %s close on %s" % (hm(t), hm(s[1]), day))
            moved = at - (datetime.datetime.combine(day, CLOSE) - close_dt)
            if moved.time() <= OPEN:
                raise PlanError("flat_by %s in the config is at or after the close" % hm(t))
            t = moved.time()
            note = "moved up for the half day, continuous trading ends 12:30 (yarım gün: %s)" % BIST_HALF_DAYS.get(
                day.isoformat(), "")
    if entry_time and entry_time >= t:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (ampm(entry_time), hm(t)))
    return hm(t), note


def gather(a):
    """The entry facts, from trader/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("v2 plans longs only: no stops above a short entry. Plan a buy instead.")
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": from_yahoo(a.symbol), "entry": a.entry, "base": None, "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "source": "flags"}
    else:
        path = a.from_path or "trader/entry.json"
        doc = read_json(path, "entry (run /trader-entry first, or give --symbol and --entry)")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: v2 plans longs only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan ahead"
                            % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        lim = doc.get("limits") if isinstance(doc.get("limits"), dict) else {}
        base = lim.get("base") if lim.get("base") is not None else doc.get("base_price")
        if a.date and str(a.date) != str(doc.get("date")):
            base = None     # entry.json's base price is for its own session
        info = {"symbol": from_yahoo(str(doc.get("symbol") or "")), "base": base,
                "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "source": path}
        if not info["symbol"] or info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info


def build_plan(a, now_utc):
    info = gather(a)
    cfg = read_config(a.config)
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X or atr:K"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    day = None
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = to_eastern(now_utc).date()
    entry_time = parse_clock(info["entry_time"], "the entry time") if info["entry_time"] else None
    atr = atr_n = None
    chart = None
    if stop_rule == "atr":
        chart = load_chart(info["symbol"], info["interval"], day, a.offline)
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], parse_now(a.now) if a.now else None)
        if len(bars) < 3:
            raise PlanError("an atr stop needs at least 3 closed %s bars of %s on %s by the entry; found %d"
                            % (info["interval"], info["symbol"], day, len(bars)))
        atr, atr_n = atr_of(bars)
    elif a.offline and a.base_price is None:
        try:                       # a saved chart is free to read: its meta has the previous close
            chart = load_chart(info["symbol"], info["interval"], day, a.offline)
        except PlanError:
            chart = None
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                atr, atr_n)
    # daily limits: --base-price, else the chart's previous close, else trader/entry.json
    base, base_src = None, None
    if a.base_price is not None:
        if a.base_price <= 0:
            raise PlanError("--base-price must be above 0")
        base, base_src = a.base_price, "--base-price"
    elif chart is not None and chart_base(chart, day) is not None:
        base, base_src = chart_base(chart, day), "chart"
    elif info.get("base") is not None:
        base, base_src = info["base"], "entry.json"
    limits = daily_limits(base)
    if limits:
        limits["source"] = base_src
    # round-trip costs a share
    costs = costs_of(c["entry"], c["stop"], c["target"], cfg)
    risk_net = c["risk"] + D(costs["round_trip_at_stop"])
    reward_net = (c["target"] - c["entry"]) - D(costs["round_trip_at_target"])
    rr_net = (reward_net / risk_net).quantize(CENT, rounding=ROUND_HALF_UP)
    flat_by, flat_note = resolve_flat_by(day, a.flat_by, cfg, entry_time)
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(cents(info["or_high"])), "low": float(cents(info["or_low"])),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "trader-exit", "symbol": info["symbol"], "side": "buy", "date": day.isoformat(),
            "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": ampm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]), "flat_by": flat_by,
            "stop_detail": c["stop_detail"], "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "note": NOTE,
            "tick_stop": tick_size(float(c["stop"])), "tick_target": tick_size(float(c["target"])),
            "limits": limits,
            "costs": costs, "cost_per_share": costs["round_trip_at_stop"],
            "cost_per_share_at_target": costs["round_trip_at_target"],
            "risk_per_share_net": cost4(risk_net), "reward_per_share_net": cost4(reward_net),
            "reward_risk_net": float(rr_net)}
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    warnings = []
    if limits:
        if c["stop"] < D(limits["taban"]):
            warnings.append("The stop %s is under today's taban %s (base %s): the price cannot trade there today, so "
                            "this stop can never fill. Use a stop at or above the taban." % (
                                px(c["stop"]), px(limits["taban"]), px(limits["base"])))
        if c["target"] > D(limits["tavan"]):
            warnings.append("The target %s is over today's tavan %s (base %s): the price cannot trade there today, so "
                            "this target can never fill. Use a target at or under the tavan." % (
                                px(c["target"]), px(limits["tavan"]), px(limits["base"])))
        if not D(limits["taban"]) <= c["entry"] <= D(limits["tavan"]):
            warnings.append("The entry %s is outside today's limits %s-%s (base %s): check the base price." % (
                px(c["entry"]), px(limits["taban"]), px(limits["tavan"]), px(limits["base"])))
        elif c["entry"] >= D(limits["tavan"]) * D("0.99"):
            warnings.append("The entry %s is within 1%% of today's tavan %s: a breakout near the tavan can lock "
                            "limit-up and a buy may not fill." % (px(c["entry"]), px(limits["tavan"])))
    else:
        warnings.append("No base price (previous close), so the taban/tavan were not checked: "
                        "give --base-price PRICE.")
    if c["entry"] != D(round_tick(float(c["entry"]), "nearest")):
        warnings.append("The entry %s is not on the %s tick: a limit order goes in at %s." % (
            px(c["entry"]), num(D(repr(tick_size(float(c["entry"]))))), px(round_tick(float(c["entry"]), "nearest"))))
    if rr_net <= 0:
        warnings.append("After costs the target earns nothing: reward:risk %s gross, %s after costs." % (
            ratio(c["reward_risk"]), ratio(rr_net)))
    if flat_note:
        warnings.append("Flat-by %s: %s." % (flat_by, flat_note))
    mrr = cfg.get("min_reward_risk")
    try:
        if mrr is not None and c["reward_risk"] < D(mrr):
            warnings.append("Reward:risk %s is under your min_reward_risk %s in the config: trader-risk checks that."
                            % (ratio(c["reward_risk"]), num(D(mrr))))
    except PlanError:
        pass
    today = to_eastern(now_utc).date()
    if day != today:
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), today.isoformat()))
    plan["warnings"] = list(warnings)
    return plan, warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    setup = {"orb": "opening-range breakout", "level": "close above a level"}.get(plan["setup"], plan["setup"])
    e = D(plan["entry"])
    risk = D(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(CENT, rounding=ROUND_HALF_UP)
    when = ""
    if plan.get("entry_time"):
        when = " at %s" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s long, %s, %s on %s bars" % (
        plan["symbol"], day.strftime("%a %Y-%m-%d"), setup, "5-minute" if plan["interval"] == "5m" else "1-minute")]
    out.append("  Entry         %s%s" % (px(plan["entry"]), when))
    out.append("  Stop          %s  %s%s" % (px(plan["stop"]), plan["stop_label"],
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (px(plan["target"]), plan["target_label"]))
    out.append("  Risk a share  %s (%s%% of the entry)" % (money(plan["risk_per_share"]), pct))
    costs = plan.get("costs")
    if costs:
        out.append("  Costs a share %s round trip if stopped (%s in + %s out), %s at the target: "
                   "komisyon + BSMV + borsa payı, estimated" % (
                       money(costs["round_trip_at_stop"]), money(costs["entry_per_share"]),
                       money(costs["exit_per_share_at_stop"]), money(costs["round_trip_at_target"])))
    if plan.get("reward_risk_net") is not None:
        out.append("  Reward:risk   %s gross, %s after costs" % (ratio(plan["reward_risk"]),
                                                                ratio(plan["reward_risk_net"])))
    else:
        out.append("  Reward:risk   %s" % ratio(plan["reward_risk"]))
    lim = plan.get("limits")
    if lim:
        out.append("  Daily limits  taban %s, tavan %s (base %s, the previous close)" % (
            px(lim["taban"]), px(lim["tavan"]), px(lim["base"])))
    out.append("  Flat by       %s TSİ, win or lose: no overnight hold" % plan["flat_by"])
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


def cmd_plan(a):
    now = parse_now(a.now)
    plan, warnings = build_plan(a, now)
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print(("WARNING: " if ("taban" in w or "tavan" in w) and "not checked" not in w else "Note: ") + w)
    print("Yahoo's BIST data can be ~15 minutes delayed: confirm the prices in your broker's screen.")
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /trader-timemachine replays it, then /trader-budget sizes it to your max loss. Nothing was placed.")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    for line in describe(dict({"setup": "orb", "interval": "5m", "stop_label": "", "target_label": ""}, **plan)):
        print(line)
    for w in plan.get("warnings") or []:
        if ("taban" in w or "tavan" in w) and "not checked" not in w:
            print("WARNING: " + w)
    today = to_eastern(parse_now(a.now)).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Turkish text (TSİ) on a non-UTF-8 Windows pipe
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Exit Planner (trader-exit): the stop and target before the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json to plan from (default trader/entry.json)")
    p.add_argument("--symbol")
    p.add_argument("--entry", type=float, help="entry price (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time, e.g. 10:20 or 10:20 AM")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="v2: buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--flat-by", help="HH:MM Istanbul time, TSİ (default: config flat_by, 17:50)")
    p.add_argument("--base-price", type=float,
                   help="the base price (previous close) for today's taban/tavan (default: the chart or entry.json)")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for an atr stop (and the base price)")
    p.add_argument("--now", help='pretend it is this Istanbul time (TSİ), e.g. "2026-10-19 10:30"')
    p.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH",
                   help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
