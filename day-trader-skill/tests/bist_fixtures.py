"""SYNTHETIC Borsa İstanbul bars for the tests (not market data).

Builds Yahoo v8 chart JSON the way Yahoo answers for a `.IS` symbol: unix
timestamps of each bar's START, Istanbul = UTC+3 all year, continuous trading
10:00-18:00 TSİ (12:30 on half days), prices on the BIST tick grid.

    from bist_fixtures import chart, orb_day, trading_days, ts

    doc = chart("THYAO", trading_days("2026-08-10", "2026-11-09"), seed=7)
    doc = orb_day("THYAO", "2026-11-09", or_low=305.25, or_high=310.75, trigger_close=312.50)

Everything is deterministic (random.Random(seed)).
"""

import datetime
import math
import random

TR = datetime.timedelta(hours=3)
OPEN = datetime.time(10, 0)
CLOSE = datetime.time(18, 0)
EARLY = datetime.time(12, 30)

# same lists as docs/BIST-SPEC.md
HOLIDAYS = {
    "2026-01-01", "2026-03-20", "2026-04-23", "2026-05-01", "2026-05-19", "2026-05-27", "2026-05-28",
    "2026-05-29", "2026-07-15", "2026-10-29", "2027-01-01", "2027-03-09", "2027-03-10", "2027-03-11",
    "2027-04-23", "2027-05-17", "2027-05-18", "2027-05-19", "2027-07-15", "2027-08-30", "2027-10-29",
}
HALF_DAYS = {"2026-03-19", "2026-05-26", "2026-10-28", "2027-03-08", "2027-10-28"}

TICKS = ((0.0, 0.01), (20.0, 0.02), (50.0, 0.05), (100.0, 0.10),
         (250.0, 0.25), (500.0, 0.50), (1000.0, 1.00), (2500.0, 2.50))


def tick(price):
    t = TICKS[0][1]
    for lo, step in TICKS:
        if price >= lo:
            t = step
    return t


def on_tick(price):
    t = tick(price)
    return round(round(price / t) * t, 2)


def ts(day, hhmm):
    """'2026-11-09', '10:15' (TSİ) -> unix seconds."""
    d = datetime.datetime.strptime("%s %s" % (day, hhmm), "%Y-%m-%d %H:%M") - TR
    return int((d - datetime.datetime(1970, 1, 1)).total_seconds())


def trading_days(start, end):
    """ISO dates from start to end inclusive, weekends and BIST holidays out."""
    d = datetime.date.fromisoformat(start)
    e = datetime.date.fromisoformat(end)
    out = []
    while d <= e:
        if d.weekday() < 5 and d.isoformat() not in HOLIDAYS:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def slots(day, interval=5):
    close = EARLY if day in HALF_DAYS else CLOSE
    t = datetime.datetime.combine(datetime.date.fromisoformat(day), OPEN)
    end = datetime.datetime.combine(datetime.date.fromisoformat(day), close)
    out = []
    while t < end:
        out.append(t.strftime("%H:%M"))
        t += datetime.timedelta(minutes=interval)
    return out


def _doc(symbol, rows, prev_close, interval, rng_label):
    """rows: [(unix, o, h, l, c, v)]."""
    meta = {
        "currency": "TRY", "symbol": symbol + ".IS", "exchangeName": "IST", "fullExchangeName": "Istanbul",
        "instrumentType": "EQUITY", "gmtoffset": 10800, "timezone": "TRT",
        "exchangeTimezoneName": "Europe/Istanbul",
        "regularMarketPrice": rows[-1][4] if rows else None,
        "regularMarketVolume": sum(r[5] for r in rows[-(480 // interval):]) if rows else 0,
        "chartPreviousClose": prev_close, "previousClose": prev_close,
        "dataGranularity": "%dm" % interval, "range": rng_label,
    }
    return {"chart": {"result": [{
        "meta": meta,
        "timestamp": [r[0] for r in rows],
        "indicators": {"quote": [{
            "open": [r[1] for r in rows], "high": [r[2] for r in rows], "low": [r[3] for r in rows],
            "close": [r[4] for r in rows], "volume": [r[5] for r in rows]}]},
    }], "error": None}}


def chart(symbol, days, seed=19, start=300.0, vol=0.0025, interval=5, drift=0.0, volume=40000):
    """A random walk over `days` (ISO dates), every slot present. Prices on the tick grid."""
    rnd = random.Random(seed)
    rows, px, prev_close, first_prev = [], float(start), None, None
    for i, day in enumerate(days):
        if i == len(days) - 1:
            first_prev = prev_close
        for hhmm in slots(day, interval):
            o = on_tick(px)
            c = on_tick(max(1.0, px * math.exp(drift + rnd.gauss(0, vol))))
            h = on_tick(max(o, c) * (1 + abs(rnd.gauss(0, vol / 2))))
            lo = on_tick(min(o, c) * (1 - abs(rnd.gauss(0, vol / 2))))
            rows.append((ts(day, hhmm), o, max(h, o, c), min(lo, o, c), c, int(volume * (0.5 + rnd.random()))))
            px = c
        prev_close = px
    return _doc(symbol, rows, first_prev if first_prev is not None else on_tick(start), interval,
                "%dd" % len(days))


def daily(symbol, closes, end_day, volume=2000000):
    """Daily bars (interval 1d) ending on end_day, one per trading day before it."""
    days = trading_days("2025-01-01", end_day)[-len(closes):]
    rows = [(ts(d, "10:00"), c, c, c, c, volume) for d, c in zip(days, closes)]
    doc = _doc(symbol, rows, closes[-2] if len(closes) > 1 else closes[0], 1, "3mo")
    doc["chart"]["result"][0]["meta"]["dataGranularity"] = "1d"
    return doc


def orb_day(symbol, day, or_low, or_high, trigger_close, trigger_at="10:25", until="18:00",
            prev_close=None, or_minutes=15, after="flat"):
    """One session: an opening range from or_low to or_high over the first or_minutes, closes
    inside it until the bar that STARTS at trigger_at closes at trigger_close, then `after`
    ('flat' = sits at trigger_close, 'up' = climbs 0.5 % a bar, 'down' = falls 0.5 % a bar).
    Only bars that start before `until` are included (like a live range=1d answer)."""
    mid = on_tick((or_low + or_high) / 2)
    rows = []
    n_range = or_minutes // 5
    px = trigger_close
    for k, hhmm in enumerate(slots(day)):
        if hhmm >= until:
            break
        if k < n_range:
            o, c = mid, mid
            h, lo = (or_high, mid) if k == 0 else (mid, or_low) if k == 1 else (mid, mid)
        elif hhmm < trigger_at:
            o = c = mid
            h, lo = on_tick(mid + tick(mid)), on_tick(mid - tick(mid))
            h, lo = min(h, or_high), max(lo, or_low)
        elif hhmm == trigger_at:
            o, c = mid, trigger_close
            h, lo = trigger_close, mid
        else:
            o = px
            if after == "up":
                px = on_tick(px * 1.005)
            elif after == "down":
                px = on_tick(px * 0.995)
            c = px
            h, lo = max(o, c), min(o, c)
        rows.append((ts(day, hhmm), o, h, lo, c, 50000))
    return _doc(symbol, rows, prev_close if prev_close is not None else on_tick(or_low * 0.99), 5, "1d")


def spark(docs):
    """{SYM: v8 doc} -> one Yahoo spark answer."""
    res = []
    for sym, d in docs.items():
        r = d["chart"]["result"][0]
        res.append({"symbol": sym + ".IS", "response": [r]})
    return {"spark": {"result": res, "error": None}}
