---
name: trader-budget
description: >-
  Budget Manager for Borsa İstanbul (BIST). Sizes every position to your
  budget and your max loss per trade, in Turkish lira, with round-trip costs
  (komisyon + BSMV + borsa payı) counted as part of the risk. Reads the trade
  plan from trader-exit (entry, stop, target, on the BIST tick grid), finds
  the whole shares (1 lot = 1 share) that fit inside four limits (max loss
  per trade, position cap, max order, cash after your buffer), says which
  limit decided it, and writes ONE bracket order (entry, stop-loss,
  take-profit) to trader/orders.json. A stop too far away for your max loss
  is a plain refusal, not a smaller order. Nothing is placed or sent. Use
  when the user says "kaç lot almalıyım", "pozisyon büyüklüğü", "bu işlemi
  boyutlandır", "100.000 TL'im var", "işlem başına en fazla 1.000 TL
  kaybetmek istiyorum", "kurallarımı ayarla", "%10 nakit tut", "how many
  shares", or after /trader-exit writes a plan.
argument-hint: "[budget in TL]"
---

# trader-budget

**Talk to the user in Turkish.** Use BIST examples (THYAO, ASELS, GARAN),
TL amounts written the Turkish way (100.000 TL, 994,76 TL) and Istanbul time
(TSİ). Never call anything investment advice: "yatırım tavsiyesi değildir".

The Budget Manager turns a plan ("THYAO al 312,50, stop 308,00") into an
exact order ("181 lot al"), inside rules the user sets once. The stop and the
costs decide the size: the further away the stop, the fewer shares. It writes
a proposal only. trader-risk checks it and trader-broker asks the user evet
or hayır and prints an emir fişi the user types into their own broker app.
Day trading loses most people money. Sizing to a max loss keeps each loss
small; it does not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                        # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit --write        # a limit order at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 312.50 --json  # a limit order at 312,50
python3 "${CLAUDE_SKILL_DIR}/size.py" --weights THYAO=40,GARAN=60 --config split.json   # weights mode, below
```

`--now "2026-11-09 10:30"` pins the clock (Istanbul time, TSİ, UTC+3 all
year). A plan for another day is refused: make a new one with trader-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader/plan.json` | the trade: entry, stop, target, setup, flat-by time; optional `limits` (taban / tavan), `warnings`, `cost_per_share` (trader-exit writes it) |
| `trader/prices.json` | optional: a fresh price above the plan's entry means a bigger risk (trader-alerts writes it) |
| `trader/portfolio.json` | optional: its cash caps the size (trader-broker keeps it) |
| `trader/orders.json` | the one proposed bracket order it writes with `--write` (`"market": "BIST"`, `"currency": "TRY"`) |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` into
`trader/config.json` and ask the user for each value in plain Turkish. Every
amount is Turkish lira. Some key names end in `_usd` for compatibility with
the rest of the pack; the amounts in them are still TL.

- **budget**: how much money this is for (example 100.000 TL). Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_usd**: the most one trade may lose, costs included, if the stop fills at
  the stop (example 1.000 TL).
- **max_position_pct**, **max_order_usd**, **min_order_usd**: the biggest position (75% of the
  budget), the biggest order (80.000 TL) and the smallest order (100 TL).
- The day limits trader-risk enforces: **max_daily_loss_usd** (3.000 TL), **max_trades_per_day** (3),
  **min_reward_risk** (1.0), **require_stop** (true), **no_entries_before** / **no_entries_after**
  ("10:15" / "17:30") and **flat_by** ("17:50"), all Istanbul time (TSİ). Continuous trading
  runs 10:00 to 18:00; on a half day (arefe) it ends at 12:30.
- **Costs** (ask what their broker actually charges):
  - **commission_rate**: broker commission per side as a fraction of the amount (0.0015 = binde 1,5).
  - **bsmv_rate**: BSMV, the banking and insurance transaction tax charged on the commission (0.05).
  - **exchange_fee_rate**: borsa payı, Borsa İstanbul's fee per side (about 0.00003).
  - Stopaj (withholding tax) on gains from BIST shares is currently 0% for individuals, so it is
    not in the cost. Say this is not tax advice ("vergi tavsiyesi değildir"), rules can change.
- **blocked**: tickers they never want to trade.
- **paper**: keep it true ("kağıt üzerinde / sanal") until they have watched the whole flow work
  on paper or in a demo account.
- **pdt_guard**: false. The US pattern-day-trader rule does not exist on BIST.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only: on BIST 1 lot = 1 share, no fractions. First it works out
the round-trip cost of one share: commission + BSMV + borsa payı on the buy
at the entry, and on the sell at the stop. The size is then the smallest of
four limits:

| limit | shares |
| --- | --- |
| max loss | `max_loss_per_trade_usd` / (risk a share + cost a share), risk = entry minus stop |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_usd` / entry |
| cash | (budget minus the cash buffer) / (entry + buy-side cost a share), never more than the cash in `trader/portfolio.json` |

Prices sit on the BIST tick grid (fiyat adımı): 0,01 under 20 TL, 0,02 to 50,
0,05 to 100, 0,10 to 250, 0,25 to 500, 0,50 to 1.000, 1,00 to 2.500, 2,50
above. The stop rounds down, the target up, a limit price to the nearest tick
(it says so when it moved your price).

