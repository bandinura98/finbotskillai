#!/usr/bin/env python3
"""Position sizing for trader-budget (Borsa Istanbul). Standard library only, no network.

Default mode: sizes ONE day trade from trader/plan.json (written by
trader-exit) to your budget and your max loss per trade, then writes one
bracket order (entry, stop-loss, take-profit) to trader/orders.json. It never
places anything and never sends anything anywhere: trader-risk checks the
order, trader-broker asks you evet or hayir and prints an order slip (emir
fisi) that YOU type into your own broker app.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit                          # a limit order at the plan's entry
    python3 size.py --limit 312.50 --write           # a limit order at 312.50
    python3 size.py --now "2026-11-09 10:30" --json  # pin the clock (Istanbul time, TSI)

How many shares. Whole shares only: on BIST 1 lot = 1 share, no fractions.

    cost a share   round-trip costs: commission + BSMV + borsa payi on the buy
                   at the entry and on the sell at the stop
    shares = floor of the smallest of
      max loss       max_loss_per_trade_usd / (risk per share + cost a share)
      position cap   budget x max_position_pct / entry
      max order      max_order_usd / entry
      cash           (budget minus the cash buffer) / (entry + buy-side cost a share)
                     (and no more than the cash in trader/portfolio.json, if you have one)

The money keys keep their old names (max_loss_per_trade_usd, ...) so every
tool reads the same config, but every amount is Turkish lira (TL).

Prices sit on the BIST tick grid (fiyat adimi): the stop rounds down, the
target rounds up, a limit price rounds to the nearest tick. If the plan has
today's daily limits (taban / tavan), a stop under the taban or a target over
the tavan is refused: it can never fill today.

The entry it sizes at: a limit order's limit price; for a market order the
plan's entry, or the price in trader/prices.json if that is fresh (under 15
minutes old) and higher, because a price that ran up since the plan means a
bigger loss at the same stop. Zero shares (the stop is too far away for your
max loss) is a plain refusal: no order is written. So is a reward:risk under
min_reward_risk (the price ran toward the target since the plan).

Weights mode (kept from v1, no longer the headline): split a budget across a
list of stocks by weight.

    python3 size.py --weights THYAO=30,ASELS=25,GARAN=20,BIMAS=15,KCHOL=10
    python3 size.py --targets my-weights.json --budget 100000 --write
    python3 size.py --weights ... --whole-shares --prices trader/prices.json

  1. cash buffer   budget x cash_buffer_pct stays in cash, never invested; the
                   estimated buy costs (commission + BSMV + borsa payi) are kept
                   aside too, so orders plus costs never dip into the buffer
  2. weights       renormalised to 100 over the stocks you keep (blocked
                   tickers are dropped first; --top N keeps the N biggest)
  3. cap           no stock gets more than max_position_pct of the budget;
                   anything over the cap is shared out to the others by
                   weight, again and again until nobody is over
  4. min order     a slice under min_order_usd (TL) is dropped and its weight
                   shared out, so you never place a tiny order
  5. rounding      whole lira by default (--cents for kurus); the rounding
                   lira goes to the biggest position that is not capped
  6. whole shares  with --whole-shares or "fractional": false (BIST); needs prices

Money that cannot be invested is reported as "left idle", never hidden.
"""

import argparse
import datetime
import json
import math
import os
import sys

