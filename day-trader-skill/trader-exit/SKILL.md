---
name: trader-exit
description: >-
  Exit Planner for Borsa İstanbul (BIST). Sets your stop and your target
  before you enter, so you know when to get out. Reads the entry from
  trader-entry (or a price you give), puts the stop at the opening-range
  midpoint by default (or the range low, your price, a percent, or a
  multiple of the ATR), rounds it down to the BIST tick, and sets the target
  at 2R (or 1R, or your price), rounded up to the tick. Checks the day's
  taban/tavan (±10 % of the previous close), adds the round-trip costs
  (komisyon + BSMV + borsa payı) and shows reward:risk gross and after costs.
  Refuses a stop at or above the entry, a target at or below it, or a stop
  too tight to survive the spread, and says why. Writes trader/plan.json for
  the Time Machine, the Budget Manager and Price Alerts. Long side only. Use
  when the user says "where's my stop", "stop nereye", "set my stop and
  target", "zarar kes", "hedef fiyat", "plan the exit", "when do I get out",
  "stop loss", "what's my R", "risk reward", or right after /trader-entry
  triggers, before /trader-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K] [--target 2R|1R|price:X] [--base-price X]"
---

# trader-exit

**Talk to the user in Turkish.** The commands, flags and the script's output
stay as they are; explain them in Turkish. Never call anything investment
advice ("yatırım tavsiyesi değildir").

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set BEFORE the entry, written down and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap or a fast market can fill past it. Day trading loses most people money,
and a neat plan does not change that.

Market: Borsa İstanbul, Istanbul time (TSİ, UTC+3 all year), continuous
trading 10:00-18:00 (12:30 on a half day). Codes are bare BIST codes
(`THYAO`); Yahoo's `.IS` data can be ~15 minutes delayed, so the user should
confirm prices in their broker's screen.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # trader/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (Yahoo or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol THYAO --entry 312.50 --or-high 310.75 --or-low 305.25 --base-price 306
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current trader/plan.json
```

Other flags: `--flat-by 17:50`, `--base-price X` (the previous close, for
the taban/tavan), `--json`, `--now "2026-10-19 10:30"` (Istanbul time, TSİ),
`--entry PRICE` with a waiting entry.json to plan ahead, `--setup level`,
`--offline FILE` (a saved Yahoo chart JSON for an ATR stop; its meta also
gives the base price).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from trader-entry: symbol, entry price, time, bar, opening range, and the base price (`limits.base` / `base_price`) when it had one |
| `trader/config.json` | `flat_by` (17:50), `min_reward_risk` (1.0: a note here, trader-risk enforces it), `commission_rate` (0.0015), `bsmv_rate` (0.05), `exchange_fee_rate` (0.00003) |
| Yahoo chart API | only for an `atr:K` stop: the session's bars of `THYAO.IS` up to the entry bar, free and unofficial |
| `trader/plan.json` | with `--write`: entry, stop and its rule, target and its R, risk a share, reward:risk, flat by, plus `limits`, `warnings`, `costs`, `cost_per_share`, `reward_risk_net`. trader-timemachine, trader-budget, trader-risk and trader-alerts read it |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |

A level setup has no opening range to lean on, so it needs a stop you name.

- **Ticks.** BIST prices move in ticks (fiyat adımı): 0.01 TL under 20 TL,
  0.02 to 50, 0.05 to 100, 0.10 to 250, 0.25 to 500, 0.50 to 1000, 1.00 to
  2500, 2.50 above. The stop rounds DOWN to the tick (away from the entry),
  the target rounds UP to the tick. Risk = entry minus the rounded stop.
  Target = entry + R x risk, then up to the tick. `2R` by default; `1R`,
  `1.5R` or `price:X` also work. A Turkish decimal comma is read as one:
  `price:308,50` is 308.50 and `pct:1,5` is 1.5 %.
- **Taban / tavan.** A share trades only between the previous close -10 %
  (taban) and +10 % (tavan), on the tick. The base price comes from
  `--base-price`, else the chart's meta (previous close), else
  `trader/entry.json`. A stop under the taban or a target over the tavan can
  never fill today: the plan prints a WARNING and records it in `warnings`
  and `limits` (trader-risk blocks it). Without a base price it says the
  limits were not checked.
- **Costs.** Each side costs the broker commission plus BSMV (5 % on the
  commission) plus the borsa payı. `cost_per_share` is the round trip if the
  stop is hit; the plan shows reward:risk gross and after costs. Set your own
  broker's rate in the config. Stopaj on BIST share day-trading gains is
  currently 0 % for individuals; this is no tax advice.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A risk under 0.05% of the price: too tight to survive the
  spread, normal wiggle would stop you out. An entry.json that has not
  triggered (unless you give `--entry`). A short: v2 plans longs only.
- **Flat by.** 17:50 TSİ by default (or `flat_by` in the config). Out by
  then, win or lose, no overnight hold. On a half day it moves up with the
  12:30 close, to 12:20. An entry at or after the flat-by time is refused.

## Steps

1. Run it right after `/trader-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back, in Turkish, the entry, the stop and
   its rule, the target and its rule, the risk a share, the costs,
   reward:risk gross and after costs, the taban/tavan and flat by.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target). If it warns about the taban or tavan, say it plainly and suggest
   a stop or target inside the limits. Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee, and that Yahoo's data
   can be ~15 minutes delayed: check the prices in the broker's screen.
6. Hand off to `/trader-timemachine` to replay the plan, then `/trader-budget`
   to size it to the max loss per trade. Offer `/trader-alerts` for the stop,
   the target and the flat-by time. Claude can keep `entry.py watch` running
   in the background or on a `/loop` until a setup triggers, then come here.

## Example

THYAO, Monday 2026-10-19 (illustrative numbers). The entry is 312.50 at
10:20 AM (the 10:15 bar, bar 3), the opening range is 305.25 to 310.75 and
the previous close is 306.00. `exit.py plan --stop or-mid --target 1R --write`:

```
Exit plan: THYAO long, Mon 2026-10-19, opening-range breakout on 5-minute bars
  Entry         312.50 at 10:20 AM (bar 3)
  Stop          308.00  opening-range midpoint: (310.75 + 305.25) / 2 = 308
  Target        317.00  1R (one times the risk)
  Risk a share  4,50 TL (1.44% of the entry)
  Costs a share 1,00 TL round trip if stopped (0,50 TL in + 0,49 TL out), 1,01 TL at the target: komisyon + BSMV + borsa payı, estimated
  Reward:risk   1.0 gross, 0.63 after costs
  Daily limits  taban 275.50, tavan 336.50 (base 306.00, the previous close)
  Flat by       17:50 TSİ, win or lose: no overnight hold
```

The default 2R target would be 312.50 + 2 x 4.50 = 321.50. Costs matter on
a tight stop: here they eat more than a third of a 1R reward. trader-budget
then sizes it: a 1.000 TL max loss per trade buys floor(1000 / 4.50) = 222
shares (1 lot = 1 share).

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, costs, reward:risk.
  It never prints a P&L or a return.
- Nothing here promises winning trades or money. A 1R target is not a
  prediction that the price gets there. Yatırım tavsiyesi değildir.
- Long side only. v2 plans longs only.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

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
