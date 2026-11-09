# BIST adaptation spec (the single source of truth for every trader-* script)

Every script in this pack stays **self-contained, standard-library Python 3.8+**.
They each carry their own copy of the clock / calendar / money helpers; when you
adapt a script, replace its copy with the canonical code below so that all
eleven agree byte-for-byte on the market rules.

Keep internal function names that other code in the same file calls
(`eastern_offset`, `to_eastern`, `eastern_to_utc`, `ts_to_eastern`, ...) so the
diff stays small, but update their docstrings to say Istanbul. Rename only
user-facing text.

## 1. Clock

Turkey has been on fixed UTC+3 all year since September 2016 (no DST).

```python
TR_OFFSET = 3  # Europe/Istanbul, UTC+3 all year since 2016, no DST

def eastern_offset(utc):
    """Hours from UTC for Istanbul (TSİ): +3 all year, no daylight time."""
    return TR_OFFSET

def to_eastern(utc):
    """naive UTC -> naive Istanbul time (name kept for the diff)."""
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=TR_OFFSET)

def eastern_to_utc(local):
    return local - datetime.timedelta(hours=TR_OFFSET)
```

`ts_to_eastern(ts)` keeps its body (it calls `to_eastern`).
A `--now` without an offset is Istanbul time. Every label `ET` becomes `TSİ`,
every "New York time" becomes "Istanbul time (TSİ)". `_nth_sunday` and the US DST
rule are deleted.

## 2. Session (Borsa İstanbul Pay Piyasası, Yıldız/Ana Pazar, continuous trading)

