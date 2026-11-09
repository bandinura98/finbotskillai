#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk (Borsa Istanbul). Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json), today's plan (trader/plan.json, for the daily
price limits) and what was placed today (trader/state/broker.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so buys that are fine one by one but break
a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-11-09 10:30"        # pin the clock (Istanbul time, TSİ)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Money amounts are Turkish lira (TL), even where a config key ends in _usd
(the names are kept so every tool reads the same config).

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, ticks, asset and short
sales only; every other rule turns into a warning for it.

What blocks an order (every order):

  format        missing symbol or side, both or neither of qty / notional,
                notional on anything but a market day order, a limit order
                without a limit price, unknown type, extended hours; a
                bracket that is not whole shares, not day / gtc, or misses a leg
  tick          a price (limit, stop, target) that is not on the BIST tick
                grid (fiyat adimi): the exchange rejects it
  asset         options / VIOP unless "allow_options": true; crypto and
                anything that is not a BIST share code (THYAO, ASELS, ...)
  short         selling more than you hold, unless "allow_short": true
  band          a limit price outside today's taban-tavan band (when the
                plan or the order carries "limits")

What blocks an entry (on top):

  blocked       a ticker on your "blocked" list
  size          under min_order_usd, over max_order_usd (TL)
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin
  duplicate     the same symbol and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true (a bracket stop_loss_stop_price
                or the order's plan stop); a stop on the wrong side of the entry
  limits        the stop under today's taban or the target over today's tavan:
                it can never fill today
  loss          qty x (entry - stop + round-trip costs a share) over
                max_loss_per_trade_usd. Costs: commission + BSMV + borsa payi
                on the buy at the entry and the sell at the stop. The entry is
                the limit price for a limit order, else the HIGHER of the plan's
                entry and a fresh price (under 15 minutes old), so a stale plan
                cannot understate the risk
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  daily loss    the max losses at the stops of today's entries plus this one
                over max_daily_loss_usd (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (Istanbul
                time, TSI), or the market is closed (weekend, BIST holiday,
                outside 10:00 to 18:00; on a half day continuous trading ends
                12:30 and the entry window ends 5h30m earlier too)
  pdt           only with "pdt_guard": true (default false). The US
                pattern-day-trader rule does not apply to BIST; kept only for
                anyone who wants a self-imposed cap of 3 day trades in 5 days

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so trader-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

# Amounts in TL. Key names kept for compatibility across the pack.
DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_usd": 80000,
    "min_order_usd": 100, "max_loss_per_trade_usd": 1000, "max_daily_loss_usd": 3000, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "10:15", "no_entries_after": "17:30", "flat_by": "17:50",
    "paper": True, "blocked": [], "allow_options": False, "allow_margin": False, "allow_short": False,
    "pdt_guard": False,
    "commission_rate": 0.0015, "bsmv_rate": 0.05, "exchange_fee_rate": 0.00003,
}
# The order fields that identify an order (same tuple in trader-broker/ticket.py: the fingerprint).
ORDER_FIELDS = ("symbol", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                "stop_loss_limit_price", "advanced_instructions")
PRICE_FIELDS = ("qty", "notional", "limit_price", "stop_price", "take_profit_limit_price",
                "stop_loss_stop_price", "stop_loss_limit_price")
TICK_FIELDS = ("limit_price", "stop_price", "take_profit_limit_price", "stop_loss_stop_price",
               "stop_loss_limit_price")
TYPES = {"market", "limit", "stop", "stop_limit", "trailing_stop"}
TIFS = {"day", "gtc", "ioc", "fok"}
OCC = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
VIOP = re.compile(r"^[OF]_")                       # VIOP options / futures, e.g. O_THYAOE1124C300.00
TICKER = re.compile(r"^[A-Z][A-Z0-9]{2,5}$")        # BIST codes: THYAO, A1CAP, KOZAL, XU100
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CENT = 0.005
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "stop price": "the stop price", "stale price": "an old price"}

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


class RiskError(Exception):
    pass


