---
name: trader-alerts
description: >-
  Price Alerts for Borsa İstanbul (BIST). Rings when a price, a stop, the
  daily limit or the opening bell hits. Turns the trade plan into three
  alerts in one go (the stop, the target and "flat by close" at your flat-by
  time, 17:50 TSİ by default), plus above / below / stop / target levels,
  percent moves from the previous close, near-tavan / near-taban, any time of
  day, and the 10:00 opening bell, each ringing once so you are not spammed.
  Knows BIST hours (opening auction 09:40-09:55, continuous trading
  10:00-18:00, closing auction 18:00-18:10) with the 2026 and 2027 holidays
  and 12:30 half days, works out Istanbul time (TSİ) itself, fetches free
  delayed Yahoo quotes (THYAO.IS) one ticker at a time, and writes
  trader/prices.json for sizing and the risk check. Use when the user says
  "alarm kur", "THYAO 290'a gelince haber ver", "stopumu izle", "borsa açık
  mı", "alert me when", "watch my stop", "remind me to close by 17:50", "is
  the market open", "get me a price", or right after /trader-exit writes a
  plan.
argument-hint: "[BIST code] [price]"
---

# trader-alerts

**Language:** always talk to the user in Turkish (Türkçe). The tool's output
is English; translate it faithfully and keep every number and time exactly as
printed. Never call anything investment advice: "yatırım tavsiyesi değildir".

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. On BIST the stop and the take-profit live in
the user's own broker app (if they entered them there); the alerts tell the
user what happened and when it is time to be flat.