# Defaults: the contract's config.example.json. Missing keys fall back to these.
# Amounts are Turkish lira even where the key name ends in _usd (kept for compatibility).
DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_usd": 80000,
    "min_order_usd": 100,
    "max_loss_per_trade_usd": 1000,
    "max_daily_loss_usd": 3000,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "10:15",
    "no_entries_after": "17:30",
    "flat_by": "17:50",
    "fractional": False,
    "broker": "manual",
    "paper": True,
    "blocked": [],
    "allow_options": False,
    "allow_margin": False,
    "allow_short": False,
    "pdt_guard": False,
    "commission_rate": 0.0015,
    "bsmv_rate": 0.05,
    "exchange_fee_rate": 0.00003,
}
EPS = 1e-9
FRESH_MINUTES = 15
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce"}


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- time (Istanbul, no tz files)

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
        raise SizeError("--now wants a time like \"2026-11-09 10:30\" (Istanbul, TSİ) or ...Z (UTC)")
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    """"2026-11-09T07:52:13Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def utc_iso(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- BIST ticks and costs

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
    t = tick_size(price)
    q = round(price / t, 3)        # float32 guard: Yahoo's 48.31999969 is 48.32, not a tick under it
    q = math.floor(q + 1e-9) if mode == "down" else math.ceil(q - 1e-9) if mode == "up" else round(q)
    return round(q * t, 2)


DEFAULT_COSTS = {
    "commission_rate": 0.0015,   # broker commission per side, fraction of notional (binde 1,5); set yours
    "bsmv_rate": 0.05,           # BSMV on the commission
    "exchange_fee_rate": 0.00003,  # borsa payı per side (approx.)
}


def side_cost(notional, c):
    com = notional * c["commission_rate"]
    return com * (1 + c["bsmv_rate"]) + notional * c["exchange_fee_rate"]


def cost_rates(cfg):
    """The three cost rates from the config, defaults for missing keys."""
    out = dict(DEFAULT_COSTS)
    for k in DEFAULT_COSTS:
        v = _f((cfg or {}).get(k))
        if v is not None and v >= 0:
            out[k] = v
    return out


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(read_json(path, "config"))
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_weights(data):
    """Accept a weights file, a {ticker: weight} dict or a list of rows.

    Returns [{"ticker", "name", "weight"}] with weight as a float."""
    if isinstance(data, dict) and "weights" in data:
        data = data["weights"]
    rows = []
    if isinstance(data, dict):
        for t, w in data.items():
            rows.append({"ticker": t, "name": "", "weight": w})
    elif isinstance(data, list):
        for i, r in enumerate(data):
            if not isinstance(r, dict):
                raise SizeError("weights[%d] is not an object" % i)
            t = r.get("ticker") or r.get("symbol")
            w = r.get("weight", r.get("weight_pct", r.get("pct")))
            if not t or w is None:
                raise SizeError("weights[%d] needs a ticker and a weight" % i)
            rows.append({"ticker": t, "name": r.get("name", ""), "weight": w})
    else:
        raise SizeError("weights must be a list or an object")
    out = []
    for r in rows:
        try:
            w = float(r["weight"])
        except (TypeError, ValueError):
            raise SizeError("weight for %s is not a number: %r" % (r["ticker"], r["weight"]))
        out.append({"ticker": bare(r["ticker"]), "name": r["name"] or "", "weight": w})
    return out


def bare(sym):
    """THYAO.IS -> THYAO: state files keep the bare BIST code."""
    s = str(sym or "").strip().upper()
    return s[:-3] if s.endswith(".IS") else s


def weights_from_arg(text):
    """THYAO=30,ASELS=25 -> {ticker: weight}"""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SizeError("--weights wants TICKER=WEIGHT pairs, got %r" % part)
        t, w = part.split("=", 1)
        out[bare(t)] = w.strip()
    return out


def load_prices(path):
    if not path:
        return {}
    data = read_json(path, "prices")
    prices = data.get("prices", data) if isinstance(data, dict) else {}
    out = {}
    for t, p in prices.items():
        try:
            out[bare(t)] = float(p)
        except (TypeError, ValueError):
            continue
    return out


def price_time(doc, sym):
    """When this symbol's price is from: the quote's own time (detail[sym].time), else when it
    was fetched (detail[sym].fetched), else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, fetched_utc, fresh) for sym from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- money helpers

def round_cent(x):
    """Round an amount to the kurus (half up)."""
    return math.floor(float(x) * 100 + 0.5 + EPS) / 100.0


def shares_for(amount, per_share):
    """Whole shares that fit in amount at per_share, in exact integer kurus."""
    u, p = int(round(float(amount) * 100)), int(round(float(per_share) * 100))
    if p <= 0 or u <= 0:
        return 0
    return u // p


def _price(x):
    """312.5 -> "312.50": a share price on the tick grid, as a string in the order."""
    return "%.2f" % round_tick(float(x))


def _amt(x):
    """21.25 -> "21.25": a per-share amount (risk, cost), NOT a price, so never snapped to a tick."""
    return "%.2f" % float(x)


def _num(x):
    """1250 -> "1250", 1118.13 -> "1118.13"."""
    x = round(float(x), 2)
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return "%.2f" % x


def money(x):
    """1234.5 -> '1.234,50 TL' (Turkish grouping)."""
    s = "{:,.2f}".format(float(x))
    return s.replace(",", "_").replace(".", ",").replace("_", ".") + " TL"


