---
name: trader-broker
description: >-
  Order slip writer for Borsa İstanbul (BIST). Claude never places or sends
  an order: there is no broker API here. For each order trader-risk passed,
  it shows a plain ticket plus a manual emir fişi (Hisse, Yön AL/SAT, Lot,
  Emir tipi Limit/Piyasa, Fiyat, Zarar-durdur, Kâr-al, Süre Gün, estimated
  costs) that the user types into their own Turkish broker app themselves,
  only after an explicit "evet" for that one order, recorded word for word.
  Paper (kağıt üzerinde / sanal) by default. Builds the sell ticket that
  closes a day trade by your flat-by time, records what you entered and
  your fills with costs, and logs each trade's max loss so trader-risk can
  hold the daily limit. Use when the user says "emri gir", "al", "emir
  fişini göster", "pozisyonu kapat", "flat ol", "beni çıkar", "place the
  order", "flatten", or after /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

**Talk to the user in Turkish.** Use BIST examples (THYAO, ASELS, GARAN),
TL amounts written the Turkish way (56.562,50 TL) and Istanbul time (TSİ).
Never call anything investment advice: "yatırım tavsiyesi değildir".

There is no Alpaca on BIST, and most Turkish brokers (aracı kurumlar) have no
public order API. So this skill **never places an order and never sends
anything anywhere**. It prepares an order slip (emir fişi) and the user types
it into their own broker app. An emir fişi is shown for entry only after two
gates, every time, for every order:

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit "evet" for this one order** in the chat.

