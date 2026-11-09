#!/usr/bin/env python3
"""Time Machine for trader-timemachine. Standard library only.

Borsa İstanbul (BIST) edition. Replays a planned day trade before you risk a
lira: 10,000 simulated runs of the rest of the session from your entry, plus
the same rule on every past session in the data.

    python3 timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader/timemachine.json
    python3 timemachine.py run --until 17:50            # stop each replay at your flat-by time
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol THYAO --date 2026-11-09 --entry 285.50 \\
        --stop 282.00 --target 289.00 --entry-time 10:30
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py chart --from trader/timemachine.json

How the replays work (the reel's sim.py, same algorithm, same random draws):
  1. Fetch 60 days of 5-minute bars (Yahoo v8 chart, range=60d interval=5m,
     symbol <CODE>.IS; one request). Keep only FULL sessions before the plan's
     date: 96 bars, 10:00 to 18:00 Istanbul time (TSİ). Half days (yarım gün,
     continuous trading ends 12:30), gaps and today are left out. The opening
     auction (09:40-09:55) and the closing auction (18:00-18:10) are not bars.
  2. The pool: for every bar slot 1..95 of the day, each past session's bar as
     ratios to the previous bar's close: (open, high, low, close) / prev close.
  3. One replay starts at the entry price and walks the rest of the session.
     For each bar i it picks a slot j = i + randint(-window, window), kept in
     1..95, then one past bar from pool[j] (random.choice), and scales it to
     the current price. The stop is checked before the target in every bar,
     so a bar that touches both counts as the stop (the conservative call).
     When the plan carries "limits" ({"base", "taban", "tavan"}, written by
     trader-exit) every simulated price is clamped to taban..tavan for that
     day: a replay cannot trade beyond the daily price limits.
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at --until (default 18:00, the close, or
     12:30 on a BIST half day).
     "green" = target first, or above the entry at --until (below for a sell).
     R = the target's R at the target, -1 at the stop, otherwise
     (price at --until - entry) / risk. A gap through the stop still counts
     -1R here; a real fill can be worse. When the plan has "cost_per_share"
     (round-trip commission + BSMV + borsa payı) the report also gives the
     average R after costs: avg_R - cost_per_share / risk.
  5. The price at --until: median and the 5th to 95th percentile of all
     replays (pct(a, q) = sorted a at index int(q * n)). The fan chart uses a
     second run of 2,000 paths with the same seed. The histogram has 24 bins
     between the 1st and 99th percentile.
  6. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R) re-run on every one of those past
     full sessions, first close beyond the range = entry, same stop-first rule.
     Stops and targets sit on the BIST tick, away from the entry.

The verdict line looks at the replays that hit one side (target or stop) and
compares the target's share of them, target / (target + stop), with the share
that breaks even at the plan's target R: 1 / (1 + R), so 50 % at 1R and 33 % at
2R. The gap, in percentage points:
  less than 5 points    "Close to a coin flip" (1R) / "Close to break-even" (other R)
  5 to 15 points        "Leans toward the target" / "Leans toward the stop"
  15 points or more     "Strongly toward the target" / "Strongly toward the stop"
Below it: a note when more than 40 % of the replays hit neither (the price at
--until decides those), and the average R when its sign goes against the lean.

5-minute bars only: Yahoo keeps 60 days of 5-min bars but only about 7 days of
1-min bars, too few sessions for a fair pool. The run refuses with a plain
message when there are fewer than 20 full sessions or the entry bar is not in
the data. Same plan, same data and same --seed give the same numbers on any
machine. A simulation built from the stock's own recent bars is not a
prediction: earnings, news and halts can do what the last 60 days never did.

Data: https://query2.finance.yahoo.com/v8/finance/chart/<CODE>.IS?range=60d&interval=5m
(unofficial and free, can lag ~15 minutes; falls back to query1 on 429 or 5xx).
--offline FILE reads a saved chart JSON (.json or .json.gz) instead.
Times are Istanbul time (TSİ, UTC+3 all year); --now "2026-11-09 10:30" pins
the clock.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
PAUSE = 0.5          # seconds between requests: be polite to free endpoints
BACKOFF = 5.0        # seconds to wait once after a 429
HOSTS = ("query2", "query1")
INTERVAL = 300       # 5-minute bars, in seconds
OPEN = datetime.time(10, 0)
CLOSE = datetime.time(18, 0)
EARLY = datetime.time(12, 30)   # half days (arefe / 28 Ekim): continuous trading ends 12:30
SESSION_BARS = 96               # 10:00 to 18:00 in 5-minute bars (was 78)
OPEN_MIN = OPEN.hour * 60 + OPEN.minute
CLOSE_MIN = CLOSE.hour * 60 + CLOSE.minute
EARLY_MIN = EARLY.hour * 60 + EARLY.minute
assert SESSION_BARS * 5 == CLOSE_MIN - OPEN_MIN
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05     # verdict thresholds, see the docstring
STRONG = 0.15
MANY_NEITHER = 0.40
EPOCH = datetime.datetime(1970, 1, 1)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
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


class TMError(Exception):
    pass


# ---------------------------------------------------------------- symbols and money

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
        raise TMError("--now wants a time like \"2026-11-09 10:30\" (Istanbul, TSİ) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_clock(text, what):
    """'10:30', '17:50', '10:30 AM' or '5:50' (a bare 1 to 6 o'clock means 13:00 to 18:00,
    since BIST trades 10:00 to 18:00) -> minutes after midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not m:
        raise TMError("%s wants an Istanbul (TSİ) time like 10:30 or 17:50, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise TMError("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 6:
        h += 12
    return h * 60 + mi


def clock12(minutes):
    """Minutes after midnight -> '10:30' (24-hour, as used in Turkey; name kept for the diff)."""
    return "%02d:%02d" % divmod(minutes, 60)


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


# ---------------------------------------------------------------- data

def chart_url(sym, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m" % (
        host, urllib.parse.quote(to_yahoo(sym)))


def _sleep(seconds):
    time.sleep(seconds)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_chart(sym):
    """One request for 60 days of 5-min bars. query2 first, one retry after a 429,
    then query1 on 429 or 5xx."""
    last = None
    for n, host in enumerate(HOSTS):
        if n:
            _sleep(PAUSE)
        for attempt in (0, 1):
            url = chart_url(sym, host)
            try:
                return _get(url)
            except urllib.error.HTTPError as e:
                last = e.code
                try:
                    e.close()
                except Exception:
                    pass
                if e.code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if e.code == 429 or e.code >= 500:
                    break
                if e.code == 404:
                    raise TMError("Yahoo has no chart for %s (HTTP 404): check the ticker" % sym.upper())
                raise TMError("Yahoo answered HTTP %d for %s" % (e.code, sym.upper()))
            except urllib.error.URLError as e:
                raise TMError("could not reach Yahoo: %s" % e.reason)
            except OSError as e:
                raise TMError("could not reach Yahoo: %s" % e)
    if last == 429:
        raise TMError("Yahoo is rate limiting (HTTP 429). Wait a minute and run it again.")
    raise TMError("Yahoo answered HTTP %s for %s on both hosts. Try again later." % (last, sym.upper()))


def read_offline(path, sym):
    """A saved Yahoo chart file (.json or .json.gz), or a folder holding <SYM>-60d5m.json(.gz)."""
    if os.path.isdir(path):
        for name in ("%s-60d5m.json" % sym.upper(), "%s-60d5m.json.gz" % sym.upper()):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no %s-60d5m.json(.gz) in %s" % (sym.upper(), path))
    if not os.path.exists(path):
        raise TMError("no offline chart file %s" % path)
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        raise TMError("could not read %s: %s" % (path, e))


def chart_result(text, sym):
    try:
        data = json.loads(text)
    except ValueError:
        raise TMError("Yahoo returned something that is not JSON for %s" % sym.upper())
    chart = data.get("chart") if isinstance(data, dict) else None
    if not isinstance(chart, dict):
        raise TMError("Yahoo returned something unexpected for %s" % sym.upper())
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise TMError("Yahoo has no data for %s: %s" % (sym.upper(), err.get("description") or "unknown ticker"))
    return res[0]


def parse_bars(result, now_utc):
    """Yahoo chart result -> (meta, bars, dropped). Regular-session 5-min bars only,
    oldest first. Dropped: bars with a missing price, bars off the 5-minute grid
    (the live tick, e.g. 10:49:45), bars not complete yet (now < start + 5 min,
    where now is the earlier of --now and the data's own regularMarketTime), and
    bars outside 10:00 to 18:00 Istanbul time (the opening auction 09:40-09:55
    and the closing-auction print at 18:00 are not continuous-trading bars)."""
    meta = result.get("meta") or {}
    stamps = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    as_of = now_utc
    rmt = meta.get("regularMarketTime")
    if isinstance(rmt, (int, float)) and utc_of(rmt) < as_of:
        as_of = utc_of(rmt)
    dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0}
    bars = {}
    for i, ts in enumerate(stamps):
        o, h, l, c, v = [col[i] if i < len(col) else None for col in cols]
        if ts is None or None in (o, h, l, c):
            dropped["no_price"] += 1
            continue
        ts = int(ts)
        if ts % INTERVAL:
            dropped["off_grid"] += 1
            continue
        start = utc_of(ts)
        local = to_eastern(start)
        mins = local.hour * 60 + local.minute - OPEN_MIN
        if mins < 0 or mins >= SESSION_BARS * 5:
            dropped["outside_hours"] += 1
            continue
        if start + datetime.timedelta(seconds=INTERVAL) > as_of:
            dropped["not_complete"] += 1
            continue
        bars[ts] = {"date": local.date().isoformat(), "slot": mins // 5, "start": local.strftime("%H:%M"),
                    "o": o, "h": h, "l": l, "c": c, "v": v or 0}
    return meta, [bars[t] for t in sorted(bars)], dropped


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def full_sessions(days, before):
    """Full sessions (all SESSION_BARS = 96 bars) strictly before the plan's date,
    oldest first, and the short ones that were left out (BIST half days, gaps)."""
    full, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        if len(s) == SESSION_BARS and d not in BIST_HALF_DAYS:
            full.append((d, [s[i] for i in range(SESSION_BARS)]))
        else:
            short.append({"date": d, "bars": len(s)})
    return full, short


