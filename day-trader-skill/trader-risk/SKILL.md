---
name: trader-risk
description: >-
  Risk Manager for Borsa İstanbul (BIST). Checks every trade against your
  limits and stops anything over them before an order slip (emir fişi) is
  ever printed. For a day trade: a stop is required, prices must sit on the
  BIST tick grid, the stop may not be under today's taban nor the target over
  today's tavan, the loss at the stop plus round-trip costs (komisyon + BSMV
  + borsa payı) must fit your max loss per trade (measured from the higher of
  the plan's entry and a fresh price), reward:risk must meet your minimum,
  the day's total risk must fit your daily loss limit, no more than your
  trades per day, and no new entries outside your entry window (TSİ) or when
  BIST is closed. It also keeps the basics: blocked tickers, order size,
  position cap, cash buffer, margin, short sales, options / VİOP, malformed
  or bad bracket orders, duplicates and a daily order limit. Getting out is
  never blocked by a day limit. Prints PASS or BLOCK with the reason and
  records the verdicts so trader-broker only shows an order that passed. Use
  when the user says "emrimi kontrol et", "risk kontrolü", "bu kurallarıma
  uyuyor mu", "bu işleme girebilir miyim", "bugün ne kadar risk aldım",
  "check my order", or before /trader-broker, always.
argument-hint: "[path to orders.json]"
---

# trader-risk

**Talk to the user in Turkish.** Use BIST examples (THYAO, ASELS, GARAN),
TL amounts written the Turkish way (1.000 TL, 994,76 TL) and Istanbul time
(TSİ). Never call anything investment advice: "yatırım tavsiyesi değildir".

The Risk Manager is the gate between a proposal and the user's yes. Every
order written by trader-budget or trader-broker's flatten passes through it.
A BLOCK is final for that order: it does not go to the user for an evet, and
no emir fişi is printed for it. Most people who day trade lose money. These
limits keep each loss and each day's losses small. They do not make a trade
win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # trader/ files; writes trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-11-09 10:30"  # pin the clock (Istanbul time, TSİ)
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget, or trader-broker's flatten) |
| `trader/config.json` | the limits and cost rates (trader-budget ships `config.example.json`) |
| `trader/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `trader/prices.json` | a fresh price raises the entry it measures risk from; also values holdings |
| `trader/plan.json` | optional: today's `limits` (base, taban, tavan) when the order does not carry them |
| `trader/state/broker.json` | what was recorded as placed today: duplicates, trades per day, daily loss |
| `trader/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |

All amounts are Turkish lira, including the config keys whose names end in
`_usd` (the names are kept for compatibility across the pack).

## Entries and exits

An **entry** opens or adds to a position: a buy, or a sell that would go
short. An **exit** only reduces what you already hold. Getting out must
always be possible, so an exit is checked for format, ticks, asset and short
sales only. Every other rule (day limits, entry window, loss, reward:risk,
blocked list, size, duplicates) turns into a warning for an exit, never a
BLOCK.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | no symbol, side not buy or sell, both or neither of qty and notional, a qty that is not whole shares (1 lot = 1 share), a notional order that is not market + day, a limit order with no limit price, extended hours; a bracket that is not `day` or `gtc`, or misses its stop-loss or take-profit |
| tick | a limit, stop or target price that is not on the BIST tick grid (fiyat adımı): the exchange would reject it |
| band | a limit price outside today's taban-tavan band |
| asset | an option or VİOP symbol while `allow_options` is false; crypto or anything that is not a BIST share code |
| short | a sell of more than you hold (`allow_short` false) |
| blocked | (entries) the ticker is on the `blocked` list |
| size | (entries) under `min_order_usd` or over `max_order_usd` (TL) |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the buy plus its costs would leave less than the cash buffer, or need margin |
| stop | (entries) no stop while `require_stop` is true (a bracket `stop_loss_stop_price`, or the order's plan stop); a stop on the wrong side of the entry |
| limits | (entries) the stop under today's taban or the target over today's tavan: it can never fill today |
| loss | (entries) qty x (entry minus stop + round-trip costs a share) over `max_loss_per_trade_usd` |
| reward:risk | (entries) (target minus entry) / (entry minus stop) under `min_reward_risk` |
| daily loss | (entries) the max losses at the stops of today's entries, plus this one, over `max_daily_loss_usd` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` or after `no_entries_after`, or BIST is closed |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the limit price for a limit order. For a
  market order, the HIGHER of the plan's entry and a fresh price from
  `trader/prices.json` (under 15 minutes old). A plan written at 10:30 cannot
  understate the risk at 11:10 after the price ran up. An older price is not
  used, and it says so.
- **Costs**: commission (`commission_rate`, default 0.0015 = binde 1,5 a
  side) + BSMV on the commission (`bsmv_rate`, 0.05) + borsa payı
  (`exchange_fee_rate`, about 0.00003 a side), on the buy at the entry and the
  sell at the stop. They are part of the max loss. Stopaj on BIST share gains
  is currently 0% for individuals, so it is not counted (not tax advice).
- **Ticks**: 0,01 under 20 TL, 0,02 to 50, 0,05 to 100, 0,10 to 250, 0,25 to
  500, 0,50 to 1.000, 1,00 to 2.500, 2,50 above (Pay Piyasası, in force since
  2023-11-06).
- **Taban / tavan**: ±10% of the previous close, rounded to the tick. Read
  from the order's `limits`, its plan's `limits`, or `trader/plan.json` for the
  same symbol. Without them this check is skipped.
- **Daily loss** counts the full max loss of every entry placed today, win or
  lose, as trader-broker logged it. Three full-size trades use up a 3.000 TL day.
- **The window** is Istanbul time (TSİ, UTC+3 all year, no daylight saving),
  worked out in the tool, never from the computer's clock zone. Continuous
  trading is 10:00 to 18:00. Weekends and BIST holidays are closed. On a half
  day (arefe, continuous trading ends 12:30) the entry window ends 5 hours 30
  minutes earlier too (17:30 becomes 12:00). The 2027 bayram dates are
  estimates until Borsa İstanbul publishes its calendar. Set both
  `no_entries_*` to null to switch the window off.
- **pdt_guard** is off by default and should stay off: the US
  pattern-day-trader rule does not apply to BIST. The code is kept only as an
  optional self-imposed cap (3 day trades in 5 business days).

Warnings do not block, but say them to the user: a LIVE config, a stop that
lives only in the plan and not on the order, a price too old to use, a half
day today, a year outside the holiday list.

## Steps

1. If the price may have moved since the plan, refresh it first
   (trader-alerts: `alerts.py quote THYAO --write`).
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked.
3. Tell the user the result in plain Turkish, order by order: the max loss at
   the stop with costs, reward:risk, and how much of today's limits it uses.
   For a BLOCK, give the reason and the fix (fewer lots, a closer stop from
   trader-exit, a price on the tick grid, wait for the window, or skip the
   trade). Never change a limit yourself.
4. If the order changes in any way, run the check again. trader-broker matches
   each order to its verdict by fingerprint, so an edited order is not ready
   until it passes again, and a verdict from yesterday does not count today.

## Example

The THYAO bracket from trader-budget at 10:30 TSİ on Monday 2026-11-09, on a
fresh 100.000 TL paper account with the example config:

```
PASS  #1 buy THYAO 181 shares, entry
          entry 312.50 (the plan's entry), stop 308.00, target 317.00: risk 4.50 + costs 1.00 a share
          max loss 994,76 TL with costs (limit 1.000,00 TL), reward:risk 1.00 (min 1)
          today's band: taban 274.50, tavan 335.50
          today before this: 0 of 3 trades, 0,00 TL of 3.000,00 TL daily loss

1 passed, 0 blocked at 2026-11-09 10:30 TSİ. Cash after the passed orders: 43.346,72 TL (kağıt üzerinde / sanal).
```

One more lot and it is blocked:

```
BLOCK #1 buy THYAO 182 shares, entry
        - loss: 182 shares x (4.50 risk + 1.00 costs a share) = 1.000,25 TL at the stop, over your 1.000,00 TL max loss per trade
```

A stop of 308.10 is blocked as off the tick grid (the tick is 0,25 between
250 and 500 TL; nearest 308.00). At 10:05 the same order is blocked by the
window (no new entries before 10:15). The 17:50 sell that closes the
position passes, even though it is after the 17:30 entry cutoff: it is an
exit.

## Rules

- A PASS only means the order may be shown to the user. It is not an evet.
- Every number comes from the config, the order, the plan, the portfolio, the prices or the broker log.
- Free Yahoo prices (`.IS`) are unofficial and can be delayed; the broker's price is the one that
  fills, and a stop can fill worse than its price in a fast market or on a gap.

## What it never does

It never places or sends an order, never edits an order to make it pass,
never loosens a limit, never blocks a sell that only gets the user out, and
never treats a PASS as the user's evet.

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
