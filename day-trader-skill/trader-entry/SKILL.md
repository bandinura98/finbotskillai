---
name: trader-entry
description: >-
  Entry Timer for Borsa İstanbul (BIST). Watches a setup bar by bar and tells
  you the moment to get in. Reads free Yahoo 1-minute or 5-minute bars for
  BIST codes (THYAO -> THYAO.IS), builds the opening range (10:00-10:15 TSİ
  by default) and calls the entry on the first completed bar that CLOSES
  above the range high, or above a level you give it (like a 20-day high from
  the scanner), with an optional volume check. Says waiting and how far,
  triggered with the time and the price, expired, or not open yet, records
  the day's taban/tavan, warns near the tavan, and writes trader/entry.json
  for /trader-exit. Long side only. It never places an order. Use when the
  user says "tell me when to get in", "watch THYAO", "ne zaman gireyim",
  "kırdı mı", "has it broken out", "did it break the opening range", "entry
  signal", "giriş sinyali", "check my watchlist", or after /trader-scanner
  and /trader-news, before /trader-exit.
argument-hint: "[BIST CODE] [--setup orb|level] [--interval 5m|1m]"
---

# trader-entry

**Talk to the user in Turkish.** The commands, flags and the script's output
stay as they are; explain them in Turkish. Never call anything investment
advice ("yatırım tavsiyesi değildir").

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads bars, 1-minute or 5-minute, and waits for each one to finish. A price
that pokes above the high in the middle of a bar is not an entry. A bar that
CLOSES above it is. Then it says the time and the price, writes them down for
the Exit Planner, and stops. It never buys anything. Day trading loses most
people money, and a clean signal does not change that.