The entry it sizes at: the limit price with `--limit`. For a market order, the
plan's entry, or the price in `trader/prices.json` if it is fresh (under 15
minutes old) and higher. A price that ran up since the plan means a bigger
loss at the same stop, so fewer shares. A lower price never shrinks the risk.

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk is under `min_reward_risk`, the stop is not
below the entry, the target is not above it (the move already happened), the
stop is under today's taban or the target over today's tavan (the plan's
`limits`: it could never fill today), the plan is for another day, the ticker
is blocked, or the plan is a short. Say the reason to the user in one line,
in Turkish. Pass on any `warnings` from the plan.

## Step 3: show the user

Show the four limits, which one decided the size, the lots, the TL amount, the
costs, and the max loss at the stop with costs. Say plainly that a stop is an
order, not a promise: in a fast market, or on a gap, it can fill below the
stop. Then hand over to `/trader-risk`.

## Example

THYAO on Monday 2026-11-09. Opening-range breakout: entry 312,50 at 10:30
TSİ, stop 308,00 (the opening-range midpoint), target 317,00 (1R). Risk 4,50
a share. Costs at the example rates: 0,50 on the buy plus 0,49 on the sell at
the stop = 0,9959 a share. The example config: 100.000 TL budget, 10% cash
buffer, 1.000 TL max loss.

| limit | the math | shares |
| --- | --- | --- |
| max loss | 1.000 TL / (4,50 + 0,9959) | **181 (decides it)** |
| position cap | 75% of 100.000 = 75.000 TL / 312,50 | 240 |
| max order | 80.000 TL / 312,50 | 256 |
| cash | 100.000 minus 10.000 = 90.000 TL / 313,00 | 287 |

181 lot = 56.562,50 TL. Max loss at the stop: 181 x 4,50 = 814,50 TL plus
180,26 TL round-trip costs = 994,76 TL. It writes:

```json
{"symbol": "THYAO", "side": "buy", "qty": "181", "type": "market", "time_in_force": "day",
 "order_class": "bracket", "take_profit_limit_price": "317.00", "stop_loss_stop_price": "308.00",
 "why": "opening-range breakout on THYAO: 181 shares, bound by your 1.000,00 TL max loss per trade (181 x (4.50 risk + 1.00 costs) = 994,76 TL at the stop)",
 "plan": {"entry": 312.5, "stop": 308.0, "target": 317.0, "risk_per_share": 4.5, "cost_per_share": 0.9959,
          "max_loss": 994.76, "setup": "orb", "entry_time": "10:30 AM", "flat_by": "17:50",
          "limits": {"base": 305.0, "taban": 274.5, "tavan": 335.5}}}
```

The orders file around it carries `"market": "BIST"`, `"currency": "TRY"`,
`"paper"` and `"status": "proposed"`. Prices in the order are strings with a
dot (machine format); the emir fişi trader-broker prints shows them the
Turkish way (312,50). `why` and `plan` are notes for trader-risk and
trader-broker. Note how costs bite on a short day trade: 0,99 a share against
a 4,50 risk, so reward:risk after costs is only 0,63 (a win pays the
buy plus 0,51 on the sell at the target: (4,50 - 1,01) / 5,4959).

## Weights mode (kept from v1)

`--weights THYAO=30,GARAN=20` or `--targets FILE` (`targets.example.json`
here, five BIST 30 names with made-up example weights) splits a budget across
a list by weight: the cash buffer comes off the top, then the estimated buy
costs (so the orders plus their commissions never dip into the buffer; 144,22
TL on 90.000 TL at the example rates), weights are renormalised
to 100, any stock over `max_position_pct` is pinned at the cap and its excess
shared out, slices under the minimum order are dropped, and amounts round to
whole lira. On BIST use whole shares (`"fractional": false`, the example
config, or `--whole-shares`) with prices (`--prices trader/prices.json`).
These are plain buys with no stop: trader-risk blocks them while
`require_stop` is true and counts each one against `max_trades_per_day`. Use
a separate config (`--config`) for a long-term split.

## Rules

- Proposals only. This skill never contacts a broker and never sends anything anywhere.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- Free Yahoo prices (`.IS` symbols) are unofficial and can be delayed. The broker's price is the one that fills.
- Costs are estimates from the config rates. The broker's statement is the truth.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, never leaves costs out of the
max loss, and never sizes options (VİOP), margin (kredili işlem) or short
(açığa satış) positions.

## Safety

- Education and tooling, not investment advice (yatırım tavsiyesi değildir). A PASS is a rules
  check, not a recommendation. Nothing here is tax advice either.
- Paper by default (`"paper": true`, kağıt üzerinde / sanal). Going live is the user's own
  decision, in their own config.
- Nothing in this pack places an order. trader-broker prints an emir fişi only when trader-risk
  passed that exact order today and the user typed an explicit "evet" for that one order in the
  chat; the user then types it into their own broker app themselves.
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Never ask for broker passwords, PINs or SMS codes. They never go in the chat.
