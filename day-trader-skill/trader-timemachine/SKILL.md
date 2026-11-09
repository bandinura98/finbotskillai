---
name: trader-timemachine
description: >-
  Time Machine for Borsa İstanbul (BIST). Replays every possible outcome of a
  trade before you risk a lira: 10,000 simulated sessions plus every past
  time the setup fired. Takes the plan from trader-exit (entry, stop, target,
  optional taban/tavan limits and costs), rebuilds the rest of the day 10,000
  times from the stock's own 5-minute bars (Yahoo .IS) over the last 60 days,
  and re-runs the same rule on every past session. Prints the odds of target
  first, stop first, or neither by the 18:00 TSİ close, where the price sits
  at the close, the average R (and after costs), the history, and one honest
  verdict, then hands off to /trader-budget. Use when the user says "zaman
  makinesini çalıştır", "bu işlemi simüle et", "olasılıklar ne", "run the
  time machine", "replay this trade", "monte carlo", "backtest this setup",
  "is this a coin flip", or after /trader-exit writes a plan, before
  /trader-budget.
argument-hint: "[path to plan.json, or BIST code like THYAO]"
---

# trader-timemachine

**Language:** always talk to the user in Turkish (Türkçe). The tool's output
is English; translate it faithfully, keep every number exactly as printed.
Never call anything investment advice: "yatırım tavsiyesi değildir".

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or typed into
the broker app, this skill asks one question: if today plays out like the
stock's own last 60 days on Borsa İstanbul, how often does this plan hit the
target first, the stop first, or neither? It answers with 10,000 replays and
with the real record of the same rule on every past session. It is a
simulation built from the stock's own recent bars, not a prediction. Day
trading loses most people money, and a good-looking replay does not change
that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 17:50      # end each replay at your flat-by time
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol THYAO --date 2026-11-09 \
    --entry 285.50 --stop 282.00 --target 289.00 --entry-time 10:30
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--offline FILE` (a saved Yahoo chart JSON, `.json` or `.json.gz`),
`--now "2026-11-09 10:30"` (Istanbul time, TSİ) to pin the clock. Times are
24-hour TSİ (UTC+3 all year). Symbols are bare BIST codes (`THYAO`); the tool
adds `.IS` for Yahoo itself.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range; optional `limits` `{"base","taban","tavan"}` and `cost_per_share` |
| Yahoo chart API | one request: 60 days of 5-minute bars for `<CODE>.IS` (`range=60d&interval=5m`), free and unofficial, can be ~15 minutes delayed |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths, a 24-bin histogram of the close, and (when the plan has them) `limits` and `avg_R_after_costs` |

## How it works

1. **The pool.** It keeps only FULL sessions before the plan's date: all 96
   five-minute bars, 10:00 to 18:00 TSİ (continuous trading, sürekli işlem).
   Half days (yarım gün, continuous trading ends 12:30: bayram arefesi,
   28 Ekim), gaps and today are left out. The opening auction (09:40-09:55)
   and the closing auction (18:00-18:10) are not bars. Every bar becomes four
   ratios to the bar before it: open, high, low and close over the previous
   close.
2. **One replay.** Start at the entry price. For each bar left in the day,
   pick a past bar from the same time of day (plus or minus 2 bars) at
   random, and scale it to the current price. Bar by bar, it checks the stop
   first, then the target. A bar that touches both counts as the stop.
3. **Daily limits.** When the plan has `limits` (taban / tavan, ±10 % of the
   previous close, from trader-exit), every simulated price is clamped to
   that band: a replay cannot trade beyond the tavan or below the taban. A
   target above the tavan or a stop below the taban cannot fill today; the
   tool says so in a note. Without `limits` nothing is clamped.
4. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until` (default 18:00, the
   close, or 12:30 on a half day). Green, which means target first or above
   the entry at `--until`.
   R is +1 at a 1R target, -1 at the stop, and otherwise the distance from
   the entry at `--until` divided by the risk. A gap through the stop still
   counts as -1R here. A real fill can be worse.
5. **Costs.** When the plan has `cost_per_share` (round-trip commission +
   BSMV + borsa payı), the report adds the average R after costs
   (`avg_R - cost_per_share / risk`) and the expected result per share in TL.
6. **The history.** The plan's own rule (the same opening range, stop rule
   and target R) runs again on every one of those past sessions, with the
   stop and target on the BIST tick (fiyat adımı) the way trader-exit sets
   them. That is what really happened, not a simulation.
7. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them.
8. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), and the
   average R when its sign goes against the lean.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Clamping to the limits never changes the draws. Say the seed with every
result so the user can repeat it.

**5-minute bars only.** Yahoo keeps 60 days of 5-min bars but only about 7
days of 1-min bars, too few sessions for a fair pool. It refuses, in one
plain line, when there are fewer than 20 full sessions, when the entry bar is
not in the data (wrong date, a BIST holiday, not closed yet, a future day),
or when the plan does not make sense (a stop above a buy's entry, an entry
off the 5-minute grid). A live in-progress bar is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. Add `--until 17:50` when the plan has a
   flat-by time, so "neither" means still open at the time you would get out.
3. Tell the user, in Turkish and in this order: the four odds, the median
   and the 5 to 95 range of the price at the close, the average R (and after
   costs, if printed), the history line, then the verdict line and any line
   under it, translated faithfully. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If costs turn a small positive average R negative, say
   that. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 60 days and is not a prediction.
   KAP disclosures, earnings, news, circuit breakers and limit days can do
   things those 60 days never did. Yahoo .IS data can be ~15 minutes delayed.
   Check `/trader-news` for the stock before going on.
6. If the user still wants the trade, hand off to `/trader-budget` to size it.

## Example (illustrative format, not a real run)

THYAO, Monday 2026-11-09. The 15-minute opening range is 10:00 to 10:15. The
10:25 bar closes above it: entry 285.50 at 10:30 (bar 5), stop 282.00, target
289.00 (1R, risk 3.50 a share), limits taban 257.00 / tavan 314.00, costs
0,90 TL a share round trip. The output has this shape:

```
10,000 replays of the rest of the session, 10:30 to 18:00 (90 five-minute bars), seed 19.
  target first   ...
  stop first     ...
  neither        ...      still open at 18:00
  green          ...      target first, or above the entry at 18:00
  Price at 18:00: median ..., 90 % of replays between ... and ....
  Average R: ...
  After costs: average R ... (round-trip costs 0,90 TL a share = 0.257R; ... a share expected).
  Prices clamped to today's limits: taban 257.00, tavan 314.00.
History: the same rule ... fired ... times: ... target, ... stop, ... neither. Average R ....
<verdict line>
```

Relay it in Turkish, for example: "Hedef önce: %.. , stop önce: %.. ,
ikisi de değil: %.. (18:00'de hâlâ açık). Seed 19." Then the verdict, then
"Bu bir simülasyondur, tahmin değildir; yatırım tavsiyesi değildir."

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Translate
  them faithfully, do not rewrite their meaning. Never judge a 2R or 3R plan
  by target first vs stop first alone: the verdict already weighs the
  target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Free Yahoo data is unofficial and can be ~15 minutes delayed; it is not
  exchange-grade. Bars are 5-minute bars, checked bar by bar, never tick by
  tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

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
