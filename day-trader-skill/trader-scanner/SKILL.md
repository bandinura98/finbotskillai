---
name: trader-scanner
description: >-
  Grafik Tarayıcı (Chart Scanner) for Borsa İstanbul (BIST). Scans the BIST
  share list for setups: opening-range breakouts, flags, oversold bounces.
  Builds the universe from the BIST 100 list shipped with the skill
  (bist_universe.txt), runs a pre-market scan of daily closes (20-day
  closing-high breakout, bull flag, reclaimed the 20-day average, oversold,
  oversold bounce) and an intraday opening-range breakout scan on 5-minute
  bars from 10:00 TSİ, then writes trader/watchlist.json for trader-news and
  trader-entry. Free Yahoo .IS data, bar by bar, often about 15 minutes late.
  Use when the user says "piyasayı tara", "bana kurulum bul", "ne kırılıyor",
  "açılış aralığı kırılımı", "bayrak formasyonu tara", "aşırı satılmış
  hisseler", "seans öncesi tarama", "izleme listemi oluştur", "bugün neye
  bakmalıyım", "scan BIST", "find me setups", or at the start of a BIST
  trading day before /trader-news.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade on Borsa İstanbul (Pay
Piyasası, continuous trading 10:00 to 18:00 Istanbul time, TSİ). It reads
free charts for every code in the BIST share list and lists the ones that
match a rule. That is all it does. A scan is a list of charts that match a
rule, not a list of stocks to buy, and a setup on a chart is not a
prediction. Most people who day trade lose money; the scanner does not change
that. It only saves you from flipping through a hundred charts by hand.