Market: Borsa İstanbul Pay Piyasası, Istanbul time (TSİ, UTC+3 all year).
Opening auction 09:40-09:55, continuous trading 10:00-18:00, closing auction
18:00-18:10. On a half day (yarım gün: bayram arefesi, 28 Ekim) continuous
trading ends at 12:30. The user types BIST codes (`THYAO`, `ASELS`, `GARAN`);
the script asks Yahoo for `THYAO.IS` and stores the bare code.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check THYAO                  # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check THYAO --write          # also save trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check THYAO --setup level --level 312.50
python3 "${CLAUDE_SKILL_DIR}/entry.py" check THYAO --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch THYAO --until 17:30    # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                    # every hit in trader/watchlist.json
```

Other flags: `--or-minutes N` (default 15), `--json`, `--offline FILE` (a
saved Yahoo chart JSON, `.json` or `.json.gz`, or a folder of
`<CODE>.json`), `--now "2026-10-19 10:30"` (Istanbul time, TSİ) to replay a
day. `watch` with `--now` or `--offline` runs on a simulated clock and
replays the day bar by bar without waiting.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | the scanner's hits (`hits[].symbol`), for `watchlist` |
| `trader/config.json` | `no_entries_before` (10:15) and `no_entries_after` (17:30), Istanbul time (TSİ). Optional |
| Yahoo chart API | today's bars for `THYAO.IS`, `range=1d&interval=5m` (or `1m`): free, unofficial, can be ~15 minutes delayed on BIST |
| `trader/entry.json` | with `check --write`, or when `watch` triggers: symbol (bare code), setup, status, time, bar, price, level, opening range, `base_price`, `taban`, `tavan`, `limits`, `warnings` |

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes: the
  10:00, 10:05 and 10:10 five-minute bars, or fifteen 1-minute bars. The
  level to beat is the range high.
- **Level.** A price you give, like a 20-day high from `/trader-scanner`.
- **The trigger** is the first completed bar after the range that CLOSES
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it.
- **The time** is that bar's close time (the 10:15 bar closes at 10:20 AM).
  The price is its close.
- **Only closed bars count.** A bar is closed when the clock passes its start
  plus 5 (or 1) minutes and Yahoo has caught up to that moment (it waits a
  minute at most). Yahoo's live tick, like 10:49:45, is never a bar.
- **Volume, if you want it.** `--min-volume-x 1.5` also needs the trigger
  bar's volume at 1.5 times the range bars' average. The 10:00 bar can carry
  the opening auction's volume, so 1.0 is already strict.
- **Time limits.** No entries before 10:15 or after 17:30 (config). On a half
  day the cutoff moves up with the 12:30 close, to 12:00.
- **What it says.** Waiting (and how far the last close is from the level),
  triggered, expired, or no data. Before 10:00: not open yet (BIST opens at
  10:00 after the 09:40-09:55 opening auction). Before 10:15: the range is
  still forming. Weekends and BIST holidays: the market is closed.

## Ticks and daily limits

Every BIST price sits on a tick (fiyat adımı): 0.01 TL under 20 TL, 0.02 to
50, 0.05 to 100, 0.10 to 250, 0.25 to 500, 0.50 to 1000, 1.00 to 2500, 2.50
above. A share can trade only between the taban and the tavan: the previous
close (the base price) -10 % and +10 %, on the tick. When Yahoo sends the
previous close the script records both in `trader/entry.json`, and when the
price is within 1 % of the tavan it warns: a breakout near the tavan can lock
limit-up (tavan kilit) with no sellers, so a buy may never fill. Say that
warning to the user plainly.

## Steps

1. Get the code from the user (`THYAO`, not `THYAO.IS`), or run
   `entry.py watchlist` after `/trader-scanner`. Check `/trader-news` for it
   first (KAP disclosures matter on BIST).
2. Run `entry.py check THYAO`. Tell the user the status in one line, in
   Turkish: waiting and how far, triggered at what time and price, or
   expired.
3. To wait for it, Claude can run `entry.py watch THYAO --until 17:30` in
   the background, or `entry.py check THYAO --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `trader/entry.json`) or 1 when
   the setup expires. Keep the computer awake while it watches: a sleeping
   laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time and the price, and that nothing was placed.
   If the trigger is more than a bar old, say that too: a late entry is a
   different trade. Yahoo's BIST data can be ~15 minutes delayed: tell the
   user to confirm the price in their broker's screen before acting.
5. Hand off to `/trader-exit` to set the stop and the target BEFORE anything
   is bought.

## Example

THYAO, Monday 2026-10-19, 5-minute bars (illustrative numbers). The opening
range is 305.25 to 310.75. At 10:17 the 10:15 bar is still open, so it waits:

```
WAITING  last close 308.00 (10:10 bar), 2.75 under the range high 310.75 (-0.88%).
The 10:15 bar is still forming (312.50 so far, closes 10:20 AM); it does not count until it closes.
```

At 10:20 the bar has closed:

```
THYAO  Mon 2026-10-19 10:20 TSİ  opening-range breakout (15-minute range) on 5-minute bars (Yahoo, free, can lag ~15 min)
Opening range 10:00-10:15: high 310.75, low 305.25 (3 of 3 bars).
TRIGGERED 10:20 AM at 312.50: the 10:15 bar (bar 3) closed above the range high 310.75 (+0.56%).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /trader-exit (exit.py plan --from trader/entry.json).
Daily limits: taban 275.50, tavan 336.50 (base 306.00, the previous close).
Yahoo's BIST data can be ~15 minutes delayed: confirm the price in your broker's screen.
```

## Rules

- Bar by bar, on free Yahoo 1-minute or 5-minute bars. They are unofficial
  and can be ~15 minutes delayed on BIST. Say so when a minute matters.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money. Yatırım tavsiyesi değildir.
- Long side only. v2 plans longs only (short selling stays off). If the user
  wants a short, say that.
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/trader-exit`), never moves the level after the fact, and never counts a
bar that has not closed. It never runs `/trader-broker` from a background
watch: nobody is there to say yes.

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