| | time (TSİ) |
|---|---|
| opening auction (açılış seansı) | 09:40–09:55 (no continuous bars) |
| continuous trading (sürekli işlem) | **10:00–18:00** |
| closing auction (kapanış seansı) | 18:00–18:10 (a single print; ignore as a bar, like NYSE's 16:00 print) |
| half day (yarım gün), continuous ends | **12:30** |

```python
OPEN = datetime.time(10, 0)
CLOSE = datetime.time(18, 0)
EARLY = datetime.time(12, 30)   # half days (arefe / 28 Ekim): continuous trading ends 12:30
SESSION_BARS = 96               # 10:00 to 18:00 in 5-minute bars (was 78)
```

Default rule times (config defaults, examples, help text):

| key | US value | BIST value |
|---|---|---|
| `no_entries_before` | 09:45 | **10:15** |
| `no_entries_after` | 15:30 | **17:30** |
| `flat_by` | 15:55 | **17:50** |
| ORB default range | 9:30–9:45 | **10:00–10:15** (still `--or-minutes 15`) |
| example `--at` | 10:15 | **10:30** |

On a half day every "minutes before the close" offset shifts the same way the US
code did for its 13:00 early close (CLOSE - EARLY = 5h30m).
A bare hour 1–8 in a time parser no longer means p.m. on BIST (trading is
10–18): make bare 1–8 mean 13:00–18:00 only where the old code had that rule,
i.e. "5:50" -> 17:50.

## 3. Calendar

```python
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
```

Source: Borsa İstanbul 2026 resmi tatil listesi (as reported in the Turkish press,
read 2026-11-09). The 2027 bayram dates are estimates until Borsa İstanbul
publishes its 2027 calendar (usually in December); leave the "(tahmini)" marks.
Rename `NYSE_HOLIDAYS` -> `BIST_HOLIDAYS`, `NYSE_EARLY_CLOSE` -> `BIST_HALF_DAYS`
everywhere in the file. Reason strings: "BIST tatili: <name>", "yarım gün: <name>".

## 4. Symbols

Users type BIST codes: `THYAO`, `ASELS`, `GARAN`. Yahoo needs a `.IS` suffix.
Indices: `XU100`, `XU030` -> `XU100.IS`.

```python
def to_yahoo(sym):
    """THYAO -> THYAO.IS. A symbol that already has a suffix or starts with ^ is left alone."""
    s = sym.strip().upper()
    if s.startswith("^") or "." in s:
        return s
    return s + ".IS"

def from_yahoo(ysym):
    s = (ysym or "").strip().upper()
    return s[:-3] if s.endswith(".IS") else s
```

Ticker regex for BIST codes: `^[A-Z][A-Z0-9]{2,5}$` (THYAO, A1CAP, KOZAL, XU100).
State files (`trader/*.json`) store the bare code (`THYAO`), never `THYAO.IS`.
Example ticker everywhere in docs/help: `THYAO` (and `ASELS`, `GARAN` for lists).

## 5. Money

Currency is Turkish lira. Use one helper:

```python
def money(x):
    """1234.5 -> '1.234,50 TL' (Turkish grouping)."""
    s = "{:,.2f}".format(float(x))
    return s.replace(",", "_").replace(".", ",").replace("_", ".") + " TL"
```

Prices of a share print without "TL" via the file's existing price formatter,
but rounded to the tick (section 6). JSON stays plain floats.

## 6. Price ticks (fiyat adımı) and daily limits (taban / tavan)

Pay Piyasası tick table (Table 1, in force since 2023-11-06; verify against
Pay Piyasası Prosedürü §5.1.9 before trading real money):

```python
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
```

Daily price limits: ±10 % of the base price (the previous session's closing
price; Yahoo meta `chartPreviousClose` / `previousClose`), rounded to the tick:
tavan = round_tick(base * 1.10, "down"), taban = round_tick(base * 0.90, "up").
A stop under the taban or a target over the tavan can never fill today: the
exit planner warns, the risk check BLOCKs.

Rounding rule for a long trade: stop -> `round_tick(stop, "down")`,
target -> `round_tick(target, "up")`, limit entry -> nearest.

## 7. Costs

No US PDT rule exists on BIST: `pdt_guard` defaults to false and docs say it
does not apply. Short selling stays off.

Round-trip cost per share is part of risk:

```python
DEFAULT_COSTS = {
    "commission_rate": 0.0015,   # broker commission per side, fraction of notional (binde 1,5); set yours
    "bsmv_rate": 0.05,           # BSMV on the commission
    "exchange_fee_rate": 0.00003,  # borsa payı per side (approx.)
}

def side_cost(notional, c):
    com = notional * c["commission_rate"]
    return com * (1 + c["bsmv_rate"]) + notional * c["exchange_fee_rate"]
```

Config keys with the same names live in `trader/config.json`; missing keys use
the defaults. Stopaj on day-trading gains of BIST shares is currently 0 % for
individuals: the docs say "no tax advice".

## 8. Broker

There is no Alpaca on BIST and most Turkish brokers have no public order API.
`trader-broker` therefore never sends anything: on yes it prints a manual
**emir fişi** (order slip) the user types into their own broker app
(symbol, side, lots, order type, limit price, stop, target, validity "Gün"),
and records the decision in `trader/state/broker.json` as before.
1 lot = 1 share on BIST.

## 9. Data sources

* Bars and quotes: Yahoo Finance v8 chart / v7 spark with `.IS` symbols.
  The spark endpoint is used as before.
* Universe: `trader-scanner/bist_universe.txt` (one `CODE|Name` a line),
  shipped with the pack and user-editable. `scan.py universe` copies it to
  `trader/universe.txt`.
* News: Google News RSS in Turkish
  `https://news.google.com/rss/search?q=<CODE>+hisse&hl=tr&gl=TR&ceid=TR:tr`
  plus Yahoo's headline RSS for `<CODE>.IS`; KAP (kap.org.tr) is linked for
  official disclosures. No SEC.
* Financials: İş Yatırım's public MaliTablo JSON endpoint (unofficial), see
  trader-analyst.
* Congress trades (trader-pelosi): removed, US only.

## 10. Language

Code comments and CLI output may stay English. Every SKILL.md tells Claude to
talk to the user in Turkish and uses BIST examples. Never call anything
investment advice ("yatırım tavsiyesi değildir").