**Delayed data.** Quotes come from Yahoo's free `.IS` feed and can be about
15 minutes behind the exchange. The alerts are not exchange-grade: before the
user acts on any alert, they confirm the price in their broker app. Say this
the first time an alert is set up in a conversation and whenever one rings.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # BIST open or closed, auctions, next open
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by close from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell             # rings at 10:00 TSİ
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol THYAO --stop 282.00
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol THYAO --target 289.00
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 17:50 --label "flat by close" --before-close
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol ASELS --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol GARAN --near-limit 1   # within 1 % of tavan / taban
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote THYAO --write            # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan tickers
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset thyao-plan-stop
```

`--now "2026-11-09 17:50"` pins the clock (Istanbul time, TSİ, UTC+3 all
year); `--offline DIR` reads saved quote files instead of the network.
Symbols are bare BIST codes (`THYAO`, `ASELS`, `GARAN`); the tool adds `.IS`
for Yahoo itself and stores the bare code in every file.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from trader-exit, for `add --from-plan` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest quotes, merged in, for trader-budget and trader-risk |
| `trader/portfolio.json` | the tickers held, for `quote --holdings` |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `near_limit` | price within `pct` percent of today's tavan (+10 %) or taban (-10 %), from the previous close, tick-rounded | once per day at most |
| `time` | the Istanbul clock passed `at` ("17:50") on a trading day | once per trading day |
| `opening_bell` | BIST continuous trading is open (from 10:00 TSİ) | once per trading day |

A rule with `"date"` is watched on that Istanbul date only; `--from-plan`
rules carry the plan's date, so yesterday's stop never rings today. A `time`
rule with `"before_close": true` moves 5 h 30 min earlier on a half day
(continuous trading ends 12:30 instead of 18:00), so "flat by close" at 17:50
rings at 12:20 on those days. A time that is already before 12:30 (a half-day
plan from trader-exit says `"flat_by": "12:20"`) is not moved again.

## Quotes

- `yahoo` (default): `query2.finance.yahoo.com/v8/finance/chart/<CODE>.IS`,
  an unofficial free JSON endpoint, one request per ticker, half a second
  apart. On a 429 it waits and tries once more; on a 429 or a 5xx after that
  it tries `query1`. It identifies itself as `day-trader-skill/2.0`.
- `stooq`: Stooq has no BIST shares; the option only reads saved `--offline`
  files.
- Free quotes are unofficial and can be ~15 minutes delayed. They are fine
  for alerts and for checking a size, not for timing an order. The broker
  app's price is the one that fills. Each price in `trader/prices.json` keeps
  its quote time, and trader-risk only uses one under 15 minutes old.
- If Yahoo is down, the user can read the price from their broker app and
  you write it into `trader/prices.json` as
  `{"as_of": "...Z", "prices": {"THYAO": 286.25}}`.
- `quote` also prints today's taban and tavan (±10 % of the previous close,
  rounded to the BIST tick).

## The BIST clock

Borsa İstanbul Pay Piyasası, Istanbul time (TSİ):

| phase | time |
| --- | --- |
| opening auction (açılış seansı) | 09:40-09:55 |
| continuous trading (sürekli işlem) | 10:00-18:00 |
| closing auction (kapanış seansı) | 18:00-18:10 |
| half day (yarım gün) | continuous trading ends 12:30 |

`clock` says which phase it is; "open" means continuous trading. Closed on
weekends and on these days (Borsa İstanbul 2026 tatil listesi, read
2026-11-09; 2027 bayram dates are estimates until Borsa İstanbul publishes
its calendar):

- 2026: Jan 1, Mar 20 (Ramazan Bayramı), Apr 23, May 1, May 19, May 27-29
  (Kurban Bayramı), Jul 15, Oct 29. Half days: Mar 19, May 26, Oct 28.
- 2027: Jan 1, Mar 9-11 (Ramazan Bayramı, tahmini), Apr 23, May 17-19
  (Kurban Bayramı / 19 Mayıs, tahmini), Jul 15, Aug 30, Oct 29. Half days:
  Mar 8 (tahmini), Oct 28.

Turkey is on UTC+3 all year (no daylight saving), so the tool needs no time
zone files and ignores the computer's own zone. For any other year it says
the holiday list is unknown: check borsaistanbul.com.

## Steps

1. Right after trader-exit writes the plan, run `add --from-plan`. Confirm
   the three alerts back to the user in Turkish: the stop, the target and the
   flat-by time (17:50 unless the plan says otherwise).
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the tickers in today's
   rules and rings each alert once. A check reads the latest delayed quote;
   it is not a live feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each, in Turkish, and remind them the
   quote may be ~15 minutes old: confirm in the broker app. A stop or target
   alert means a stop / take-profit order in their broker app may have
   filled: they check there and record the fill with trader-broker. "Flat by
   close" means close anything still open: trader-broker `flatten` prints the
   emir fişi, then trader-risk, then the user's yes, and the user types it
   into their broker app. Never act for them.

## Example (illustrative)

THYAO on Monday 2026-11-09: entry 285.50 at 10:30, stop 282.00, target
289.00, flat by 17:50. `add --from-plan` writes `thyao-plan-stop`,
`thyao-plan-target` and `thyao-flat-by` for that day. A `check` at 11:05
while the delayed quote sits between the stop and the target prints:

```
BIST OPEN (2026-11-09 11:05 TSİ, continuous trading (sürekli işlem)).
Nothing hit. 3 rule(s) watched today, THYAO 286.25.
```

Tell the user: "Henüz bir alarm çalmadı; THYAO son gecikmeli fiyat 286,25
(Yahoo, ~15 dk gecikmeli olabilir, aracı kurum uygulamanızdan teyit edin)."

At 17:50:

```
ALERT Flat by close: it is 17:50 TSİ (set for 17:50), continuous trading ends at 18:00. If THYAO is still
open, close it: trader-broker flatten --symbol THYAO builds the sell order slip (emir fişi) for your broker
app, then trader-risk and your yes.
```

It rings once. The next morning the plan's three alerts are no longer
watched; with `add --opening-bell`, the bell rings at 10:00.

## Rules

- An alert is a fact about a delayed price or the clock. Say it as a fact,
  and say the price may be ~15 minutes old.
- Never present a quote as live or exchange-grade. Free quotes are delayed
  and unofficial; the broker app is the reference.
- Confirm every new rule back to the user: ticker, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is
live, and never predicts where a price is going.

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