**Talk to the user in Turkish.** Script output stays English; explain it in
Turkish (for example "eşleşti" / "kurala uydu", never "al sinyali"). Every
time is Istanbul time, written as TSİ (UTC+3 all year, no daylight time).
Say "yatırım tavsiyesi değildir" when you present hits.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                          # bist_universe.txt -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                             # before the open -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols THYAO,ASELS,GARAN # or --watchlist FILE, --limit 50
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                               # after 10:20 TSİ -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:30                    # cut the clock at 10:30 TSİ
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
```

Flags: `--min-price 1` (TL) and `--min-volume` (daily 1,000,000 shares, orb
0), `--or-minutes 15`, `--max-confirm 200` (orb stage 2),
`--now "2026-10-19 10:30"` (Istanbul time, TSİ) to pin the clock,
`--offline PATH` to read saved Yahoo files, `--no-write`, `--show N`,
`--json`. `universe` also takes `--from FILE` (another `CODE|Name` list).
Exit code 0 when the scan found something, 1 when it found nothing or could
not run (the reason is one plain line).

Users type bare BIST codes (`THYAO`); the scanner adds `.IS` for Yahoo
(`THYAO.IS`) and stores the bare code in every `trader/*.json` file.

## What it reads and writes

| file | what |
| --- | --- |
| `${CLAUDE_SKILL_DIR}/bist_universe.txt` | shipped with the skill, user-editable: one `CODE\|Company name` a line, `#` comments |
| `trader/universe.txt` | written by `universe`: one `CODE\|Name` a line, read by `daily` and `orb` |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `hits[]`. trader-news and trader-entry read `hits[].symbol` (bare code, `THYAO`) |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 17:30) |

## The universe

`universe` reads `bist_universe.txt` from this skill's folder (or the file
given with `--from FILE`), keeps every line whose code is a BIST ticker
(`^[A-Z][A-Z0-9]{2,5}$`: THYAO, A1CAP, KOZAL; a `.IS` suffix is dropped),
drops repeats, and writes `trader/universe.txt` in the same `CODE|Name`
format. Nothing is downloaded.

The shipped list is the BIST 100 (plus a few liquid Yıldız Pazar shares) as
best known when it was written. Borsa İstanbul rebalances the index every
quarter and companies sometimes change their codes, so the list can be out of
date: check it against Borsa İstanbul's official BIST 100 constituent list
and edit the file freely (add the codes you trade, remove the ones you do
not). A code Yahoo does not know does no harm: it simply fails at scan time
with "no data from Yahoo" and is counted under failed.

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request, so about 100
codes take 5 or 6 requests (a few seconds with the polite 0.3 s pause).
Liquidity comes from the spark meta: last price at least `--min-price` (1 TL)
and last session's volume at least `--min-volume` (1,000,000 shares). Run it
before the open (09:40 opening auction, 10:00 continuous trading) or after
the close: during the session the volume is the day's so far, and today's
daily bar is left out until 18:00 TSİ (12:30 on a half day).

The setups are computed from daily closes only (spark has no highs or lows):

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

A closing-high breakout fires more often than an intraday-high one. Hits are
listed most traded first (price times volume, i.e. lira traded). Remember the
±10 % daily price limit (taban / tavan): a share that closed at its tavan
can show as a breakout and still be unbuyable at the open.

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of continuous trading: the
10:00, 10:05 and 10:10 five-minute bars. The 09:40-09:55 opening auction has
no continuous bars and is not part of the range. A hit is the first completed
5-minute bar to CLOSE above the range high, at that bar's close time (so the
first possible hit is at 10:20 TSİ).

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range. That is a necessary condition
   (the range high is at least every range close), so no breakout is lost.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. Stage 2 checks at most `--max-confirm` (200) candidates,
   the most traded first (price times volume so far), and prints PARTIAL with
   the count it skipped. `--max-confirm 0` checks them all. With the ~100
   shipped codes the cap is rarely reached.

A chart is usable only when every 5-minute bar from 10:00 to the cutoff is
there; a missing bar means a halt (BIST circuit breaker / devre kesici) or a
thin stock. Null closes are dropped, a bar off the 5-minute grid (the live
tick, like 10:58:07) is dropped, the 18:00 closing-auction print is ignored,
and a bar counts only once it is complete (start + 5 minutes). Before 10:20
TSİ, on a weekend or BIST holiday, after the close, or after
`no_entries_after` (17:30 by default: an expired setup) it prints one plain
line and writes nothing. Half days (arefe, 28 Ekim), when continuous trading
ends at 12:30, are in the calendar.

**Data delay.** BIST data on Yahoo is free, unofficial and often delayed by
about 15 minutes. A breakout "at 10:20" in the scan may really be from 10:05
in the market, or the bars may simply not be there yet. Tell the user this
plainly every time you report an ORB scan, and ask them to confirm the bar
times and prices in their own broker's screen (real-time data) before acting.

## Data and limits

- Yahoo's chart and spark endpoints with `.IS` symbols are free, unofficial
  and can lag (BIST is commonly ~15 minutes late on Yahoo). Bars are 5-minute
  bars, bar by bar, never tick by tick. query2 first, query1 on a 429 or 5xx,
  one retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 97 of 108 charts (11 failed
  or had no data)". Never round a partial scan up to "the whole market".
- Borsa İstanbul holidays and half days for 2026 and 2027 are built in. The
  2027 bayram dates are estimates ("tahmini") until Borsa İstanbul publishes
  its 2027 calendar.

## Steps

1. Once in a while (after an index rebalance, or after editing
   `bist_universe.txt`): `scan.py universe`.
2. Before the open: `scan.py daily`. Tell the user, in Turkish, the counts
   (fetched, liquid, scanned, hits) and the top few by setup.
3. After 10:20 TSİ (10:30 is a common time): `scan.py orb`. Tell the user how
   many usable charts broke their range and when, that a later hit is closer
   to the price now, and that Yahoo's BIST data is often ~15 minutes late, so
   they should confirm the timing in their broker's screen.
4. The user picks one chart. Hand it on: `/trader-news THYAO` (what is moving
   it, KAP disclosures), then `/trader-entry THYAO` (the entry bar by bar).
   Never pick for them.

## Example

A Monday morning scan at 10:30 TSİ over the shipped list:

```
python3 scan.py orb --at 10:30
Opening-range breakout scan, Mon 2026-10-19 10:30 TSİ: 15-minute range (10:00 to 10:15), 5-minute bars, Yahoo.
Yahoo's BIST (.IS) data is free, unofficial and often about 15 minutes late: check the times in your broker's screen.
Fetched 106 of 108 charts (2 failed or had no data); 101 usable; 30 candidates, 30 confirmed with real highs and lows.
...
     10:30  THYAO      312.25  range 308.50 to 311.75  +0.16% over
```

(The numbers above show the format only; they are not real market data.)
THYAO's range is the 10:00 to 10:10 bars; the 10:25 bar closed above the
range high, so the hit is at 10:30. In Turkish you would say something like:
"THYAO 10:30'da açılış aralığının üstünde kapattı (kurala uydu, yatırım
tavsiyesi değildir). Yahoo verisi yaklaşık 15 dakika gecikmeli olabilir;
saati aracı kurumunuzun ekranından kontrol edin." The user picks THYAO and
Claude hands it to `/trader-news THYAO`, then `/trader-entry THYAO`.

## Rules

- Say what was scanned: how many codes, how many charts came back, how many
  were usable, and the time of the data (TSİ).
- Say "matched a rule" / "kurala uydu", never "a buy", "al", "a winner" or
  "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not investment advice (yatırım tavsiyesi değildir). A PASS is a rules
  check, not a recommendation. Nothing here is tax advice.
- Paper by default (`"paper": true`, kağıt üzerinde / sanal). Going live is the user's own
  decision, in their own config.
- Nothing in this pack places an order. trader-broker prints an emir fişi only when trader-risk
  passed that exact order today and the user typed an explicit "evet" for that one order in the
  chat; the user then types it into their own broker app themselves.
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Never ask for broker passwords, PINs or SMS codes. They never go in the chat.