# ---------------------------------------------------------------- time (Istanbul, no tz files)

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
        raise RiskError("--now wants a time like \"2026-11-09 10:30\" (Istanbul, TSİ) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def trading_day(d):
    """(is_trading_day, reason). Weekends and the BIST holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = BIST_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "BIST tatili: " + name
    return True, ""


def hhmm(value, key):
    """"10:15" -> time(10, 15); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be an Istanbul time (TSİ) like \"10:15\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def _shift(t, delta):
    dt = datetime.datetime.combine(datetime.date(2000, 1, 1), t) - delta
    return dt.time()


def _gap(a, b):
    """time a - time b as a timedelta (CLOSE - EARLY = 5h30m)."""
    d = datetime.date(2000, 1, 1)
    return datetime.datetime.combine(d, a) - datetime.datetime.combine(d, b)


def window_check(now_et, c):
    """Reasons an entry cannot be placed now (Istanbul time). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warns = []
    d, t = now_et.date(), now_et.time().replace(second=0, microsecond=0)
    if d.year not in CALENDAR_YEARS:
        warns.append("the holiday list covers %s only: check borsaistanbul.com for %s"
                     % (" and ".join(map(str, CALENDAR_YEARS)), d.isoformat()))
    ok, why = trading_day(d)
    stamp = now_et.strftime("%a %Y-%m-%d %H:%M")
    if not ok:
        return ["window: the market is closed today (%s, %s TSİ)" % (why, stamp)], warns
    close = EARLY if d.isoformat() in BIST_HALF_DAYS else CLOSE
    if t < OPEN or now_et.time() >= close:
        return ["window: the market is closed (%s TSİ; continuous trading is %s to %s)"
                % (stamp, OPEN.strftime("%H:%M"), close.strftime("%H:%M"))], warns
    if close == EARLY and after is not None:
        after = _shift(after, _gap(CLOSE, EARLY))
        warns.append("yarım gün: %s, continuous trading ends 12:30 today: no new entries after %s"
                     % (BIST_HALF_DAYS[d.isoformat()], after.strftime("%H:%M")))
    if before is not None and t < before:
        return ["window: no new entries before %s TSİ (it is %s)" % (before.strftime("%H:%M"),
                                                                    t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s TSİ (it is %s)" % (after.strftime("%H:%M"),
                                                                   t.strftime("%H:%M"))], warns
    return [], warns


def business_days(today, n=5):
    """The last n BIST trading days up to and including today, newest first."""
    out, d = [], today
    for _ in range(40):
        if trading_day(d)[0]:
            out.append(d.isoformat())
            if len(out) == n:
                break
        d -= datetime.timedelta(days=1)
    return out


# ---------------------------------------------------------------- BIST ticks, limits and costs

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
    t = tick_size(price)
    q = round(price / t, 3)        # float32 guard: Yahoo's 48.31999969 is 48.32, not a tick under it
    q = math.floor(q + 1e-9) if mode == "down" else math.ceil(q - 1e-9) if mode == "up" else round(q)
    return round(q * t, 2)


def on_tick(price):
    """On the BIST tick grid, within float32 noise (312.4999999 is 312.50): same 3-decimal tolerance as round_tick."""
    q = round(price / tick_size(price), 3)
    return abs(q - round(q)) < 1e-9


DEFAULT_COSTS = {
    "commission_rate": 0.0015,   # broker commission per side, fraction of notional (binde 1,5); set yours
    "bsmv_rate": 0.05,           # BSMV on the commission
    "exchange_fee_rate": 0.00003,  # borsa payı per side (approx.)
}


def side_cost(notional, c):
    com = notional * c["commission_rate"]
    return com * (1 + c["bsmv_rate"]) + notional * c["exchange_fee_rate"]


def cost_rates(cfg):
    out = dict(DEFAULT_COSTS)
    for k in DEFAULT_COSTS:
        v = _f((cfg or {}).get(k))
        if v is not None and v >= 0:
            out[k] = v
    return out


def day_limits(order, plan_doc, today):
    """Today's {"base","taban","tavan"} for this order: the order's own, its plan's, or
    trader/plan.json's when that plan is for the same symbol (and today, if it is dated). None if unknown."""
    cands = [order.get("limits"), (order.get("plan") or {}).get("limits") if isinstance(order.get("plan"), dict)
             else None]
    if isinstance(plan_doc, dict) and str(plan_doc.get("symbol", "")).upper() == str(order.get("symbol", "")).upper():
        if not plan_doc.get("date") or str(plan_doc["date"]) == today:
            cands.append(plan_doc.get("limits"))
    for lim in cands:
        if isinstance(lim, dict):
            out = {k: _f(lim.get(k)) for k in ("base", "taban", "tavan")}
            if out["taban"] or out["tavan"]:
                return out
    return None


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """Same function in trader-broker/ticket.py: the exact order that was checked."""
    core = {k: order[k] for k in ORDER_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(x):
    """1234.5 -> '1.234,50 TL' (Turkish grouping)."""
    s = "{:,.2f}".format(float(x))
    return s.replace(",", "_").replace(".", ",").replace("_", ".") + " TL"


def _cent(x):
    """A price to the kurus, half up (312.475 -> 312.48)."""
    return int(float(x) * 100 + 0.5 + 1e-9) / 100.0


def _p(x):
    return "%.2f" % float(x)


def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("ticker") or p.get("symbol") or "").upper()
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, money(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """trader-broker logs the kind trader-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Orders recorded as placed today by trader-broker."""
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and rec_day(rec) == today:
            out.append(rec)
    return out


def day_trades(state, today):
    """Day trades in the last 5 BIST business days: a (day, symbol) with both a buy
    and a sell placed or filled that day. An entry placed today counts too: it
    is meant to be flat by the close. Returns (count, sorted list of "day SYM")."""
    days = set(business_days(datetime.date.fromisoformat(today)))
    sides = {}
    for rec in (state or {}).get("orders", []):
        if rec.get("status") not in ("placed", "filled"):
            continue
        d = rec_day(rec)
        if d not in days:
            continue
        key = (d, str(rec.get("symbol", "")).upper())
        sides.setdefault(key, set()).add(str(rec.get("side", "")).lower())
        if d == today and rec.get("status") == "placed" and rec_is_entry(rec):
            sides[key].add("pending")
    hits = sorted("%s %s" % k for k, s in sides.items()
                  if ("buy" in s and "sell" in s) or (k[0] == today and "pending" in s))
    return len(hits), hits


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          plan_doc=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its Istanbul date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh
    plan_doc     trader/plan.json (optional): its "limits" (taban / tavan) for the same symbol"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    costs = cost_rates(c)
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    now_utc = now or parse_now(None)
    now_et = to_eastern(now_utc)
    today = today or now_et.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(str(r.get("symbol", "")).upper(), r.get("side")) for r in done_today}
    win_reasons, win_warns = window_check(now_et, c)
    pdt_n, pdt_list = day_trades(state, today) if c.get("pdt_guard") else (0, [])
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE (\"paper\": false): you will type these orders into your real broker account")
    top_warn.extend(win_warns)

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        sym = str(o.get("symbol", "")).strip().upper()
        side = str(o.get("side", "")).strip().lower()
        typ = str(o.get("type", "market")).strip().lower()
        tif = str(o.get("time_in_force", "day")).strip().lower()
        qty, notional = o.get("qty"), o.get("notional")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        oc = str(o.get("order_class") or "").strip().lower()
        legs = o.get("take_profit_limit_price") is not None or o.get("stop_loss_stop_price") is not None
        bracket = oc == "bracket" or (not oc and legs)
        value = None
        lim = day_limits(o, plan_doc, today)

        # format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell, got %r" % o.get("side"))
        if (qty is None) == (notional is None):
            reasons.append("format: give exactly one of qty or notional")
        for k in PRICE_FIELDS:
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
        if _f(qty) is not None and abs(_f(qty) - round(_f(qty))) > 1e-9:
            reasons.append("format: BIST trades whole shares (1 lot = 1 share), got qty %r" % qty)
        if typ not in TYPES:
            reasons.append("format: unknown order type %r" % typ)
        if tif not in TIFS:
            reasons.append("format: unknown time_in_force %r (day, gtc, ioc or fok)" % tif)
        if notional is not None and (typ != "market" or tif != "day"):
            reasons.append("format: a notional (TL amount) order must be a market order with time_in_force day")
        if typ in ("limit", "stop_limit") and o.get("limit_price") is None:
            reasons.append("format: a %s order needs a limit_price" % typ)
        if typ in ("stop", "stop_limit") and o.get("stop_price") is None:
            reasons.append("format: a %s order needs a stop_price" % typ)
        if o.get("extended_hours"):
            reasons.append("format: extended hours: this pack trades BIST continuous trading (10:00-18:00) only")
        if o.get("stop_loss_limit_price") is not None and o.get("stop_loss_stop_price") is None:
            reasons.append("format: stop_loss_limit_price needs a stop_loss_stop_price")
        if bracket:
            if notional is not None:
                reasons.append("format: a bracket order needs a whole-share qty, not a notional amount")
            if tif not in ("day", "gtc"):
                reasons.append("format: a bracket order needs time_in_force day or gtc, got %r" % tif)
            if o.get("take_profit_limit_price") is None or o.get("stop_loss_stop_price") is None:
                reasons.append("format: a bracket order needs both take_profit_limit_price and "
                               "stop_loss_stop_price")
        elif oc or o.get("advanced_instructions"):
            warns.append("advanced order (%s): check every leg before you say evet"
                         % (oc or "advanced_instructions"))

        # tick grid (fiyat adimi) and the taban-tavan band, for every order
        for k in TICK_FIELDS:
            v = _f(o.get(k))
            if v is not None and v > 0 and not on_tick(v):
                reasons.append("tick: %s %s is not on the BIST tick grid (tick %s at this price; nearest %s)"
                               % (k, o.get(k), _p(tick_size(v)), _p(round_tick(v))))
        lp = _f(o.get("limit_price"))
        if lim and lp:
            if lim.get("tavan") and lp > lim["tavan"] + CENT:
                reasons.append("band: the limit price %s is over today's tavan %s" % (_p(lp), _p(lim["tavan"])))
            if lim.get("taban") and lp < lim["taban"] - CENT:
                reasons.append("band: the limit price %s is under today's taban %s" % (_p(lp), _p(lim["taban"])))

        # asset
        if sym and (OCC.match(sym.replace(" ", "")) or VIOP.match(sym)):
            if not c.get("allow_options"):
                reasons.append("asset: %s is a derivative (option / VIOP); options are off "
                               "(\"allow_options\": false)" % sym)
            else:
                warns.append("derivative order: options can lose 100%% of what you pay")
        elif sym and ("/" in sym or not TICKER.match(sym)):
            hint = " (use the bare code %s, without .IS)" % sym[:-3] if sym.endswith(".IS") else ""
            reasons.append("asset: %s does not look like a BIST share code%s; "
                           "this pack trades BIST shares only" % (sym, hint))

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if typ in ("limit", "stop_limit"):
            ref, ref_from = _f(o.get("limit_price")), "limit"
        elif typ == "stop":
            ref, ref_from = _f(o.get("stop_price")), "stop price"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                ref = _cent(ref)
                if live and ref_from == "price" and plan_entry and live > plan_entry + CENT:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), _p(plan_entry), _p(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if plan_entry and live is None and sym in prices and price_times is not None and typ == "market":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        q = _f(qty)
        if notional is not None and _f(notional):
            value = _f(notional)
        elif q:
            if ref:
                value = q * ref

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9:
            if q is not None:
                exiting = q <= held + 1e-9
            elif value is not None:
                exiting = value <= held_value + CENT
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to size a %s-share order; add it to "
                         "trader/prices.json (trader-alerts: alerts.py quote %s --write)" % (sym, qty, sym))

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None:
            if value < float(c.get("min_order_usd") or 0) - CENT:
                later.append("size: %s is under your %s minimum order"
                             % (money(value), money(c["min_order_usd"])))
            if c.get("max_order_usd") and value > float(c["max_order_usd"]) + CENT:
                later.append("size: %s is over your %s max order"
                             % (money(value), money(c["max_order_usd"])))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_amt = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "buy" and kind == "entry":
            after_cash = cash - value - side_cost(value, costs)
            if after_cash < -CENT and not c.get("allow_margin"):
                later.append("cash: needs %s with costs but only %s cash; that would be margin (borrowed money)"
                             % (money(value + side_cost(value, costs)), money(cash)))
            elif after_cash < buffer_amt - CENT:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (money(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), money(buffer_amt)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + CENT:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, money(pos_after), 100.0 * pos_after / total, money(total),
                                _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            if q is not None:
                over = q > held + 1e-9
            elif value is not None:
                over = value > held_value + CENT
            else:
                over = held <= 0
            if over and not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (sym, _num(held)))

        # day-trade checks: stop, limits, loss, reward:risk, daily loss, trades, window, pdt
        max_loss = risk_ps = cost_ps = rr = stop = target = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            leg_stop, plan_stop = _f(o.get("stop_loss_stop_price")), _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            leg_target = _f(o.get("take_profit_limit_price"))
            target = leg_target or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > CENT:
                warns.append("the bracket stop %s differs from the plan's stop %s: the bracket's is the one "
                             "on the emir fisi" % (_p(leg_stop), _p(plan_stop)))
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only, not on the order: set a stop alert "
                             "(trader-alerts: alerts.py add --from-plan)" % _p(stop))
                if not on_tick(stop):
                    later.append("tick: the plan's stop %s is not on the BIST tick grid (nearest %s)"
                                 % (_p(stop), _p(round_tick(stop))))
            if target and not leg_target and not on_tick(target):
                later.append("tick: the plan's target %s is not on the BIST tick grid (nearest %s)"
                             % (_p(target), _p(round_tick(target))))
            if lim and long:
                if stop and lim.get("taban") and stop < lim["taban"] - CENT:
                    later.append("limits: the stop %s is under today's taban %s (base %s): it can never fill "
                                 "today" % (_p(stop), _p(lim["taban"]), _p(lim["base"]) if lim.get("base") else "?"))
                if target and lim.get("tavan") and target > lim["tavan"] + CENT:
                    later.append("limits: the target %s is over today's tavan %s (base %s): it can never fill "
                                 "today" % (_p(target), _p(lim["tavan"]), _p(lim["base"]) if lim.get("base") else "?"))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): a bracket "
                                 "stop_loss_stop_price, from trader-exit and trader-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - CENT) or (not long and stop <= ref + CENT):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side))
                else:
                    risk_c = int(round(abs(ref - stop) * 100))
                    risk_ps = risk_c / 100.0
                    cost_ps = round(side_cost(ref, costs) + side_cost(stop, costs), 4)
                    shares = q if q is not None else ((value or 0) / ref)
                    max_loss = round(shares * (risk_ps + cost_ps), 2)
                    cap = _f(c.get("max_loss_per_trade_usd"))
                    if cap and max_loss > cap + CENT:
                        later.append("loss: %s shares x (%s risk + %s costs a share) = %s at the stop, over your "
                                     "%s max loss per trade" % (_num(shares), _p(risk_ps), "%.2f" % cost_ps,
                                                                money(max_loss), money(cap)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + CENT) or (not long and target >= ref - CENT):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            rr = round(int(round(abs(target - ref) * 100)) / float(risk_c), 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = _f(c.get("max_daily_loss_usd"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + CENT:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used), money(max_loss), money(used + max_loss),
                                                       money(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            later.extend(win_reasons)
            if c.get("pdt_guard") and pdt_n >= 3:
                later.append("pdt: %d day trades in the last 5 business days (%s); your own cap is 3 "
                             "(\"pdt_guard\": true; no such rule exists on BIST)"
                             % (pdt_n, ", ".join(pdt_list)))
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s + costs %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps), "%.2f" % cost_ps))
                info.append("max loss %s with costs%s, reward:risk %s%s"
                            % (money(max_loss), (" (limit %s)" % money(c["max_loss_per_trade_usd"]))
                               if _f(c.get("max_loss_per_trade_usd")) else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if lim:
                info.append("today's band: taban %s, tavan %s" % (_p(lim["taban"]) if lim.get("taban") else "?",
                                                                 _p(lim["tavan"]) if lim.get("tavan") else "?"))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               money(loss_today + batch_loss), money(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side, sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if side == "buy":
                if value is not None:
                    acct["cash"] -= value + side_cost(value, costs)
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                if q is not None:
                    acct["shares"][sym] = held + q
                elif ref and value is not None:
                    acct["shares"][sym] = held + value / ref
            elif side == "sell":
                if value is not None:
                    acct["cash"] += value - side_cost(value, costs)
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                if q is not None:
                    acct["shares"][sym] = held - q
                elif ref and value is not None:
                    acct["shares"][sym] = held - value / ref
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind,
            "amount": money(_f(notional) or 0) if notional is not None else ("%s shares" % qty),
            "value_try": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "cost_per_share": cost_ps,
            "max_loss": max_loss, "reward_risk": rr, "limits": lim, "fingerprint": fingerprint(o),
            "verdict": verdict, "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "market": "BIST",
        "currency": "TRY",
        "now_istanbul": now_et.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2), "day_trades_5d": pdt_n if c.get("pdt_guard") else None},
    }


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s, %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["amount"],
                                                r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s TSİ. Cash after the passed orders: %s (%s)."
                 % (res["pass"], res["block"], res["now_istanbul"], money(res["cash_after"]),
                    "kağıt üzerinde / sanal" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders never get an emir fisi. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you evet or hayir for each one.")
    lines.append("Education and tooling, not investment advice (yatırım tavsiyesi değildir).")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive UTC or None}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of."""
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        s = str(s).upper()
        out[s] = float(p)
        d = detail.get(s) or {}
        times[s] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
    return out, times


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed BIST orders against your limits (trader-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--plan", default="trader/plan.json", help="today's plan: its taban / tavan limits, if any")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker recorded today")
    ap.add_argument("--now", help="pretend it is this Istanbul time (TSİ), e.g. \"2026-11-09 10:30\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in Istanbul)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):      # TSİ and Turkish text on a non-UTF-8 pipe
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-11-09")
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        plan_doc = read_json(a.plan, "plan", required=False)
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times, plan_doc)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = now.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")   # --now pins it too
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2, ensure_ascii=False) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