# ---------------------------------------------------------------- the simulation (sim.py)

def build_pool(sessions):
    """pool[i] = every past session's bar i as (o, h, l, c) / bar i-1's close, in date order."""
    pool = [[] for _ in range(SESSION_BARS)]
    for _, D in sessions:
        for i in range(1, SESSION_BARS):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy", limits=None):
    """Monte Carlo replays of bars k+1..last. Same draws and order as the reel's sim.py.
    limits = (taban, tavan) clamps every simulated price to the day's BIST price
    limits; it never changes the random draws, so the seed gives the same paths."""
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)
    if limits:
        floor_, cap_ = limits

        def clamp(x):
            return min(cap_, max(floor_, x))
    else:
        def clamp(x):
            return x

    def slot(rng, i):
        return i if window == 0 else min(SESSION_BARS - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = clamp(p * h), clamp(p * l), clamp(p * c)
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"        # both inside one bar: count the loss (conservative)
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = clamp(p * c)
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "r_at_target": r_target, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
        "limits": ({"taban": limits[0], "tavan": limits[1]} if limits else None),
    }


# ---------------------------------------------------------------- stop rules and the history replay

TICKS = (  # (price from, tick), BIST Pay Piyasası fiyat adımı
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
    q = round(price / t, 3)  # float32 noise from Yahoo (48.31999969 is 48.32), legit off-tick >= 0.004 ticks
    q = math.floor(q + 1e-9) if mode == "down" else math.ceil(q - 1e-9) if mode == "up" else round(q)
    return round(q * t, 2)


def _snap(x):
    """Yahoo sends float32 prices: 48.31999969482422 is 48.32. Snap such float noise to the
    cent before a floor / ceil to the tick, or a price already on the tick moves a tick."""
    c = round(x, 2)
    return c if abs(x - c) < 1e-3 else x


def cents_away(x, long):
    """Round a stop to the BIST tick, away from the entry: down for a buy, up for a sell
    (name kept for the diff; it rounded to the cent on US stocks)."""
    return round_tick(_snap(x), "down" if long else "up")


def opening_range(D, n):
    """(high, low) of the first n bars of a session given as {slot: bar} or a list."""
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def rule_stop(rule, oh, ol, entry, long):
    r = rule["stop_rule"]
    if r == "or_mid":
        return cents_away((oh + ol) / 2, long)
    if r in ("or_low", "or_high"):
        return cents_away(ol if long else oh, long)
    frac = rule["stop_pct"] / 100.0
    return cents_away(entry * (1 - frac) if long else entry * (1 + frac), long)


def history_replay(sessions, rule, last, side="buy"):
    """The plan's rule on every past full session: entry = the first bar after the
    opening range to close beyond it (up to the --until bar), then stop first, then
    target, bar by bar, else the close at --until."""
    long = side == "buy"
    n = rule["or_minutes"] // 5
    trades = []
    for d, D in sessions:
        oh, ol = opening_range(D, n)
        kk = next((i for i in range(n, last + 1) if (D[i]["c"] > oh if long else D[i]["c"] < ol)), None)
        if kk is None:
            continue
        e = round(D[kk]["c"], 2)
        s = rule_stop(rule, oh, ol, e, long)
        risk = (e - s) if long else (s - e)
        if risk <= 0:
            continue
        # the target on the tick away from the entry, as trader-exit sets it: up for a buy, down for a sell
        t = round_tick(_snap(e + rule["target_r"] * risk if long else e - rule["target_r"] * risk),
                       "up" if long else "down")
        oc = None
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= s) if long else (b["h"] >= s):
                oc = "stop"
                break
            if (b["h"] >= t) if long else (b["l"] <= t):
                oc = "target"
                break
        cl = D[last]["c"]
        R = ((t - e) if long else (e - t)) / risk if oc == "target" else -1.0 if oc == "stop" else \
            ((cl - e) if long else (e - cl)) / risk
        trades.append({"date": d, "time": clock12(OPEN_MIN + 5 * (kk + 1)), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2)})
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def load_plan(a, now_utc):
    """The plan from --plan (trader/plan.json) or from the flags, checked and normalized."""
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": a.side, "date": a.date or to_eastern(now_utc).date().isoformat(),
               "entry": a.entry, "stop": a.stop, "target": a.target, "entry_time": a.entry_time,
               "setup": a.setup, "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "interval": "5m"}
        src = "flags"
    else:
        if not os.path.exists(a.plan):
            raise TMError("no plan at %s: run /trader-exit first, or pass --symbol --date --entry --stop "
                          "--target --entry-time" % a.plan)
        try:
            with open(a.plan, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (a.plan, e))
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % a.plan)
        src = a.plan
    return normalize_plan(raw, src)