def plan_limits(plan):
    """The plan's daily limits {"base","taban","tavan"} as floats, or None (optional field)."""
    lim = (plan or {}).get("limits")
    if not isinstance(lim, dict):
        return None
    out = {k: _f(lim.get(k)) for k in ("base", "taban", "tavan")}
    if not out["taban"] and not out["tavan"]:
        return None
    return out


# ---------------------------------------------------------------- plan mode (the default)

def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the limit price for a limit order (None = market)
    price  a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash   the cash in trader/portfolio.json, if there is one
    today  Istanbul date (YYYY-MM-DD); a plan for another day is refused"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    costs = cost_rates(c)
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = bare(plan.get("symbol"))
    if not sym:
        raise SizeError("the plan has no symbol (run trader-exit)")
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) day trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s in Istanbul: make a new plan with trader-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit); a bracket order needs entry, "
                            "stop and target" % name)
    blocked = {bare(b) for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")

    if limit is not None:
        ref, ref_from = round_tick(limit), "limit"
    elif price is not None and round_tick(price) > round_tick(entry_plan):
        ref, ref_from = round_tick(price), "price"
    else:
        ref, ref_from = round_tick(entry_plan), "plan"
    stop, target = round_tick(stop, "down"), round_tick(target, "up")
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop), _price(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with trader-exit" % (_price(target), _price(ref)))
    risk_c = int(round((ref - stop) * 100))
    reward_c = int(round((target - ref) * 100))
    risk = risk_c / 100.0
    buy_cost_ps = side_cost(ref, costs)
    cost_ps = round(buy_cost_ps + side_cost(stop, costs), 4)      # round trip: buy at entry, sell at the stop
    loss_ps = risk + cost_ps

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "budget %s minus the %s cash buffer" % (money(budget), money(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "cash %s in trader/portfolio.json minus the %s buffer" % (money(cash), money(cash_buffer))
    available = max(round(available, 2), 0.0)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_usd"))
    if max_loss:
        n = max(0, int(math.floor(max_loss / loss_ps + EPS)))
        limits.append({"name": "max loss", "amount": max_loss, "per": round(loss_ps, 4), "shares": n,
                       "how": "%s max loss / (%s risk + %s costs a share)"
                              % (money(max_loss), _amt(risk), "%.4f" % cost_ps)})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "amount": cap, "per": ref, "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), money(budget), money(cap), _price(ref))})
    max_order = _f(c.get("max_order_usd"))
    if max_order:
        limits.append({"name": "max order", "amount": max_order, "per": ref, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (money(max_order), _price(ref))})
    per_cash = ref + buy_cost_ps
    limits.append({"name": "cash", "amount": available, "per": round(per_cash, 4),
                   "shares": max(0, int(math.floor(available / per_cash + EPS))),
                   "how": "%s = %s / %s (with buy costs)" % (cash_note, money(available), "%.2f" % per_cash)})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    cost = round(qty * ref, 2)
    loss_gross = round(qty * risk_c / 100.0, 2)
    costs_rt = round(qty * cost_ps, 2)
    loss = round(loss_gross + costs_rt, 2)
    # after costs: a win sells at the target (not the stop), so its round trip is the buy plus a sell at the target
    cost_tgt_ps = buy_cost_ps + side_cost(target, costs)
    rr_net = round((reward_c / 100.0 - cost_tgt_ps) / loss_ps, 2) if loss_ps > 0 else None

    day = plan_limits(plan)
    refusal = None
    if day and day.get("taban") and stop < day["taban"] - EPS:
        refusal = ("the stop %s is under today's taban (lower limit) %s: it can never fill today. No order. "
                   "Make a new plan with trader-exit." % (_price(stop), _price(day["taban"])))
    elif day and day.get("tavan") and target > day["tavan"] + EPS:
        refusal = ("the target %s is over today's tavan (upper limit) %s: it can never fill today. No order. "
                   "Make a new plan with trader-exit." % (_price(target), _price(day["tavan"])))
    elif day and limit is not None and ((day.get("tavan") and ref > day["tavan"] + EPS)
                                        or (day.get("taban") and ref < day["taban"] - EPS)):
        refusal = ("the limit price %s is outside today's taban-tavan band %s-%s. No order."
                   % (_price(ref), _price(day.get("taban") or 0), _price(day.get("tavan") or 0)))
    elif qty == 0:
        if bound[0] == "max loss":
            refusal = ("one share risks %s (entry %s minus stop %s) plus %s round-trip costs = %s, more than "
                       "your %s max loss per trade. No order. Skip this trade, or plan a closer stop with "
                       "trader-exit." % (money(risk), _price(ref), _price(stop), money(cost_ps), money(loss_ps),
                                        money(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, money(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif _f(c.get("min_reward_risk")) and reward_c < _f(c["min_reward_risk"]) * risk_c - EPS:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so trader-risk "
                   "would block it. No order. Skip it, or make a new plan with trader-exit."
                   % (reward_c / float(risk_c), _price(target), _price(ref), _price(stop), _num(c["min_reward_risk"])))
    elif cost < float(c.get("min_order_usd") or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, money(cost), money(c["min_order_usd"])))
    return {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": round_tick(entry_plan),
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target,
        "risk_per_share": risk, "cost_per_share": cost_ps, "reward_risk": round(reward_c / float(risk_c), 2),
        "reward_risk_net": rr_net,
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "max_loss_per_trade_usd": max_loss, "limits": limits, "qty": qty, "bound_by": bound[0],
        "cost": cost, "loss_at_stop": loss_gross, "costs_round_trip": costs_rt, "max_loss": loss,
        "costs": costs, "daily_limits": day,
        "flat_by": plan.get("flat_by") or c.get("flat_by"),
        "order_type": "limit" if limit is not None else "market", "refusal": refusal,
        "currency": "TRY",
    }


def bracket_order(s):
    """The one proposed order: entry, stop-loss and take-profit (prices as strings) plus why and plan."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    o = {"symbol": s["symbol"], "side": "buy", "qty": str(int(s["qty"])), "type": s["order_type"],
         "time_in_force": "day"}
    if s["order_type"] == "limit":
        o["limit_price"] = _price(s["entry"])
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "day trade")
    why = ("%s on %s: %d shares, bound by your %s (%d x (%s risk + %s costs) = %s at the stop)"
           % (setup, s["symbol"], s["qty"], {"max loss": "%s max loss per trade" % money(s["max_loss_per_trade_usd"] or 0),
                                             "position cap": "position cap", "max order": "max order",
                                             "cash": "cash after the buffer"}[s["bound_by"]],
              s["qty"], _amt(s["risk_per_share"]), "%.2f" % s["cost_per_share"], money(s["max_loss"])))
    plan = {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
            "risk_per_share": s["risk_per_share"], "cost_per_share": s["cost_per_share"],
            "max_loss": s["max_loss"], "setup": s["setup"],
            "entry_time": s["entry_time"], "flat_by": s["flat_by"]}
    if s.get("daily_limits"):
        plan["limits"] = s["daily_limits"]
    o.update({
        "order_class": "bracket",
        "take_profit_limit_price": _price(s["target"]),
        "stop_loss_stop_price": _price(s["stop"]),
        "why": why,
        "plan": plan,
    })
    return o


def plan_report(s, order, notes=()):
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (s["symbol"], setup, _price(s["entry_plan"]),
                                            (" at " + s["entry_time"] + " TSİ") if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_price(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _price(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    if s.get("daily_limits"):
        d = s["daily_limits"]
        lines.append("Today's limits: taban %s, tavan %s (base %s, +/-10%%)."
                     % (_price(d.get("taban") or 0), _price(d.get("tavan") or 0),
                        _price(d["base"]) if d.get("base") else "?"))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f%s."
                 % (_price(s["entry"]), src, _amt(s["risk_per_share"]), _price(s["entry"]), _price(s["stop"]),
                    s["reward_risk"], (" (%.2f after costs)" % s["reward_risk_net"])
                    if s.get("reward_risk_net") is not None else ""))
    c = s["costs"]
    lines.append("Costs: %.4f a share round trip (commission %s%% + BSMV %s%% on it + borsa payi %s%%, "
                 "on the buy at %s and the sell at the stop %s)."
                 % (s["cost_per_share"], _pct(c["commission_rate"]), _pct(c["bsmv_rate"]),
                    _pct(c["exchange_fee_rate"]), _price(s["entry"]), _price(s["stop"])))
    lines.append("")
    lines.append("Limits (whole shares, 1 lot = 1 share; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-66s %5d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not investment advice (yatırım tavsiyesi değildir).")
        return "\n".join(lines)
    lines.append("Size: %d shares (lots) of %s = %s at %s." % (s["qty"], s["symbol"], money(s["cost"]),
                                                              _price(s["entry"])))
    lines.append("Max loss at the stop: %d x %s = %s, plus %s round-trip costs = %s."
                 % (s["qty"], _amt(s["risk_per_share"]), money(s["loss_at_stop"]),
                    money(s["costs_round_trip"]), money(s["max_loss"])))
    lines.append("A stop is an order, not a promise: in a fast market it can fill below %s." % _price(s["stop"]))
    lines.append("")
    lines.append("Proposed bracket order (nothing has been sent anywhere):")
    how = "limit %s" % order["limit_price"] if order["type"] == "limit" else "market"
    lines.append("  buy %s %s, %s, day, take-profit %s, stop-loss %s"
                 % (order["qty"], order["symbol"], how, order["take_profit_limit_price"],
                    order["stop_loss_stop_price"]))
    if s["flat_by"]:
        lines.append("Flat by %s TSİ: anything still open then gets closed (trader-alerts rings, "
                     "trader-broker flatten)." % s["flat_by"])
    lines.append("")
    lines.append("Next: trader-risk checks it, then trader-broker asks you evet or hayir and prints the emir fisi.")
    lines.append("Education and tooling, not investment advice (yatırım tavsiyesi değildir).")
    return "\n".join(lines)


def _pct(rate):
    """0.0015 -> "0.15", 0.05 -> "5"."""
    x = float(rate) * 100
    return ("%.4f" % x).rstrip("0").rstrip(".")


# ---------------------------------------------------------------- weights mode (v1)

def normalise(rows, blocked=(), top=None):
    """Drop blocked and non-positive weights, keep the top N, renormalise to 100."""
    blocked = {bare(b) for b in (blocked or [])}
    dropped, keep, seen = [], [], set()
    for r in rows:
        if r["ticker"] in seen:
            raise SizeError("%s is listed twice in the weights" % r["ticker"])
        seen.add(r["ticker"])
        if r["ticker"] in blocked:
            dropped.append({"ticker": r["ticker"], "reason": "on your blocked list"})
        elif r["weight"] <= 0:
            dropped.append({"ticker": r["ticker"], "reason": "weight is zero or negative"})
        else:
            keep.append(dict(r))
    keep.sort(key=lambda r: -r["weight"])
    if top:
        for r in keep[top:]:
            dropped.append({"ticker": r["ticker"], "reason": "outside the top %d" % top})
        keep = keep[:top]
    total = sum(r["weight"] for r in keep)
    for r in keep:
        r["source_weight"] = r["weight"]
        r["weight"] = 100.0 * r["weight"] / total if total else 0.0
    return keep, dropped


def allocate(rows, investable, cap):
    """Exact (unrounded) lira per ticker with the cap enforced.

    Proportional to weight; any stock over the cap is pinned at the cap and
    the rest of the money is shared over the others, repeated until no stock
    is over. Returns ({ticker: lira}, set of capped tickers)."""
    alloc, capped = {}, set()
    active = [r for r in rows]
    money_left = investable
    while active:
        wsum = sum(r["weight"] for r in active)
        trial = {r["ticker"]: money_left * r["weight"] / wsum for r in active}
        over = [r for r in active if trial[r["ticker"]] > cap + EPS]
        if not over:
            alloc.update(trial)
            break
        for r in over:
            alloc[r["ticker"]] = cap
            capped.add(r["ticker"])
            money_left -= cap
        active = [r for r in active if r["ticker"] not in capped]
        if money_left <= EPS:
            for r in active:
                alloc[r["ticker"]] = 0.0
            break
    return alloc, capped


def round_to_unit(exact, capped, order, cap, unit):
    """Round to whole lira (unit 1) or kurus (unit 0.01) and keep the total.

    The total placed is the exact total rounded DOWN to the unit, so it never
    spends money you do not have. Each slice is rounded to the nearest unit,
    then the difference (usually one unit) goes to the biggest stock that is
    not capped, then the next, never pushing anyone over the cap."""
    def units(x):
        return int(math.floor(x / unit + EPS))

    cap_u = units(cap)
    total_u = units(sum(exact.values()))
    out = {}
    for t, v in exact.items():
        out[t] = cap_u if t in capped else int(math.floor(v / unit + 0.5 + EPS))
    diff = total_u - sum(out.values())
    takers = [t for t in order if t not in capped]
    guard = 0
    while diff != 0 and takers and guard < 100000:
        moved = False
        for t in takers:
            if diff == 0:
                break
            if diff > 0 and out[t] + 1 <= cap_u:
                out[t] += 1
                diff -= 1
                moved = True
            elif diff < 0 and out[t] > 0:
                out[t] -= 1
                diff += 1
                moved = True
        if not moved:
            break
        guard += 1
    return {t: round(u * unit, 2) for t, u in out.items()}


def whole_shares(amounts, prices, order, cap, cash):
    """Whole shares: floor each slice, then buy one more share at a time for the
    stock furthest below its slice while cash lasts and the cap holds."""
    shares = {}
    for t in order:
        shares[t] = int(math.floor(amounts[t] / prices[t] + EPS)) if amounts[t] > 0 else 0
    spent = sum(shares[t] * prices[t] for t in order)
    left = cash - spent
    while True:
        best, gap = None, None
        for t in order:
            p = prices[t]
            if p > left + EPS or (shares[t] + 1) * p > cap + EPS:
                continue
            g = amounts[t] - shares[t] * p
            if best is None or g > gap + EPS:
                best, gap = t, g
        if best is None:
            break
        shares[best] += 1
        left -= prices[best]
    return shares


def size(rows, cfg, budget=None, prices=None, whole=None, cents=False, top=None):
    """Weights mode. Pure function: weights + rules -> the full sizing result (a dict).

    Amounts are lira; the result keeps the key "dollars" for each position for compatibility."""
    budget = float(budget if budget is not None else (cfg.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json")
    buf_pct = float(cfg.get("cash_buffer_pct", 10))
    cap_pct = float(cfg.get("max_position_pct", 25))
    min_order = float(cfg.get("min_order_usd", 5) or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    if not 0 < cap_pct <= 100:
        raise SizeError("max_position_pct must be above 0 and at most 100")
    if whole is None:
        whole = not cfg.get("fractional", True)
    prices = prices or {}

    keep, dropped = normalise(rows, cfg.get("blocked"), top)
    if not keep:
        raise SizeError("nothing to size: no weights left after the blocked list")

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    # the buys pay commission + BSMV + borsa payi on top: keep that out of the money split up, so the
    # orders plus their costs never dip into the cash buffer (trader-risk checks exactly that)
    buy_rate = side_cost(1.0, cost_rates(cfg))
    investable = math.floor((budget - cash_buffer) / (1.0 + buy_rate) * 100 + EPS) / 100.0
    cost_reserve = round(budget - cash_buffer - investable, 2)
    cap = round(budget * cap_pct / 100.0, 2)
    unit = 0.01 if (cents or whole) else 1.0

    if whole:
        missing = [r["ticker"] for r in keep if not prices.get(r["ticker"])]
        if missing:
            raise SizeError("whole shares need a price for: %s (add them to trader/prices.json, "
                            "e.g. with trader-alerts: alerts.py quote %s --write)"
                            % (", ".join(missing), " ".join(missing)))

    while True:
        live = [dict(r) for r in keep]
        tot = sum(r["source_weight"] for r in live)
        for r in live:
            r["weight"] = 100.0 * r["source_weight"] / tot
        exact, capped = allocate(live, investable, cap)
        order = [r["ticker"] for r in sorted(live, key=lambda r: -exact[r["ticker"]])]
        amounts = round_to_unit(exact, capped, order, cap, unit)
        shares = None
        if whole:
            shares = whole_shares(amounts, prices, order, cap, sum(amounts.values()))
            amounts = {t: round(shares[t] * prices[t], 2) for t in order}
        small = [r for r in live if amounts[r["ticker"]] < min_order - EPS]
        if not small:
            break
        worst = min(small, key=lambda r: (r["source_weight"], r["ticker"]))
        if whole and shares is not None and shares[worst["ticker"]] == 0 and prices.get(worst["ticker"], 0) > 0:
            reason = ("one share costs %s, more than its slice; it cannot be bought in whole shares"
                      % money(prices[worst["ticker"]]))
        else:
            reason = "its slice (%s) is under your %s minimum order" % (
                money(amounts[worst["ticker"]]), money(min_order))
        dropped.append({"ticker": worst["ticker"], "reason": reason})
        keep = [r for r in keep if r["ticker"] != worst["ticker"]]
        if not keep:
            raise SizeError("every slice is under your minimum order; raise the budget or hold fewer stocks")

    positions = []
    for r in sorted(live, key=lambda r: -r["weight"]):
        t = r["ticker"]
        p = {"ticker": t, "name": r["name"], "weight": round(r["weight"], 2),
             "source_weight": r["source_weight"], "dollars": amounts[t],
             "capped": t in capped}
        if prices.get(t):
            p["price"] = prices[t]
            p["shares"] = shares[t] if whole else round(amounts[t] / prices[t], 4)
        positions.append(p)
    invested = round(sum(p["dollars"] for p in positions), 2)
    return {
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer,
        "investable": investable, "cost_reserve": cost_reserve, "max_position_pct": cap_pct, "cap": cap,
        "min_order_usd": min_order, "whole_shares": bool(whole),
        "positions": positions, "invested": invested,
        "idle": round(investable - invested, 2), "dropped": dropped, "currency": "TRY",
    }


def orders_from(result, source=""):
    """Weights mode: proposed buy orders, one per position (qty in whole shares, or a TL notional)."""
    out = []
    for p in result["positions"]:
        if p["dollars"] <= 0:
            continue
        why = "%.2f%% of the split" % p["weight"]
        if source:
            why = "%s, weights from %s" % (why, source)
        if p["capped"]:
            why += "; capped at %s%% of the %s budget" % (_num(result["max_position_pct"]), money(result["budget"]))
        o = {"symbol": p["ticker"], "side": "buy"}
        if result["whole_shares"]:
            o["qty"] = str(int(p["shares"]))
        else:
            o["notional"] = _num(p["dollars"])
        o.update({"type": "market", "time_in_force": "day", "why": why})
        out.append(o)
    return out


def report(result, orders, source=""):
    """Weights mode table."""
    r = result
    lines = []
    if source:
        lines.append("Weights from: %s" % source)
    lines.append("Budget %s, cash buffer %s%% = %s, %s kept for the buy costs, investing %s, cap %s%% = %s "
                 "per stock" % (money(r["budget"]), _num(r["cash_buffer_pct"]), money(r["cash_buffer"]),
                                money(r.get("cost_reserve") or 0), money(r["investable"]),
                                _num(r["max_position_pct"]), money(r["cap"])))
    lines.append("")
    has_price = any("price" in p for p in r["positions"])
    head = "%-7s %-22s %8s %16s" % ("ticker", "name", "weight", "amount")
    if has_price:
        head += " %14s %10s" % ("price", "shares")
    lines.append(head + "  note")
    for p in r["positions"]:
        row = "%-7s %-22s %7.2f%% %16s" % (p["ticker"], (p["name"] or "")[:22], p["weight"], money(p["dollars"]))
        if has_price:
            row += " %14s %10s" % (money(p["price"]) if "price" in p else "", p.get("shares", ""))
        if p["capped"]:
            row += "  capped at %s%%" % _num(r["max_position_pct"])
        lines.append(row)
    lines.append("%-7s %-22s %8s %16s" % ("", "invested", "", money(r["invested"])))
    lines.append("%-7s %-22s %8s %16s" % ("", "buy costs (kept)", "", money(r.get("cost_reserve") or 0)))
    lines.append("%-7s %-22s %8s %16s" % ("", "cash buffer", "", money(r["cash_buffer"])))
    lines.append("%-7s %-22s %8s %16s" % ("", "left idle", "", money(r["idle"])))
    if r["idle"] >= 1:
        lines.append("")
        lines.append("Left idle %s: %s" % (money(r["idle"]), (
            "whole shares do not divide evenly" if r["whole_shares"]
            else "every stock is at its cap; add stocks or raise max_position_pct")))
    for d in r["dropped"]:
        lines.append("Dropped %s: %s" % (d["ticker"], d["reason"]))
    lines.append("")
    lines.append("Proposed orders (nothing has been sent anywhere):")
    for o in orders:
        amount = ("notional \"%s\" TL" % o["notional"]) if "notional" in o else ("qty \"%s\"" % o["qty"])
        lines.append("  %s %s %s, %s, %s" % (o["side"], o["symbol"], amount, o["type"], o["time_in_force"]))
    if not r["whole_shares"]:
        lines.append("BIST trades whole shares (1 lot = 1 share): use --whole-shares or \"fractional\": false "
                     "for orders you can type into a broker app.")
    lines.append("")
    lines.append("These buys have no stop. trader-risk blocks them while \"require_stop\" is true and applies "
                 "your day-trade limits to every buy.")
    lines.append("Next: trader-risk checks every order, then trader-broker asks you evet or hayir for each one.")
    lines.append("Education and tooling, not investment advice (yatırım tavsiyesi değildir).")
    return "\n".join(lines)


# ---------------------------------------------------------------- orders file

def write_orders(path, orders, cfg, source="", now_utc=None):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    doc = {
        "created_by": "trader-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "market": "BIST",
        "currency": "TRY",
        "paper": bool(cfg.get("paper", True)),
        "note": ("Proposals only. Nothing is sent anywhere: trader-risk checks each order, you say evet "
                 "to it in the chat, and you type the emir fisi into your own broker app yourself."),
        "source": source,
        "orders": orders,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = to_eastern(now).date().isoformat()
    plan = read_json(a.plan, "plan (run trader-exit first, or use --weights for a split)")
    limit = None
    notes = []
    if a.limit is not None:
        if a.limit == "plan":
            limit = _f((plan or {}).get("entry"))
        else:
            limit = _f(str(a.limit).replace(",", "."))
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 312.50")
        if limit and abs(round_tick(limit) - limit) > 1e-6:
            notes.append("Limit %s is off the BIST tick grid (tick %s at this price): using %s."
                         % (str(a.limit), "%.2f" % tick_size(limit), _price(limit)))
    sym = bare((plan or {}).get("symbol"))
    for w in (plan or {}).get("warnings") or []:
        notes.append("Plan warning: %s" % w)
    price = None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    if ppath and limit is None and sym:
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        if p is not None and fresh:
            price = p
            if round_tick(p) > round_tick(_f(plan.get("entry")) or 0):
                notes.append("%s last %s (as of %s TSİ) is above the plan's entry %s, so the risk "
                             "is bigger: sized at %s." % (sym, _price(p), to_eastern(t).strftime("%H:%M"),
                                                          _price(plan.get("entry")), _price(p)))
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + to_eastern(t).strftime("%Y-%m-%d %H:%M") + " TSİ")
                            if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today)
    order = bracket_order(s)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes,
                          "market": "BIST", "currency": "TRY"}, indent=2, ensure_ascii=False))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed bracket order to %s" % a.write)
    return 0 if order else 1


def run_weights(a, cfg):
    source = ""
    if a.weights:
        rows = parse_weights(weights_from_arg(a.weights))
    else:
        data = read_json(a.targets, "weights file")
        rows = parse_weights(data)
        if isinstance(data, dict):
            source = data.get("source", "")
    prices = load_prices(a.prices)
    if not prices and os.path.exists("trader/prices.json") and not a.prices:
        prices = load_prices("trader/prices.json")
    whole = True if a.whole_shares else None
    result = size(rows, cfg, a.budget, prices, whole, a.cents, a.top)
    orders = orders_from(result, source)
    if a.write:
        write_orders(a.write, orders, cfg, source, parse_now(a.now))
    if a.json:
        print(json.dumps({"result": result, "orders": orders}, indent=2, ensure_ascii=False))
    else:
        print(report(result, orders, source))
        if a.write:
            print("Wrote %d proposed orders to %s" % (len(orders), a.write))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size a BIST day trade to your budget and max loss (trader-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config (TL)")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a limit order instead of market, at the plan's entry or at PRICE")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this Istanbul time (TSİ), e.g. \"2026-11-09 10:30\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    g = ap.add_argument_group("weights mode (split a budget across a list)")
    g.add_argument("--weights", help="inline weights: THYAO=30,ASELS=25")
    g.add_argument("--targets", help="a weights file: {\"weights\": [{\"ticker\", \"weight\"}]}")
    g.add_argument("--top", type=int, help="keep only the N biggest weights")
    g.add_argument("--whole-shares", action="store_true", help="whole shares only (needs --prices)")
    g.add_argument("--cents", action="store_true", help="size to the kurus instead of whole lira")
    a = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):      # TSİ and Turkish text on a non-UTF-8 pipe
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        cfg = load_config(a.config)
        if a.weights or a.targets:
            return run_weights(a, cfg)
        if a.top or a.whole_shares or a.cents:
            raise SizeError("--top, --whole-shares and --cents work with --weights or --targets")
        return run_plan(a, cfg)
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