Then the user, not Claude, enters the order in their broker app and confirms
it there. No evet, no slip. Claude never logs into a broker, never asks for a
password, PIN or SMS code, and never claims an order was placed until the
user says so.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket + emir fişi, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "evet"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id 123456 --filled-qty 181 --filled-price 312.50
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol THYAO --side sell --qty 181 --price 317.00
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol THYAO --qty 181
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to a broker. It renders tickets and the emir fişi,
checks that a reply is a real evet for that order, builds the flatten
ticket, and keeps the log in `trader/state/broker.json`. `--now "2026-11-09
10:30"` pins the clock (Istanbul time, TSİ).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `flatten` replaces it) |
| `trader/config.json` | `paper`, `broker` (a display name, e.g. your broker's name, or "manual") and the cost rates |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every evet, no, placed, failed and filled, with each entry's max loss |
| `trader/portfolio.json` | cash and lots, updated by `fill` and by `result --filled-qty` (costs included) |
| `trader/plan.json` | the flat-by time for `flatten` |

## The emir fişi

`show` prints, for each order, a plain ticket and this slip:

| field | meaning |
| --- | --- |
| Hisse | the BIST code, e.g. THYAO (no `.IS`) |
| Yön | AL (buy) or SAT (sell) |
| Lot | whole shares; on BIST 1 lot = 1 share |
| Emir tipi | Limit or Piyasa (market) |
| Fiyat | the limit price on the tick grid, written 312,50; "piyasa" for a market order |
| Zarar-durdur | the stop: sell if the price falls to it |
| Kâr-al | the target: sell if the price rises to it |
| Süre | Gün (day): the order ends at today's close |
| Tahmini tutar / maliyet | lots x price, and commission + BSMV + borsa payı for this order and the round trip |

Most Turkish broker apps do not take a bracket as one order. Tell the user:
enter the AL order first; once it fills, enter the zarar-durdur and kâr-al as
conditional (şartlı / stop) sell orders if the app supports them. If it does
not, trader-alerts watches the stop, the target and the flat-by time, and the
user sells by hand when an alert rings.

With `"paper": true` the ticket says **KAĞIT ÜZERİNDE (sanal)**: the user
should track the trade in a demo account or a notebook, not in their real
account. Going live is the user's own decision, in their own config.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/trader-risk`.
2. Show ONE ticket with its emir fişi and ask, in Turkish: "Bu emri onaylıyor
   musun? Evet ya da hayır yaz." One order per question. Never offer "hepsine
   evet".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit evet for this
   order (for example "tamam", "olur", "hepsine evet", a reply with a doubt
   like "evet ama bekle", or an evet that names a different stock or amount).
   Ask again. Never turn a reply into an evet yourself.
4. After "Recorded your evet", show the emir fişi it printed and tell the user
   to type it into their broker app themselves and confirm it there. Claude
   does nothing else with it.
5. When the user tells you what happened, record it: `ticket.py result
   --index N --status placed --broker-id <the app's emir no> --filled-qty Q
   --filled-price P`, or `--status denied` (they decided not to enter it), or
   `--status failed --note "..."` (the app rejected it). A failed order is not
   retried without a new check and a new evet. The log keeps the entry's max
   loss for trader-risk's daily limit.

## Steps: while it is open, and out by the close

- trader-alerts watches the stop, the target and the flat-by time
  (`alerts.py add --from-plan`).
- When the zarar-durdur or the kâr-al fills, record it:
  `ticket.py fill --symbol THYAO --side sell --qty 181 --price 308.00`
  (`--fee` with the real commission if the user has it; otherwise it is
  estimated from the config).
- By `flat_by` (17:50 TSİ; continuous trading ends at 18:00, 12:30 on a half
  day), anything still open gets closed:
  1. Ask the user how many lots they hold (from their broker app).
  2. `ticket.py flatten --symbol THYAO --qty 181` writes a market sell for
     those lots to `trader/orders.json`.
  3. `/trader-risk`. A sell that only closes what the user holds is never
     blocked by the daily limits or the entry window.
  4. `ticket.py show`, then the user's evet for that one order, as above.
  5. If the zarar-durdur / kâr-al sells are still waiting in the app, they
     hold those lots and a new sell can be rejected: the user cancels them in
     the app first, then enters the flatten emir fişi, then `result`.

## Example

```
ORDER 1 of 1, KAĞIT ÜZERİNDE (sanal), do not type it into a real account, manual entry in your broker app
  BUY 181 THYAO, market, good for today
  stop-loss 308.00
  take-profit 317.00
  max loss about 994,76 TL with costs if the stop fills at 308.00 (it can fill worse in a fast market)
  why: opening-range breakout on THYAO: 181 shares, bound by your 1.000,00 TL max loss per trade (...)
  READY: trader-risk PASS, waiting for your evet
  EMİR FİŞİ (bunu kendi aracı kurum uygulamana SEN gir; Claude hiçbir yere göndermez)
    Hisse           : THYAO
    Yön             : AL
    Lot             : 181
    Emir tipi       : Piyasa
    Fiyat           : piyasa (o anki en iyi fiyat)
    Zarar-durdur    : 308,00 (stop: fiyat buna inerse SAT)
    Kâr-al          : 317,00 (hedef: fiyat buna çıkarsa SAT)
    Süre            : Gün
    Tahmini tutar   : ~56.562,50 TL
    Tahmini maliyet : ~90,78 TL bu emir (komisyon + BSMV + borsa payı), ~180,26 TL gidiş-dönüş (stop'ta satışla)
```

The user replies "evet". Claude records it with `approve`, shows the slip
again and waits. The user types it into their app; Claude records the result
when the user reports it.

## What it never does

- Never places, sends or submits an order, to any broker, API or website.
- Never prints an approved slip without a PASS and an explicit evet for that one order; never two orders on one evet.
- Never acts from a scheduled or background task: nobody is there to say evet.
- Never asks for, reads, stores or repeats broker passwords, PINs, SMS codes or API keys.
- Never switches `"paper"` to live. The user does that themselves.
- Never promises a fill price: a market order and a stop fill at whatever the market gives.

## Safety

- Education and tooling, not investment advice (yatırım tavsiyesi değildir). A PASS is a rules
  check, not a recommendation. Nothing here is tax advice.
- Paper by default (`"paper": true`, kağıt üzerinde / sanal). Going live is the user's own
  decision, in their own config.
- Nothing in this pack places an order. This skill prints an emir fişi only when trader-risk
  passed that exact order today and the user typed an explicit "evet" for that one order in the
  chat; the user then types it into their own broker app themselves.
- No options (VİOP), margin (kredili işlem), short selling (açığa satış) or leverage unless the
  config allows it and the user confirms.