def normalize_plan(raw, src="flags"):
    """A plan dict (trader/plan.json shape) -> the checked plan the time machine runs."""
    sym = from_yahoo(str(raw.get("symbol") or ""))   # state keeps the bare BIST code: THYAO
    if not re.match(r"^[A-Z][A-Z0-9]{2,5}$", sym):
        raise TMError("the plan needs a BIST code like THYAO, got %r" % raw.get("symbol"))
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise TMError("side must be buy or sell, got %r" % raw.get("side"))
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise TMError("5-minute bars only (the plan says %s): Yahoo keeps 60 days of 5-min bars but only about "
                      "7 days of 1-min bars, too few sessions for a fair replay pool" % interval)
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise TMError("the plan needs a date like 2026-10-19, got %r" % raw.get("date"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    if not long and not target < entry < stop:
        raise TMError("a sell needs target < entry < stop (got target %s, entry %s, stop %s)" % (target, entry, stop))
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % 5:
            raise TMError("entry_time %s is not on the 5-minute grid (use the bar's close, e.g. 10:30)"
                          % raw["entry_time"])
        k = (close_min - OPEN_MIN) // 5 - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number, got %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise TMError("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                          % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 10:30) or entry_bar")
    if not 0 <= k <= SESSION_BARS - 2:
        raise TMError("the entry must close between %s and %s TSİ to leave bars to replay "
                      "(got %s)" % (clock12(OPEN_MIN + 5), clock12(CLOSE_MIN - 5), clock12(OPEN_MIN + 5 * (k + 1))))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes must be a number")
    if or_min < 5 or or_min % 5 or or_min >= CLOSE_MIN - OPEN_MIN:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    risk = (entry - stop) if long else (stop - entry)
    limits = None   # optional {"base", "taban", "tavan"} from trader-exit
    lim = raw.get("limits")
    if isinstance(lim, dict) and lim.get("taban") is not None and lim.get("tavan") is not None:
        try:
            taban, tavan = float(lim["taban"]), float(lim["tavan"])
        except (TypeError, ValueError):
            raise TMError("the plan's limits need numbers for taban and tavan, got %r" % lim)
        if not 0 < taban < tavan:
            raise TMError("the plan's limits need 0 < taban < tavan, got %r" % lim)
        limits = dict(lim, taban=taban, tavan=tavan)
    cost = raw.get("cost_per_share")
    if cost is not None:
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            raise TMError("cost_per_share must be a number, got %r" % cost)
        if cost < 0 or math.isinf(cost) or cost != cost:
            raise TMError("cost_per_share must be zero or more, got %r" % raw.get("cost_per_share"))
    return {
        "symbol": sym, "side": side, "date": date, "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": clock12(OPEN_MIN + 5 * (k + 1)), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "5m",
        "flat_by": raw.get("flat_by"), "from": src,
        "limits": limits, "cost_per_share": cost,
    }


def resolve_rule(plan, entry_day):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule
    (or_mid, or_low / or_high, or 'pct' = the plan's stop distance as a percent of
    the entry), and the plan's target R. 'auto' works the stop rule out from the
    plan day's own opening range. None when the setup is not orb."""
    if plan["setup"].lower() != "orb":
        return None
    long = plan["side"] == "buy"
    n = plan["opening_range"]["minutes"] // 5
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if abs(cents_away((oh + ol) / 2, long) - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or_mid"
            elif abs(cents_away(ol if long else oh, long) - plan["stop"]) < 0.005:
                rule["stop_rule"] = far
    elif rule["stop_rule"] in ("or_low", "or_high") and rule["stop_rule"] != far:
        rule["stop_rule"] = "pct"
    elif rule["stop_rule"] not in ("or_mid", far):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- the run

def _until12(mc):
    t = mc.get("until") or clock24(CLOSE_MIN)
    return clock12(int(t[:2]) * 60 + int(t[3:]))


def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one side,
    minus the share that breaks even at the target's R. None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    """One line from the numbers, see the docstring for the thresholds."""
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (
            _until12(mc), _until12(mc))
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    pn = mc.get("p_neither", 0)
    if pn > MANY_NEITHER and lean(mc) is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * pn, _until12(mc), _until12(mc)))
    lv = lean(mc)
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    return notes


def time_machine(text, plan, now_utc, replays=10000, seed=19, window=2, until=None, source="yahoo"):
    """Chart JSON text + a normalized plan -> the timemachine.json document."""
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    sym = plan["symbol"]
    result = chart_result(text, sym)
    meta, bars, dropped = parse_bars(result, now_utc)
    msym = from_yahoo(str(meta.get("symbol") or ""))   # THYAO.IS -> THYAO
    if msym and msym != sym:
        raise TMError("the data is for %s but the plan is for %s" % (msym, sym))
    gran = meta.get("dataGranularity")
    if gran and gran != "5m":
        raise TMError("the data has %s bars; the time machine needs 5-minute bars" % gran)
    if not bars:
        raise TMError("no complete 5-minute bars for %s in the data" % sym)
    k = plan["entry_bar"]
    early = plan["date"] in BIST_HALF_DAYS
    end_min = EARLY_MIN if early else CLOSE_MIN
    until_min = end_min if until is None else parse_clock(until, "--until")
    if (until_min - OPEN_MIN) % 5 or until_min > end_min:
        raise TMError("--until must be on the 5-minute grid and no later than %s%s"
                      % (clock12(end_min), (" (yarım gün: %s)" % BIST_HALF_DAYS[plan["date"]]) if early else ""))
    last = (until_min - OPEN_MIN) // 5 - 1
    if last <= k:
        raise TMError("--until %s leaves nothing to replay after the %s entry" % (clock12(until_min),
                                                                             plan["entry_time"]))
    days = split_sessions(bars)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        reason = ("BIST tatili: " + BIST_HOLIDAYS[plan["date"]] if plan["date"] in BIST_HOLIDAYS else
                  "no bars for that day" if not entry_day else
                  "the data stops at the %s bar" % entry_day[max(entry_day)]["start"])
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (clock24(OPEN_MIN + 5 * k), plan["entry_time"], plan["date"], sym, reason))
    eb = entry_day[k]
    warn = []
    if not eb["l"] - 0.01 <= plan["entry"] <= eb["h"] + 0.01:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f"
                    % (plan["entry"], eb["l"], eb["h"]))
    lim = plan.get("limits")
    limits = (lim["taban"], lim["tavan"]) if lim else None
    if limits:
        long = plan["side"] == "buy"
        if not limits[0] <= plan["entry"] <= limits[1]:
            warn.append("the plan's entry %.2f is outside today's limits (taban %.2f, tavan %.2f)"
                        % (plan["entry"], limits[0], limits[1]))
        far_t = plan["target"] > limits[1] if long else plan["target"] < limits[0]
        far_s = plan["stop"] < limits[0] if long else plan["stop"] > limits[1]
        if far_t:
            warn.append("the target %.2f is beyond the %s %.2f: it cannot fill today"
                        % (plan["target"], "tavan" if long else "taban", limits[1] if long else limits[0]))
        if far_s:
            warn.append("the stop %.2f is beyond the %s %.2f: it cannot fill today"
                        % (plan["stop"], "taban" if long else "tavan", limits[0] if long else limits[1]))
    sessions, short = full_sessions(days, plan["date"])
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d full session(s) of %s before %s in the data; the time machine needs at least %d"
                      % (len(sessions), sym, plan["date"], MIN_SESSIONS))
    pool = build_pool(sessions)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window, plan["side"],
                  limits)
    mc = dict(mc, until=clock24(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0],
              band_times=[clock24(OPEN_MIN + 5 * (j + 1)) for j in range(k, last + 1)])
    cost = plan.get("cost_per_share")
    if cost is not None:
        risk = plan["risk_per_share"] or 1e-9
        mc["cost_per_share"] = cost
        mc["cost_R"] = round(cost / risk, 3)
        mc["avg_R_after_costs"] = round(mc["avg_R"] - cost / risk, 3)
        mc["expectancy_per_share_after_costs"] = round(mc["avg_R"] * plan["risk_per_share"] - cost, 4)
    rule = resolve_rule(plan, entry_day)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, rule, last, plan["side"])
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    last_bar = bars[-1]
    return {
        "created_by": "trader-timemachine",
        "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "data": {"symbol": msym or sym, "interval": "5m", "range": "60d",
                 "last_bar": "%s %s TSİ" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "mc": mc,
        "history": history,
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d full sessions. It is not a prediction: earnings, "
                 "KAP disclosures, news, halts and limit days can do things those sessions never did. "
                 "Same seed, same numbers." % (sym, len(sessions))),
    }


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    long = p["side"] == "buy"
    until = clock12(int(m["until"][:2]) * 60 + int(m["until"][3:]))
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    r = p["target_r"]
    lines = [
        "%s %s, %s %s: entry %.2f at %s (bar %d), stop %.2f, target %.2f (%sR, risk %.2f a share)."
        % (p["symbol"], p["side"], wd, p["date"], p["entry"], p["entry_time"], p["entry_bar"], p["stop"],
           p["target"], ("%g" % r), p["risk_per_share"]),
        "%s replays of the rest of the session, %s to %s (%d five-minute bars), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, m["bars_left"], m["seed"]),
        "Each bar is drawn whole from %s's own 5-min bars at the same time of day (+-%d bars) over its last "
        "%d full sessions (%s to %s)." % (p["symbol"], m["window"], m["pool_sessions"], m["pool_from"], m["pool_to"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s" % (pc(m["p_neither"]), until),
        "  green          %-8s target first, or %s the entry at %s" % (pc(m["p_green"]),
                                                                       "above" if long else "below", until),
        "",
        "  Price at %s: median %.2f, 90 %% of replays between %.2f and %.2f."
        % (until, m["median_close"], m["p5_close"], m["p95_close"]),
        "  Average R: %+.3f (R = the %.2f risk: %sR at the target, -1R at the stop)."
        % (m["avg_R"], p["risk_per_share"], ("+%g" % m["r_at_target"]), ),
    ]
    if m.get("avg_R_after_costs") is not None:
        lines.append("  After costs: average R %+.3f (round-trip costs %s a share = %.3fR; %s a share expected)."
                     % (m["avg_R_after_costs"], money(m["cost_per_share"]), m["cost_R"],
                        money(m["expectancy_per_share_after_costs"])))
    if m.get("limits"):
        lines.append("  Prices clamped to today's limits: taban %.2f, tavan %.2f." % (m["limits"]["taban"],
                                                                                   m["limits"]["tavan"]))
    lines.append("")
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low",
                      "or_high": "opening-range high"}.get(ru["stop_rule"],
                                                           "%.2f %% from the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target) on each of "
                         "those %d real sessions fired %d times: %d target, %d stop, %d neither. Average R %+.3f."
                         % (ru["or_minutes"], stop_words, ru["target_r"], h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"]))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d short session(s) (half days or gaps): %s."
                     % (len(doc["data"]["short_sessions_left_out"]),
                        ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"])))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-min bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        lab = times[j]   # 24-hour TSİ, e.g. 10:30
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")


def compute(a):
    now = parse_now(a.now)
    plan = load_plan(a, now)
    if a.offline:
        text, source = read_offline(a.offline, plan["symbol"]), "offline"
    else:
        text, source = fetch_chart(plan["symbol"]), "yahoo"
    return time_machine(text, plan, now, a.replays, a.seed, a.window, a.until, source)


def cmd_run(a):
    doc = compute(a)
    if a.write:
        _write(a.write, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: /trader-budget sizes it to your budget and your max loss per trade.")
    return 0


def cmd_chart(a):
    if a.from_file:
        if not os.path.exists(a.from_file):
            raise TMError("no %s: run timemachine.py run --write first" % a.from_file)
        try:
            with open(a.from_file, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Turkish text (TSİ) on a non-UTF-8 Windows pipe
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned BIST day trade (trader-timemachine).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="trader/plan.json", help="the plan from trader-exit")
        p.add_argument("--symbol", help="or give the plan as flags: ticker")
        p.add_argument("--side", default="buy", choices=["buy", "sell"])
        p.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, Istanbul)")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help='the entry bar\'s close, TSİ, e.g. "10:30"')
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "or_high", "pct"])
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        p.add_argument("--until", help="end each replay at this TSİ time (default 18:00, the close; "
                                       "12:30 on a half day), e.g. 17:50")
        p.add_argument("--offline", metavar="FILE", help="a saved Yahoo chart JSON (.json or .json.gz), no network")
        p.add_argument("--now", help='pretend it is this time ("2026-11-09 10:30" Istanbul TSİ, or ISO with Z)')

    p = sub.add_parser("run", help="replay the plan: 10,000 simulated sessions plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="save the result (default trader/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="draw a saved result instead of running again")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except TMError as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
